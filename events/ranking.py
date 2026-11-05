"""Which events are worth a place on the tab, and in what order.

The tab used to show every published event in the city, so a stadium show and
a bar's Tuesday quiz night sat side by side, and a play running for six weeks
took up a line on every one of those days. Ticket sellers list everything they
sell; being listed says nothing about whether anyone is going.

So every event gets a score, 0–100, out of what can actually be known about it:

    how many independent sources list it       Ticketmaster and the venue's own
                                               page and the research pass all
                                               finding it is not an accident
    how popular the research pass found it     events/ingest/research.py reads
                                               the press, the ticket sellers
                                               and public social posts, and
                                               rates 1–5 with its evidence
    how big the room is                        events/ingest/venues.py
    whether the listing is complete            a picture, a start time, tickets
    what kind of thing it is                   workshops, classes, quiz nights
                                               and the like are not what
                                               people open this tab for
    what people here did with it               going (attendees) and reporting

and the tab shows the crawled events that clear the bar, one line per run of
performances, a bounded number per day. Events people create in the app and
events someone on the team checked by hand (`curated`, `editor_pick`) are
never filtered: those are the point of the app, not noise in it.

What this deliberately does not do is touch `attendees`. The app's featured
carousel sorts by it, and it would be easy to nudge the carousel by inflating
it — but it is a count of real people, shown as such, and stays one.

Settings (all optional):

    EVENTS_MIN_QUALITY   the bar for a crawled event (default 45)
    EVENTS_MAX_PER_DAY   crawled events per city per day (default 8)
"""

import re
from datetime import timedelta

from django.conf import settings

from .ingest.dedupe import fold, has_no_time, match_key
from .ingest.venues import tier_for

#: The bar a crawled event must clear to be shown.
MIN_QUALITY = 45

#: Below the bar but not junk: used to keep a small city's tab from being
#: empty when it has little better (see select_for_listing).
FLOOR = 30

#: A city with fewer upcoming events than this above the bar is topped up from
#: the ones between FLOOR and the bar, best first.
CITY_MINIMUM = 6

#: Crawled events shown per city per day, best first.
MAX_PER_DAY = 8

#: Something that started this long ago is still on.
STILL_ON = timedelta(hours=6)

#: Source kinds that are a person's judgement rather than a crawl.
HAND_PICKED = {'curated'}

#: Titles of things that are not what people open an events tab for. Matched
#: on the folded title only: a concert's description mentioning its
#: "masterclass" the next morning is not a reason to bury the concert.
_LOW_SIGNAL = re.compile(
    r'(?<!\w)('
    r'εργαστηρι\w*|σεμιναρι\w*|μαθημα\w*|ξεναγηση\w*|ημεριδα\w*|διαλεξη\w*|'
    r'παρουσιαση βιβλιου|παιδικ\w*|για παιδια|bazaar|bazar|παζαρι\w*|'
    r'workshop\w*|seminar\w*|masterclass\w*|webinar\w*|online|'
    r'karaoke|καραοκε|open mic|jam session|quiz( night)?|yoga|γιογκα'
    r')(?!\w)'
)
_TRIBUTE = re.compile(r'(?<!\w)(tribute|αφιερωμα)(?!\w)')

#: A match between two sides ("ΑΡΗΣ – ΠΑΝΑΘΗΝΑΪΚΟΣ AKTOR"), one of them a club
#: the whole country follows. Only with the dash between two sides: "Άρης" on
#: its own is as likely to be an actor on a theatre poster.
_FIXTURE = re.compile(r'\s[–—-]\s|\svs\.?\s')
_BIG_CLUBS = re.compile(
    # Folded text (dedupe.fold) has no final sigma: casefold turns ς into σ.
    r'(?<!\w)(ολυμπιακοσ|παναθηναικοσ|αεκ|παοκ|αρησ|εθνικη ομαδα|ελλαδα|greece)(?!\w)')

_POPULARITY_POINTS = {5: 35, 4: 22, 3: 10, 2: 0, 1: -15}
_VENUE_POINTS = {3: 18, 2: 10, 1: 4}


def _setting(name, default):
    value = getattr(settings, name, None)
    return default if value in (None, '') else value


def is_crawled(event):
    """Found by an ingester rather than made by a person here or picked by hand."""
    return bool(event.source) and event.source not in HAND_PICKED


def always_shown(event):
    return event.editor_pick or not is_crawled(event)


def sources_of(event):
    """The independent sources that have listed [event]."""
    signals = event.signals if isinstance(event.signals, dict) else {}
    found = {s for s in signals.get('sources') or [] if isinstance(s, str) and s}
    if event.source and not found:
        found.add(event.source)
    return found


def popularity_of(event):
    signals = event.signals if isinstance(event.signals, dict) else {}
    try:
        value = int(signals.get('popularity') or 0)
    except (TypeError, ValueError):
        return 0
    return value if 1 <= value <= 5 else 0


