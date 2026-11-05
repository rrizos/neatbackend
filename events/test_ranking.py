"""What the tab shows, and how the research pass feeds it.

The failure this guards against is the one that prompted it: a tab full of
whatever a ticket seller happens to list. So the cases are about what is left
out as much as what is kept — the quiz night, the forty nights of one play,
the listing with no hour — and about never leaving out what people here made.

No test touches the network: the model providers are stubbed with the shapes
their APIs return, and every page fetch is a fixture.
"""

import importlib.util
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock, skipUnless

from django.contrib.auth import get_user_model
from django.test import TestCase

from accounts.models import AuthToken, Profile
from events import ranking
from events.ingest import dedupe, pipeline, research, research_claude, research_gemini, robots
from events.ingest.venues import tier_for
from events.models import Event
from events.views import _wall_clock_now

User = get_user_model()
ATH = 'Αθήνα'
SMALL = 'Γρεβενά'


def at(days, hour=21):
    """A start [days] ahead, in the stored convention (Athens wall clock)."""
    return (_wall_clock_now() + timedelta(days=days)).replace(
        hour=hour, minute=0, second=0, microsecond=0)


def make(title, days=3, *, city=ATH, source='ticketmaster', status=Event.PUBLISHED,
         hour=21, **fields):
    return Event.objects.create(
        city=city, event_type=Event.OFFICIAL, title=title, date=at(days, hour),
        status=status, source=source, **fields)


def shown(events=None):
    events = list(Event.objects.all()) if events is None else events
    return [e.title for e in ranking.select_for_listing(events, now=_wall_clock_now())]


class ScoreTests(TestCase):
    def test_a_bare_listing_scores_under_the_bar(self):
        event = make('Κάποιο live', image_url='https://x/a.jpg', tickets_url='https://t/1')

        self.assertLess(ranking.score(event)[0], ranking.MIN_QUALITY)

    def test_corroboration_popularity_and_a_big_room_lift_it(self):
        event = make('Μεγάλη συναυλία', location='ΟΑΚΑ, Μαρούσι',
                     image_url='https://x/a.jpg',
                     signals={'sources': ['ticketmaster', 'research'], 'popularity': 5})

        points, reasons = ranking.score(event)

        self.assertGreaterEqual(points, 90)
        self.assertIn('listed by 2 sources', reasons)
        self.assertIn('venue tier 3', reasons)

    def test_a_workshop_is_not_a_night_out(self):
        plain = make('Βραδιά τζαζ', image_url='https://x/a.jpg')
        class_ = make('Εργαστήρι κεραμικής', image_url='https://x/a.jpg')

        self.assertLess(ranking.score(class_)[0], ranking.score(plain)[0] - 20)

    def test_a_day_with_no_hour_is_marked_down(self):
        timed = make('Συναυλία Α')
        undated_hour = make('Συναυλία Β', hour=0)

        self.assertIn('no start time', ranking.score(undated_hour)[1])
        self.assertLess(ranking.score(undated_hour)[0], ranking.score(timed)[0])

    def test_a_big_club_playing_counts_but_a_namesake_on_a_poster_does_not(self):
        match = make('ΑΡΗΣ – ΠΑΝΑΘΗΝΑΪΚΟΣ AKTOR')
        play = make('Ο Άρης Σερβετάλης στον Άμλετ')

        self.assertIn('a top-flight club', ranking.score(match)[1])
        self.assertNotIn('a top-flight club', ranking.score(play)[1])

    def test_people_going_counts_and_reports_count_against(self):
        event = make('Συναυλία', attendees=4)

        self.assertEqual(ranking.score(event)[0] - ranking.score(event, reports=2)[0], 20)
        self.assertIn('4 going', ranking.score(event)[1])


