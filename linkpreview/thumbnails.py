"""Keeping a copy of a link's picture, because the original does not last.

Cards used to store whatever image URL the page advertised and hand that
straight to the client. For most sites that is fine. For TikTok — and Instagram
and Facebook, which sign the same way — it is not: their CDN URLs carry an
`x-expires` timestamp and a signature, and roughly two days later the URL
returns 403 forever.

The card outlived its own picture. `GOOD_TTL` keeps a resolved card for seven
days, so for the last five of those the app was rendering an <img> pointing at
a dead URL, and the client's errorBuilder quietly collapsed it to nothing. That
is the "thumbnails disappear when I open the app again" bug: nothing had gone
wrong locally, the picture had simply expired upstream while the card that
referenced it had not.

So the picture is copied onto our own disk at resolve time and served from
/media/ like any other upload — no expiry, no signature, no dependency on
somebody else's CDN staying friendly, and it inherits the `immutable` caching
nginx already applies there.
"""

import io
import logging
import uuid

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from .fetcher import fetch_image

logger = logging.getLogger(__name__)

try:
    from PIL import Image, ImageOps
    _PIL_OK = True
except Exception:  # Pillow missing (shouldn't happen in prod).
    _PIL_OK = False

# Cards render a few hundred points wide at most; 1080 keeps a portrait 9:16
# frame sharp on a 3x screen without storing the source at full size.
THUMB_MAX_PX = 1080
STORAGE_DIR = 'linkpreview'


def store_thumbnail(image_url):
    """Copy [image_url] to our own media storage.

    Returns a MEDIA_URL-relative path, or '' if the picture could not be
    fetched or decoded — in which case the caller keeps the original URL and
    the card behaves exactly as it did before. Never raises: a preview is
    decoration, and failing to copy its picture must not fail the post that
    contains the link.
    """
    if not _PIL_OK or not image_url or not image_url.startswith(('http://', 'https://')):
        return ''
    try:
        raw, _ctype = fetch_image(image_url)
        # Decode before trusting it: `content-type: image/jpeg` on a payload
        # that is not an image would otherwise be served back from our domain.
        img = Image.open(io.BytesIO(raw))
        img = ImageOps.exif_transpose(img)
        img.thumbnail((THUMB_MAX_PX, THUMB_MAX_PX), Image.LANCZOS)

        out = io.BytesIO()
        img.convert('RGB').save(
            out, format='JPEG', quality=82, optimize=True, progressive=True
        )
        name = default_storage.save(
            f'{STORAGE_DIR}/{uuid.uuid4()}.jpg', ContentFile(out.getvalue())
        )
        return default_storage.url(name)
    except Exception as exc:
        # Expected often enough not to be worth a traceback: hotlink blocks,
        # timeouts, SVG favicons, and pages that advertise an image they do not
        # actually serve.
        logger.info('link preview thumbnail not copied (%s): %s', exc, image_url[:120])
        return ''


def discard_thumbnail(media_url):
    """Best-effort delete of a copy previously made here."""
    from django.conf import settings

    if not media_url or not media_url.startswith(settings.MEDIA_URL):
        return
    name = media_url[len(settings.MEDIA_URL):].lstrip('/')
    # Stored values are ours, but deletion is not where that gets assumed.
    if not name.startswith(f'{STORAGE_DIR}/') or '..' in name:
        return
    try:
        if default_storage.exists(name):
            default_storage.delete(name)
    except Exception:
        logger.warning('could not delete thumbnail %s', media_url, exc_info=True)
