"""Ticketmaster's Discovery API.

The one source that answers "what is on in this city" as data rather than as a
page, and the reason it is worth having despite the venues being covered
already: it gives real start times. A programme page read by a model often
says only which day, and a page about tonight needs the hour.

It is asked by city, which is how its index works and also how ours does. The
key is free and comes from the environment; without one this source is skipped
with a line saying so, exactly like the model-backed ones.
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)

BASE = 'https://app.ticketmaster.com/discovery/v2/events.json'

#: Their maximum page is 200, and deep paging is rate-limited. Two pages is
#: already more than a city produces in the window we care about.
PAGE_SIZE = 100
MAX_PAGES = 2
TIMEOUT = 30

#: Greece. Asking by city alone matches Athens, Georgia.
COUNTRY = 'GR'

#: Their index is in Latin script: asking for "Αθήνα" returns nothing at all,
#: while "Athens" returns ninety-odd events. Only the cities big enough to
#: appear there are listed; anything else is asked for under its own name,
#: finds nothing, and says so in the run rather than failing.
LATIN_NAMES = {
    'Αθήνα': 'Athens',
    'Θεσσαλονίκη': 'Thessaloniki',
    'Πάτρα': 'Patras',
    'Ηράκλειο': 'Heraklion',
    'Λάρισα': 'Larissa',
    'Βόλος': 'Volos',
    'Ιωάννινα': 'Ioannina',
    'Χανιά': 'Chania',
    'Ρόδος': 'Rhodes',
    'Κέρκυρα': 'Corfu',
    'Καβάλα': 'Kavala',
    'Καλαμάτα': 'Kalamata',
    'Αλεξανδρούπολη': 'Alexandroupoli',
    'Χαλκίδα': 'Chalkida',
    'Ρέθυμνο': 'Rethymno',
}


def latin_name(city):
    """The name Ticketmaster files [city] under."""
    return LATIN_NAMES.get(city, city)


def _get(url):
    context = None
    try:
        import ssl

        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    request = urllib.request.Request(url, headers={'Accept': 'application/json'})
    with urllib.request.urlopen(request, timeout=TIMEOUT, context=context) as response:
        return json.loads(response.read().decode('utf-8', 'replace'))


def fetch_city(city, *, api_key, country=COUNTRY, pages=MAX_PAGES):
    """Their events for one city, as raw rows. Empty on any failure."""
    rows = []
    for page in range(pages):
        query = urllib.parse.urlencode({
            'city': latin_name(city),
            'countryCode': country,
            'size': PAGE_SIZE,
            'page': page,
            'sort': 'date,asc',
            'apikey': api_key,
        })
        try:
            payload = _get(f'{BASE}?{query}')
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:200].decode('utf-8', 'replace')
            logger.warning('ticketmaster %s page %s: %s %s', city, page, exc.code, detail)
            break
        except Exception:
            logger.warning('ticketmaster %s page %s could not be reached', city, page,
                           exc_info=True)
            break

        batch = (payload.get('_embedded') or {}).get('events') or []
        rows.extend(batch)
        info = payload.get('page') or {}
        if len(batch) < PAGE_SIZE or page + 1 >= (info.get('totalPages') or 1):
            break
    return rows


def _best_image(row):
    """The widest image they offer, which is the one worth showing.

    They return the same picture at a dozen sizes; anything under 600px wide
    looks soft in a full-width card.
    """
    images = [i for i in (row.get('images') or []) if i.get('url')]
    if not images:
        return ''
    images.sort(key=lambda i: i.get('width') or 0, reverse=True)
    return images[0]['url']


def to_row(event, *, fallback_city):
    """One of their events as the plain dict `normalise.from_ticketmaster` takes.

    Their shape is deep and half-optional, so the digging happens here and the
    normaliser stays about meaning rather than about JSON.
    """
    start = (event.get('dates') or {}).get('start') or {}
    local_date = start.get('localDate') or ''
    local_time = start.get('localTime') or ''
    if not local_date:
        return None

    venue = ((event.get('_embedded') or {}).get('venues') or [{}])[0]
    venue_name = (venue.get('name') or '').strip()
    address = ((venue.get('address') or {}).get('line1') or '').strip()
    city = ((venue.get('city') or {}).get('name') or '').strip() or fallback_city

    classification = (event.get('classifications') or [{}])[0]
    category = ((classification.get('segment') or {}).get('name') or '').strip()

    return {
        'id': event.get('id') or '',
        'title': (event.get('name') or '').strip(),
        # Their localTime is the venue's wall clock, which is what a reader
        # wants; joining them keeps it local rather than converting twice.
        'starts_at': f'{local_date}T{local_time[:5]}' if local_time else local_date,
        'location': ', '.join(p for p in (venue_name, address) if p),
        'city': city,
        'image_url': _best_image(event),
        'category': category,
        'url': event.get('url') or '',
        'has_tickets': bool(event.get('url')),
    }


def rows_for(city, *, api_key=None):
    """Every usable row Ticketmaster has for [city]."""
    api_key = api_key or getattr(settings, 'TICKETMASTER_API_KEY', '')
    if not api_key:
        logger.info('no Ticketmaster key configured; skipping %s', city)
        return []
    found = (to_row(e, fallback_city=city) for e in fetch_city(city, api_key=api_key))
    return [r for r in found if r and r['title']]