def score(event, *, reports=0):
    """(0–100, [reasons]) for [event]. Pure: reads the row, writes nothing."""
    reasons = []
    signals = event.signals if isinstance(event.signals, dict) else {}

    if event.editor_pick:
        return 100, ['editor pick']
    if not event.source:
        points = 50
        reasons.append('created in the app')
    elif event.source in HAND_PICKED:
        points = 70
        reasons.append('checked by hand')
    else:
        points = 25

    extra = len(sources_of(event)) - 1
    if extra > 0:
        points += 15 * min(extra, 2)
        reasons.append(f'listed by {extra + 1} sources')
    elif signals.get('verified'):
        # One source, but its claim was checked against the page it cited.
        points += 8
        reasons.append('confirmed on its source page')

    popularity = popularity_of(event)
    if popularity:
        points += _POPULARITY_POINTS[popularity]
        reasons.append(f'popularity {popularity}/5')

    tier = tier_for(event.location, event.source_url)
    if tier:
        points += _VENUE_POINTS[tier]
        reasons.append(f'venue tier {tier}')

    if event.date is not None and not has_no_time(event.date):
        points += 5
    else:
        # A day with no hour cannot honestly sit on a page about tonight.
        points -= 25
        reasons.append('no start time')
    if event.image_url:
        points += 7
    if event.tickets_url or event.has_tickets:
        points += 3
    if len(event.description or '') >= 80:
        points += 2

    title = fold(event.title)
    if _FIXTURE.search(title) and _BIG_CLUBS.search(title):
        points += 12
        reasons.append('a top-flight club')
    if _LOW_SIGNAL.search(title):
        points -= 25
        reasons.append('a class or workshop, not a night out')
    elif _TRIBUTE.search(title):
        points -= 12
        reasons.append('a tribute act')

    if event.attendees:
        points += min(event.attendees * 3, 24)
        reasons.append(f'{event.attendees} going')
    if reports:
        points -= 10 * reports
        reasons.append(f'reported {reports}×')

    return max(0, min(100, points)), reasons


def refresh(event, *, save=False):
    """Recompute and store [event]'s score. Returns it."""
    reports = event.reports.count() if event.pk else 0
    event.quality = score(event, reports=reports)[0]
    if save and event.pk:
        type(event).objects.filter(pk=event.pk).update(quality=event.quality)
    return event.quality


def _is_upcoming(event, now):
    return event.date is not None and event.date >= now - STILL_ON


def _collapse_runs(events, now):
    """One line per show, however many nights it runs.

    The occurrence kept is the next one still on; for a run that has ended, the
    last night, so it can sit once in the app's "completed" section.
    """
    runs = {}
    for event in events:
        # A title that is all noise ("Live") says nothing about which show it
        # is, so it is a run of one rather than one with every other "Live".
        key = match_key(event.title) or f'#{event.pk or id(event)}'
        runs.setdefault((event.city, key), []).append(event)
    kept = []
    for occurrences in runs.values():
        ahead = [e for e in occurrences if _is_upcoming(e, now)]
        if ahead:
            kept.append(min(ahead, key=lambda e: e.date))
        else:
            kept.append(max(occurrences, key=lambda e: e.date))
    return kept


def select_for_listing(events, *, now, min_quality=None, max_per_day=None):
    """The events the tab should show out of [events], best first.

    [events] is everything published for the request's city (or cities) in
    the window the tab covers; [now] is in the stored convention (see
    events.views._wall_clock_now). Each returned event carries `rank`.
    """
    min_quality = int(_setting('EVENTS_MIN_QUALITY', MIN_QUALITY)
                      if min_quality is None else min_quality)
    max_per_day = int(_setting('EVENTS_MAX_PER_DAY', MAX_PER_DAY)
                      if max_per_day is None else max_per_day)

    for event in events:
        event.rank = score(event, reports=getattr(event, 'report_count', 0) or 0)[0]

    kept = [e for e in events if always_shown(e)]
    crawled = _collapse_runs(
        [e for e in events if not always_shown(e) and e.date is not None], now)

    passed = [e for e in crawled if e.rank >= min_quality]
    spare = sorted((e for e in crawled
                    if FLOOR <= e.rank < min_quality and _is_upcoming(e, now)),
                   key=lambda e: (-e.rank, e.date))

    # A small city with three listings in a month is better served by its
    # best three than by an empty tab — but never by the ones below FLOOR.
    upcoming = {}
    for event in kept + passed:
        if _is_upcoming(event, now):
            upcoming[event.city] = upcoming.get(event.city, 0) + 1
    for event in spare:
        if upcoming.get(event.city, 0) < CITY_MINIMUM:
            passed.append(event)
            upcoming[event.city] = upcoming.get(event.city, 0) + 1

    by_day = {}
    for event in sorted(passed, key=lambda e: (-e.rank, e.date)):
        key = (event.city, event.date.date())
        if len(by_day.setdefault(key, [])) < max_per_day:
            by_day[key].append(event)

    shown = kept + [e for day in by_day.values() for e in day]
    shown.sort(key=lambda e: (-e.rank, e.date is None, e.date or now))
    return shown
