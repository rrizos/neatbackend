"""Give already-uploaded videos the poster frame new ones get automatically.

Videos posted before posters existed have an empty `thumb_url`, so anywhere
that shows a video without playing it — a DM's shared-post card, a
notification row — still falls back to a black square. This walks those rows
once and takes the frame.

One at a time and `nice`d (generate_poster runs ffmpeg that way), so it can be
run on the live box without competing with anything serving requests.
"""

import os

from django.conf import settings
from django.core.management.base import BaseCommand

from posts.models import PostMedia
from posts.transcode import generate_poster


class Command(BaseCommand):
    help = 'Generate poster frames for videos that have none.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=0,
                            help='Stop after this many (0 = all).')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        qs = PostMedia.objects.filter(media_type='video', thumb_url='').order_by('id')
        total = qs.count()
        if options['limit']:
            qs = qs[:options['limit']]

        self.stdout.write(f'{total} video(s) without a poster')
        if options['dry_run']:
            return

        done = failed = missing = 0
        for media in qs:
            rel = media.url[len(settings.MEDIA_URL):].lstrip('/')
            abs_path = os.path.join(settings.MEDIA_ROOT, rel)
            if not os.path.exists(abs_path):
                # A row whose file is gone: nothing to take a frame from.
                missing += 1
                continue
            url = generate_poster(abs_path)
            if url:
                media.thumb_url = url
                media.save(update_fields=['thumb_url', 'updated'])
                done += 1
                self.stdout.write(f'  ok   post {media.post_id} -> {url}')
            else:
                failed += 1
                self.stdout.write(f'  fail post {media.post_id} ({media.url})')

        self.stdout.write(
            f'done: {done} generated, {failed} failed, {missing} missing file(s)'
        )
