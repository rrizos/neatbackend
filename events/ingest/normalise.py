"""One shape, whatever the source said.

A schema.org Event and a VEVENT describe the same evening in different words.
This turns either into a Candidate, which is the same set of fields a person
fills in when they add an event in the app — so everything downstream, from
matching to review, works on one thing.

Two rules run through it. Times are Athens time unless the source says
otherwise, because an event happens where it happens and a naive timestamp
here has always meant local. And nothing is invented: a field the source did
not give stays empty rather than being guessed at, since a plausible wrong
venue is worse than a missing one.
"""

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ATHENS = ZoneInfo('Europe/Athens')

#: Model field widths, so a long source value is cut here rather than by the
#: database. Kept as plain numbers to avoid importing models into a parser.
TITLE_MAX = 180
LOCATION_MAX = 200
ORGANIZER_MAX = 150
CATEGORY_MAX = 50
DESCRIPTION_MAX = 4000
EXTERNAL_ID_MAX = 200


@dataclass
class Candidate:
    """An event we found, before deciding whether we already had it."""

    title: str
    starts_at: datetime
    city: str
    source: str
    source_url: str
    external_id: str = ''
    description: str = ''
    location: str = ''
    image_url: str = ''
    organizer: str = ''
    category: str = ''
    tickets_url: str = ''
    has_tickets: bool = False
    #: False when the source gave a day but no clock time. Listing pages very
    #: often do, and midnight is then a fact about our parser rather than
    #: about the event — a reviewer has to supply the time before this can
    #: honestly appear on a page about tonight.
    time_known: bool = True
    warnings: list = field(default_factory=list)

    @property
    def content_hash(self):
        """What we took from the source, in one line.

        A re-crawl compares this and stops: an unchanged page should cost one
        string comparison, not an UPDATE on every event in a season.
        """
        parts = [
            self.title, self.description, self.location, self.image_url,
            self.organizer, self.category, self.tickets_url,
            self.starts_at.isoformat() if self.starts_at else '',
        ]
        return hashlib.sha256('\x1f'.join(parts).encode('utf-8')).hexdigest()


def _text(value, limit):
    """A schema.org value as plain text. Strings, language maps and nested
    objects all turn up where a string is specified."""
    if value is None:
        return ''
    if isinstance(value, dict):
        value = value.get('name') or value.get('@value') or value.get('text') or ''
    if isinstance(value, list):
        value = next((v for v in value if v), '')
        return _text(value, limit)
    text = re.sub(r'\s+', ' ', str(value)).strip()
    return text[:limit]


def _url(value):
    if isinstance(value, dict):
        value = value.get('url') or value.get('@id') or ''
    if isinstance(value, list):
        value = next((v for v in value if v), '')
        return _url(value)
    text = str(value or '').strip()
    return text if text.startswith(('http://', 'https://')) else ''


def parse_iso(value, default_tz=ATHENS):
    """A schema.org startDate, which is ISO 8601 in theory.

    In practice it is ISO 8601 with a `Z`, or without a zone at all, or a bare
    date. A missing zone means local time — reading it as UTC moves a 21:00
    concert to midnight, in the direction that makes it look like tomorrow.
    """
    text = str(value or '').strip()
    if not text:
        return None
    text = text.replace('Z', '+00:00')
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for shape in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
            try:
                parsed = datetime.strptime(text, shape)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=default_tz)
    return parsed


def _place(node):
    """A schema.org location as one readable line.

    `location` is a Place with an address, and the address is itself sometimes
    an object and sometimes a string. What a reader wants is "Γκάζι, Αθήνα",
    not a JSON tree.
    """
    if not node:
        return ''
    if isinstance(node, list):
        node = next((n for n in node if n), None)
    if isinstance(node, str):
        return _text(node, LOCATION_MAX)
    if not isinstance(node, dict):
        return ''

    name = _text(node.get('name'), LOCATION_MAX)
    address = node.get('address')
    if isinstance(address, dict):
        bits = [
            _text(address.get('streetAddress'), 120),
            _text(address.get('addressLocality'), 80),
        ]
        address_text = ', '.join(b for b in bits if b)
    else:
        address_text = _text(address, LOCATION_MAX)

    both = ', '.join(b for b in (name, address_text) if b)
    return both[:LOCATION_MAX]


def _city_from(node, fallback):
    """The city a schema.org Place names, when we recognise it.

    The source list already says which city a feed is for; this only overrides
    it when the page itself is explicit, which is what makes a national
    ticketing site usable as one source instead of one per city.
    """
    from lockedcities.cities import APP_CITIES

    if isinstance(node, list):
        node = next((n for n in node if n), None)
    locality = ''
    if isinstance(node, dict):
        address = node.get('address')
        if isinstance(address, dict):
            locality = _text(address.get('addressLocality'), 80)
        elif isinstance(address, str):
            locality = _text(address, 120)
        if not locality:
            locality = _text(node.get('name'), 120)

    if locality:
        folded = locality.casefold()
        for city in APP_CITIES:
            if city.casefold() in folded:
                return city
    return fallback


def _offers(node):
    """(tickets_url, has_tickets) from an Offer or a list of them."""
    if isinstance(node, list):
        node = next((n for n in node if n), None)
    if not isinstance(node, dict):
        return '', False
    url = _url(node.get('url'))
    availability = str(node.get('availability') or '').lower()
    sold_out = 'soldout' in availability
    return url, bool(url) and not sold_out


def has_clock_time(value):
    """Whether a startDate said anything about *when* in the day."""
    return ':' in str(value or '')


