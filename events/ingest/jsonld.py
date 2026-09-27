"""schema.org events, as published in the page itself.

Most venue and ticketing pages already describe their events in a machine
readable block — `<script type="application/ld+json">` — because that is what
puts them in Google's event results. Reading that is the difference between
one parser for the whole web and one parser per site.

Nothing here guesses at prose. A page without the block yields nothing and is
left for a later pass to look at with a model; a page with it needs no model
at all.
"""

import json
from html.parser import HTMLParser

#: The types worth taking. schema.org has a long tail of Event subtypes and
#: they all carry the same fields.
EVENT_TYPES = {
    'event', 'musicevent', 'theaterevent', 'festival', 'screeningevent',
    'comedyevent', 'danceevent', 'sportsevent', 'exhibitionevent',
    'socialevent', 'educationevent', 'foodevent', 'literaryevent',
    'businessevent', 'childrensevent', 'courseinstance', 'publicationevent',
    'saleevent', 'visualartsevent',
}

#: A page that lists a season can carry hundreds. Take a sensible slice rather
#: than letting one URL fill a city's review queue.
MAX_PER_PAGE = 200


class _ScriptCollector(HTMLParser):
    """The text of every ld+json script tag on the page."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.blocks = []
        self._collecting = False
        self._buffer = []

    def handle_starttag(self, tag, attrs):
        if tag != 'script':
            return
        a = {k.lower(): (v or '') for k, v in attrs}
        # The type is sometimes given with a charset or in a different case.
        if 'ld+json' in a.get('type', '').lower():
            self._collecting = True
            self._buffer = []

    def handle_endtag(self, tag):
        if tag == 'script' and self._collecting:
            self._collecting = False
            text = ''.join(self._buffer).strip()
            if text:
                self.blocks.append(text)

    def handle_data(self, data):
        if self._collecting:
            self._buffer.append(data)


def _is_event(node):
    types = node.get('@type') or node.get('type') or ''
    if isinstance(types, str):
        types = [types]
    return any(str(t).lower() in EVENT_TYPES for t in types)


def _walk(node, found):
    """Every Event object anywhere in the document.

    They turn up at the top level, inside `@graph`, inside an `itemListElement`
    list, and nested as `subEvent` of a run of performances — so this looks
    everywhere rather than at the two places it usually is.
    """
    if len(found) >= MAX_PER_PAGE:
        return
    if isinstance(node, list):
        for item in node:
            _walk(item, found)
        return
    if not isinstance(node, dict):
        return
    if _is_event(node):
        found.append(node)
    for key, value in node.items():
        # An Event's own `location`/`organizer` are not events; everything
        # else is worth descending into.
        if key in ('location', 'organizer', 'performer', 'offers'):
            continue
        if isinstance(value, (list, dict)):
            _walk(value, found)


def events_in(html_text):
    """Every schema.org Event described in [html_text], as raw dicts."""
    collector = _ScriptCollector()
    try:
        collector.feed(html_text)
    except Exception:
        # A malformed page is a page without events, not a crash. Whatever was
        # collected before it went wrong is still worth keeping.
        pass

    found = []
    for block in collector.blocks:
        try:
            data = json.loads(block)
        except ValueError:
            # Trailing commas and stray newlines are common enough that one bad
            # block should not cost the page its other blocks.
            continue
        _walk(data, found)
    return found[:MAX_PER_PAGE]
