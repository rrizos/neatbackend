"""Where to look, and what looking actually found.

A source is four things: a name, the city it speaks for, how to read it
(`jsonld` or `ical`), and a URL. Add one here and the next run picks it up.

    {'name': 'snfcc', 'city': 'Αθήνα', 'kind': 'jsonld', 'url': 'https://…'}

## What a survey of the Greek web found (2026-09-27)

This list is short because the honest answer is that the structured-data
route, which works well in a lot of countries, mostly does not work here yet.
Every one of these was fetched and inspected:

    megaron.gr              ld+json present, no Event objects
    nationalopera.gr        ld+json present, no Event objects; links out to
                            ticketservices.gr, whose event pages have none
    snfcc.org/en/events     no Event markup
    onassis.org/whats-on    no Event markup
    technopolis-athens.com  no ld+json at all
    thessaloniki.gr         no Event markup
    tif-helexpo.gr          no Event markup
    more.com                no ld+json at all
    athinorama.gr           ld+json present, no Event objects
    eventbrite.com/d/…      44 parseable Event objects per city page — but
                            their robots.txt is itself behind a bot wall and
                            their terms point at an API instead, so it is not
                            in this list. Use their API.

So the parsers here are right and the seed list is the problem, which is the
useful thing to have learned cheaply. Three ways forward, in order of yield:

1. **Official APIs.** Ticketmaster Discovery covers Greece and Eventbrite has
   a free API; both want a key and neither needs any of this guessing. Each
   becomes a `kind` here alongside jsonld and ical.
2. **A model reading the page.** The venues above all publish their programme
   as ordinary HTML for people. Extraction with Claude turns that into the
   same Candidate this module already produces — the pipeline does not care
   where a Candidate came from.
3. **Venues connecting their own accounts**, which is the only lawful route
   to the Instagram long tail.

`--url` on the management command runs any page through the parsers without
adding it here, which is the quick way to check a new candidate source.
"""

#: Read on every run. Each was fetched and checked before being put here;
#: `kind: llm` means the page publishes no data and has to be read (see
#: extract.py), which is the only thing that works on the Greek venues.
#:
#: Verified 2026-09-28: ticketmaster 96 in Αθήνα and 2 in Θεσσαλονίκη (with
#: real start times), megaron 15, tch 56.
SEEDS = [
    # Asked by city, and worth having even where the venues below cover the
    # same nights: it is the only source that reliably knows the hour.
    {'name': 'ticketmaster-ath', 'city': 'Αθήνα', 'kind': 'ticketmaster', 'url': ''},
    {'name': 'ticketmaster-skg', 'city': 'Θεσσαλονίκη', 'kind': 'ticketmaster', 'url': ''},
    {'name': 'megaron', 'city': 'Αθήνα', 'kind': 'llm',
     'url': 'https://www.megaron.gr/'},
    {'name': 'tch', 'city': 'Θεσσαλονίκη', 'kind': 'llm',
     'url': 'https://www.tch.gr/'},
]

#: Checked and not usable, so nobody spends an afternoon rediscovering it:
#:
#:   snfcc.org, onassis.org   serve ~300 bytes of HTML and render in the
#:                            browser; they need a headless browser, not a
#:                            better prompt
#:   technopolis-athens.com   no ld+json, and the programme is an image
#:   eventbrite.com           the API cannot search (see below) and the site
#:                            is behind a bot wall
#:
#: Eventbrite, checked 2026-09-28 with a real token: /v3/events/search/ is
#: gone (404) — they withdrew public search — /v3/events/{id}/ works but only
#: for an id you already have, and the account owns no organisations. So their
#: API cannot discover anything, and the ids only exist on the site they do
#: not want crawled. Ticketmaster's Discovery API still has real search with
#: Greek coverage and is the one worth adding next.

def for_cities(cities=None):
    """The seeds, optionally narrowed to a set of cities."""
    if not cities:
        return list(SEEDS)
    wanted = {c.strip() for c in cities if c and c.strip()}
    return [s for s in SEEDS if s['city'] in wanted]


def one_off(url, city, kind='jsonld'):
    """A source made on the spot, for `ingest_events --url`."""
    return {'name': f'{kind}:{url}', 'city': city, 'kind': kind, 'url': url}
