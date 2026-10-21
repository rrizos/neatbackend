"""Events a person researched, loaded from a file.

The crawler (pipeline.py) covers what venues publish in a form a program can
read. Plenty of what people actually go to — a sold-out arena show, a derby, a
film festival — is announced on pages it cannot read, or by sources it does
not have. This is the other way in: a JSON file of events somebody looked up
and checked against the organiser's own page, kept in events/curated/.

Each entry:

    {
      "title": "...", "category": "Music Concert", "city": "Αθήνα",
      "start": "2026-11-14T21:00:00", "time_known": true,
      "venue": "...", "address": "...", "lat": 37.97, "lon": 23.72,
      "description": "...", "organizer": "...",
      "tickets_url": "https://...", "has_tickets": true,
      "source_url": "https://...", "image_url": "https://..."
    }

`start` is the local time printed on the poster. Because a person checked
these, they are published straight away; everything else about them is what
an ingested event gets — the same dedupe against what is already listed, the
same storage convention for times, and `source = "curated"`, so the whole set
can be found, updated in place by loading a newer file, or removed.
"""

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import timedelta

from .dedupe import find_existing, fingerprint
from . import normalise

logger = logging.getLogger(__name__)

SOURCE = 'curated'

#: How far either side of an entry to look for the same event listed already.
_NEARBY = timedelta(days=1)

#: The categories the app filters on. Stored in English; the app translates.
CATEGORIES = {
    'Music Concert', 'Live Concert', 'Sports', 'Art & Culture',
    'Food & Drinks', 'Tech', 'Comedy', 'Networking',
}


@dataclass
class Report:
    created: list = field(default_factory=list)
    updated: list = field(default_factory=list)
    unchanged: list = field(default_factory=list)
    skipped: list = field(default_factory=list)  # (title, reason)

    def lines(self):
        for label, rows in (('new', self.created), ('updated', self.updated),
                            ('unchanged', self.unchanged)):
            for title in rows:
                yield f'  {label:9} {title}'
        for title, reason in self.skipped:
            yield f'  skipped   {title} — {reason}'
        yield (f'{len(self.created)} new, {len(self.updated)} updated, '
               f'{len(self.unchanged)} unchanged, {len(self.skipped)} skipped')


#: Event.tickets_url is a URLField of the default length.
TICKETS_URL_MAX = 200


def _fits(url, limit):
    """[url] if it fits the column; without its query string if that does; else ''."""
    if len(url) <= limit:
        return url
    bare = url.split('?', 1)[0]
    return bare if len(bare) <= limit else ''


def external_id(entry):
    """Stable across reloads of the same entry, even if its wording changes."""
    key = '|'.join([
        str(entry.get('city', '')),
        str(entry.get('start', ''))[:10],
        fingerprint(str(entry.get('title', ''))),
    ])
    return f'{SOURCE}:{hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]}'


def location_of(entry):
    """The `location` string the app reads: "lat,lon|what to show".

    With coordinates the map drops its pin exactly; without them it geocodes
    the text, which works for most addresses but not every venue.
    """
    venue = (entry.get('venue') or '').strip()
    address = (entry.get('address') or '').strip()
    shown = venue
    if address and address.lower() not in venue.lower():
        shown = f'{venue}, {address}' if venue else address
    lat, lon = entry.get('lat'), entry.get('lon')
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        return f'{lat:.6f},{lon:.6f}|{shown}'
    return shown


