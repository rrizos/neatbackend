"""Reading a programme page the way a person would.

The Greek venues all publish their programme as ordinary HTML for humans and
none of them publish it as data — that survey is in sources.py. So this asks a
model to do what a reader does: look at the page and say what is on and when.

Three things make that safe enough to run unattended. The model is given the
page text and nothing else, so it cannot reach anything; it must answer in a
fixed JSON shape, so a chatty reply is a parse failure rather than a bad
event; and everything it produces lands in the review queue like every other
source, because a model reading a Greek theatre page will sometimes turn a
season announcement into a date.

The provider sits behind one function. Today that is Gemini, on the free tier,
because it costs nothing and handles Greek well; `PROVIDERS` is where another
goes, and nothing above this module knows which one answered.
"""

import json
import logging
import re
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser

from django.conf import settings

logger = logging.getLogger(__name__)

#: Free tiers answer 503 under load rather than queueing, and the flash models
#: are the ones under load. Try in order; the lite models are both cheaper and
#: likelier to answer, and extraction does not need the bigger ones.
GEMINI_MODELS = ('gemini-3.5-flash-lite', 'gemini-3.1-flash-lite')
GEMINI_URL = 'https://generativelanguage.googleapis.com/v1beta/interactions'

#: How much of a page to send. Beyond this is navigation, footers and the
#: cookie banner; the programme is always near the top.
MAX_PAGE_CHARS = 14000

#: Free tiers count requests per minute. One page a second is far under every
#: published limit and keeps a crawl from looking like an attack.
PAUSE_BETWEEN_CALLS = 1.0

REQUEST_TIMEOUT = 180

#: What the model must answer with. A schema rather than a description: a
#: field it cannot fill is then absent rather than invented.
SCHEMA = {
    'type': 'object',
    'properties': {
        'events': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'title': {'type': 'string'},
                    'starts_at': {'type': 'string'},
                    'location': {'type': 'string'},
                    'description': {'type': 'string'},
                    'category': {'type': 'string'},
                    'tickets_url': {'type': 'string'},
                },
                'required': ['title', 'starts_at'],
            },
        },
    },
    'required': ['events'],
}

PROMPT = """You are reading a venue's programme page. List every event it announces.

Rules, in order of importance:
- Only events this page actually announces. Nothing implied, nothing invented.
- starts_at is ISO 8601 local time: "YYYY-MM-DDTHH:MM". If the page gives a day
  but no clock time, answer "YYYY-MM-DD" with no time rather than guessing one.
- The page is Greek and often omits the year. Today is {today}; choose the next
  occurrence of the date it gives.
- Skip anything already over, and skip a run of dates that names no single date.
- Leave a field out when the page does not say. An empty field is correct; a
  plausible guess is not.
- Keep titles as the page writes them, in Greek if that is how they are written.

PAGE: {url}

{text}"""


class _Text(HTMLParser):
    """Visible text, in reading order."""

    _SKIP = {'script', 'style', 'noscript', 'svg', 'head'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skipping = 0
        self._parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skipping += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skipping:
            self._skipping -= 1

    def handle_data(self, data):
        if not self._skipping and data.strip():
            self._parts.append(data.strip())


def page_text(html, limit=MAX_PAGE_CHARS):
    """[html] as the text a reader sees, trimmed to something worth sending."""
    parser = _Text()
    try:
        parser.feed(html or '')
    except Exception:
        pass
    joined = ' \n'.join(parser._parts)
    return re.sub(r'[ \t]+', ' ', joined)[:limit]


def _json_in(node):
    """The JSON object the model answered with.

    The Interaction resource wraps the answer in a structure that has grown
    before and will again, so this looks for the payload rather than walking a
    fixed path into it.
    """
    if isinstance(node, dict):
        text = node.get('text')
        if node.get('type') == 'text' and isinstance(text, str):
            stripped = text.strip()
            if stripped.startswith(('{', '[')):
                try:
                    return json.loads(stripped)
                except ValueError:
                    pass
        for value in node.values():
            found = _json_in(value)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _json_in(item)
            if found is not None:
                return found
    return None


def _post(url, payload, headers, timeout=REQUEST_TIMEOUT):
    body = json.dumps(payload).encode('utf-8')
    request = urllib.request.Request(url, data=body, headers=headers)
    # certifi for the same reason linkpreview does it: the system store is not
    # something to rely on here.
    context = None
    try:
        import ssl

        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        return json.loads(response.read().decode('utf-8', 'replace'))


def ask_gemini(prompt, *, api_key, models=GEMINI_MODELS):
    """The model's answer as a dict, or None if none of them would answer."""
    for model in models:
        try:
            return _post(
                GEMINI_URL,
                {
                    'model': model,
                    'input': prompt,
                    'response_format': {
                        'type': 'text',
                        'mime_type': 'application/json',
                        'schema': SCHEMA,
                    },
                },
                {'Content-Type': 'application/json', 'x-goog-api-key': api_key},
            )
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504):
                # Busy or rate limited: the next model, not a failure. Both of
                # these happen routinely on a free tier at a busy hour.
                logger.info('gemini %s unavailable (%s), trying the next', model, exc.code)
                time.sleep(PAUSE_BETWEEN_CALLS)
                continue
            detail = exc.read()[:300].decode('utf-8', 'replace')
            logger.warning('gemini %s refused: %s %s', model, exc.code, detail)
            return None
        except Exception:
            logger.warning('gemini %s could not be reached', model, exc_info=True)
            return None
    return None


PROVIDERS = {'gemini': ask_gemini}


def extract(html, *, url, today, api_key=None, provider='gemini'):
    """Every event the page announces, as plain dicts. Empty when unavailable.

    Deliberately total: a provider that is down, out of quota or talking
    nonsense yields nothing, and a source that yields nothing is a line in the
    run's output rather than an exception.
    """
    api_key = api_key or getattr(settings, 'GEMINI_API_KEY', '')
    if not api_key:
        logger.info('no API key configured; skipping extraction for %s', url)
        return []

    text = page_text(html)
    if len(text) < 200:
        return []

    ask = PROVIDERS.get(provider)
    if ask is None:
        logger.warning('no such extraction provider: %s', provider)
        return []

    answer = ask(PROMPT.format(today=today, url=url, text=text), api_key=api_key)
    if not answer:
        return []

    payload = _json_in(answer)
    if not isinstance(payload, dict):
        logger.warning('extraction for %s did not come back as JSON', url)
        return []

    rows = payload.get('events')
    return [r for r in rows if isinstance(r, dict) and r.get('title')] if isinstance(rows, list) else []
