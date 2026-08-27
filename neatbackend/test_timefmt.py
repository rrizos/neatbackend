"""The wire format the app reads times from.

Deliberately locked down by tests, because the shape looks like a mistake: an
instant is sent without an offset. Anyone tidying it into `+00:00` would break
every app already installed, and anyone tidying it into `+03:00` would break
them just as thoroughly while looking correct — Dart folds any offset back to
UTC, so both read three hours behind.
"""

import datetime

from django.test import TestCase
from django.utils import timezone

from neatbackend.timefmt import local_iso


class LocalIsoTests(TestCase):
    def setUp(self):
        # 19:00 UTC == 22:00 in Athens on this date.
        self.instant = datetime.datetime(
            2026, 8, 23, 19, 0, tzinfo=datetime.timezone.utc)

    def test_it_reads_as_greek_wall_clock(self):
        self.assertTrue(local_iso(self.instant).startswith('2026-08-23T22:00:00'))

    def test_it_carries_no_offset(self):
        """The whole point.

        An app that reads the hour without converting shows whatever is
        written here; an offset — any offset — sends it back to UTC.
        """
        out = local_iso(self.instant)
        self.assertNotIn('+', out)
        self.assertFalse(out.endswith('Z'))

    def test_the_instant_is_unchanged_only_its_wording(self):
        parsed = datetime.datetime.fromisoformat(local_iso(self.instant))
        athens = timezone.localtime(self.instant)
        self.assertEqual(parsed, athens.replace(tzinfo=None))

    def test_nothing_becomes_an_empty_string(self):
        self.assertEqual(local_iso(None), '')

    def test_a_caller_can_ask_for_a_different_empty(self):
        self.assertIsNone(local_iso(None, empty=None))

    def test_a_naive_value_is_not_shifted_twice(self):
        """localtime() refuses naive input, so this must not blow up on one."""
        aware = timezone.make_aware(
            datetime.datetime(2026, 8, 23, 22, 0),
            datetime.timezone.utc,
        )
        self.assertTrue(local_iso(aware).startswith('2026-08-24T01:00:00'))
