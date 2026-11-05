"""Reading events off the public web instead of waiting for someone to type them.

The shape is deliberately boring: fetch a page, pull whatever structured event
data it already publishes, turn that into the same fields a person fills in,
and leave it for review. Four small modules, each doing one of those:

    jsonld.py     schema.org/Event out of <script type="application/ld+json">
    ical.py       VEVENT out of an .ics feed
    extract.py    a model reading a programme page that publishes no data
    ticketmaster.py  Ticketmaster's Discovery API
    research.py   a model with web search asked what the city is talking
                  about — the press, the ticket sellers, public social posts —
                  rating each event's popularity (the "hybrid" half)
    normalise.py  any of those into one Candidate, in Athens time
    pipeline.py   fetch, parse, match against what we already have, write,
                  and record which sources saw each event
    venues.py     the big rooms, which say how big a night is

What the tab then shows out of all of it is decided in events/ranking.py.

What is *not* here, on purpose: anything that logs into Instagram or Facebook,
crawls them, or works around a site telling us not to read it. research.py
reads what a web search shows anyone, which includes public social posts, and
never fetches a social site itself. Meta closed public event
access in 2018 and the terms are explicit; a venue that wants to be listed can
connect its own page, which is a conversation rather than a crawler. Every
fetch here goes through linkpreview.fetcher, which already refuses private
addresses, pins a CA bundle, caps the body and times out — the same care a
link preview gets, for the same reason.
"""
