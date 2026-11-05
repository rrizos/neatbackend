"""The research pass on Claude, with Anthropic's server-side web search.

Claude searches and reads pages on Anthropic's side, then hands the result in
through one tool of ours, `submit_events`, so the answer arrives as data
rather than as prose to be parsed. What makes this provider the stricter of
the two: every URL a search or a fetch returned in the conversation is
visible to us, so an event citing a page that no search returned — which is
what a confidently remembered date looks like — is dropped here.

Settings:

    ANTHROPIC_API_KEY        required
    EVENTS_RESEARCH_MODEL    default claude-opus-5-5
    EVENTS_RESEARCH_EFFORT   default medium

The `anthropic` package is imported only when this runs, so a server without
it, or without a key, simply skips research and says so.
"""

import logging

from django.conf import settings

from .research import EVENT_FIELDS, url_key

logger = logging.getLogger(__name__)

MODEL = 'claude-opus-5-5'
EFFORT = 'medium'

#: Searches and page reads Claude may spend on one city. Ten searches cover
#: the ticket sellers, the press and the social buzz for a big city; the
#: fetches are for confirming a date the search snippet left vague.
MAX_SEARCHES = 10
MAX_FETCHES = 8

#: Each continuation is one more request: a paused server-side search loop,
#: or a nudge when Claude finished without handing anything in.
MAX_ROUNDS = 5

MAX_TOKENS = 64000

SYSTEM = (
    'You are a careful events researcher for a Greek city guide. You only '
    'report what pages you have read actually say, and you rate popularity '
    'from evidence, not from how an event describes itself.'
)

SUBMIT = {
    'name': 'submit_events',
    'description': (
        'Hand in the researched events for the city. Call it once, at the end, '
        'with every event you found that belongs on the list.'
    ),
    # The answer is streamed (see research()), so it arrives unvalidated and
    # is checked here and again in research.clean_rows and normalise.
    'eager_input_streaming': True,
    'input_schema': {
        'type': 'object',
        'properties': {
            'events': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'properties': {
                        **{name: {'type': 'string', 'description': what}
                           for name, what in EVENT_FIELDS.items()
                           if name not in ('source_urls', 'popularity')},
                        'source_urls': {'type': 'array', 'items': {'type': 'string'},
                                        'description': EVENT_FIELDS['source_urls']},
                        'popularity': {'type': 'integer',
                                       'description': EVENT_FIELDS['popularity']},
                    },
                    'required': ['title', 'starts_at', 'source_urls', 'popularity'],
                },
            },
        },
        'required': ['events'],
    },
}


def _tools():
    return [
        {'type': 'web_search_20260209', 'name': 'web_search', 'max_uses': MAX_SEARCHES},
        {'type': 'web_fetch_20260209', 'name': 'web_fetch', 'max_uses': MAX_FETCHES},
        SUBMIT,
    ]


def _seen_urls(content):
    """Every page a search or a fetch returned in [content]."""
    seen = set()
    for block in content:
        kind = getattr(block, 'type', '')
        if kind == 'web_search_tool_result':
            results = getattr(block, 'content', None)
            # An error comes back as one object rather than a list.
            if isinstance(results, list):
                for result in results:
                    url = getattr(result, 'url', '')
                    if url:
                        seen.add(url_key(url))
        elif kind == 'web_fetch_tool_result':
            result = getattr(block, 'content', None)
            url = getattr(result, 'url', '')
            if url:
                seen.add(url_key(url))
    return seen


def _submitted(content):
    for block in content:
        if getattr(block, 'type', '') == 'tool_use' and getattr(block, 'name', '') == SUBMIT['name']:
            return block
    return None


def only_seen(rows, seen):
    """[rows] with each citation checked against what search returned.

    A row left with no citation that search returned is dropped: it is either
    a remembered event or a made-up page, and neither is a listing.
    """
    kept = []
    for row in rows:
        urls = [u for u in row.get('source_urls') or []
                if isinstance(u, str) and url_key(u) in seen]
        if urls:
            kept.append({**row, 'source_urls': urls})
    return kept


def research(city, *, prompt, client=None):
    """Claude's events for [city], as dicts with checked citations."""
    import anthropic

    api_key = getattr(settings, 'ANTHROPIC_API_KEY', '')
    if client is None:
        if not api_key:
            logger.info('no ANTHROPIC_API_KEY; skipping research for %s', city)
            return []
        client = anthropic.Anthropic(api_key=api_key)

    model = getattr(settings, 'EVENTS_RESEARCH_MODEL', '') or MODEL
    effort = getattr(settings, 'EVENTS_RESEARCH_EFFORT', '') or EFFORT
    messages = [{'role': 'user', 'content': prompt}]
    seen = set()

    for _ in range(MAX_ROUNDS):
        try:
            with client.beta.messages.stream(
                model=model,
                max_tokens=MAX_TOKENS,
                system=SYSTEM,
                messages=messages,
                tools=_tools(),
                output_config={'effort': effort},
                # If a safety classifier declines the request, the API reruns
                # it on Anthropic's recommended fallback model instead of
                # returning the refusal.
                betas=['server-side-fallback-2026-07-01'],
                fallbacks='default',
            ) as stream:
                response = stream.get_final_message()
        except ValueError:
            # The streamed tool input was not JSON at all.
            logger.warning('research for %s: unreadable answer', city)
            return []
        except anthropic.APIStatusError as exc:
            logger.warning('research for %s: API refused (%s)', city, exc.status_code)
            return []
        except anthropic.APIConnectionError:
            logger.warning('research for %s: API unreachable', city)
            return []

        seen |= _seen_urls(response.content)

        if response.stop_reason == 'refusal':
            logger.warning('research for %s was declined', city)
            return []
        submitted = _submitted(response.content)
        if submitted is not None:
            if response.stop_reason == 'max_tokens':
                # A cut-off answer parses as a shorter valid one; not trusted.
                logger.warning('research for %s was cut off', city)
                return []
            events = (submitted.input or {}).get('events') if isinstance(submitted.input, dict) else None
            if not isinstance(events, list):
                return []
            return only_seen([e for e in events if isinstance(e, dict)], seen)

        messages.append({'role': 'assistant', 'content': response.content})
        if response.stop_reason == 'pause_turn':
            # The server's search loop paused; sending the turn back resumes it.
            continue
        messages.append({'role': 'user', 'content':
                         'Hand in what you found now, by calling submit_events.'})

    logger.warning('research for %s never handed anything in', city)
    return []