def candidate_from(entry, *, cities):
    """An entry as a Candidate, or (None, reason) when it cannot be one."""
    title = normalise._text(entry.get('title'), normalise.TITLE_MAX)
    if not title:
        return None, 'no title'
    city = (entry.get('city') or '').strip()
    if city not in cities:
        return None, f'unknown city {city!r}'
    category = (entry.get('category') or '').strip()
    if category not in CATEGORIES:
        return None, f'unknown category {category!r}'
    starts_at = normalise.parse_iso(entry.get('start'))
    if starts_at is None:
        return None, 'no usable start'
    source_url = normalise._url(entry.get('source_url'))
    if not source_url:
        # The whole point of a curated entry is that someone can check it.
        return None, 'no source_url to check it against'

    tickets_url = _fits(normalise._url(entry.get('tickets_url')), TICKETS_URL_MAX)
    candidate = normalise.Candidate(
        title=title,
        starts_at=starts_at,
        city=city,
        source=SOURCE,
        source_url=source_url,
        external_id=external_id(entry),
        description=normalise._text(entry.get('description'), normalise.DESCRIPTION_MAX),
        location=location_of(entry)[:normalise.LOCATION_MAX],
        image_url=normalise._url(entry.get('image_url')),
        organizer=normalise._text(entry.get('organizer'), normalise.ORGANIZER_MAX),
        category=category,
        tickets_url=tickets_url,
        # Only with somewhere to buy them: a tickets button with nowhere to
        # go is worse than none.
        has_tickets=bool(tickets_url),
        time_known=bool(entry.get('time_known', True)),
    )
    return candidate, ''


def load(entries, *, dry_run=False, now=None, copy_images=True, store_image=None):
    """Publish [entries], updating any loaded before. Returns a Report."""
    from django.utils import timezone

    from events.images import discard_event_image, store_event_image_from_url
    from events.models import Event
    from lockedcities.cities import APP_CITIES

    store_image = store_image or store_event_image_from_url
    now = now or timezone.now()
    report = Report()

    for entry in entries:
        label = str(entry.get('title') or '(untitled)')[:80]
        candidate, reason = candidate_from(entry, cities=set(APP_CITIES))
        if candidate is None:
            report.skipped.append((label, reason))
            continue
        if not normalise.is_worth_keeping(candidate, now=now):
            report.skipped.append((label, 'already over, or too far ahead'))
            continue

        candidate.starts_at = normalise.as_wall_clock(candidate.starts_at)
        digest = candidate.content_hash

        mine = Event.objects.filter(source=SOURCE, external_id=candidate.external_id).first()
        if mine is None:
            # Listed already by someone else — a person, or the crawler —
            # is one listing too many.
            nearby = Event.objects.filter(
                city=candidate.city,
                date__gte=candidate.starts_at - _NEARBY,
                date__lte=candidate.starts_at + _NEARBY,
            ).exclude(status=Event.REJECTED)
            if find_existing(candidate, list(nearby)) is not None:
                report.skipped.append((label, 'already listed'))
                continue
        elif mine.content_hash == digest:
            report.unchanged.append(label)
            continue

        if dry_run:
            (report.created if mine is None else report.updated).append(label)
            continue

        image = candidate.image_url
        if copy_images and image:
            image = store_image(image) or image

        event = mine or Event(source=SOURCE, external_id=candidate.external_id,
                              event_type=Event.OFFICIAL)
        previous_image = event.image_url
        event.city = candidate.city
        event.title = candidate.title
        event.description = candidate.description
        event.location = candidate.location
        event.category = candidate.category
        event.date = candidate.starts_at
        event.organizer = candidate.organizer
        event.has_tickets = candidate.has_tickets
        event.tickets_url = candidate.tickets_url
        event.source_url = candidate.source_url
        event.image_url = image
        event.content_hash = digest
        event.status = Event.PUBLISHED
        event.save()
        if previous_image and previous_image != image:
            discard_event_image(previous_image)
        (report.created if mine is None else report.updated).append(label)

    return report


def remove(entries=None):
    """Delete curated events: those in [entries], or every one. Returns count."""
    from events.images import discard_event_image
    from events.models import Event

    rows = Event.objects.filter(source=SOURCE)
    if entries is not None:
        rows = rows.filter(external_id__in=[external_id(e) for e in entries])
    count = 0
    for event in rows:
        discard_event_image(event.image_url)
        event.delete()
        count += 1
    return count
