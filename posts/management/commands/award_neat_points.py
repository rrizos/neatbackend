"""Snapshot every city's Virals top-10 and bank the Neat Points it earns.

Run on a timer (hourly), because the balance is cumulative and a post's time
in the charts does not wait for its author to open the app. Each run records
the high-water mark for the current day, so a post that peaks at noon and has
slipped by evening still keeps what it was worth at noon.

Safe to run as often as you like: awards are keyed on (user, post, day) and
only ever revised upward.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from posts.viral_points import period_key_for, refresh_all
from posts.views import _viral_period_start


class Command(BaseCommand):
    help = "Award Neat Points to everyone currently in a city's Virals top-10."

    def add_arguments(self, parser):
        parser.add_argument(
            '--quiet', action='store_true',
            help='Only report problems (for unattended timer runs).',
        )

    def handle(self, *args, **options):
        now = timezone.now()
        period_start = _viral_period_start('daily')
        cities = refresh_all(now, period_start)
        if not options['quiet']:
            self.stdout.write(
                f'{period_key_for(now)}: refreshed {cities} '
                f'{"city" if cities == 1 else "cities"}'
            )
