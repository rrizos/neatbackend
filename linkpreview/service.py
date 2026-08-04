"""Resolving the link inside a piece of user text, for callers other than the
preview endpoint.

The app asks for previews itself, one link at a time. This is for the places
that need the card server-side: the Netlify edge functions that build the
Open Graph tags and the social image for /post/<id>, so a neat post shared
into WhatsApp shows what the post is about rather than the raw URL its author
pasted.
"""

import re

from django.utils import timezone

from .fetcher import fetch_preview, normalise_url
from .models import LinkPreview, url_fingerprint

# Mirrors the client-side regex in lib/src/core/link_preview.dart: an explicit
# scheme, a www. host, or a bare host.tld — keep the two in step so the card
# the author saw while typing is the card their share produces.
_URL_RE = re.compile(
    r'\b(?:https?://|www\.)[^\s<>"]+'
    r'|\b[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9-]+)*'
    r'\.(?:com|gr|org|net|io|dev|app|edu|gov|co|uk|de|eu|info|me|tv|news|xyz)'
    r'(?:/[^\s<>"]*)?',
    re.IGNORECASE,
)

_TRAILING = '.,;:!?)]}\'"«»…'


def _trim(raw):
    end = len(raw)
    while end > 0 and raw[end - 1] in _TRAILING:
        if raw[end - 1] == ')' and raw[:end].count('(') >= raw[:end].count(')'):
            break
        end -= 1
    return raw[:end]


def first_url(text):
    """The first link in [text], or None. Ignores the domain half of emails."""
    for match in _URL_RE.finditer(text or ''):
        if match.start() > 0 and text[match.start() - 1] == '@':
            continue
        url = _trim(match.group(0))
        if url:
            return url
    return None


def _remember_failure(fingerprint, url):
    LinkPreview.objects.update_or_create(
        url_hash=fingerprint,
        defaults={'url': url, 'ok': False, 'fetched_at': timezone.now()},
    )


def cached_row(url):
    """The stored row for [url] if it is still fresh, else None."""
    row = LinkPreview.objects.filter(url_hash=url_fingerprint(url)).first()
    return row if (row is not None and not row.is_stale) else None


def stale_row(url):
    """The stored row for [url] whether or not it is fresh."""
    return LinkPreview.objects.filter(url_hash=url_fingerprint(url)).first()


def resolve_and_store(url):
    """Fetch [url], write the result to the cache, and return the row or None.

    Failures are cached as well as successes, so a dead link costs one caller
    the timeout rather than every caller who ever sees it.
    """
    fingerprint = url_fingerprint(url)
    try:
        data = fetch_preview(url)
    except Exception:
        # UnsafeUrl, a timeout, a TLS failure, a reset — all "no card".
        _remember_failure(fingerprint, url)
        return None

    # A page with neither a title nor an image has nothing worth rendering.
    if not data['title'] and not data['image_url']:
        _remember_failure(fingerprint, url)
        return None

    row, _ = LinkPreview.objects.update_or_create(
        url_hash=fingerprint,
        defaults={
            'url': url,
            'resolved_url': data['url'],
            'title': data['title'],
            'description': data['description'],
            'image_url': data['image_url'],
            'image_width': data.get('image_width', 0),
            'image_height': data.get('image_height', 0),
            'site_name': data['site_name'],
            'author_name': data.get('author_name', ''),
            'author_handle': data.get('author_handle', ''),
            'author_url': data.get('author_url', ''),
            'kind': data.get('kind', ''),
            'ok': True,
            'fetched_at': timezone.now(),
        },
    )
    return row


def preview_for_text(text, resolve=True):
    """The card for the first link in [text], as a dict, or None.

    With [resolve] the link is fetched when it isn't cached yet — the first
    share of a given URL pays for it, everyone after that reads the cache.
    """
    url = first_url(text)
    if not url:
        return None
    url = normalise_url(url)

    row = cached_row(url)
    if row is not None:
        return row.to_dict() if row.ok else None
    if not resolve:
        return None

    row = resolve_and_store(url)
    return row.to_dict() if row is not None else None
