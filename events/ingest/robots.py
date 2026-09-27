"""Asking whether a site minds.

robots.txt is not a law and not a security boundary, and honouring it costs us
almost nothing: one small fetch per host per run, cached. What it buys is the
difference between a crawler people tolerate and one they block — and a
straight answer when a venue asks what we do.

A host that does not answer, or answers with something that is not a robots
file, is treated as allowing it: that is what the standard says, and a 404
here is the ordinary case rather than a refusal.
"""

import logging
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

logger = logging.getLogger(__name__)

#: What we call ourselves, both in robots lookups and in the fetch itself.
USER_AGENT = 'neatbot'

_cache = {}


def _robots_url(url):
    parts = urlsplit(url)
    return f'{parts.scheme}://{parts.netloc}/robots.txt'


def allowed(url, *, fetch=None):
    """Whether robots.txt lets us read [url]."""
    if fetch is None:
        from linkpreview.fetcher import fetch_head_html as fetch

    robots_url = _robots_url(url)
    parser = _cache.get(robots_url)

    if parser is None:
        parser = RobotFileParser()
        try:
            _, text = fetch(robots_url)
        except Exception:
            # No robots file, or the host would not give it to us. Neither is
            # a refusal of the page we actually want.
            text = ''
        # A site behind a bot wall answers robots.txt with an HTML block page.
        # That is not permission to ignore it, but it is not a rule set
        # either: parse what came back and let the absence of rules speak.
        try:
            parser.parse(text.splitlines())
        except Exception:
            parser.parse([])
        _cache[robots_url] = parser

    try:
        return parser.can_fetch(USER_AGENT, url)
    except Exception:
        return True


def forget():
    """Drop the cache. One run, one set of answers."""
    _cache.clear()
