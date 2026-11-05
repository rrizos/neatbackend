"""Asking what is actually on in a city, the way a well-read local would.

The other sources answer "what is for sale": Ticketmaster lists everything it
sells, and a venue's programme lists everything it hosts. Neither says what
people are talking about, which is the question the events tab has to answer.
This one asks it directly: a model with web search reads the ticket sellers,
the culture press, local news and the public social posts that search engines
index (Instagram, Facebook and TikTok pages of venues, artists and promoters),
and comes back with the city's notable events, each with the pages it read
and a 1–5 rating of how big it is, with its evidence.

That makes it two things in the pipeline at once:

- a **source**, for what the others cannot reach — a derby, a festival, a
  sold-out show sold through a club's own site;
- a **signal** on what the others found: when it names an evening Ticketmaster
  already listed, the row gains its popularity rather than a duplicate. See
  events/ranking.py for how much that counts.

What it does not do is log into Instagram or Facebook, or crawl them. Meta's
terms forbid automated collection and closed public event access in 2018;
reading what a search engine shows anyone is a different thing, and is what
this does. Every page we fetch ourselves (to check a claim) goes through the
same robots.txt check and SSRF-safe fetcher as the rest of the ingester, and
social sites are never fetched at all.

Three guards against a model that sounds sure and is wrong:

1. Each event must cite a page. With Claude, the cited page must be one its
   search actually returned in this conversation; with Gemini, the answer
   must be grounded in a search at all, or the whole answer is dropped.
2. The best-rated events are checked: we fetch a cited page and look for the
   title and the date on it. That sets `verified`, which ranking.py rewards.
3. It lands as `pending`, like every crawl — unless a later step finds a
   second independent source agreeing (see pipeline.py's auto-publish).

Providers (EVENTS_RESEARCH_PROVIDER, default: whichever key is configured):

    claude   research_claude.py, Anthropic's web search tool (ANTHROPIC_API_KEY)
    gemini   research_gemini.py, Google Search grounding (GEMINI_API_KEY)
"""

import json
import logging
import re
from datetime import date, timedelta
from urllib.parse import urlsplit

from django.conf import settings

from . import curated, robots
from .dedupe import fold, match_key

logger = logging.getLogger(__name__)

#: How far ahead to look. Further than the tab's featured horizon (45 days)
#: is wasted search; nearer misses the big shows people book a month out.
DAYS = 35

#: How many events to ask for per city. More than a day-by-day tab can use,
#: fewer than would push the model into listing filler to reach the number.
MAX_EVENTS = 25

#: Only rows at least this popular are worth a fetch to verify: the rest
#: will not clear the bar either way.
VERIFY_FROM = 3
MAX_VERIFY_FETCHES = 20

#: Never fetched by us, whatever they cite: login walls, and sites whose
#: terms forbid it. Citing them is fine — the model saw them through search.
SOCIAL_HOSTS = ('instagram.com', 'facebook.com', 'fb.com', 'fb.me', 'tiktok.com',
                'x.com', 'twitter.com', 'threads.net', 'youtube.com', 'youtu.be')

CATEGORIES = sorted(curated.CATEGORIES)

#: One event, as both providers must describe it.
EVENT_FIELDS = {
    'title': 'as the organiser or ticket seller writes it, usually in Greek; no venue in it',
    'starts_at': 'local time "YYYY-MM-DDTHH:MM", or "YYYY-MM-DD" if no page gives the hour',
    'venue': 'the venue, and the area if useful, e.g. "Θέατρο Λυκαβηττού, Αθήνα"',
    'category': 'one of: ' + ', '.join(CATEGORIES) + ' — or empty',
    'description': 'one or two sentences in Greek that a reader would find useful',
    'tickets_url': 'where to buy tickets, or empty',
    'image_url': 'the official poster image URL if a page gives one, or empty',
    'source_urls': 'the pages you saw that state the date; the ticketing or official page first',
    'popularity': 'integer 1–5, see the scale',
    'why': 'one short sentence: the evidence it is popular',
}