class VenueTests(TestCase):
    def test_capitals_and_accents_do_not_matter(self):
        self.assertEqual(tier_for('ΘΕΑΤΡΟ ΛΥΚΑΒΗΤΤΟΥ'), 3)
        self.assertEqual(tier_for('Θέατρο Λυκαβηττού, Αθήνα'), 3)

    def test_a_short_name_matches_only_as_a_word(self):
        self.assertEqual(tier_for('ΣΕΦ, Φάληρο'), 3)
        self.assertEqual(tier_for('Καφέ Σεφέρης'), 0)

    def test_a_venue_site_names_its_own_room(self):
        self.assertEqual(tier_for('Αίθουσα Αιμίλιος Ριάδης', 'https://www.tch.gr/el/x'), 3)
        self.assertEqual(tier_for('', 'https://www.more.com/x'), 0)


class ListingTests(TestCase):
    def _good(self, title, days=3, **fields):
        return make(title, days, image_url='https://x/a.jpg', location='Gazarte',
                    signals={'sources': ['ticketmaster', 'research'], 'popularity': 4},
                    **fields)

    def test_the_city_best_are_shown_and_filler_is_not(self):
        for n in range(7):
            self._good(f'Μεγάλο όνομα {n}', days=n + 1)
        make('Βραδιά κουίζ', image_url='https://x/a.jpg')
        make('Άγνωστο live σε μπαρ')

        titles = shown()

        self.assertEqual(len(titles), 7)
        self.assertNotIn('Βραδιά κουίζ', titles)
        self.assertNotIn('Άγνωστο live σε μπαρ', titles)

    def test_what_people_made_here_is_always_shown(self):
        for n in range(7):
            self._good(f'Μεγάλο όνομα {n}', days=n + 1)
        Event.objects.create(city=ATH, event_type=Event.COMMUNITY, title='Το πάρτι μου',
                             date=at(2), status=Event.PUBLISHED)
        make('Επιλογή της ομάδας', editor_pick=True)
        make('Ελεγμένο με το χέρι', source='curated')

        titles = shown()

        for title in ('Το πάρτι μου', 'Επιλογή της ομάδας', 'Ελεγμένο με το χέρι'):
            self.assertIn(title, titles)
        self.assertEqual(titles[0], 'Επιλογή της ομάδας')

    def test_a_run_of_performances_is_one_line(self):
        for n in range(1, 6):
            self._good('Ο Βυσσινόκηπος', days=n)

        result = ranking.select_for_listing(list(Event.objects.all()), now=_wall_clock_now())

        self.assertEqual([e.title for e in result], ['Ο Βυσσινόκηπος'])
        self.assertEqual(result[0].date, at(1))

    def test_one_day_cannot_take_over_the_tab(self):
        for n in range(12):
            self._good(f'Παράσταση {n}', days=4)

        self.assertEqual(len(shown()), ranking.MAX_PER_DAY)

    def test_a_small_city_keeps_its_best_rather_than_going_empty(self):
        make('Τοπική συναυλία', city=SMALL, image_url='https://x/a.jpg')
        make('Παιδικό εργαστήρι', city=SMALL, image_url='https://x/a.jpg')

        titles = shown()

        self.assertEqual(titles, ['Τοπική συναυλία'])


class EndpointTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('reader2', password='x')
        Profile.objects.update_or_create(user=self.user, defaults={'city': ATH})
        self.auth = f'Token {AuthToken.create_for_user(self.user).key}'

    def test_the_tab_returns_the_ranked_list_in_the_same_shape(self):
        for n in range(7):
            make(f'Μεγάλο όνομα {n}', days=n + 1, location='Fuzz Club',
                 image_url='https://x/a.jpg', signals={'sources': ['a', 'b'], 'popularity': 4})
        make('Karaoke night')

        res = self.client.get('/api/events/', HTTP_AUTHORIZATION=self.auth)

        self.assertEqual(res.status_code, 200)
        events = res.json()['events']
        self.assertEqual(len(events), 7)
        self.assertNotIn('Karaoke night', {e['title'] for e in events})
        # The app reads these keys; nothing was added or removed.
        self.assertEqual(set(events[0]), {
            'id', 'city', 'eventType', 'title', 'description', 'location', 'imageUrl',
            'category', 'date', 'creator', 'organizer', 'hasTickets', 'ticketsUrl',
            'attendees', 'isAttending', 'created'})


