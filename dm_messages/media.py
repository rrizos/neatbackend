"""DM photos and voice notes as files, not as base64 inside the message row.

`Message.text` carries the payload behind a `__neat_image__:` or
`__neat_voice__:` prefix. Measured on the live database, that is **11.3 MB of a
21 MB database** — over half of everything stored — and it is the reason chat
pictures are slower to appear than anything else in the app:

  * base64 is a third larger than the file it encodes;
  * a row cannot be served by nginx, so every byte goes through Django;
  * and it cannot be cached by URL, so it is fetched again on every device that
    ever displays it.

Ordinary media moves to `MEDIA_ROOT/dm/`. **Temporary photos deliberately do
not.** A view-once photo promises the bytes stop existing once the viewings are
spent, and `message_open` delivers that by clearing the column — a file sitting
at a stable URL cannot make the same promise, since anyone holding the link
would bypass the counter entirely. Rare, short-lived, and worth keeping in the
row where the guarantee is enforceable.
"""

import base64
import io
import logging
import uuid

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from neatbackend.cdn import cdn

logger = logging.getLogger(__name__)

IMAGE_PREFIX = '__neat_image__:'
VOICE_PREFIX = '__neat_voice__:'
STORAGE_DIR = 'dm'

try:
    from PIL import Image, ImageOps
    _PIL_OK = True
except Exception:
    _PIL_OK = False

# Chat photos are drawn in a 260pt bubble and open fullscreen; 1080 covers both
# on a 3x screen. Matches what the client already sends.
MAX_PX = 1080


def split_payload(text):
    """('image'|'voice', base64, suffix) for a media message, else None.

    A voice note carries `|<seconds>` after its data, which has to survive.
    """
    for kind, prefix in (('image', IMAGE_PREFIX), ('voice', VOICE_PREFIX)):
        if not text.startswith(prefix):
            continue
        body = text[len(prefix):]
        if not body:
            return None  # already stripped for this client
        suffix = ''
        bar = body.rfind('|')
        if kind == 'voice' and bar >= 0:
            suffix = body[bar:]
            body = body[:bar]
        return kind, body, suffix
    return None


def store_message_media(text):
    """Write a message's payload to disk. Returns (media_url, new_text).

    `new_text` keeps the prefix and any suffix but drops the bytes, so the row
    still says what kind of message it is. Returns (None, text) unchanged for
    anything that is not storable media.
    """
    parsed = split_payload(text or '')
    if parsed is None:
        return None, text
    kind, encoded, suffix = parsed
    try:
        raw = base64.b64decode(encoded)
    except Exception:
        return None, text
    if not raw:
        return None, text

    try:
        if kind == 'image':
            if not _PIL_OK:
                return None, text
            img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw)))
            img.thumbnail((MAX_PX, MAX_PX), Image.LANCZOS)
            out = io.BytesIO()
            img.convert('RGB').save(
                out, format='JPEG', quality=82, optimize=True, progressive=True
            )
            name = f'{STORAGE_DIR}/{uuid.uuid4()}.jpg'
            data = out.getvalue()
        else:
            # Voice notes are already compressed; re-encoding would only lose
            # quality, so the bytes are written exactly as they arrived.
            name = f'{STORAGE_DIR}/{uuid.uuid4()}.m4a'
            data = raw
        stored = default_storage.save(name, ContentFile(data))
    except Exception:
        logger.exception('could not store DM media')
        return None, text

    prefix = IMAGE_PREFIX if kind == 'image' else VOICE_PREFIX
    return default_storage.url(stored), f'{prefix}{suffix}'


def discard_message_media(url):
    """Best-effort delete of a file written here."""
    if not url or not url.startswith(settings.MEDIA_URL):
        return
    name = url[len(settings.MEDIA_URL):].lstrip('/')
    if not name.startswith(f'{STORAGE_DIR}/') or '..' in name:
        return
    try:
        if default_storage.exists(name):
            default_storage.delete(name)
    except Exception:
        logger.warning('could not delete DM media %s', url, exc_info=True)


def public_url(url):
    if url and url.startswith(settings.MEDIA_URL):
        # Through the CDN when one is configured; otherwise this host, which
        # is what every installed build already fetches from.
        edge = cdn(url)
        return edge if edge != url else f'{settings.PUBLIC_BASE_URL}{url}'
    return url


def as_data_url(media_url, kind_prefix):
    """Read a stored file back as the base64 payload an old client expects.

    Builds released before this only understand `text` carrying the bytes, and
    they must keep working — so for them the file is read back and re-encoded.
    Slower than it was, but correct, and only for clients that cannot do better.
    """
    if not media_url:
        return ''
    name = media_url[len(settings.MEDIA_URL):].lstrip('/')
    try:
        with default_storage.open(name, 'rb') as fh:
            return base64.b64encode(fh.read()).decode('ascii')
    except Exception:
        logger.warning('could not read back DM media %s', media_url, exc_info=True)
        return ''
