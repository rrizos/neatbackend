"""Re-encode post images that were stored exactly as the phone sent them.

Camera JPEGs arrive close to lossless — 2 MB for 828x1792 in this database,
against about 200 KB for the same pixels at quality 85. A feed of twenty is the
difference between 40 MB and 4 MB, which on a phone connection is most of the
time a cold feed spends showing grey boxes.

Rewrites in place at the same URL, so nothing that already points at these
files has to change. Skips anything that would not actually get smaller, and
leaves the original alone if the re-encode fails.
"""

import io
import os

from django.core.management.base import BaseCommand
from django.conf import settings

from posts.models import Post, PostMedia

QUALITY = 85
MAX_PX = 1600


class Command(BaseCommand):
    help = 'Re-encode oversized post images in place.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Actually rewrite. Without it, only reports.')
        parser.add_argument('--min-kb', type=int, default=400,
                            help='Only touch files larger than this.')

    def handle(self, *args, **options):
        try:
            from PIL import Image, ImageOps
        except ImportError:
            self.stderr.write('Pillow is not installed')
            return

        apply_changes = options['apply']
        floor = options['min_kb'] * 1024
        root = settings.MEDIA_ROOT
        saved = examined = rewritten = 0

        # Both places an image can live. Older posts keep theirs on
        # Post.image_url; newer ones have a PostMedia row. The file on disk is
        # the same kind of thing either way, and the oversized ones are split
        # across both.
        urls = [m.url or '' for m in
                PostMedia.objects.filter(media_type='image').iterator()]
        urls += [p.image_url or '' for p in
                 Post.objects.exclude(image_url='').iterator()]

        for url in dict.fromkeys(urls):   # de-duplicated, order kept
            if not url or not url.startswith(settings.MEDIA_URL):
                continue  # remote (Giphy and friends) — not ours to touch
            path = os.path.join(root, url[len(settings.MEDIA_URL):])
            if not os.path.isfile(path):
                continue
            before = os.path.getsize(path)
            examined += 1
            if before < floor:
                continue
            try:
                img = ImageOps.exif_transpose(Image.open(path))
                img.thumbnail((MAX_PX, MAX_PX), Image.LANCZOS)
                buf = io.BytesIO()
                img.convert('RGB').save(buf, format='JPEG', quality=QUALITY,
                                        optimize=True, progressive=True)
            except Exception as exc:
                self.stderr.write(f'  skipped {os.path.basename(path)}: {exc}')
                continue

            after = buf.tell()
            if after >= before:
                continue  # already efficient; rewriting would only lose detail

            self.stdout.write(
                f'  {os.path.basename(path)[:12]}  '
                f'{before / 1048576:.2f}MB -> {after / 1048576:.2f}MB  '
                f'({100 - after * 100 // before}% smaller)'
            )
            saved += before - after
            rewritten += 1
            if apply_changes:
                # Written to a neighbour first, then moved into place, so an
                # interrupted run cannot leave a half-written image at a URL
                # that is already being served.
                tmp = path + '.new'
                with open(tmp, 'wb') as fh:
                    fh.write(buf.getvalue())
                os.replace(tmp, path)

        verb = 'rewrote' if apply_changes else 'would rewrite'
        self.stdout.write(self.style.SUCCESS(
            f'{verb} {rewritten} of {examined} images, '
            f'saving {saved / 1048576:.1f} MB'
        ))
        if not apply_changes and rewritten:
            self.stdout.write('re-run with --apply to make the change')
