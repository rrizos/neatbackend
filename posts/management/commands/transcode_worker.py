"""Background work that must not happen inside a request.

Two jobs, both for the same reason — they involve waiting on something slow,
and a request that waits is a request slot nobody else can use:

  * **Encoding uploaded video.** See posts/transcode.py.
  * **Resolving link previews.** A card needs an outbound fetch of somebody
    else's page, which can take seconds. Building it lazily — when the first
    reader scrolls past — meant a freshly posted link had no card at the moment
    its author was looking at it, and a link nobody had opened lately had none
    at all. Doing it here keeps the card ahead of the reader.

Videos take priority: a person is waiting on one and nobody is waiting on a
preview.

Run by the `neat-transcode` systemd unit. See posts/transcode.py for why the
encode is not done in the view any more.

Deliberately a polling loop over a database column rather than Celery or RQ:
the queue is a handful of videos a day, Redis here is the Channels layer and
not a broker, and a second broker is a second thing that can be down at 3am.
The cost of polling is one indexed SELECT every few seconds.

Safe to run more than one of, though there is no reason to: claiming a job is
a conditional UPDATE, so two workers cannot take the same row.
"""

import logging
import signal
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from linkpreview import service as linkpreview_service
from posts.models import PostMedia
from posts.transcode import transcode_media

logger = logging.getLogger(__name__)

# How long to wait after finding nothing to do. Long enough that an idle box
# stays idle, short enough that a poster does not sit watching a spinner.
IDLE_SLEEP_SECONDS = 5
# How many links to resolve in one pass. Each is an outbound fetch, so this
# bounds how long the worker is unavailable for the video queue.
PREVIEW_BATCH = 10
# A video that has failed this many times keeps its original file and stops
# being retried. Almost always something ffmpeg will never accept, and retrying
# it forever would starve the queue behind it.
MAX_ATTEMPTS = 3


class Command(BaseCommand):
    help = 'Transcode queued post videos to H.264 (runs continuously).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--once',
            action='store_true',
            help='Drain the queue and exit, instead of waiting for more work.',
        )

    def handle(self, *args, **options):
        self._running = True
        # Watermarks, so each pass looks only at content created since the last
        # one. Reset on restart, which costs one cheap full scan.
        self._post_mark = 0
        self._message_mark = 0
        # Finish the encode in flight rather than abandoning a half-written
        # file, then stop. systemd sends TERM on restart and deploy.
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)

        once = options['once']
        logger.info('transcode worker started (once=%s)', once)

        while self._running:
            # CONN_MAX_AGE keeps connections open across requests; in a loop
            # that is idle for minutes, this is what drops the dead ones.
            close_old_connections()
            try:
                did_work = self._process_one()
            except Exception:
                # Never let one bad row end the worker -- systemd would restart
                # it straight into the same row.
                logger.exception('unexpected error in transcode worker')
                did_work = False

            # Only when there is no video waiting: somebody is watching a
            # progress bar for that, and nobody is watching a preview.
            if not did_work:
                did_work = self._resolve_previews()

            if once and not did_work:
                break
            if not did_work:
                self._sleep(IDLE_SLEEP_SECONDS)

        logger.info('transcode worker stopped')

    def _resolve_previews(self):
        """Build cards for links in newly created posts and messages."""
        try:
            resolved, self._post_mark, self._message_mark = (
                linkpreview_service.resolve_pending(
                    post_min_id=self._post_mark,
                    message_min_id=self._message_mark,
                    limit=PREVIEW_BATCH,
                )
            )
        except Exception:
            # A preview is decoration; never let one stop the worker.
            logger.exception('link preview pass failed')
            return False
        if resolved:
            logger.info('resolved %s link preview(s)', resolved)
        return bool(resolved)

    def _stop(self, *_):
        self.stdout.write('shutdown requested; finishing current job')
        self._running = False

    def _sleep(self, seconds):
        """Sleep in short slices so a stop signal is noticed promptly."""
        deadline = time.monotonic() + seconds
        while self._running and time.monotonic() < deadline:
            time.sleep(0.5)

    def _claim(self):
        """Take the oldest queued video, or None.

        The conditional UPDATE is the claim: whoever changes the row from
        PENDING to PROCESSING owns it, and anyone else gets 0 rows back.
        """
        while True:
            candidate = (
                PostMedia.objects
                .filter(status=PostMedia.PENDING, media_type='video')
                .order_by('id')
                .first()
            )
            if candidate is None:
                return None
            claimed = (
                PostMedia.objects
                .filter(pk=candidate.pk, status=PostMedia.PENDING)
                .update(status=PostMedia.PROCESSING, attempts=candidate.attempts + 1)
            )
            if claimed:
                candidate.refresh_from_db()
                return candidate
            # Somebody else took it between the read and the update; look again.

    def _process_one(self):
        media = self._claim()
        if media is None:
            return False

        started = time.monotonic()
        try:
            # Pointedly *not* inside transaction.atomic(): the encode takes
            # 20-55 seconds, and wrapping it would hold a transaction open on
            # the managed database for that whole time, once per video, for no
            # benefit — there is a single row write here and it happens after
            # ffmpeg has already succeeded.
            media.status = PostMedia.READY
            transcode_media(media)
        except Exception:
            self._fail(media)
            return True

        logger.info(
            'post %s media %s done in %.1fs',
            media.post_id, media.pk, time.monotonic() - started,
        )
        return True

    def _fail(self, media):
        """Put a failed job back, or retire it still pointing at the original."""
        exhausted = media.attempts >= MAX_ATTEMPTS
        media.status = PostMedia.FAILED if exhausted else PostMedia.PENDING
        media.save(update_fields=['status', 'updated'])
        if exhausted:
            # The original upload stays as `url` and stays playable. A video
            # that cannot be re-encoded is worth more than no video.
            logger.error(
                'giving up on media %s after %s attempts; serving the original',
                media.pk, media.attempts,
            )
        else:
            logger.warning('media %s failed (attempt %s); will retry', media.pk, media.attempts)
            # Don't spin straight back onto the same row.
            self._sleep(IDLE_SLEEP_SECONDS)