def from_jsonld(node, *, city, source_url, source='jsonld'):
    """A schema.org Event dict as a Candidate, or None if it is unusable."""
    title = _text(node.get('name'), TITLE_MAX)
    raw_start = node.get('startDate')
    starts_at = parse_iso(raw_start)
    if not title or starts_at is None:
        # Both are load-bearing: an event with no name cannot be shown and one
        # with no time cannot be put on a day.
        return None

    location_node = node.get('location')
    tickets_url, has_tickets = _offers(node.get('offers'))
    image = node.get('image')
    if isinstance(image, dict):
        image = image.get('url') or image.get('contentUrl')
    if isinstance(image, list):
        image = next((i for i in image if i), '')
        if isinstance(image, dict):
            image = image.get('url') or image.get('contentUrl')

    external_id = _text(node.get('@id') or node.get('url') or '', EXTERNAL_ID_MAX)

    return Candidate(
        title=title,
        starts_at=starts_at,
        city=_city_from(location_node, city),
        source=source,
        source_url=_url(node.get('url')) or source_url,
        external_id=external_id or f'{source_url}#{title}#{starts_at.isoformat()}'[:EXTERNAL_ID_MAX],
        description=_text(node.get('description'), DESCRIPTION_MAX),
        location=_place(location_node),
        image_url=_url(image),
        organizer=_text(node.get('organizer'), ORGANIZER_MAX),
        category=_text(node.get('eventAttendanceMode') and '' or node.get('genre'), CATEGORY_MAX),
        tickets_url=tickets_url,
        has_tickets=has_tickets,
        time_known=has_clock_time(raw_start),
        warnings=[] if has_clock_time(raw_start) else ['no start time in the source'],
    )


def from_ical(row, *, city, source_url, source='ical'):
    """A parsed VEVENT as a Candidate, or None if it is unusable."""
    title = _text(row.get('title'), TITLE_MAX)
    starts_at = row.get('starts_at')
    if not title or starts_at is None:
        return None

    uid = _text(row.get('uid'), EXTERNAL_ID_MAX)
    return Candidate(
        title=title,
        starts_at=starts_at,
        city=city,
        source=source,
        source_url=_url(row.get('url')) or source_url,
        external_id=uid or f'{source_url}#{title}#{starts_at.isoformat()}'[:EXTERNAL_ID_MAX],
        description=_text(row.get('description'), DESCRIPTION_MAX),
        location=_text(row.get('location'), LOCATION_MAX),
        category=_text(row.get('category'), CATEGORY_MAX),
        time_known=row.get('time_known', True),
    )


def from_extracted(row, *, city, source_url, source='llm'):
    """A row a model read off a page, as a Candidate.

    Trusted no further than any other source: the fields go through the same
    cleaning and the same length limits, the date through the same parser, and
    the result into the same review queue. What a model adds is only that
    there was something to read at all.
    """
    title = _text(row.get('title'), TITLE_MAX)
    raw_start = row.get('starts_at')
    starts_at = parse_iso(raw_start)
    if not title or starts_at is None:
        return None

    tickets_url = _url(row.get('tickets_url'))
    return Candidate(
        title=title,
        starts_at=starts_at,
        city=city,
        source=source,
        source_url=source_url,
        external_id=f'{source_url}#{title}#{starts_at.isoformat()}'[:EXTERNAL_ID_MAX],
        description=_text(row.get('description'), DESCRIPTION_MAX),
        location=_text(row.get('location'), LOCATION_MAX),
        category=_text(row.get('category'), CATEGORY_MAX),
        tickets_url=tickets_url,
        has_tickets=bool(tickets_url),
        time_known=has_clock_time(raw_start),
        warnings=[] if has_clock_time(raw_start) else ['no start time on the page'],
    )


def from_ticketmaster(row, *, city, source='ticketmaster'):
    """A Ticketmaster row as a Candidate.

    Their city is the venue's city, so it wins over the city we asked under —
    the same rule the schema.org path follows, and it is what keeps a suburb
    from being filed under the wrong feed.
    """
    from lockedcities.cities import APP_CITIES

    title = _text(row.get('title'), TITLE_MAX)
    raw_start = row.get('starts_at')
    starts_at = parse_iso(raw_start)
    if not title or starts_at is None:
        return None

    named = _text(row.get('city'), 80)
    matched = next((c for c in APP_CITIES if c.casefold() in named.casefold()), '') if named else ''

    url = _url(row.get('url'))
    return Candidate(
        title=title,
        starts_at=starts_at,
        city=matched or city,
        source=source,
        source_url=url,
        external_id=_text(row.get('id'), EXTERNAL_ID_MAX) or f'{url}#{title}',
        location=_text(row.get('location'), LOCATION_MAX),
        image_url=_url(row.get('image_url')),
        category=_text(row.get('category'), CATEGORY_MAX),
        tickets_url=url,
        has_tickets=bool(row.get('has_tickets')),
        time_known=has_clock_time(raw_start),
        warnings=[] if has_clock_time(raw_start) else ['no start time from the API'],
    )


def is_worth_keeping(candidate, now=None, horizon_days=120):
    """Whether an event is still ahead of us and not absurdly far off.

    A season page lists last October as readily as next March, and a feed with
    a broken year will happily claim 2036. Both are noise in a page about
    today.
    """
    now = now or datetime.now(timezone.utc)
    if candidate.starts_at is None:
        return False
    delta = candidate.starts_at - now
    return -ical_grace() < delta.total_seconds() < horizon_days * 86400


def ical_grace():
    """Seconds after a start time that an event still counts as on.

    Something that began two hours ago is still happening; something from last
    week is not, and neither is a page that keeps it listed.
    """
    return 6 * 3600
