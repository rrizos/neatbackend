"""oEmbed lookups for the sites that don't put the good stuff in og: tags.

Some providers describe a *specific piece of content* far better through
oEmbed than through Open Graph. TikTok is the clearest case: its crawler HTML
gives only "TikTok · Zach King" and a thumbnail, while its oEmbed endpoint
returns the caption, the creator's display name, their handle, and the video
thumbnail — which is what a preview card actually wants to show.

Only providers whose endpoints are known-public and unauthenticated are listed
here. Instagram is deliberately absent: its public oEmbed was retired, and the
Graph replacement returns an embed <blockquote> with no thumbnail or author
unless you hold a Meta app token. Instagram is handled by the normal Open
Graph path instead, which does return the caption, author and real thumbnail
once we identify as a bot (see fetcher.USER_AGENT).
"""

import json
from urllib.parse import quote, urlsplit

# host suffix -> oEmbed endpoint template ({url} is the percent-encoded target)
_PROVIDERS = (
    ('tiktok.com', 'https://www.tiktok.com/oembed?url={url}'),
    ('youtube.com', 'https://www.youtube.com/oembed?url={url}&format=json'),
    ('youtu.be', 'https://www.youtube.com/oembed?url={url}&format=json'),
    ('vimeo.com', 'https://vimeo.com/api/oembed.json?url={url}'),
    ('soundcloud.com', 'https://soundcloud.com/oembed?format=json&url={url}'),
    ('open.spotify.com', 'https://open.spotify.com/oembed?url={url}'),
)


def endpoint_for(url):
    """The oEmbed endpoint for [url], or None if we don't know one."""
    host = (urlsplit(url).hostname or '').lower().removeprefix('www.')
    for suffix, template in _PROVIDERS:
        if host == suffix or host.endswith('.' + suffix):
            return template.format(url=quote(url, safe=''))
    return None


def parse(payload):
    """Reduce an oEmbed document to the fields a preview card uses.

    Returns None when the response carries nothing worth rendering, so the
    caller can fall through to Open Graph rather than showing a blank card.
    """
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    title = (data.get('title') or '').strip()
    thumbnail = (data.get('thumbnail_url') or '').strip()
    if not title and not thumbnail:
        return None

    author = (data.get('author_name') or '').strip()
    author_url = (data.get('author_url') or '').strip()
    # TikTok returns the bare handle separately; prefer it for the "@name"
    # line because author_name is the display name ("Zach King" vs "zachking").
    handle = (data.get('author_unique_id') or '').strip()

    def dimension(key):
        try:
            return max(0, int(data.get(key) or 0))
        except (TypeError, ValueError):
            return 0

    return {
        'title': title,
        'thumbnail_url': thumbnail,
        # Short-form video is portrait (TikTok returns 576x1090). Carrying the
        # real dimensions lets the card render it portrait instead of cropping
        # a 9:16 frame into a letterbox.
        'image_width': dimension('thumbnail_width'),
        'image_height': dimension('thumbnail_height'),
        'author_name': author,
        'author_handle': handle,
        'author_url': author_url,
        'site_name': (data.get('provider_name') or '').strip(),
        # oEmbed types are video/photo/rich/link; ours are video/photo/article.
        'kind': 'video' if data.get('type') == 'video' else '',
    }
