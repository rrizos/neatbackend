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

from .fetcher import fetch_preview, normalise_url  # noqa: F401  (re-exported)
from .thumbnails import discard_thumbnail, store_thumbnail
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
    """Record that a fetch failed -- without throwing away a working card.

    This used to `update_or_create(ok=False)` unconditionally, so the first
    failed *refresh* of an already-resolved link marked it dead. A card would
    work for GOOD_TTL, go stale, fail one refresh against a host that blocks
    crawlers, and be served as "no preview" from then on. Losing a good card
    because we could not re-confirm it is strictly worse than showing one that
    is slightly old.
    """
    existing = LinkPreview.objects.filter(url_hash=fingerprint).first()
    if existing is not None and existing.ok and (existing.title or existing.image_url):
        # Push the clock forward only, so the next refresh is attempted later
        # rather than on every single request.
        existing.fetched_at = timezone.now()
        existing.save(update_fields=['fetched_at'])
        return
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

    # Nothing worth rendering: no title, no picture, and nobody to credit.
    # The author matters here — a TikTok photo post has no oEmbed and serves a
    # crawler no image at all, so the creator's handle is the only true thing
    # about it, and "@grstorm.2 on TikTok" is a real card.
    if not data['title'] and not data['image_url'] and not data.get('author_handle'):
        _remember_failure(fingerprint, url)
        return None

    # Nor does a title on its own. TikTok answers every request for a video it
    # will not describe -- deleted, private, or a short link that resolves to an
    # empty username -- with its boilerplate page title and nothing else, which
    # became a card showing "TikTok - Make Your Day" above a blank space. A
    # title with no picture and no description is barely more than the URL, and
    # the client already renders a bare link tappably.
    if (not data['image_url'] and not data['description']
            and not data.get('author_handle')):
        _remember_failure(fingerprint, url)
        return None

    # Copy the picture onto our own disk. TikTok/Instagram sign their CDN URLs
    # with a ~2 day expiry while this card lives for seven, so storing their URL
    # meant the thumbnail vanished mid-life. Falls back to the original URL when
    # the copy fails, which is exactly the old behaviour.
    stored_image = store_thumbnail(data['image_url']) if data['image_url'] else ''

    previous = LinkPreview.objects.filter(url_hash=fingerprint).first()
    previous_local = (
        previous.image_url
        if previous is not None and previous.is_local_image
        else ''
    )

    row, _ = LinkPreview.objects.update_or_create(
        url_hash=fingerprint,
        defaults={
            'url': url,
            'resolved_url': data['url'],
            'title': data['title'],
            'description': data['description'],
            'image_url': stored_image or data['image_url'],
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
    # Only once the row points at the new copy, so a failure above leaves the
    # old picture in place rather than a card referencing a deleted file.
    if previous_local and previous_local != row.image_url:
        discard_thumbnail(previous_local)
    return row


def previews_for_texts(texts):
    """Cached cards for the first link in each of [texts], keyed by URL.

    One query, and never a fetch. This is what feeds and inboxes use: a list
    must not wait on outbound requests, and a link nobody has resolved yet is
    simply absent — the client asks for that one itself. Everything already
    known arrives with the list instead of costing a round trip per row, which
    is the difference between thumbnails being there on arrival and appearing
    a minute later once the rate limiter has let them all through.
    """
    wanted = {}
    for text in texts:
        url = first_url(text or '')
        if not url:
            continue
        url = normalise_url(url)
        wanted[url_fingerprint(url)] = url
    if not wanted:
        return {}
    rows = LinkPreview.objects.filter(url_hash__in=list(wanted), ok=True)
    # Stale rows are included on purpose. A card that resolved a fortnight ago
    # still has the right title, description and picture -- staleness is a hint
    # that it is worth refreshing, not a reason to show nothing. Dropping them
    # here is what made a link show its card for a week and then go blank for
    # good: past GOOD_TTL the feed stopped attaching it, the client had to
    # re-resolve it live, and for hosts that refuse a crawler (Instagram,
    # TikTok) that re-resolve fails every time.
    return {row.url: row.to_dict() for row in rows}


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


def resolve_pending(post_min_id=0, message_min_id=0, limit=20):
    """Resolve links in newly created content that has no card yet.

    Previews are otherwise built lazily — the first person to see a post pays
    for the fetch, and until then the card is simply missing. That is fine for
    an old post nobody opens and wrong for one just published, which is exactly
    when its author is looking at it. A worker calling this keeps the card
    ahead of the reader instead of behind them.

    Scans only ids above the given watermarks, so a caller in a loop looks at
    each row once. Returns (resolved_count, highest_post_id, highest_message_id).
    """
    from dm_messages.models import Message
    from posts.models import Post

    posts = list(
        Post.objects.filter(id__gt=post_min_id).order_by('id').values_list('id', 'text')[:200]
    )
    messages = list(
        Message.objects.filter(id__gt=message_min_id).order_by('id').values_list('id', 'text')[:200]
    )

    highest_post = max([i for i, _ in posts], default=post_min_id)
    highest_message = max([i for i, _ in messages], default=message_min_id)

    urls = []
    for _id, text in posts + messages:
        url = first_url(text or '')
        if url:
            urls.append(normalise_url(url))

    resolved = 0
    seen = set()
    for url in urls:
        if url in seen or len(seen) >= limit:
            continue
        seen.add(url)
        # Anything already known — card or recorded failure — is left alone.
        if LinkPreview.objects.filter(url_hash=url_fingerprint(url)).exists():
            continue
        if resolve_and_store(url) is not None:
            resolved += 1

    return resolved, highest_post, highest_message
