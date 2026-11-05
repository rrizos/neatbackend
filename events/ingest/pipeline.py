"""Fetch a source, and turn what it publishes into rows.

One source at a time, and one source failing is just that source failing: a
venue with an expired certificate should cost its own events, not the run.

What comes out of here is never visible in the app by default. Everything
written lands as `pending` and waits to be looked at — see events/models.py —
unless auto-publishing is switched on (EVENTS_AUTO_PUBLISH_SCORE, or
`ingest_events --publish-above`), and then only for an event with a real start
time that two independent sources agree on, scoring at least that much. An
event a person creates is unaffected and still goes live immediately.

Every source also leaves a trace on each event it matches, in
`Event.signals`: its name in `sources`, and from the research pass a
popularity rating with its evidence. That is what events/ranking.py scores,
so an evening Ticketmaster, the venue and the press all mention outranks one
only a ticket seller lists.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta

from django.utils import timezone

from urllib.parse import urlsplit

from django.conf import settings

from . import extract, ical, jsonld, normalise, research, robots, ticketmaster
from .dedupe import find_existing

logger = logging.getLogger(__name__)

#: Nothing this run writes can exceed this, per source. A misparsed season
#: page should be a bad afternoon for one reviewer, not ten thousand rows.
MAX_WRITES_PER_SOURCE = 100


def _hosted_image(url):
    """A source's poster, copied onto our own host — see events/images.py.

    Crawled candidates used to go straight into `image_url` as the source's
    own link, which breaks the moment that site blocks hotlinking, deletes
    the page, or moves the file — exactly the kind of thing a site a reviewer
    has never heard of does without notice. events/ingest/curated.py already
    copies images this way for hand-picked listings; this brings the
    automated crawl to the same place instead of trusting someone else's URL
    to keep working. Never raises — a copy failure falls back to the
    original link rather than losing the picture outright.
    """
    from events.images import store_event_image_from_url
    return store_event_image_from_url(url)


@dataclass
class Result:
    """What one source did, in the words the command prints."""

    source: str
    found: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    published: int = 0
    error: str = ''

    def line(self):
        if self.error:
            return f'  {self.source}: {self.error}'
        line = (f'  {self.source}: found {self.found}, new {self.created}, '
                f'updated {self.updated}, unchanged {self.unchanged}, '
                f'skipped {self.skipped}')
        return line + (f', published {self.published}' if self.published else '')


def candidates_from(source, text, final_url, *, today=None):
    """Whatever [text] publishes, as Candidates."""
    city = source['city']
    if source['kind'] == 'llm':
        # The page says nothing a parser can read, so something has to read it
        # the way a person would. Everything it returns is still pending.
        rows = extract.extract(text, url=final_url, today=today)
        found = [normalise.from_extracted(r, city=city, source_url=final_url) for r in rows]
    elif source['kind'] == 'ical':
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
    for name in ('title', 'description', 'location', 'organizer', 'category'):
        value = getattr(candidate, name)
        if value:
            setattr(event, name, value)
    if candidate.image_url and candidate.image_url != event.image_url:
        event.image_url = _hosted_image(candidate.image_url) or candidate.image_url

    after = (event.title, event.description, event.location, event.image_url,
             event.organizer, event.category, event.tickets_url,
             event.has_tickets, event.date)
    return before != after


def _publish_threshold(publish_above):
    if publish_above is not None:
        return publish_above
    value = getattr(settings, 'EVENTS_AUTO_PUBLISH_SCORE', None)
    return int(value) if value not in (None, '') else None


def ingest_source(source, *, dry_run=False, now=None, fetch=None, publish_above=None):
    """Read one source and write what is new. Returns a Result."""
    now = now or timezone.now()
    result = Result(source=source['name'])
    threshold = _publish_threshold(publish_above)

    if source['kind'] == 'ticketmaster':
        # An API, so there is no page to fetch and nothing to ask robots.txt
        # about: the key is the permission.
        rows = ticketmaster.rows_for(source['city'])
        candidates = [c for c in
                      (normalise.from_ticketmaster(r, city=source['city']) for r in rows)
                      if c is not None]
        return _write(result, candidates, now=now, dry_run=dry_run, publish_above=threshold)

    if fetch is None:
        from linkpreview.fetcher import fetch_head_html as fetch

    if source['kind'] == 'research':
        # A model with web search, asked what the city is talking about. The
        # fetch is only for checking its claims against the pages it cites.
        if not research.provider_name():
            result.error = 'no research provider configured (ANTHROPIC_API_KEY or GEMINI_API_KEY)'
            return result
        rows = research.research_city(
            source['city'], today=now.astimezone(normalise.ATHENS).date().isoformat(),
            fetch=fetch)
        candidates = [c for c in
                      (normalise.from_research(r, city=source['city']) for r in rows)
                      if c is not None]
        return _write(result, candidates, now=now, dry_run=dry_run, publish_above=threshold)

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

    candidates = candidates_from(
        source, text, final_url, today=now.astimezone(normalise.ATHENS).date().isoformat())
    return _write(result, candidates, now=now, dry_run=dry_run, publish_above=threshold)


#: Source kinds that are a way of reading a page rather than a publisher.
#: Two venues read the same way are still two sources, so these are told
#: apart by the site they were read from.
_PAGE_KINDS = {'llm', 'jsonld', 'ical'}


def _sighting(source, source_url):
    if source in _PAGE_KINDS:
        host = (urlsplit(source_url or '').hostname or '').lower()
        host = host[4:] if host.startswith('www.') else host
        return f'{source}:{host}' if host else source
    return source


def sighting_of(candidate):
    """The name an independent source goes by in `signals['sources']`."""
    return _sighting(candidate.source, candidate.source_url)


def _note(event, candidate):
    """Record on [event] that [candidate]'s source lists it. True if anything changed."""
    signals = dict(event.signals) if isinstance(event.signals, dict) else {}
    before = dict(signals)

    sources = [s for s in signals.get('sources') or [] if isinstance(s, str)]
    if not sources and event.source and event.pk:
        # A row from before signals existed: its own source is its first.
        sources.append(_sighting(event.source, event.source_url))
    name = sighting_of(candidate)
    if name not in sources:
        sources.append(name)
    signals['sources'] = sources

    if candidate.popularity:
        # The latest research reading wins: popularity moves (a second date
        # is added, a show sells out) and the newest evidence is the best.
        signals['popularity'] = candidate.popularity
        if candidate.why:
            signals['why'] = candidate.why
    if candidate.citations:
        cited = [c for c in signals.get('citations') or [] if isinstance(c, str)]
        for url in candidate.citations:
            if url not in cited:
                cited.append(url)
        signals['citations'] = cited[-8:]
    if candidate.verified:
        signals['verified'] = True

    if signals == before:
        return False
    event.signals = signals
    return True


