"""Move DM photos and voice notes out of the database and onto disk.

Measured before this ran: 11.3 MB of a 21 MB database was base64 inside
`Message.text` — over half of everything stored, and the reason chat pictures
were the slowest thing in the app.

Temporary photos are skipped on purpose. `message_open` guarantees the bytes
stop existing once the viewings are spent, and it delivers that by clearing the
column; a file at a stable URL could not make the same promise.
"""

from django.core.management.base import BaseCommand

from dm_messages.media import store_message_media
from dm_messages.models import Message


class Command(BaseCommand):
    help = 'Store DM media as files instead of base64 in the database.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--limit', type=int, default=0)

    def handle(self, *args, **options):
        rows = [
            m for m in Message.objects.filter(photo_mode='', media_url='').order_by('id')
            if m.text.startswith(('__neat_image__:', '__neat_voice__:'))
            and len(m.text) > 40
        ]
        if options['limit']:
            rows = rows[:options['limit']]
        inline = sum(len(m.text) for m in rows)
        self.stdout.write(
            f'{len(rows)} message(s) with inline media, {inline/1024/1024:.1f} MB'
        )
        if options['dry_run']:
            return

        moved = failed = freed = 0
        for message in rows:
            before = len(message.text)
            url, new_text = store_message_media(message.text)
            if not url:
                failed += 1
                continue
            message.media_url = url
            message.text = new_text
            message.save(update_fields=['media_url', 'text'])
            moved += 1
            freed += before - len(new_text)

        self.stdout.write(
            f'done: {moved} moved, {failed} failed, '
            f'{freed/1024/1024:.1f} MB of database text freed'
        )
