"""What the event ingester must get right.

Three things carry the risk, and they are what these cover. Times, because a
date read in the wrong zone or invented out of a bare day is how a "what is on
today" page starts lying. Duplicates, because the same evening reaches us from
several places and three copies read as a broken app. And the review gate,
because nothing crawled should ever reach a reader unchecked.

No test here touches the network: every fetch is a stub returning a fixture,
which is also how to add a source — paste its page in and see what comes out.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone as dj_timezone

from accounts.models import AuthToken, Profile
from events.ingest import dedupe, ical, jsonld, normalise, pipeline, robots
from events.models import Event

User = get_user_model()
ATHENS = ZoneInfo('Europe/Athens')
ATH = 'Αθήνα'
SKG = 'Θεσσαλονίκη'


def page(*blocks):
    """An HTML page carrying these JSON-LD blocks."""
    scripts = ''.join(
        f'<script type="application/ld+json">{b}</script>' for b in blocks
    )
    return f'<html><head><title>x</title>{scripts}</head><body>ignored</body></html>'


CONCERT = '''
{"@context": "https://schema.org", "@type": "MusicEvent",
 "@id": "https://venue.gr/e/1",
 "name": "Μια συναυλία",
 "startDate": "2026-11-20T21:00",
 "description": "  Καλή   βραδιά ",
 "location": {"@type": "Place", "name": "Γκάζι",
              "address": {"@type": "PostalAddress",
                          "streetAddress": "Πειραιώς 100",
                          "addressLocality": "Αθήνα"}},
 "image": "https://venue.gr/a.jpg",
 "offers": {"@type": "Offer", "url": "https://tickets.gr/1",
            "availability": "https://schema.org/InStock"}}
'''


class JsonLdTests(TestCase):
    def test_it_finds_an_event_and_ignores_everything_else(self):
        html = page('{"@type": "Organization", "name": "Venue"}', CONCERT)

        found = jsonld.events_in(html)

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['name'], 'Μια συναυλία')

    def test_it_looks_inside_graphs_and_lists(self):
        html = page('{"@graph": [' + CONCERT + ']}')

        self.assertEqual(len(jsonld.events_in(html)), 1)

    def test_one_broken_block_does_not_cost_the_others(self):
        html = page('{"@type": "Event", oops}', CONCERT)

        self.assertEqual(len(jsonld.events_in(html)), 1)

    def test_a_page_with_no_structured_data_yields_nothing(self):
        """Which is the common case on the Greek sites surveyed — it has to be
        quiet, not an error."""
        self.assertEqual(jsonld.events_in('<html><body>Πρόγραμμα</body></html>'), [])


class TimeTests(TestCase):
    def test_a_time_without_a_zone_is_athens_time(self):
        """The bug this prevents: 21:00 read as UTC becomes midnight, which
        moves the event to the next day."""
        parsed = normalise.parse_iso('2026-11-20T21:00')

        self.assertEqual(parsed.tzinfo, ATHENS)
        self.assertEqual(parsed.hour, 21)

    def test_a_zoned_time_is_left_alone(self):
        parsed = normalise.parse_iso('2026-11-20T21:00:00Z')

        self.assertEqual(parsed.utcoffset(), timedelta(0))

    def test_a_day_without_a_time_is_flagged_rather_than_guessed(self):
        candidate = normalise.from_jsonld(
            {'@type': 'Event', 'name': 'Έκθεση', 'startDate': '2026-11-20'},
            city=ATH, source_url='https://x.gr',
        )

        self.assertFalse(candidate.time_known)
        self.assertTrue(candidate.warnings)

    def test_an_ical_all_day_entry_is_flagged_too(self):
        rows = ical.events_in(
            'BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:1\r\nSUMMARY:Γιορτή\r\n'
            'DTSTART;VALUE=DATE:20261120\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n',
            default_tz=ATHENS,
        )

        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]['time_known'])

    def test_ical_reads_folded_lines_and_local_times(self):
        rows = ical.events_in(
            'BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:42\r\n'
            'SUMMARY:Μια πολύ μεγάλη\r\n  γραμμή\r\n'
            'DTSTART;TZID=Europe/Athens:20261120T210000\r\n'
            'LOCATION:Μέγαρο\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n',
            default_tz=ATHENS,
        )

        self.assertEqual(rows[0]['title'], 'Μια πολύ μεγάλη γραμμή')
        self.assertEqual(rows[0]['starts_at'].hour, 21)
        self.assertTrue(rows[0]['time_known'])


class CandidateTests(TestCase):
    def _candidate(self):
        return normalise.from_jsonld(
            __import__('json').loads(CONCERT), city=SKG, source_url='https://venue.gr/x',
        )

    def test_it_reads_the_fields_a_person_would_have_typed(self):
        c = self._candidate()

        self.assertEqual(c.title, 'Μια συναυλία')
        self.assertEqual(c.description, 'Καλή βραδιά')
        self.assertEqual(c.location, 'Γκάζι, Πειραιώς 100, Αθήνα')
        self.assertEqual(c.tickets_url, 'https://tickets.gr/1')
        self.assertTrue(c.has_tickets)

    def test_the_page_can_correct_the_city_the_source_was_filed_under(self):
        """A national ticket site is one source, not one per city — the
        address on the event is what decides."""
        self.assertEqual(self._candidate().city, ATH)

    def test_a_sold_out_offer_is_not_a_ticket_link(self):
        c = normalise.from_jsonld(
            {'@type': 'Event', 'name': 'x', 'startDate': '2026-11-20T21:00',
             'offers': {'url': 'https://t.gr/1',
                        'availability': 'https://schema.org/SoldOut'}},
            city=ATH, source_url='https://x.gr',
        )

        self.assertFalse(c.has_tickets)

    def test_an_event_with_no_name_or_no_date_is_not_an_event(self):
        for node in ({'@type': 'Event', 'startDate': '2026-11-20T21:00'},
                     {'@type': 'Event', 'name': 'Χωρίς ώρα'}):
            self.assertIsNone(
                normalise.from_jsonld(node, city=ATH, source_url='https://x.gr'))

    def test_last_month_is_not_news_and_neither_is_2036(self):
        now = datetime(2026, 11, 1, tzinfo=timezone.utc)
        def at(when):
            return normalise.from_jsonld(
                {'@type': 'Event', 'name': 'x', 'startDate': when},
                city=ATH, source_url='https://x.gr')

        self.assertFalse(normalise.is_worth_keeping(at('2026-10-01T21:00'), now=now))
        self.assertTrue(normalise.is_worth_keeping(at('2026-11-20T21:00'), now=now))
        self.assertFalse(normalise.is_worth_keeping(at('2036-11-20T21:00'), now=now))


class DedupeTests(TestCase):
    def setUp(self):
        self.when = dj_timezone.now() + timedelta(days=10)
        self.event = Event.objects.create(
            city=ATH, event_type=Event.OFFICIAL, title='Μια συναυλία',
            date=self.when, source='jsonld', external_id='https://venue.gr/e/1',
        )

    def _candidate(self, **over):
        fields = dict(title='Μια συναυλία', starts_at=self.when, city=ATH,
                      source='jsonld', source_url='https://venue.gr/x',
                      external_id='https://venue.gr/e/1')
        fields.update(over)
        return normalise.Candidate(**fields)

    def test_the_same_source_id_is_the_same_event(self):
        found = dedupe.find_existing(self._candidate(title='Renamed'), [self.event])

        self.assertEqual(found, self.event)

    def test_another_source_describing_the_same_evening_matches_on_title_and_time(self):
        candidate = self._candidate(
            source='ical', external_id='uid-99',
            title='Μια συναυλία — SOLD OUT',
            starts_at=self.when + timedelta(minutes=30),
        )

        self.assertEqual(dedupe.find_existing(candidate, [self.event]), self.event)

    def test_a_different_city_is_a_different_event(self):
        candidate = self._candidate(city=SKG, source='ical', external_id='uid-99')

        self.assertIsNone(dedupe.find_existing(candidate, [self.event]))

    def test_the_same_name_a_different_evening_is_a_different_event(self):
        candidate = self._candidate(
            source='ical', external_id='uid-99',
            starts_at=self.when + timedelta(hours=5),
        )

        self.assertIsNone(dedupe.find_existing(candidate, [self.event]))


class PipelineTests(TestCase):
    """The whole path, with the network replaced by a fixture."""

    def setUp(self):
        robots.forget()
        self.when = (dj_timezone.now() + timedelta(days=12)).astimezone(ATHENS)
        self.source = {'name': 'venue', 'city': ATH, 'kind': 'jsonld',
                       'url': 'https://venue.gr/programme'}

    def _page(self, title='Μια συναυλία', when=None, extra=''):
        when = (when or self.when).strftime('%Y-%m-%dT%H:%M:%S')
        return page(f'''
            {{"@type": "Event", "@id": "https://venue.gr/e/1", "name": "{title}",
              "startDate": "{when}" {extra}}}
        ''')

    def _fetch(self, html):
        def fetch(url, *a, **kw):
            if url.endswith('robots.txt'):
                return url, 'User-agent: *\nDisallow:\n'
            return url, html
        return fetch

    def test_it_files_what_it_finds_for_review_and_not_into_the_app(self):
        result = pipeline.ingest_source(self.source, fetch=self._fetch(self._page()))

        self.assertEqual((result.found, result.created), (1, 1))
        event = Event.objects.get()
        self.assertEqual(event.status, Event.PENDING)
        self.assertEqual(event.city, ATH)
        self.assertEqual(event.source, 'jsonld')

    def test_reading_the_same_page_again_adds_nothing(self):
        fetch = self._fetch(self._page())
        pipeline.ingest_source(self.source, fetch=fetch)

        again = pipeline.ingest_source(self.source, fetch=fetch)

        self.assertEqual(Event.objects.count(), 1)
        self.assertEqual((again.created, again.unchanged), (0, 1))

    def test_a_moved_event_is_updated_in_place(self):
        pipeline.ingest_source(self.source, fetch=self._fetch(self._page()))
        moved = self.when + timedelta(days=1)

        result = pipeline.ingest_source(
            self.source, fetch=self._fetch(self._page(when=moved)))

        self.assertEqual((Event.objects.count(), result.updated), (1, 1))
        self.assertEqual(Event.objects.get().date.date(), moved.date())

    def test_a_dry_run_writes_nothing(self):
        result = pipeline.ingest_source(
            self.source, dry_run=True, fetch=self._fetch(self._page()))

        self.assertEqual(result.created, 1)
        self.assertEqual(Event.objects.count(), 0)

    def test_a_site_that_says_no_is_not_read(self):
        def fetch(url, *a, **kw):
            if url.endswith('robots.txt'):
                return url, 'User-agent: *\nDisallow: /\n'
            raise AssertionError('fetched a page robots.txt disallowed')

        result = pipeline.ingest_source(self.source, fetch=fetch)

        self.assertIn('robots', result.error)
        self.assertEqual(Event.objects.count(), 0)

    def test_a_source_being_down_costs_only_that_source(self):
        def fetch(url, *a, **kw):
            if url.endswith('robots.txt'):
                return url, ''
            raise OSError('connection refused')

        results = pipeline.ingest([self.source], fetch=fetch)

        self.assertTrue(results[0].error)

    def test_someones_own_event_is_not_rewritten_by_a_crawler(self):
        """A person's words stay theirs. The crawl may only note that a source
        describes the same evening."""
        author = User.objects.create_user('mihaliss', password='x')
        Profile.objects.update_or_create(user=author, defaults={'city': ATH})
        mine = Event.objects.create(
            city=ATH, event_type=Event.COMMUNITY, title='Μια συναυλία',
            description='το γράφω εγώ', date=self.when, creator=author,
        )

        pipeline.ingest_source(self.source, fetch=self._fetch(self._page()))

        mine.refresh_from_db()
        self.assertEqual(Event.objects.count(), 1)
        self.assertEqual(mine.description, 'το γράφω εγώ')
        self.assertEqual(mine.status, Event.PUBLISHED)


class ReviewGateTests(TestCase):
    """Nothing pending reaches the app, whoever asks."""

    def setUp(self):
        self.user = User.objects.create_user('reader', password='x')
        Profile.objects.update_or_create(user=self.user, defaults={'city': ATH})
        self.token = AuthToken.create_for_user(self.user).key
        when = dj_timezone.now() + timedelta(days=3)
        Event.objects.create(city=ATH, event_type=Event.OFFICIAL, title='Εγκεκριμένο',
                             date=when, status=Event.PUBLISHED)
        Event.objects.create(city=ATH, event_type=Event.OFFICIAL, title='Σε αναμονή',
                             date=when, status=Event.PENDING, source='jsonld')
        Event.objects.create(city=ATH, event_type=Event.OFFICIAL, title='Απορρίφθηκε',
                             date=when, status=Event.REJECTED, source='jsonld')

    def test_the_tab_shows_only_what_was_approved(self):
        res = self.client.get('/api/events/', HTTP_AUTHORIZATION=f'Token {self.token}')

        self.assertEqual(res.status_code, 200)
        titles = {e['title'] for e in res.json()['events']}
        self.assertEqual(titles, {'Εγκεκριμένο'})

    def test_an_event_someone_creates_in_the_app_is_live_at_once(self):
        res = self.client.post(
            '/api/events/',
            data=__import__('json').dumps({'title': 'Δικό μου', 'city': ATH}),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Token {self.token}',
        )

        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(Event.objects.get(title='Δικό μου').status, Event.PUBLISHED)
