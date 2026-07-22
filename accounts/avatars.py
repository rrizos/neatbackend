"""Avatar image normalization.

Profile avatars are stored in `Profile.avatar_url` as base64 image data URLs
uploaded straight from the client. Historically these were never resized, so a
full-resolution photo (hundreds of KB) got embedded inline in every payload that
carries the author's avatar -- the feed, the viral/charts list, notifications,
messages. Downscaling them to a small avatar-sized image shrinks that by ~95%
with no visible change (avatars render in ~50-100px circles).
"""

import base64
import io

try:
    from PIL import Image
    _PIL_OK = True
except Exception:  # Pillow missing (shouldn't happen in prod) -> pass through.
    _PIL_OK = False

# Longest edge for a stored avatar. Avatars display in small circles, so 256px
# is already generous.
AVATAR_MAX_PX = 256
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
            img.convert('RGB').save(out, format='JPEG', quality=82, optimize=True)
            mime = 'image/jpeg'
        encoded = base64.b64encode(out.getvalue()).decode('ascii')
        resized = f'data:{mime};base64,{encoded}'
        # Guard against the rare case where re-encoding grew the payload.
        return resized if len(resized) < len(value) else value
    except Exception:
        return value
