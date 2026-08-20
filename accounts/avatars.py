"""Avatar image normalization.

Profile avatars are stored in `Profile.avatar_url` as base64 image data URLs
uploaded straight from the client. Historically these were never resized, so a
full-resolution photo (hundreds of KB) got embedded inline in every payload that
carries the author's avatar -- the feed, the viral/charts list, notifications,
messages. Downscaling them to a small avatar-sized image shrinks that by ~95%
with no visible change (avatars render in ~50-100px circles).

That downscale is also why a profile picture looked soft: 256px is right for a
circle in a feed row, and far too little for the tap-to-enlarge view, which
paints it at 240dp -- 720 physical px on a 3x phone, nearly three times the
stored image. Both readings are correct, so avatars are now stored twice:

- `Profile.avatar_url` keeps the small inline copy, unchanged in size, because
  it rides along in every payload that mentions the user and is the thing that
  has to stay cheap.
- `Profile.avatar_full_url` is a *file* on disk under ``MEDIA_ROOT/avatars/``,
  served by nginx and fetched only by the two screens that show an avatar
  large. It costs the feed nothing and is cached by URL on the device.
"""

import base64
import io
import uuid

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

try:
    from PIL import Image, ImageOps
    _PIL_OK = True
except Exception:  # Pillow missing (shouldn't happen in prod) -> pass through.
    _PIL_OK = False

# Longest edge for the inline avatar. Avatars display in small circles, so
# 256px is already generous -- and this copy is duplicated into every payload
# carrying the user, so growing it is the most expensive change available.
AVATAR_MAX_PX = 256
# Longest edge for the on-disk copy behind the enlarged view. 1024 covers a
# 3x phone showing it full-width with room to spare; the crop the client sends
# is 1200px, so this is nearly always a mild downscale rather than an upscale.
AVATAR_FULL_MAX_PX = 1024
# The inline copy's dimensions, but written to disk instead of into every
# payload. Same picture, fetched once and cached by URL.
AVATAR_THUMB_MAX_PX = 256
# Only touch data URLs clearly larger than a well-compressed avatar; small ones
# are left byte-for-byte identical.
_RESIZE_THRESHOLD_BYTES = 60_000


def resize_avatar_data_url(value, max_px=AVATAR_MAX_PX):
    """Downscale an oversized base64 image data URL to an avatar-sized image.

    Pass-through (returns the input unchanged) for: empty values, non-data-URL
    strings (e.g. already-hosted file URLs), data URLs already under the size
    threshold, and anything that fails to decode. Never raises -- a bad image
    must not block a profile save.
    """
    try:
        if not _PIL_OK or not value or not value.startswith('data:'):
            return value
        comma = value.find(',')
        if comma < 0:
            return value
        raw = base64.b64decode(value[comma + 1:])
        if len(raw) <= _RESIZE_THRESHOLD_BYTES:
            return value

        img = Image.open(io.BytesIO(raw))
        has_alpha = img.mode in ('RGBA', 'LA') or (
            img.mode == 'P' and 'transparency' in img.info
        )
        img.thumbnail((max_px, max_px), Image.LANCZOS)

        out = io.BytesIO()
        if has_alpha:
            img.convert('RGBA').save(out, format='PNG', optimize=True)
            mime = 'image/png'
        else:
            # q88 rather than q82: the inline copy is drawn at up to 96dp
            # (288px on a 3x phone) against a stored 256px, so it is already
            # being upscaled slightly and JPEG artefacts show. A few KB on an
            # image this small is a cheap way to stop that looking soft.
            img.convert('RGB').save(out, format='JPEG', quality=88, optimize=True)
            mime = 'image/jpeg'
        encoded = base64.b64encode(out.getvalue()).decode('ascii')
        resized = f'data:{mime};base64,{encoded}'
        # Guard against the rare case where re-encoding grew the payload.
        return resized if len(resized) < len(value) else value
    except Exception:
        return value


def store_full_avatar(value, previous_url=''):
    """Write the full-resolution copy of a base64 avatar to disk.

    Returns the media URL of the new file, or ``previous_url`` unchanged when
    there is nothing new to write (empty value, a non-data-URL, an undecodable
    image, or any failure at all). Like `resize_avatar_data_url` this never
    raises: a profile save must not fail because a picture could not be
    written, it must simply keep the smaller inline copy it already has.

    Deletes `previous_url`'s file on success, so replacing an avatar does not
    leave the old one behind -- there is no other cleanup path for these.
    """
    if not _PIL_OK or not value or not value.startswith('data:'):
        return previous_url
    try:
        comma = value.find(',')
        if comma < 0:
            return previous_url
        raw = base64.b64decode(value[comma + 1:])

        img = Image.open(io.BytesIO(raw))
        # The client bakes EXIF rotation into pixels before uploading, but a
        # payload from anywhere else may not have, and a sideways avatar is a
        # worse bug than a soft one.
        img = ImageOps.exif_transpose(img)
        img.thumbnail((AVATAR_FULL_MAX_PX, AVATAR_FULL_MAX_PX), Image.LANCZOS)

        out = io.BytesIO()
        img.convert('RGB').save(
            out,
            format='JPEG',
            quality=90,
            optimize=True,
            # Renders in passes over a slow connection instead of top-to-bottom.
            progressive=True,
        )
        name = default_storage.save(
            f'avatars/{uuid.uuid4()}.jpg', ContentFile(out.getvalue())
        )
        url = default_storage.url(name)
    except Exception:
        return previous_url

    _delete_media_file(previous_url)
    return url