PROMPT = """City: {city}{latin}, Greece. Today is {today} (Europe/Athens). Window: {today} to {until}.

You are researching what is on in {city} for neat, a local social app. People
open its events tab to decide where to go, so the list should hold the events
the city is actually talking about and buying tickets for — not every listing
a ticket site carries. Find the most popular, most talked-about events in or
right around {city} in that window.

Where to look:
- Ticket sellers: more.com, viva.gr, ticketservices.gr, ticketmaster.gr, and the
  clubs' and federations' own sites for sport.
- The culture and listings press: athinorama.gr, lifo.gr, popaganda.gr,
  culturenow.gr, monopoli.gr, in2life.gr, parallaxi.gr,
  thessalonikiartsandculture.gr, and the local news sites of {city}.
- Public social posts that search shows: the Instagram, Facebook and TikTok
  pages of venues, artists, promoters and festivals. Treat these as evidence
  of buzz — a sold-out notice, an extra date added, a post shared widely — and
  confirm the date on a ticketing or official page wherever you can.

What belongs: big-name concerts and tours, arena and stadium shows, festivals,
top-flight sport (Super League, Euroleague, derbies, the national team), major
theatre, opera and dance premieres, well-known stand-up, large food, wine, tech
and culture events, and anything sold out, selling fast, or given an extra
date because of demand. In a smaller city the bar is relative: the events most
of the town would know about.

What does not belong: workshops, classes, seminars, lectures, children's
activities, guided tours, recurring weekly bar nights, open mics, karaoke,
quizzes, bazaars, trade-only conferences, online events, private events,
anything already over, and a run of dates that names no single date.

Rules:
- Only events with a specific date stated on a page you actually saw. Never
  guess a date or a time; if no page gives the hour, give the day alone.
- A run of performances (a play on for weeks) is listed once, at its next date.
- Every event cites at least one page in source_urls.
- popularity, honestly:
    5 = a city-wide headline: a stadium or arena show, a derby, a major festival
    4 = a big name at a major venue, or sold out / extra date added
    3 = a well-known act or production that will draw a crowd
    2 = established but niche
    1 = small and local
- Leave a field empty rather than guess.
- At most {max_events} events, the most notable first.

Each event has these fields:
{fields}
"""


def latin_name(city):
    from .ticketmaster import latin_name as tm_latin

    name = tm_latin(city)
    return '' if name == city else name


def prompt_for(city, *, today):
    start = date.fromisoformat(today)
    latin = latin_name(city)
    return PROMPT.format(
        city=city,
        latin=f' ({latin})' if latin else '',
        today=start.isoformat(),
        until=(start + timedelta(days=DAYS)).isoformat(),
        max_events=MAX_EVENTS,
        fields='\n'.join(f'- {name}: {what}' for name, what in EVENT_FIELDS.items()),
    )


def json_object_in(text):
    """The JSON object in a model's text answer, fences and preamble allowed."""
    if not isinstance(text, str):
        return None
    stripped = re.sub(r'^```(?:json)?\s*|\s*```$', '', text.strip())
    start, end = stripped.find('{'), stripped.rfind('}')
    if start < 0 or end <= start:
        return None
    try:
        found = json.loads(stripped[start:end + 1])
    except ValueError:
        return None
    return found if isinstance(found, dict) else None


def url_key(url):
    """A URL reduced to what identifies the page, for "did search return it"."""
    parts = urlsplit((url or '').strip())
    host = (parts.hostname or '').lower()
    if host.startswith('www.'):
        host = host[4:]
    return f'{host}{parts.path.rstrip("/")}'


def clean_rows(rows):
    """The rows that are dicts with a title, a date and at least one cited URL."""
    kept = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        if not isinstance(row.get('title'), str) or not row['title'].strip():
            continue
        if not isinstance(row.get('starts_at'), str) or not row['starts_at'].strip():
            continue
        urls = row.get('source_urls')
        if isinstance(urls, str):
            urls = [urls]
        urls = [u.strip() for u in urls or [] if isinstance(u, str)
                and u.strip().startswith(('http://', 'https://'))]
        if not urls:
            continue
        kept.append({**row, 'source_urls': urls})
    return kept


# ── Checking a claim against the page it cites

