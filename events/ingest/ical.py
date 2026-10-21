"""VEVENT out of an .ics feed.

Municipalities, universities, cultural centres and cinemas publish calendars
long before they publish APIs, and an .ics feed is the one format that is
already exactly a list of events with real start times.

Hand-rolled rather than a dependency: what is needed here is five properties
out of one block type. The parts of RFC 5545 that actually bite — folded
lines, escaped commas, a DTSTART that is sometimes a date, sometimes local
time, sometimes UTC — are handled; recurrence rules are not, and a feed that
leans on them will under-report rather than invent dates.
"""

import re
from datetime import date, datetime, time, timedelta, timezone

#: `DTSTART;TZID=Europe/Athens:20260305T210000` — name, parameters, value.
_LINE = re.compile(r'^(?P<name>[A-Za-z0-9-]+)(?P<params>;[^:]*)?:(?P<value>.*)$')


def _unfold(text):
    """RFC 5545 wraps long values onto continuation lines starting with a space
    or a tab. Rejoin them before anything tries to read a value."""
    out = []
    for raw in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        if raw[:1] in (' ', '\t') and out:
            out[-1] += raw[1:]
        else:
            out.append(raw)
    return out


def _unescape(value):
    return (value.replace('\\n', '\n').replace('\\N', '\n')
                 .replace('\\,', ',').replace('\\;', ';')
                 .replace('\\\\', '\\'))


def parse_datetime(value, params, default_tz):
    """A DTSTART/DTEND value as an aware datetime, or None.

    Three forms in the wild, and the difference matters: a bare date (an
    all-day event), a local time (meant in the calendar's own zone), and a
    `Z`-suffixed UTC time. Reading the second as UTC is how a 21:00 concert
    becomes a midnight one.
    """
    value = (value or '').strip()
    if not value:
        return None

    if value.endswith('Z'):
        try:
            return datetime.strptime(value, '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    if len(value) == 8 or params.get('VALUE', '').upper() == 'DATE':
        try:
            day = datetime.strptime(value[:8], '%Y%m%d').date()
        except ValueError:
            return None
        # An all-day entry has no time. Noon keeps it on the right day in any
        # nearby zone, which midnight does not.
        return datetime.combine(day, time(12, 0), tzinfo=default_tz)

    try:
        naive = datetime.strptime(value, '%Y%m%dT%H%M%S')
    except ValueError:
        return None
    # TZID names a zone we may not have; the feed's own zone is the better
    # fallback than UTC, since the feed is about one place.
    tzid = params.get('TZID', '')
    if tzid:
        try:
            from zoneinfo import ZoneInfo
            return naive.replace(tzinfo=ZoneInfo(tzid))
        except Exception:
            pass
    return naive.replace(tzinfo=default_tz)


def _params_of(raw):
    """`;TZID=Europe/Athens;VALUE=DATE` as a dict."""
    params = {}
    for part in (raw or '').lstrip(';').split(';'):
        if '=' in part:
            key, _, value = part.partition('=')
            params[key.strip().upper()] = value.strip().strip('"')
    return params


def events_in(text, default_tz=timezone.utc, limit=200):
    """Every VEVENT in [text], as dicts of the properties worth having."""
    events = []
    current = None

    for line in _unfold(text):
        stripped = line.strip()
        if stripped == 'BEGIN:VEVENT':
            current = {}
            continue
        if stripped == 'END:VEVENT':
            if current:
                events.append(current)
            current = None
            if len(events) >= limit:
                break
            continue
        if current is None:
            continue

        match = _LINE.match(stripped)
        if not match:
            continue
        name = match.group('name').upper()
        params = _params_of(match.group('params'))
        value = match.group('value')

        if name == 'DTSTART':
            current['starts_at'] = parse_datetime(value, params, default_tz)
            # An all-day entry says which day and nothing more. Downstream has
            # to know that the time is ours, not the calendar's.
            current['time_known'] = not (
                len(value.strip()) == 8 or params.get('VALUE', '').upper() == 'DATE'
            )
        elif name == 'DTEND':
            current['ends_at'] = parse_datetime(value, params, default_tz)
        elif name == 'SUMMARY':
            current['title'] = _unescape(value).strip()
        elif name == 'DESCRIPTION':
            current['description'] = _unescape(value).strip()
        elif name == 'LOCATION':
            current['location'] = _unescape(value).strip()
        elif name == 'URL':
            current['url'] = value.strip()
        elif name == 'UID':
            current['uid'] = value.strip()
        elif name == 'CATEGORIES':
            current['category'] = _unescape(value).split(',')[0].strip()

    return events


def looks_like_ical(text):
    """Whether this is a calendar at all — a 404 page served with a .ics URL
    is a thing that happens, and it should cost one check rather than a
    hundred confused parses."""
    return 'BEGIN:VCALENDAR' in (text or '')[:2000].upper()


#: Events that finished before this much time ago are not news. Kept here so
#: the pipeline and the tests agree on what "over" means.
STALE_AFTER = timedelta(hours=6)
