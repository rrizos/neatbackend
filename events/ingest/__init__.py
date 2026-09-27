"""Reading events off the public web instead of waiting for someone to type them.

The shape is deliberately boring: fetch a page, pull whatever structured event
data it already publishes, turn that into the same fields a person fills in,
and leave it for review. Four small modules, each doing one of those:

    jsonld.py     schema.org/Event out of <script type="application/ld+json">
    ical.py       VEVENT out of an .ics feed
    normalise.py  either of those into one Candidate, in Athens time
    pipeline.py   fetch, parse, match against what we already have, write

What is *not* here, on purpose: anything that logs into Instagram or Facebook
or works around a site telling us not to read it. Meta closed public event
access in 2018 and the terms are explicit; a venue that wants to be listed can
connect its own page, which is a conversation rather than a crawler. Every
fetch here goes through linkpreview.fetcher, which already refuses private
addresses, pins a CA bundle, caps the body and times out — the same care a
link preview gets, for the same reason.
"""