def _delete_media_file(url):
    """Best-effort delete of a media URL previously produced here."""
    if not url or not url.startswith('/media/'):
        return
    try:
        name = url[len('/media/'):]
        # Refuse anything that could climb out of the avatars directory --
        # this value is stored, not user-supplied, but deletion is not the
        # place to rely on that.
        if not name.startswith('avatars/') or '..' in name:
            return
        if default_storage.exists(name):
            default_storage.delete(name)
    except Exception:
        pass


def store_thumb_avatar(value, previous_url=''):
    """Write the small avatar to disk and return its media URL.

    The twin of `resize_avatar_data_url`: identical picture and dimensions, but
    a file rather than base64 inside the JSON. Everything about a person —
    a feed row, a notification, a chat list entry, a search result — carries
    their avatar, so this is the difference between a payload that grows with
    the number of people mentioned and one that does not.

    Never raises; returns `previous_url` if anything goes wrong.
    """
    if not _PIL_OK or not value or not value.startswith('data:'):
        return previous_url
    try:
        comma = value.find(',')
        if comma < 0:
            return previous_url
        raw = base64.b64decode(value[comma + 1:])
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw)))
        img.thumbnail((AVATAR_THUMB_MAX_PX, AVATAR_THUMB_MAX_PX), Image.LANCZOS)
        out = io.BytesIO()
        img.convert('RGB').save(
            out, format='JPEG', quality=88, optimize=True, progressive=True
        )
        name = default_storage.save(
            f'avatars/{uuid.uuid4()}.jpg', ContentFile(out.getvalue())
        )
        url = default_storage.url(name)
    except Exception:
        return previous_url

    _delete_media_file(previous_url)
    return url


def avatar_for(profile):
    """The avatar to serialise for whoever is asking.

    Newer clients get the file URL, which they fetch once and cache; older ones
    get the base64 copy they have always been given, because they decode data
    URLs directly and would draw initials for anything else.
    """
    if profile is None:
        return ''
    from django.conf import settings

    from .client_version import wants_url_avatars

    thumb = getattr(profile, 'avatar_thumb_url', '')
    if wants_url_avatars() and thumb:
        return f'{settings.PUBLIC_BASE_URL}{thumb}' if thumb.startswith('/') else thumb
    return getattr(profile, 'avatar_url', '') or ''


def store_avatar_from_bytes(raw, previous_thumb='', previous_full=''):
    """Write both avatar files from raw image bytes, and the inline copy too.

    The binary-upload twin of the base64 path. The client can now send the JPEG
    as a multipart file instead of encoding it into a JSON body — base64 costs a
    third more bytes, and an avatar upload is the largest thing the app sends on
    the connection where it has least to spare.

    Returns (thumb_url, full_url, inline_data_url). The inline copy is still
    produced because builds released before URL avatars can only read that.
    """
    if not _PIL_OK or not raw:
        return previous_thumb, previous_full, ''
    try:
        source = ImageOps.exif_transpose(Image.open(io.BytesIO(raw)))
    except Exception:
        return previous_thumb, previous_full, ''

    def _encode(max_px, quality):
        img = source.copy()
        img.thumbnail((max_px, max_px), Image.LANCZOS)
        out = io.BytesIO()
        img.convert('RGB').save(
            out, format='JPEG', quality=quality, optimize=True, progressive=True
        )
        return out.getvalue()

    try:
        thumb_bytes = _encode(AVATAR_THUMB_MAX_PX, 88)
        full_bytes = _encode(AVATAR_FULL_MAX_PX, 90)
        thumb = default_storage.url(default_storage.save(
            f'avatars/{uuid.uuid4()}.jpg', ContentFile(thumb_bytes)))
        full = default_storage.url(default_storage.save(
            f'avatars/{uuid.uuid4()}.jpg', ContentFile(full_bytes)))
    except Exception:
        return previous_thumb, previous_full, ''

    _delete_media_file(previous_thumb)
    _delete_media_file(previous_full)
    inline = 'data:image/jpeg;base64,' + base64.b64encode(thumb_bytes).decode('ascii')
    return thumb, full, inline
