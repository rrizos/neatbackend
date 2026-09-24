"""Repackage stored voice notes into the one container every client opens.

The installed app writes whatever bytes it downloads to a file it always names
`.aac`, and iOS chooses its parser from that name and never from the content.
An MPEG-4 recording — what an Android phone produces — was therefore handed to
the player describing itself as something it was not, and never opened: on an
iPhone, every voice note ever sent from Android.

`store_message_media` now remuxes new ones on the way in. This is for the ones
already on disk. It is a stream copy, so the audio is untouched; what changes is the wrapper and
the file name, and the message row is pointed at the new file only once that
file is safely written. A recording that is already ADTS under a name that
says otherwise — every iPhone note from before the extension was settled — is
only renamed, since there is nothing about the bytes to fix.

    manage.py normalise_voice_notes --dry-run
    manage.py normalise_voice_notes
"""

import os

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand

from dm_messages.media import STORAGE_DIR, to_adts, voice_extension
from dm_messages.models import Message


class Command(BaseCommand):
    help = 'Store every DM voice note as ADTS AAC, whatever it was recorded as.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--limit', type=int, default=0)

    def handle(self, *args, **options):
        dry = options['dry_run']
        limit = options['limit']

        rows = list(
            Message.objects
            .filter(text__startswith='__neat_voice__:')
            .exclude(media_url='')
            .order_by('id')
        )
        if limit:
            rows = rows[:limit]

        converted = skipped = failed = 0
        for message in rows:
            name = message.media_url[len(settings.MEDIA_URL):].lstrip('/')
            try:
                with default_storage.open(name, 'rb') as fh:
                    raw = fh.read()
            except Exception:
                self.stderr.write(f'  {message.id}: cannot read {name}')
                failed += 1
                continue

            # Two ways to be wrong, and they need different repairs. Bytes in
            # the wrong container have to be repackaged; bytes in the right one
            # under a name that describes something else only have to be
            # renamed — which is the state every iPhone note recorded before
            # the extension was settled is still in.
            already_adts = voice_extension(raw) == 'aac'
            if already_adts and name.endswith('.aac'):
                skipped += 1
                continue

            what = 'rename' if already_adts else 'repackage'
            if dry:
                self.stdout.write(f'  {message.id}: would {what} {name}')
                converted += 1
                continue

            data = raw if already_adts else to_adts(raw)
            if not data:
                self.stderr.write(f'  {message.id}: ffmpeg would not repackage {name}')
                failed += 1
                continue

            # New file first, then the row, then the old file: at no point is
            # the message pointing at something that is not there.
            stored = default_storage.save(
                f'{STORAGE_DIR}/{os.path.basename(name).rsplit(".", 1)[0]}.aac',
                ContentFile(data),
            )
            message.media_url = default_storage.url(stored)
            message.save(update_fields=['media_url'])
            try:
                default_storage.delete(name)
            except Exception:
                self.stderr.write(f'  {message.id}: left {name} behind')
            converted += 1

        self.stdout.write(self.style.SUCCESS(
            f'{"would repackage" if dry else "repackaged"} {converted}, '
            f'already fine {skipped}, failed {failed}'
        ))
