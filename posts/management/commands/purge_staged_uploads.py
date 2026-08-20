"""Delete files staged for posts that were never made.

Picking a video uploads it immediately, so a user who then changes their mind
leaves the file behind with no post referring to it. Nothing else would ever
remove it.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from posts.models import StagedUpload
from posts.signals import delete_media_file


class Command(BaseCommand):
    help = 'Remove staged uploads that were never turned into a post.'

    def add_arguments(self, parser):
        parser.add_argument('--hours', type=int, default=24)
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        cutoff = timezone.now() - timezone.timedelta(hours=options['hours'])
        rows = list(StagedUpload.objects.filter(created__lt=cutoff))
        self.stdout.write(f'{len(rows)} abandoned upload(s) older than {options["hours"]}h')
        if options['dry_run']:
            return
        for row in rows:
            delete_media_file(row.url)
            row.delete()
        self.stdout.write(f'done: {len(rows)} removed')
