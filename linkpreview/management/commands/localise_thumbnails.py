"""Copy existing cards' pictures onto our own disk.

Cards resolved before thumbnails.py existed still point at whoever hosted the
image. For most sites that is fine forever — YouTube and news sites serve
stable URLs. For Instagram, TikTok and Facebook it is not: their CDN links are
signed with a short expiry and start returning 403, which is why a link showed
its picture for a while and then showed a card with a blank space where the
picture had been.

Two ways to fix one of those:

  * the image is still alive -> download it as-is, no re-resolve needed;
  * the image is already dead -> re-resolve the link, which yields a freshly
    signed URL, and copy that before it expires too.

Safe to re-run: cards already pointing at /media/ are skipped.
"""

from django.core.management.base import BaseCommand

from linkpreview import service
from linkpreview.models import LinkPreview
from linkpreview.thumbnails import store_thumbnail


class Command(BaseCommand):
    help = "Store link-preview thumbnails locally instead of hotlinking them."

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument(
            '--re-resolve', action='store_true',
            help='Re-fetch the page when its image can no longer be downloaded.',
        )

    def handle(self, *args, **options):
        rows = [
            r for r in LinkPreview.objects.filter(ok=True).exclude(image_url='')
            if not r.is_local_image
        ]
        self.stdout.write(f'{len(rows)} card(s) still hotlinking a picture')
        if options['dry_run']:
            for r in rows:
                self.stdout.write(f'  {r.url[:70]}')
            return

        copied = revived = failed = 0
        for row in rows:
            stored = store_thumbnail(row.image_url)
            if stored:
                row.image_url = stored
                row.save(update_fields=['image_url'])
                copied += 1
                self.stdout.write(f'  copied   {row.url[:64]}')
                continue

            if not options['re_resolve']:
                failed += 1
                self.stdout.write(f'  dead     {row.url[:64]}')
                continue

            # The picture is gone; the page may still hand us a new one.
            fresh = service.resolve_and_store(row.url)
            if fresh is not None and fresh.is_local_image:
                revived += 1
                self.stdout.write(f'  revived  {row.url[:64]}')
            else:
                failed += 1
                self.stdout.write(f'  lost     {row.url[:64]}')

        self.stdout.write(
            f'done: {copied} copied, {revived} revived by re-resolving, {failed} failed'
        )
