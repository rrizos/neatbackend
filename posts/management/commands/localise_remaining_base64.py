"""The last base64 in the database: comment images and shared-post snapshots.

Two leftovers after posts, events, avatars and DM media moved to disk:

  * `PostComment.image_url` — the same inline-image problem as posts had.
  * `Message.text` for a shared post (`__neat_post__:{...}`) — sharing a post
    into a chat stores a *snapshot* of it, and that snapshot embedded the
    post's picture and the author's avatar as base64. One measured 520 KB, and
    it is carried in every copy of that thread.

The snapshot's images are rewritten to point at the files those pictures now
live in, looked up by post id. Where the post is gone, the image is dropped —
the card still renders from its text, and a 520 KB card for a deleted post is
not worth keeping.
"""

import base64
import io
import json
import uuid

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand

from dm_messages.models import Message
from posts.models import Post, PostComment

try:
    from PIL import Image, ImageOps
except Exception:
    Image = None

PREFIX = '__neat_post__:'


def _store(data_url, max_px=1600):
    if Image is None or not str(data_url).startswith('data:'):
        return ''
    try:
        raw = base64.b64decode(data_url[data_url.find(',') + 1:])
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw)))
        img.thumbnail((max_px, max_px), Image.LANCZOS)
        out = io.BytesIO()
        img.convert('RGB').save(out, 'JPEG', quality=85, optimize=True, progressive=True)
        name = default_storage.save(f'posts/{uuid.uuid4()}.jpg', ContentFile(out.getvalue()))
        return default_storage.url(name)
    except Exception:
        return ''


class Command(BaseCommand):
    help = 'Rewrite the last base64 columns as file references.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        comments = [c for c in PostComment.objects.exclude(image_url='')
                    if c.image_url.startswith('data:')]
        shares = [m for m in Message.objects.filter(text__startswith=PREFIX)
                  if 'base64,' in m.text or 'data:' in m.text]
        total = sum(len(c.image_url) for c in comments) + sum(len(m.text) for m in shares)
        self.stdout.write(
            f'{len(comments)} comment image(s), {len(shares)} shared-post card(s), '
            f'{total/1024/1024:.2f} MB'
        )
        if options['dry_run']:
            return

        freed = 0
        for comment in comments:
            url = _store(comment.image_url)
            if not url:
                continue
            freed += len(comment.image_url) - len(url)
            comment.image_url = url
            comment.save(update_fields=['image_url'])

        for message in shares:
            before = len(message.text)
            try:
                data = json.loads(message.text[len(PREFIX):])
            except Exception:
                continue

            # The author's avatar is resolved by username on the client, so the
            # snapshot never needs to carry one.
            if str(data.get('avatarUrl', '')).startswith('data:'):
                data['avatarUrl'] = ''

            post = Post.objects.filter(pk=data.get('id')).first()
            live = ''
            if post is not None:
                first = post.media_items.first()
                live = (first.url if first is not None else post.image_url) or ''
                if live.startswith('data:'):
                    live = ''

            for key in ('imageUrl',):
                if str(data.get(key, '')).startswith('data:'):
                    data[key] = live or ''
            media = data.get('media')
            if isinstance(media, dict) and str(media.get('url', '')).startswith('data:'):
                if live:
                    media['url'] = live
                else:
                    data.pop('media', None)

            message.text = PREFIX + json.dumps(data, ensure_ascii=False)
            message.save(update_fields=['text'])
            freed += before - len(message.text)

        self.stdout.write(f'done: {freed/1024/1024:.2f} MB of base64 removed')