def _fill_gaps(candidate, event):
    """What a research sighting may add to a row another source owns.

    Only what is missing. A model's reading of a page is less exact than the
    page's own data, so it never moves a date or renames anything — it only
    supplies a picture, a description or a tickets link nobody else gave.
    """
    changed = False
    if candidate.image_url and not event.image_url:
        event.image_url = _hosted_image(candidate.image_url) or candidate.image_url
        changed = True
    for name in ('description', 'category', 'tickets_url', 'location'):
        value = getattr(candidate, name)
        if value and not getattr(event, name):
            setattr(event, name, value)
            changed = True
    if candidate.has_tickets and not event.has_tickets:
        event.has_tickets = True
        changed = True
    return changed


def _maybe_publish(event, threshold, result):
    """Publish a pending [event] that has earned it, when that is switched on.

    Three conditions, all needed: a real start time, two independent sources
    that agree on the evening (or one whose page we fetched and found saying
    the same), and a score at or over [threshold]. Two sources agreeing on a
    date is a stronger check than the one a reviewer does by eye.
    """
    from events import ranking
    from events.models import Event
    from .dedupe import has_no_time

    if threshold is None or event.status != Event.PENDING:
        return False
    if event.date is None or has_no_time(event.date):
        return False
    signals = event.signals if isinstance(event.signals, dict) else {}
    if len(ranking.sources_of(event)) < 2 and not signals.get('verified'):
        return False
    if (event.quality or 0) < threshold:
        return False
    event.status = Event.PUBLISHED
    result.published += 1
    return True


