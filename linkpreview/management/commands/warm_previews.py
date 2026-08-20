"""Resolve links that appear in real content but have no card yet.

Previews are resolved on demand: the feed only ever *reads* the cache, and the
client asks for anything missing when the post scrolls into view. That is right
for a feed — a list must never wait on outbound fetches — but it means a link
nobody has opened recently has no card, and the first person to see it gets a
blank space while the fetch happens behind them.

This walks recent posts and messages and resolves anything not already cached,
so the cards are there before anyone looks. Worth running after clearing bad
rows, and worth running before a launch so the first arrivals don't each pay
for a different link.
"""

from django.core.management.base import BaseCommand

from dm_messages.models import Message
from linkpreview import service
from linkpreview.models import LinkPreview, url_fingerprint
from posts.models import Post


class Command(BaseCommand):
    help = 'Resolve link previews for recent posts and messages.'

    def add_arguments(self, parser):
        parser.add_argument('--posts', type=int, default=300)
        parser.add_argument('--messages', type=int, default=300)
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        texts = []
        texts += [p.text or '' for p in Post.objects.order_by('-created')[:options['posts']]]
        texts += [m.text or '' for m in Message.objects.order_by('-id')[:options['messages']]]

        wanted = {}
        for text in texts:
            url = service.first_url(text)
            if url:
                wanted[service.normalise_url(url)] = True

        missing = [
            u for u in wanted
            if not LinkPreview.objects.filter(url_hash=url_fingerprint(u)).exists()
        ]
        self.stdout.write(f'{len(wanted)} distinct link(s), {len(missing)} without a card')
        if options['dry_run']:
            for u in missing:
                self.stdout.write(f'  {u[:76]}')
            return

        ok = failed = 0
        for url in missing:
            row = service.resolve_and_store(url)
            if row is None:
                failed += 1
                self.stdout.write(f'  no card  {url[:64]}')
                continue
            ok += 1
            self.stdout.write(
                f'  resolved {url[:52]}  '
                f'title={"yes" if row.title else "NO"} '
                f'image={"ours" if row.is_local_image else ("none" if not row.image_url else "remote")}'
            )
        self.stdout.write(f'done: {ok} resolved, {failed} with no card')