class DedupeFoldingTests(TestCase):
    def test_capitals_without_accents_are_the_same_title(self):
        self.assertTrue(dedupe.titles_match('ΛΕΥΤΕΡΗΣ ΠΑΝΤΑΖΗΣ', 'Λευτέρης Πανταζής live'))

    def test_a_day_without_an_hour_meets_the_same_day_with_one(self):
        self.assertTrue(dedupe.same_evening(at(5, hour=0), at(5, hour=21)))
        self.assertFalse(dedupe.same_evening(at(5, hour=0), at(6, hour=21)))
        self.assertFalse(dedupe.same_evening(at(5, hour=18), at(5, hour=22)))


def research_row(**over):
    row = {
        'title': 'Λευτέρης Πανταζής', 'starts_at': at(10).strftime('%Y-%m-%dT21:00'),
        'venue': 'Christmas Theater', 'category': 'Live Concert',
        'description': 'Η μεγάλη επιστροφή.', 'tickets_url': 'https://www.more.com/t/1',
        'image_url': '', 'source_urls': ['https://www.more.com/t/1'],
        'popularity': 4, 'why': 'Δεύτερη ημερομηνία μετά το sold out.',
    }
    row.update(over)
    return row


class ResearchPipelineTests(TestCase):
    """The hybrid: research landing on what the structured sources found."""

    SOURCE = {'name': 'research-Αθήνα', 'city': ATH, 'kind': 'research', 'url': ''}

    def setUp(self):
        robots.forget()

    def _run(self, rows, **kw):
        with mock.patch.object(research, 'provider_name', return_value='stub'), \
                mock.patch.object(research, '_providers', return_value={'stub': lambda c, prompt: rows}):
            return pipeline.ingest_source(self.SOURCE, fetch=self._no_fetch, **kw)

    @staticmethod
    def _no_fetch(url, *a, **kw):
        raise OSError('offline')

    def _ticketmaster_row(self):
        return Event.objects.create(
            city=ATH, event_type=Event.OFFICIAL, title='ΛΕΥΤΕΡΗΣ ΠΑΝΤΑΖΗΣ', date=at(10),
            status=Event.PENDING, source='ticketmaster', external_id='tm-1',
            location='Christmas Theater', image_url='https://x/a.jpg',
            tickets_url='https://tm/1', content_hash='tm-hash')

    def test_research_on_an_evening_already_listed_adds_its_signal_not_a_copy(self):
        tm = self._ticketmaster_row()
        before = ranking.score(tm)[0]

        result = self._run([research_row()])

        self.assertEqual(Event.objects.count(), 1)
        tm.refresh_from_db()
        self.assertEqual(tm.signals['popularity'], 4)
        self.assertEqual(set(tm.signals['sources']), {'ticketmaster', 'research'})
        self.assertGreater(tm.quality, before)
        # Research never moves the facts the owner gave, nor its hash.
        self.assertEqual((tm.title, tm.date, tm.content_hash), ('ΛΕΥΤΕΡΗΣ ΠΑΝΤΑΖΗΣ', at(10), 'tm-hash'))
        self.assertEqual(tm.status, Event.PENDING)
        self.assertEqual(result.found, 1)

    def test_two_sources_agreeing_can_publish_when_that_is_switched_on(self):
        tm = self._ticketmaster_row()

        result = self._run([research_row()], publish_above=60)

        tm.refresh_from_db()
        self.assertEqual(tm.status, Event.PUBLISHED)
        self.assertEqual(result.published, 1)

    def test_one_source_alone_is_never_auto_published(self):
        self._run([research_row()], publish_above=1)

        event = Event.objects.get()
        self.assertEqual((event.source, event.status), ('research', Event.PENDING))
        self.assertEqual(event.signals['popularity'], 4)

    def test_a_row_citing_nothing_is_not_filed(self):
        self._run([research_row(source_urls=[])])

        self.assertEqual(Event.objects.count(), 0)

    def test_a_structured_source_takes_over_a_row_research_found(self):
        self._run([research_row()])
        from events.ingest import ticketmaster

        tm_row = {'id': 'tm-9', 'name': 'ΛΕΥΤΕΡΗΣ ΠΑΝΤΑΖΗΣ', 'url': 'https://tm/9',
                  'dates': {'start': {'localDate': at(10).strftime('%Y-%m-%d'),
                                      'localTime': '21:00:00'}},
                  '_embedded': {'venues': [{'name': 'Christmas Theater', 'city': {'name': 'Athens'}}]}}
        with mock.patch.object(ticketmaster, 'fetch_city', return_value=[tm_row]), \
                self.settings(TICKETMASTER_API_KEY='k'):
            pipeline.ingest_source({'name': 'tm', 'city': ATH, 'kind': 'ticketmaster', 'url': ''})

        event = Event.objects.get()
        self.assertEqual((event.source, event.external_id), ('ticketmaster', 'tm-9'))
        self.assertEqual(set(event.signals['sources']), {'research', 'ticketmaster'})
        self.assertEqual(event.signals['popularity'], 4)

    def test_a_dry_run_does_not_copy_posters(self):
        self._ticketmaster_row()
        Event.objects.update(image_url='')

        with mock.patch.object(pipeline, '_hosted_image') as copy:
            self._run([research_row(image_url='https://www.more.com/p.jpg')], dry_run=True)

        copy.assert_not_called()
        self.assertEqual(Event.objects.get().image_url, '')

    def test_no_provider_is_a_line_in_the_run_not_a_crash(self):
        with self.settings(ANTHROPIC_API_KEY='', GEMINI_API_KEY='', EVENTS_RESEARCH_PROVIDER=''):
            result = pipeline.ingest_source(self.SOURCE, fetch=self._no_fetch)

        self.assertIn('no research provider', result.error)


