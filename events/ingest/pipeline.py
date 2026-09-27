"""Fetch a source, and turn what it publishes into rows.

One source at a time, and one source failing is just that source failing: a
venue with an expired certificate should cost its own events, not the run.

What comes out of here is never visible in the app. Everything written lands
as `pending` and waits to be looked at — see events/models.py. An event a
person creates is unaffected and still goes live immediately.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta

from django.utils import timezone

from . import ical, jsonld, normalise, robots
from .dedupe import find_existing

logger = logging.getLogger(__name__)

#: Nothing this run writes can exceed this, per source. A misparsed season
#: page should be a bad afternoon for one reviewer, not ten thousand rows.
MAX_WRITES_PER_SOURCE = 100


@dataclass
class Result:
    """What one source did, in the words the command prints."""

    source: str
    found: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    error: str = ''

    def line(self):
        if self.error:
            return f'  {self.source}: {self.error}'
        return (f'  {self.source}: found {self.found}, new {self.created}, '
                f'updated {self.updated}, unchanged {self.unchanged}, '
                f'skipped {self.skipped}')


def candidates_from(source, text, final_url):
    """Whatever [text] publishes, as Candidates."""
    city = source['city']
    if source['kind'] == 'ical':
        if not ical.looks_like_ical(text):
            return []
        rows = ical.events_in(text, default_tz=normalise.ATHENS)
        found = [normalise.from_ical(r, city=city, source_url=final_url) for r in rows]
    else:
        found = [
            normalise.from_jsonld(node, city=city, source_url=final_url)
            for node in jsonld.events_in(text)
        ]
    return [c for c in found if c is not None]


def _apply(candidate, event):
    """Copy a candidate onto an event row. Returns True if anything changed.

    Deliberately not a blanket overwrite: `attendees` belongs to our users,
    and a reviewer's edits to a title or a description should survive the next
    crawl. What the source owns is the timing and the links.
    """
    before = (event.title, event.description, event.location, event.image_url,
              event.organizer, event.category, event.tickets_url,
              event.has_tickets, event.date)

    event.date = candidate.starts_at
    event.city = candidate.city
    event.source_url = candidate.source_url
    event.tickets_url = candidate.tickets_url or event.tickets_url
    event.has_tickets = candidate.has_tickets or event.has_tickets
    for name in ('title', 'description', 'location', 'image_url', 'organizer', 'category'):
        value = getattr(candidate, name)
        if value:
            setattr(event, name, value)

    after = (event.title, event.description, event.location, event.image_url,
             event.organizer, event.category, event.tickets_url,
             event.has_tickets, event.date)
    return before != after


def ingest_source(source, *, dry_run=False, now=None, fetch=None):
    """Read one source and write what is new. Returns a Result."""
    from events.models import Event

    now = now or timezone.now()
    result = Result(source=source['name'])

    if fetch is None:
        from linkpreview.fetcher import fetch_head_html as fetch

    if not robots.allowed(source['url'], fetch=fetch):
        result.error = 'robots.txt says no'
        return result

    try:
        final_url, text = fetch(source['url'])
    except Exception as exc:
        # Includes UnsafeUrl, which is what an expired certificate, a private
        # address or a 404 arrives as. One line, not a traceback: a source
        # being down is ordinary.
        result.error = f'could not read ({exc.__class__.__name__}: {exc})'
        return result

    candidates = candidates_from(source, text, final_url)
    result.found = len(candidates)

    fresh = [c for c in candidates if normalise.is_worth_keeping(c, now=now)]
    result.skipped = len(candidates) - len(fresh)
    if not fresh:
        return result

    # One query for everything that could be a match, instead of one per
    # candidate: same cities, and a window around the dates in hand.
    earliest = min(c.starts_at for c in fresh) - timedelta(days=1)
    latest = max(c.starts_at for c in fresh) + timedelta(days=1)
    nearby = list(
        Event.objects
        .filter(city__in={c.city for c in fresh}, date__gte=earliest, date__lte=latest)
        .exclude(status=Event.REJECTED)
    )

    written = 0
    for candidate in fresh:
        if written >= MAX_WRITES_PER_SOURCE:
            result.skipped += 1
            continue

        existing = find_existing(candidate, nearby)

        if existing is None:
            if dry_run:
                result.created += 1
                continue
            event = Event(
                city=candidate.city,
                event_type=Event.OFFICIAL,
                title=candidate.title,
                description=candidate.description,
                location=candidate.location,
                image_url=candidate.image_url,
                category=candidate.category,
                date=candidate.starts_at,
                organizer=candidate.organizer,
                has_tickets=candidate.has_tickets,
                tickets_url=candidate.tickets_url,
                status=Event.PENDING,
                # Recorded even when the time is a guess — the reviewer needs
                # to see it to fix it. Nothing pending is shown in the app.
                source=candidate.source,
                source_url=candidate.source_url,
                external_id=candidate.external_id,
                content_hash=candidate.content_hash,
            )
            event.save()
            nearby.append(event)
            result.created += 1
            written += 1
            continue

        if existing.content_hash == candidate.content_hash:
            result.unchanged += 1
            continue

        # Something we already hold, from a person or from another source.
        # The date and the links are worth taking; the rest only fills gaps,
        # and a rejected event is never resurrected by a re-crawl.
        if not existing.source and existing.status == Event.PUBLISHED:
            # A person's event. Leave their words alone and only record that
            # this source describes the same evening.
            if not dry_run and not existing.source_url:
                existing.source_url = candidate.source_url
                existing.save(update_fields=['source_url'])
            result.unchanged += 1
            continue

        changed = _apply(candidate, existing)
        existing.content_hash = candidate.content_hash
        existing.external_id = existing.external_id or candidate.external_id
        existing.source = existing.source or candidate.source
        if dry_run:
            result.updated += 1 if changed else 0
            result.unchanged += 0 if changed else 1
            continue
        existing.save()
        written += 1
        if changed:
            result.updated += 1
        else:
            result.unchanged += 1

    return result


def ingest(sources, *, dry_run=False, now=None, fetch=None):
    """Every source in turn. Returns the list of Results."""
    results = []
    for source in sources:
        try:
            results.append(ingest_source(source, dry_run=dry_run, now=now, fetch=fetch))
        except Exception as exc:
            logger.exception('event ingest failed for %s', source.get('name'))
            results.append(Result(source=source.get('name', '?'),
                                  error=f'crashed ({exc.__class__.__name__})'))
    return results
