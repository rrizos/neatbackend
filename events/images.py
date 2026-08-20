"""Event pictures as files, not as base64 inside every response.

An event's picture used to live in `Event.image_url` as a base64 data URL, so
the whole image travelled inside the JSON of every list that mentioned the
event. Measured on the live server: `/api/events/` averaged 316 KB per request
and **99% of it was base64 images**.

That is the difference between a list that loads on a weak connection and one
that does not, for three reasons beyond the size itself:

  * gzip cannot help. A base64 JPEG is already-compressed data wearing a text
    costume — the whole endpoint compresses by ~25%, where real JSON manages
    80%.
  * It is re-sent every single time. A file has a URL, so the client fetches it
    once and every later render is free.
  * It blocks. The list cannot render until the last byte of the last picture
    has arrived, instead of drawing immediately and filling pictures in.

Same approach as accounts/avatars.py and linkpreview/thumbnails.py: decode
once, write a file, keep a URL.
"""

import base64
import io
import logging
import uuid

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

logger = logging.getLogger(__name__)

try:
    from PIL import Image, ImageOps
    _PIL_OK = True
except Exception:
    _PIL_OK = False

# Event cards render full-bleed at phone width; 1280 is sharp on a 3x screen
# without storing the original.
MAX_PX = 1280
STORAGE_DIR = 'events'


def store_event_image(value, previous_url=''):
    """Turn a base64 data URL into a stored file, returning its media URL.

    Pass-through for anything already a URL, and for anything that fails —
    never raises, because a picture that cannot be written must not stop
    somebody creating their event.
    """
    if not _PIL_OK or not value or not value.startswith('data:'):
        return value or previous_url
    try:
        comma = value.find(',')
        if comma < 0:
            return value
        raw = base64.b64decode(value[comma + 1:])

        img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw)))
        img.thumbnail((MAX_PX, MAX_PX), Image.LANCZOS)
        out = io.BytesIO()
        img.convert('RGB').save(
            out, format='JPEG', quality=85, optimize=True, progressive=True
        )
        name = default_storage.save(
            f'{STORAGE_DIR}/{uuid.uuid4()}.jpg', ContentFile(out.getvalue())
        )
        url = default_storage.url(name)
    except Exception:
        logger.exception('could not store event image')
        return value

    discard_event_image(previous_url)
    return url


def discard_event_image(url):
    """Best-effort delete of a file previously written here."""
    if not url or not url.startswith(settings.MEDIA_URL):
        return
    name = url[len(settings.MEDIA_URL):].lstrip('/')
    if not name.startswith(f'{STORAGE_DIR}/') or '..' in name:
        return
    try:
        if default_storage.exists(name):
            default_storage.delete(name)
    except Exception:
        logger.warning('could not delete event image %s', url, exc_info=True)


def public_url(url):
    """Absolute form, so a client can load it without knowing our host."""
    if url and url.startswith(settings.MEDIA_URL):
        return f'{settings.PUBLIC_BASE_URL}{url}'
    return url


def store_event_image_upload(uploaded, previous_url=''):
    """Store an event picture that arrived as a binary file part.

    The twin of `store_event_image`, for clients that send the JPEG rather than
    base64 of it. Returns '' when there is no file, so the caller can fall back
    to the JSON field.
    """
    if uploaded is None:
        return ''
    try:
        raw = uploaded.read()
    except Exception:
        return ''
    if not raw or not _PIL_OK:
        return ''
    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw)))
        img.thumbnail((MAX_PX, MAX_PX), Image.LANCZOS)
        out = io.BytesIO()
        img.convert('RGB').save(
            out, format='JPEG', quality=85, optimize=True, progressive=True
        )
        name = default_storage.save(
            f'{STORAGE_DIR}/{uuid.uuid4()}.jpg', ContentFile(out.getvalue())
        )
    except Exception:
        logger.exception('could not store uploaded event image')
        return ''
    discard_event_image(previous_url)
    return default_storage.url(name)