def _write(result, candidates, *, now, dry_run, publish_above=None):
    """Match [candidates] against what we hold and file what is new."""
    from events.models import Event

    result.found = len(candidates)

    fresh = [c for c in candidates if normalise.is_worth_keeping(c, now=now)]
    result.skipped = len(candidates) - len(fresh)
    if not fresh:
        return result

    # From here on, times are in the stored convention, so they compare with
    # events people created and land in the app at the hour on the poster.
    # It also changes the content hash, so rows written before this existed
    # are corrected the next time their source is read.
    for candidate in fresh:
        candidate.starts_at = normalise.as_wall_clock(candidate.starts_at)

    # One query for everything that could be a match, instead of one per
    # candidate: same cities, and a window around the dates in hand.
    earliest = min(c.starts_at for c in fresh) - timedelta(days=1)
    latest = max(c.starts_at for c in fresh) + timedelta(days=1)
    nearby = list(
        Event.objects
        .filter(city__in={c.city for c in fresh}, date__gte=earliest, date__lte=latest)
        .exclude(status=Event.REJECTED)
    )

    from events import ranking

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
                image_url=(_hosted_image(candidate.image_url) or candidate.image_url)
                    if candidate.image_url else '',
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
            _note(event, candidate)
            ranking.refresh(event)
            _maybe_publish(event, publish_above, result)
            event.save()
            nearby.append(event)
            result.created += 1
            written += 1
            continue

        noted = _note(existing, candidate)

        if existing.content_hash == candidate.content_hash:
            if noted and not dry_run:
                ranking.refresh(existing)
                _maybe_publish(existing, publish_above, result)
                existing.save(update_fields=['signals', 'quality', 'status', 'updated'])
            result.unchanged += 1
            continue

        # Something we already hold, from a person or from another source.
        # The date and the links are worth taking; the rest only fills gaps,
        # and a rejected event is never resurrected by a re-crawl.
        if (not existing.source or existing.source == 'curated') \
                and existing.status == Event.PUBLISHED:
            # A person's event, or one checked by hand. Leave their words
            # alone and only record that this source describes the same
            # evening — and, from research, how popular it is.
            if not dry_run:
                fields = ['signals', 'quality']
                if not existing.source_url:
                    existing.source_url = candidate.source_url
                    fields.append('source_url')
                ranking.refresh(existing)
                existing.save(update_fields=fields)
            result.unchanged += 1
            continue

        if candidate.source == 'research' and existing.source != 'research':
            # Another source owns this row's facts; research only adds its
            # signals and fills what is missing. The content hash stays the
            # owner's, so the owner's next crawl still sees "unchanged".
            changed = _fill_gaps(candidate, existing)
            if dry_run:
                result.updated += 1 if changed else 0
                result.unchanged += 0 if changed else 1
                continue
            ranking.refresh(existing)
            _maybe_publish(existing, publish_above, result)
            existing.save()
            written += 1
            if changed:
                result.updated += 1
            else:
                result.unchanged += 1
            continue

        if existing.source == 'research' and candidate.source != 'research':
            # A row research found, now listed by a source with real data:
            # that source takes it over, so its next crawl matches it exactly
            # by id instead of by title.
            existing.source = candidate.source
            existing.external_id = candidate.external_id

        changed = _apply(candidate, existing)
        existing.content_hash = candidate.content_hash
        existing.external_id = existing.external_id or candidate.external_id
        existing.source = existing.source or candidate.source
        if dry_run:
            result.updated += 1 if changed else 0
            result.unchanged += 0 if changed else 1
            continue
        ranking.refresh(existing)
        _maybe_publish(existing, publish_above, result)
        existing.save()
        written += 1
        if changed:
            result.updated += 1
        else:
            result.unchanged += 1

    return result


def ingest(sources, *, dry_run=False, now=None, fetch=None, publish_above=None):
    """Every source in turn. Returns the list of Results."""
    results = []
    for source in sources:
        try:
            results.append(ingest_source(source, dry_run=dry_run, now=now, fetch=fetch,
                                         publish_above=publish_above))
        except Exception as exc:
            logger.exception('event ingest failed for %s', source.get('name'))
            results.append(Result(source=source.get('name', '?'),
                                  error=f'crashed ({exc.__class__.__name__})'))
    return results
