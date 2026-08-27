"""Event times are wall-clock, not instants.

An organiser types "19:00" and everyone should read 19:00 back. That is only
true while storing and showing agree, and both sides deliberately skip the
timezone conversion the rest of the app relies on — so a change to the
project's display zone must not quietly move them.
"""

from django.test import TestCase
from django.utils import timezone

from events.views import _parse_event_datetime


class EventDateTests(TestCase):
    def test_a_typed_time_is_stored_as_that_time(self):
        dt = _parse_event_datetime('2026-09-01T19:00:00')
        self.assertEqual(dt.hour, 19)
        self.assertEqual(dt.utcoffset().total_seconds(), 0)

    def test_it_does_not_follow_the_projects_display_zone(self):
        """The regression this guards against.

        TIME_ZONE became Europe/Athens so the rest of the app shows Greek
        time. If event stamping read that, a newly typed 19:00 would be stored
        three hours away from every event already saved, and nothing in the row
        would say which convention it used.
        """
        with self.settings(TIME_ZONE='Europe/Athens'):
            dt = _parse_event_datetime('2026-09-01T19:00:00')
        self.assertEqual(dt.hour, 19)
        self.assertEqual(dt.utcoffset().total_seconds(), 0)

    def test_a_bare_date_becomes_midnight(self):
        dt = _parse_event_datetime('2026-09-01')
        self.assertEqual((dt.hour, dt.minute), (0, 0))

    def test_an_explicit_offset_is_respected(self):
        dt = _parse_event_datetime('2026-09-01T19:00:00+03:00')
        self.assertEqual(dt.utcoffset().total_seconds(), 3 * 3600)

    def test_empty_is_no_date(self):
        self.assertIsNone(_parse_event_datetime(''))
        self.assertIsNone(_parse_event_datetime(None))
