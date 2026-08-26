"""Where media is fetched from, as told to the app.

Files stay on this server and keep their `/media/...` paths in the database —
that path is also how the code finds them on disk, so it cannot change. What
changes is the address handed to clients: with a CDN configured, a media URL
goes out absolute and pointed at the edge instead of relative and pointed here.

The reason is delivery speed, measured rather than assumed. Downloading from
this box runs at about 3.6 Mbit/s from Greece; the same connection pulls 42
Mbit/s from a CDN edge. A six-second video that takes nine seconds to arrive
from here takes under one from an edge, with no change to the file at all.

Inert until MEDIA_CDN_URL is set, so this can ship before the distribution
exists and start working the moment it does — no code change, no rebuild.

Deliberately applied when serialising rather than when storing: the stored path
is what the transcoder, the image tools and every cleanup command use to find
the file, and rewriting it would break all of them.
"""

from django.conf import settings


def cdn(url):
    """The address a client should fetch [url] from.

    Anything that is not one of our own media paths — a Giphy URL, an empty
    string, something already absolute — is returned untouched.
    """
    base = getattr(settings, 'MEDIA_CDN_URL', '')
    if not base or not url:
        return url
    if not url.startswith(settings.MEDIA_URL):
        return url
    return base.rstrip('/') + url


def cdn_all(*urls):
    """[cdn] over several values at once."""
    return tuple(cdn(u) for u in urls)
