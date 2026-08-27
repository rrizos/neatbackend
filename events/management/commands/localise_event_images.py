"""Move existing event pictures out of the database and onto disk.

Events created before images.py stored their picture as base64 inside
`Event.image_url`, which is why `/api/events/` averaged 316 KB with 99% of it
being image data. This rewrites them as files; the response then carries a URL
and the picture is fetched once and cached.

Safe to re-run: anything already a URL is skipped.
"""

from django.core.management.base import BaseCommand

from events.images import store_event_image
from events.models import Event


class Command(BaseCommand):
    help = 'Store event images as files instead of base64 in the database.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        rows = [e for e in Event.objects.exclude(image_url='')
                if e.image_url.startswith('data:')]
        before = sum(len(e.image_url) for e in rows)
        self.stdout.write(
            f'{len(rows)} event(s) with an inline image, '
            f'{before/1024:.0f} KB of database text'
        )
        if options['dry_run']:
            return

        moved = failed = 0
        for event in rows:
            url = store_event_image(event.image_url)
            if url.startswith('data:'):
                failed += 1
                continue
            event.image_url = url
            event.save(update_fields=['image_url'])
            moved += 1
        after = sum(len(e.image_url) for e in Event.objects.exclude(image_url=''))
        self.stdout.write(
            f'done: {moved} moved, {failed} failed — '
            f'{before/1024:.0f} KB of inline image text is now {after/1024:.1f} KB of URLs'
        )