class VerifyTests(TestCase):
    def setUp(self):
        robots.forget()

    def _fetch(self, pages):
        def fetch(url, *a, **kw):
            if url.endswith('robots.txt'):
                return url, ''
            if url not in pages:
                raise AssertionError(f'fetched {url}')
            return url, pages[url]
        return fetch

    def test_a_page_with_the_title_and_the_date_confirms_it(self):
        day = at(10)
        row = research_row(source_urls=['https://more.com/t/1'])
        page = f'<h1>ΛΕΥΤΕΡΗΣ ΠΑΝΤΑΖΗΣ</h1><p>{day.day} {research._GREEK_MONTHS[day.month - 1].upper()}</p>'

        research.verify([row], fetch=self._fetch({'https://more.com/t/1': page}))

        self.assertTrue(row['verified'])

    def test_the_right_title_on_another_date_does_not(self):
        other = at(12)
        row = research_row(source_urls=['https://more.com/t/1'])
        page = f'<h1>Λευτέρης Πανταζής</h1><p>{other.strftime("%d/%m/%Y")}</p>'

        research.verify([row], fetch=self._fetch({'https://more.com/t/1': page}))

        self.assertFalse(row['verified'])

    def test_social_pages_are_cited_but_never_fetched(self):
        row = research_row(source_urls=['https://www.instagram.com/p/abc/'])

        research.verify([row], fetch=self._fetch({}))

        self.assertFalse(row['verified'])