_GREEK_MONTHS = ['ιανουαριου', 'φεβρουαριου', 'μαρτιου', 'απριλιου', 'μαιου', 'ιουνιου',
                 'ιουλιου', 'αυγουστου', 'σεπτεμβριου', 'οκτωβριου', 'νοεμβριου', 'δεκεμβριου']
_GREEK_SHORT = ['ιαν', 'φεβ', 'μαρ', 'απρ', 'μαι', 'ιουν', 'ιουλ', 'αυγ', 'σεπ', 'οκτ', 'νοε', 'δεκ']
_ENGLISH = ['january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
            'september', 'october', 'november', 'december']


def _date_pattern(day):
    """A regex for [day] as Greek and English pages write it."""
    d, m = day.day, day.month
    months = '|'.join(re.escape(n) for n in
                      (_GREEK_MONTHS[m - 1], _GREEK_SHORT[m - 1], _ENGLISH[m - 1],
                       _ENGLISH[m - 1][:3]))
    return re.compile(
        rf'{day.isoformat()}'
        rf'|(?<!\d)0?{d}\s*[/.\-]\s*0?{m}(?!\d)'
        rf'|(?<!\d)0?{d}(?:η|th|st|nd|rd)?\s+(?:{months})'
        rf'|(?:{_ENGLISH[m - 1]}|{_ENGLISH[m - 1][:3]})\.?\s+0?{d}(?!\d)'
    )


def _title_words(title):
    return [w for w in match_key(title).split() if len(w) >= 4][:6]


def _is_social(url):
    host = (urlsplit(url).hostname or '').lower()
    return any(host == h or host.endswith('.' + h) for h in SOCIAL_HOSTS)


def page_confirms(html, *, title, day):
    """Whether a page names [title] and [day]. Both, not either."""
    text = fold(html or '')
    words = _title_words(title)
    if not text or not words:
        return False
    hits = sum(1 for w in words if w in text)
    return hits / len(words) >= 0.6 and bool(_date_pattern(day).search(text))


def verify(rows, *, fetch, budget=MAX_VERIFY_FETCHES):
    """Mark `verified` on the rows a cited page confirms. Spends ≤ budget fetches."""
    from .normalise import parse_iso

    for row in sorted(rows, key=lambda r: -_int(r.get('popularity'))):
        row.setdefault('verified', False)
        if budget <= 0 or _int(row.get('popularity')) < VERIFY_FROM:
            continue
        starts = parse_iso(row.get('starts_at'))
        if starts is None:
            continue
        for url in row['source_urls'][:3]:
            if budget <= 0:
                break
            if _is_social(url) or not robots.allowed(url, fetch=fetch):
                continue
            budget -= 1
            try:
                _, html = fetch(url)
            except Exception:
                continue
            if page_confirms(html, title=row['title'], day=starts.date()):
                row['verified'] = True
                break
    return rows


def _int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


# ── Choosing who to ask

def provider_name():
    """Which provider is configured, or '' for none."""
    chosen = (getattr(settings, 'EVENTS_RESEARCH_PROVIDER', '') or '').strip().lower()
    if chosen:
        return chosen
    if getattr(settings, 'ANTHROPIC_API_KEY', ''):
        return 'claude'
    if getattr(settings, 'GEMINI_API_KEY', ''):
        return 'gemini'
    return ''


def _providers():
    from . import research_claude, research_gemini

    return {'claude': research_claude.research, 'gemini': research_gemini.research}


def research_city(city, *, today, fetch=None, provider=None, check=True):
    """The notable events a provider finds for [city], as plain dicts.

    Deliberately total, like extract.extract: no key, a provider that is down
    or an answer that does not parse all yield [] and a log line, never an
    exception — one city's research failing is that city's line in the run.
    """
    name = provider or provider_name()
    if not name:
        logger.info('no research provider configured; skipping %s', city)
        return []
    ask = _providers().get(name)
    if ask is None:
        logger.warning('no such research provider: %s', name)
        return []

    try:
        rows = clean_rows(ask(city, prompt=prompt_for(city, today=today)))
    except Exception:
        logger.warning('research for %s failed', city, exc_info=True)
        return []

    if check and rows:
        if fetch is None:
            from linkpreview.fetcher import fetch_head_html as fetch
        verify(rows, fetch=fetch)
    return rows
