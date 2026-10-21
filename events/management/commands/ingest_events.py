"""Read the sources and file what they publish, for review.

    manage.py ingest_events --dry-run          # say what would happen
    manage.py ingest_events                    # every seed
    manage.py ingest_events --city Αθήνα       # one city's seeds
    manage.py ingest_events --url https://… --city Αθήνα [--kind ical]

The last form is how to try a page that is not a seed yet: it runs the real
parsers over it and prints what it would have filed, without writing anything
unless you drop --dry-run.

Nothing this writes is visible in the app. Events land as `pending` and wait
for someone to look at them in /admin/events/event/.
"""

from django.core.management.base import BaseCommand, CommandError

from events.ingest import pipeline, sources


class Command(BaseCommand):
    help = 'Collect events published on the web into the review queue.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='parse and report, write nothing')
        parser.add_argument('--city', action='append', default=[],
                            help='only sources for this city (repeatable)')
        parser.add_argument('--url', help='read this page instead of the seeds')
        parser.add_argument('--kind', default='jsonld', choices=('jsonld', 'ical'),
                            help='how to read --url (default: jsonld)')

    def handle(self, *args, **options):
        dry = options['dry_run']

        if options['url']:
            cities = options['city']
            if len(cities) != 1:
                raise CommandError('--url needs exactly one --city, so the events '
                                   'it finds can be filed somewhere')
            chosen = [sources.one_off(options['url'], cities[0], options['kind'])]
        else:
            chosen = sources.for_cities(options['city'])

        if not chosen:
            self.stdout.write(self.style.WARNING(
                'No sources. events/ingest/sources.py says why, and --url runs '
                'a page through the parsers without adding one.'))
            return

        self.stdout.write(f'{"Would read" if dry else "Reading"} {len(chosen)} source(s)')
        results = pipeline.ingest(chosen, dry_run=dry)

        for result in results:
            style = self.style.ERROR if result.error else self.style.SUCCESS
            self.stdout.write(style(result.line()))

        created = sum(r.created for r in results)
        updated = sum(r.updated for r in results)
        failed = sum(1 for r in results if r.error)
        self.stdout.write(self.style.SUCCESS(
            f'{"would add" if dry else "added"} {created}, '
            f'{"would update" if dry else "updated"} {updated}, '
            f'sources that failed: {failed}'
        ))
        if created and not dry:
            self.stdout.write('Waiting for review at /admin/events/event/?status__exact=pending')
