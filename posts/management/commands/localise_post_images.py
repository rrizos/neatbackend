"""Move post pictures out of the database and onto disk.

Posts made before uploads were written as files — the WordPress import, and
early versions of the app — keep their picture as base64 in `Post.image_url`
and in the matching `PostMedia.url`. One such post measured 390 KB, and since
the feed serialises whatever those fields hold, that single post was 390 KB of
a 402 KB feed response. Every reader downloaded it, every time, and gzip could
not help because base64 JPEG is already-compressed data.

Rewrites them as files under MEDIA_ROOT/posts/, which nginx serves with a
year-long immutable cache. The URL that replaces them is a few dozen bytes.
"""

import base64
import io
import uuid

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand

from posts.models import Post, PostMedia

try:
    from PIL import Image, ImageOps
except Exception:
    Image = None

MAX_PX = 1600


def _store(data_url):
    """base64 -> a stored file's media URL, or '' if it cannot be decoded."""
    if Image is None or not data_url.startswith('data:'):
        return ''
    try:
        comma = data_url.find(',')
        raw = base64.b64decode(data_url[comma + 1:])
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw)))
        img.thumbnail((MAX_PX, MAX_PX), Image.LANCZOS)
        out = io.BytesIO()
        img.convert('RGB').save(
            out, format='JPEG', quality=85, optimize=True, progressive=True
        )
        name = default_storage.save(
            f'posts/{uuid.uuid4()}.jpg', ContentFile(out.getvalue())
        )
        return default_storage.url(name)
    except Exception:
        return ''


class Command(BaseCommand):
    help = 'Rewrite base64 post images as files.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        posts = [p for p in Post.objects.exclude(image_url='')
                 if p.image_url.startswith('data:')]
        media = [m for m in PostMedia.objects.all() if m.url.startswith('data:')]
        inline = sum(len(p.image_url) for p in posts) + sum(len(m.url) for m in media)
        self.stdout.write(
            f'{len(posts)} post(s) and {len(media)} media row(s) inline, '
            f'{inline/1024/1024:.1f} MB of base64'
        )
        if options['dry_run']:
            return

        # Convert each distinct image once, so a post and its media row that
        # hold the same picture end up pointing at the same file.
        converted = {}

        def url_for(data_url):
            if data_url not in converted:
                converted[data_url] = _store(data_url)
            return converted[data_url]

        moved = failed = 0
        for post in posts:
            url = url_for(post.image_url)
            if not url:
                failed += 1
                continue
            post.image_url = url
            post.save(update_fields=['image_url'])
            moved += 1
        for row in media:
            url = url_for(row.url)
            if not url:
                failed += 1
                continue
            row.url = url
            row.save(update_fields=['url'])
            moved += 1

        self.stdout.write(f'done: {moved} rewritten, {failed} failed')
