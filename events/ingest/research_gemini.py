"""The research pass on Gemini, grounded in Google Search.

The free option: the same GEMINI_API_KEY and the same Interactions endpoint
extract.py already uses, with Google Search switched on as a tool. Its
citations come back as `url_citation` annotations rather than as the pages
themselves, so this provider cannot check each cited URL the way the Claude
one does. What it can check is that the answer was grounded at all: an answer
with no search behind it is the model's memory, which for "what is on next
week" is always out of date, and is dropped whole.

Settings:

    GEMINI_API_KEY                 required
    EVENTS_RESEARCH_GEMINI_MODELS  comma-separated, tried in order
"""

import logging
import time
import urllib.error

from django.conf import settings

from .extract import GEMINI_URL, PAUSE_BETWEEN_CALLS, _post
from .research import json_object_in

logger = logging.getLogger(__name__)

#: Grounding works on all of these; the first that answers is used. Free
#: tiers answer 503 under load rather than queueing, hence the list.
MODELS = ('gemini-3.8-flash', 'gemini-3.6-flash', 'gemini-3.5-flash-lite')

ANSWER = """
Search the web before answering. When you have finished, answer with only one
JSON object and no other text, of the form {"events": [ ... ]}, each event an
object with the fields above."""


def _models():
    configured = getattr(settings, 'EVENTS_RESEARCH_GEMINI_MODELS', '') or ''
    names = tuple(m.strip() for m in configured.split(',') if m.strip())
    return names or MODELS


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def parse(answer):
    """(rows, grounded) out of an Interactions response."""
    texts, grounded = [], False
    for node in _walk(answer):
        kind = node.get('type')
        if kind == 'text' and isinstance(node.get('text'), str):
            texts.append(node['text'])
        elif kind in ('url_citation', 'google_search_call', 'google_search_result'):
            grounded = True
    # The final answer is the last text; earlier ones are the model thinking
    # aloud between searches.
    for text in reversed(texts):
        found = json_object_in(text)
        if found is not None:
            events = found.get('events')
            return ([e for e in events if isinstance(e, dict)] if isinstance(events, list) else []), grounded
    return [], grounded


def research(city, *, prompt, post=_post):
    """Gemini's events for [city], as dicts. [] unless the answer was grounded."""
    api_key = getattr(settings, 'GEMINI_API_KEY', '')
    if not api_key:
        logger.info('no GEMINI_API_KEY; skipping research for %s', city)
        return []

    for model in _models():
        try:
            answer = post(
                GEMINI_URL,
                {'model': model, 'input': prompt + ANSWER,
                 'tools': [{'type': 'google_search'}]},
                {'Content-Type': 'application/json', 'x-goog-api-key': api_key},
            )
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 429, 500, 502, 503, 504):
                # Busy, rate limited, or a model name this key cannot use.
                logger.info('gemini %s unavailable for research (%s)', model, exc.code)
                time.sleep(PAUSE_BETWEEN_CALLS)
                continue
            logger.warning('gemini %s refused research: %s', model, exc.code)
            return []
        except Exception:
            logger.warning('gemini %s could not be reached', model, exc_info=True)
            return []

        rows, grounded = parse(answer)
        if not grounded:
            logger.warning('research for %s came back without a search behind it', city)
            return []
        return rows
    return []