class GeminiTests(TestCase):
    def _answer(self, rows, grounded=True):
        steps = []
        if grounded:
            steps.append({'type': 'google_search_call', 'arguments': {'queries': ['x']}})
        steps.append({'type': 'model_output', 'content': [
            {'type': 'text', 'text': '```json\n' + json.dumps({'events': rows}) + '\n```',
             'annotations': [{'type': 'url_citation', 'url': 'https://more.com/t/1'}] if grounded else []}]})
        return {'steps': steps}

    def test_a_grounded_answer_is_read(self):
        with self.settings(GEMINI_API_KEY='k'):
            rows = research_gemini.research(
                ATH, prompt='p', post=lambda *a, **k: self._answer([research_row()]))

        self.assertEqual(rows[0]['title'], 'Λευτέρης Πανταζής')

    def test_an_answer_with_no_search_behind_it_is_dropped(self):
        with self.settings(GEMINI_API_KEY='k'):
            rows = research_gemini.research(
                ATH, prompt='p', post=lambda *a, **k: self._answer([research_row()], grounded=False))

        self.assertEqual(rows, [])


def block(**fields):
    return SimpleNamespace(**fields)


class FakeClaude:
    """The two calls research_claude makes, answering from a script."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **request):
        self.requests.append(request)
        response = self.responses.pop(0)

        class Stream:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def get_final_message(self_inner):
                return response
        return Stream()


def searched(*urls):
    return block(type='web_search_tool_result',
                 content=[block(type='web_search_result', url=u) for u in urls])


@skipUnless(importlib.util.find_spec('anthropic'), 'anthropic is not installed')
class ClaudeTests(TestCase):
    def test_only_citations_search_returned_survive(self):
        real = research_row(source_urls=['https://www.more.com/t/1/'])
        invented = research_row(title='Φανταστική συναυλία', source_urls=['https://made.up/x'])
        client = FakeClaude(SimpleNamespace(stop_reason='tool_use', content=[
            searched('https://more.com/t/1'),
            block(type='tool_use', name='submit_events', input={'events': [real, invented]}),
        ]))

        rows = research_claude.research(ATH, prompt='p', client=client)

        self.assertEqual([r['title'] for r in rows], ['Λευτέρης Πανταζής'])
        request = client.requests[0]
        self.assertEqual(request['model'], 'claude-opus-5-5')
        self.assertEqual({t.get('type', t.get('name')) for t in request['tools']},
                         {'web_search_20260209', 'web_fetch_20260209', 'submit_events'})

    def test_a_paused_search_is_resumed_not_restarted(self):
        paused = SimpleNamespace(stop_reason='pause_turn', content=[searched('https://more.com/t/1')])
        done = SimpleNamespace(stop_reason='tool_use', content=[
            block(type='tool_use', name='submit_events', input={'events': [research_row()]})])
        client = FakeClaude(paused, done)

        rows = research_claude.research(ATH, prompt='p', client=client)

        self.assertEqual(len(rows), 1)
        second = client.requests[1]['messages']
        self.assertEqual([m['role'] for m in second], ['user', 'assistant'])

    def test_a_cut_off_answer_is_not_trusted(self):
        client = FakeClaude(SimpleNamespace(stop_reason='max_tokens', content=[
            searched('https://more.com/t/1'),
            block(type='tool_use', name='submit_events', input={'events': [research_row()]})]))

        self.assertEqual(research_claude.research(ATH, prompt='p', client=client), [])


class SourceSelectionTests(TestCase):
    def test_research_runs_after_the_structured_sources(self):
        from events.ingest import sources

        kinds = [s['kind'] for s in sources.for_cities([ATH])]

        self.assertEqual(kinds[-1], 'research')
        self.assertTrue(all(k != 'research' for k in kinds[:-1]))

    def test_a_named_small_city_is_researched_too(self):
        from events.ingest import sources

        chosen = sources.for_cities([SMALL], only='research')

        self.assertEqual(chosen, [sources.research_seed(SMALL)])

    def test_the_prompt_names_the_city_window_and_the_social_route(self):
        text = research.prompt_for(ATH, today='2026-11-05')

        self.assertIn('Αθήνα (Athens)', text)
        self.assertIn('2026-12-10', text)
        self.assertIn('Instagram', text)
