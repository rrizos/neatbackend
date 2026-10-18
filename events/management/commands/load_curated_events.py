"""Publish a file of hand-checked events, or take them down again.

    manage.py load_curated_events events/curated/greece-2026-autumn.json --dry-run
    manage.py load_curated_events events/curated/greece-2026-autumn.json
    manage.py load_curated_events events/curated/greece-2026-autumn.json --remove
    manage.py load_curated_events --remove-all

Loading the same file again updates what changed and adds nothing twice. An
entry whose evening someone already listed — in the app, or through the
crawler — is skipped rather than duplicated. See events/ingest/curated.py.
"""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from events.ingest import curated


class Command(BaseCommand):
    help = 'Publish (or remove) a JSON file of hand-checked events.'

    def add_arguments(self, parser):
        parser.add_argument('path', nargs='?', help='a JSON file of events')
        parser.add_argument('--dry-run', action='store_true',
                            help='report what would change, write nothing')
        parser.add_argument('--no-images', action='store_true',
                            help="keep the organisers' image URLs instead of copying them")
        parser.add_argument('--remove', action='store_true',
                            help='delete the events in this file')
        parser.add_argument('--remove-all', action='store_true',
                            help='delete every curated event')

    def handle(self, *args, **options):
        if options['remove_all']:
            count = curated.remove()
            self.stdout.write(f'{count} curated events removed.')
            return

        path = options['path']
        if not path:
            raise CommandError('give a JSON file, or --remove-all')
        try:
            entries = json.loads(Path(path).read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise CommandError(f'cannot read {path}: {exc}')
        if not isinstance(entries, list):
            raise CommandError(f'{path} should hold a JSON list of events')

        if options['remove']:
            count = curated.remove(entries)
            self.stdout.write(f'{count} events from {path} removed.')
            return

        report = curated.load(entries, dry_run=options['dry_run'],
                              copy_images=not options['no_images'])
        for line in report.lines():
            self.stdout.write(line)
        if options['dry_run']:
            self.stdout.write('Dry run: nothing was written.')
