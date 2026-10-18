"""Hand-checked events, and the event list they land in.

The things that would embarrass us on a page of real events: the wrong hour, the
same evening listed twice, last month's concert, and a create button that
silently fails. Nothing here touches the network — image copying is stubbed.
"""

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.utils import timezone as dj_timezone

from accounts.models import AuthToken, Profile
from events.ingest import curated, normalise, pipeline
from events.models import Event

User = get_user_model()
ATHENS = ZoneInfo('Europe/Athens')
ATH = 'Αθήνα'


def entry(**over):
    fields = {
        'title': 'Μεγάλη συναυλία',
        'category': 'Music Concert',
        'city': ATH,
        'start': '2026-11-20T21:00:00',
        'time_known': True,
        'venue': 'Christmas Theater',
        'address': 'Λεωφ. Βουλιαγμένης 2, Γαλάτσι',
        'lat': 38.0234, 'lon': 23.7612,
        'description': 'Μια βραδιά που περιμέναμε.',
        'organizer': 'Οργανωτής',
        'tickets_url': 'https://tickets.example.gr/e/1',
        'has_tickets': True,
        'source_url': 'https://venue.example.gr/e/1',
        'image_url': 'https://venue.example.gr/poster.jpg',
    }
    fields.update(over)
    return fields


NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


def load(entries, **kw):
    kw.setdefault('now', NOW)
    kw.setdefault('store_image', lambda url: '/media/events/copied.jpg')
    return curated.load(entries, **kw)


class CuratedLoadTests(TestCase):
    def test_an_entry_is_published_as_an_official_event(self):
        report = load([entry()])

        self.assertEqual(len(report.created), 1)
        event = Event.objects.get()
        self.assertEqual(event.status, Event.PUBLISHED)
        self.assertEqual(event.event_type, Event.OFFICIAL)
        self.assertEqual(event.source, curated.SOURCE)
        self.assertEqual(event.category, 'Music Concert')
        self.assertTrue(event.has_tickets)

    def test_the_hour_on_the_poster_is_the_hour_in_the_app(self):
        load([entry(start='2026-11-20T21:00:00')])

        # Stored as the wall clock labelled UTC — what the app shows verbatim.
        self.assertEqual(Event.objects.get().date,
                         datetime(2026, 11, 20, 21, 0, tzinfo=timezone.utc))

    def test_the_map_gets_coordinates_and_the_card_gets_the_place(self):
        load([entry()])

        location = Event.objects.get().location
        coords, shown = location.split('|', 1)
        self.assertEqual(coords, '38.023400,23.761200')
        self.assertEqual(shown, 'Christmas Theater, Λεωφ. Βουλιαγμένης 2, Γαλάτσι')

    def test_the_poster_is_copied_to_our_media(self):
        load([entry()])

        self.assertEqual(Event.objects.get().image_url, '/media/events/copied.jpg')

    def test_a_poster_that_cannot_be_copied_keeps_its_original_url(self):
        load([entry()], store_image=lambda url: '')

        self.assertEqual(Event.objects.get().image_url, 'https://venue.example.gr/poster.jpg')

    def test_loading_the_same_file_again_adds_nothing(self):
        load([entry()])
        report = load([entry()])

        self.assertEqual(Event.objects.count(), 1)
        self.assertEqual(len(report.unchanged), 1)

    def test_a_corrected_entry_updates_the_same_row(self):
        load([entry()])
        first = Event.objects.get()
        report = load([entry(description='Νέα περιγραφή.')])

        self.assertEqual(len(report.updated), 1)
        event = Event.objects.get()
        self.assertEqual(event.pk, first.pk)
        self.assertEqual(event.description, 'Νέα περιγραφή.')

    def test_an_evening_someone_already_listed_is_not_listed_twice(self):
        Event.objects.create(
            city=ATH, event_type=Event.COMMUNITY, title='Μεγάλη συναυλία',
            date=datetime(2026, 11, 20, 21, 0, tzinfo=timezone.utc),
        )

        report = load([entry()])

        self.assertEqual(Event.objects.count(), 1)
        self.assertEqual(report.skipped[0][1], 'already listed')

    def test_what_cannot_be_checked_or_shown_is_skipped(self):
        report = load([
            entry(title='Παλιά', start='2026-09-01T21:00:00'),
            entry(title='Χωρίς πηγή', source_url=''),
            entry(title='Άγνωστη πόλη', city='Atlantis'),
            entry(title='Άγνωστη κατηγορία', category='Concerts'),
        ])

        self.assertEqual(Event.objects.count(), 0)
        self.assertEqual(len(report.skipped), 4)

    def test_an_overlong_ticket_link_loses_its_tracking_not_the_row(self):
        long_link = 'https://tickets.example.gr/e/1?' + 'utm=x&' * 60
        load([entry(tickets_url=long_link)])

        self.assertEqual(Event.objects.get().tickets_url, 'https://tickets.example.gr/e/1')

    def test_a_dry_run_writes_nothing(self):
        report = load([entry()], dry_run=True)

        self.assertEqual(len(report.created), 1)
        self.assertEqual(Event.objects.count(), 0)

    def test_the_file_can_be_taken_down_again(self):
        load([entry(), entry(title='Δεύτερη', start='2026-11-21T20:00:00')])
        Event.objects.create(city=ATH, event_type=Event.COMMUNITY, title='Κάποιου άλλου')

        removed = curated.remove([entry()])

        self.assertEqual(removed, 1)
        self.assertEqual(curated.remove(), 1)
        self.assertEqual(list(Event.objects.values_list('title', flat=True)), ['Κάποιου άλλου'])


class IngestedTimeTests(TestCase):
    def test_a_crawled_event_is_stored_at_the_hour_on_the_page(self):
        candidate = normalise.Candidate(
            title='Συναυλία', starts_at=datetime(2026, 11, 20, 21, 0, tzinfo=ATHENS),
            city=ATH, source='jsonld', source_url='https://v.gr', external_id='v-1',
        )

        pipeline._write(pipeline.Result('jsonld'), [candidate], now=NOW, dry_run=False)

        self.assertEqual(Event.objects.get().date.astimezone(timezone.utc).hour, 21)


class EventListTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('reader', password='x')
        Profile.objects.update_or_create(user=self.user, defaults={'city': ATH})
        self.auth = {'HTTP_AUTHORIZATION': f'Token {AuthToken.create_for_user(self.user).key}'}

    def test_creating_an_event_passes_the_csrf_check(self):
        # The app sends a token, never a CSRF cookie. Django's test client skips
        # CSRF unless asked, which is how this was broken without a failing test.
        client = Client(enforce_csrf_checks=True)
        res = client.post('/api/events/', data=json.dumps({'title': 'Δικό μου'}),
                          content_type='application/json', **self.auth)

        self.assertEqual(res.status_code, 201, res.content)

    def test_long_finished_events_are_not_sent(self):
        today = dj_timezone.now()
        for title, days in (('Χθες', -1), ('Πριν 10 μέρες', -10), ('Αύριο', 1)):
            Event.objects.create(city=ATH, event_type=Event.OFFICIAL, title=title,
                                 date=today + timedelta(days=days))
        Event.objects.create(city=ATH, event_type=Event.COMMUNITY, title='Χωρίς ημερομηνία')

        res = self.client.get('/api/events/', **self.auth)

        titles = {e['title'] for e in res.json()['events']}
        self.assertEqual(titles, {'Χθες', 'Αύριο', 'Χωρίς ημερομηνία'})
