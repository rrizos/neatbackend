"""Read the sources and file what they publish, for review.

    manage.py ingest_events --dry-run          # say what would happen
    manage.py ingest_events                    # every seed, then research
    manage.py ingest_events --city Αθήνα       # one city's seeds and research
    manage.py ingest_events --only structured  # APIs and venue pages only
    manage.py ingest_events --only research --research-all   # every app city
    manage.py ingest_events --publish-above 70 # auto-publish the corroborated
    manage.py ingest_events --url https://… --city Αθήνα [--kind ical]

A schedule that works: structured daily, research for the main cities every
day or two, research for every city weekly (it costs a model call per city).

The last form is how to try a page that is not a seed yet: it runs the real
parsers over it and prints what it would have filed, without writing anything
unless you drop --dry-run.

Nothing this writes is visible in the app unless --publish-above (or
EVENTS_AUTO_PUBLISH_SCORE) is set, and then only events with a real start
time that two independent sources agree on. Everything else lands as
`pending` and waits in /admin/events/event/, sortable by score.
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
        parser.add_argument('--kind', default='jsonld', choices=('jsonld', 'ical', 'llm'),
                            help='how to read --url (default: jsonld)')
        parser.add_argument('--only', choices=('structured', 'research'),
                            help='run only the APIs and venue pages, or only research')
        parser.add_argument('--research-all', action='store_true',
                            help='research every city in the app, not just the main ones')
        parser.add_argument('--publish-above', type=int, default=None, metavar='SCORE',
                            help='publish pending events with a real start time, two '
                                 'agreeing sources and at least this score')

    def handle(self, *args, **options):
        dry = options['dry_run']

        if options['url']:
            cities = options['city']
            if len(cities) != 1:
                raise CommandError('--url needs exactly one --city, so the events '
                                   'it finds can be filed somewhere')
            chosen = [sources.one_off(options['url'], cities[0], options['kind'])]
        else:
            chosen = sources.for_cities(options['city'], only=options['only'],
                                        research_all=options['research_all'])

        if not chosen:
            self.stdout.write(self.style.WARNING(
                'No sources. events/ingest/sources.py says why, and --url runs '
                'a page through the parsers without adding one.'))
            return

        self.stdout.write(f'{"Would read" if dry else "Reading"} {len(chosen)} source(s)')
        results = pipeline.ingest(chosen, dry_run=dry, publish_above=options['publish_above'])

        for result in results:
            style = self.style.ERROR if result.error else self.style.SUCCESS
            self.stdout.write(style(result.line()))

        created = sum(r.created for r in results)
        updated = sum(r.updated for r in results)
        failed = sum(1 for r in results if r.error)
        published = sum(r.published for r in results)
        self.stdout.write(self.style.SUCCESS(
            f'{"would add" if dry else "added"} {created}, '
            f'{"would update" if dry else "updated"} {updated}, '
            f'published {published}, sources that failed: {failed}'
        ))
        if created and not dry:
            self.stdout.write('Waiting for review at /admin/events/event/?status__exact=pending')
