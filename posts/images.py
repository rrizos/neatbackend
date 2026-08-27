"""Post and comment pictures as files.

Comment images were the reason this existed — `PostComment.image_url` still
held base64 long after post images had moved to disk.

Post images then turned out to have a different problem: they were written to
disk exactly as the phone sent them. A camera JPEG arrives at close to
lossless, so a single 828x1792 photo was **2 MB** on the wire when the same
pixels re-encoded at quality 85 are **200 KB**. A feed of twenty of those is
40 MB, which over a phone connection is the better part of a minute of
staring at grey boxes. Re-encoding on the way in gives that back at no visible
cost — the resolution does not change, only the quality setting it was saved
with.
"""

import base64
import io
import logging
import uuid

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

logger = logging.getLogger(__name__)

MAX_PX = 1600
STORAGE_DIR = 'posts'

try:
    from PIL import Image, ImageOps
    _PIL_OK = True
except Exception:
    _PIL_OK = False


def _write(raw):
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
        return default_storage.url(name)
    except Exception:
        logger.exception('could not store comment image')
        return ''


def store_post_image(uploaded):
    """Re-encode and store a post image, returning its URL.

    Falls back to storing the original if Pillow is unavailable or the file
    cannot be decoded: an upload that reaches this point has already been
    validated, and refusing it here would lose a post over a compression
    detail.
    """
    if uploaded is None:
        return ''
    try:
        uploaded.seek(0)
        raw = uploaded.read()
    except Exception:
        return ''
    url = _write(raw)
    if url:
        return url
    # Could not re-encode; keep the original rather than drop the picture.
    try:
        uploaded.seek(0)
        name = default_storage.save(f'{STORAGE_DIR}/{uuid.uuid4()}.jpg', uploaded)
        return default_storage.url(name)
    except Exception:
        logger.exception('could not store post image')
        return ''


def store_comment_image(uploaded):
    """From a binary file part."""
    if uploaded is None:
        return ''
    try:
        return _write(uploaded.read())
    except Exception:
        return ''


def store_comment_image_data(data_url):
    """From a base64 data URL, for clients that still send one."""
    if not str(data_url).startswith('data:'):
        return ''
    try:
        return _write(base64.b64decode(data_url[data_url.find(',') + 1:]))
    except Exception:
        return ''
