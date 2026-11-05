"""Recompute every event's score (events/ranking.py) and say what changed.

    manage.py rescore_events            # events from the last week onwards
    manage.py rescore_events --all      # every event
    manage.py rescore_events --show 20  # and print the city's top and bottom

The tab scores on every read, so this is not needed for the tab to be right.
It is what keeps the admin's `quality` column honest after the scoring rules
change, and it is the first thing to run after deploying them.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Count
from django.utils import timezone

from events import ranking
from events.models import Event


class Command(BaseCommand):
    help = "Recompute events' quality scores."

    def add_arguments(self, parser):
        parser.add_argument('--all', action='store_true', help='every event, not only recent ones')
        parser.add_argument('--show', type=int, default=0, metavar='N',
                            help='print the N best and worst upcoming crawled events')

    def handle(self, *args, **options):
        rows = Event.objects.annotate(report_count=Count('reports'))
        if not options['all']:
            rows = rows.filter(date__gte=timezone.now() - timedelta(days=8))

        changed = []
        for event in rows:
            quality = ranking.score(event, reports=event.report_count)[0]
            if quality != event.quality:
                event.quality = quality
                changed.append(event)
        Event.objects.bulk_update(changed, ['quality'], batch_size=500)
        self.stdout.write(self.style.SUCCESS(f'rescored {len(changed)} event(s)'))

        if options['show']:
            crawled = sorted((e for e in rows if ranking.is_crawled(e) and e.date
                              and e.date >= timezone.now()),
                             key=lambda e: -ranking.score(e, reports=e.report_count)[0])
            n = options['show']
            for label, part in (('best', crawled[:n]), ('worst', crawled[-n:])):
                self.stdout.write(f'\n{label}:')
                for event in part:
                    points, why = ranking.score(event, reports=event.report_count)
                    self.stdout.write(f'  {points:3}  {event.city:12} {event.title[:60]}'
                                      f'  — {", ".join(why) or "listed once"}')
