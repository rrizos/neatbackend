"""Comment pictures as files.

Post images moved to disk long ago; comment images did not, so
`PostComment.image_url` still held base64 — the same cost in the same shape,
just somewhere nobody had looked.
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
