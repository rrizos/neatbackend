"""Write a file copy of every avatar that currently exists only as base64.

`Profile.avatar_url` holds the picture as a data URL, which is what made every
payload naming a person expensive — a feed row, a notification, a chat list
entry and a search result each carried the whole image again, and a data URL
cannot be cached by the client, cannot be fetched alongside the list that
mentions it, and gzips barely at all.

This writes the same picture to disk so newer clients can be handed a URL. The
base64 column is deliberately left untouched: builds released before this
decode data URLs directly and would draw initials for anything else.
"""

from django.core.management.base import BaseCommand

from accounts.avatars import store_thumb_avatar
from accounts.models import Profile


class Command(BaseCommand):
    help = 'Store a file copy of every base64 avatar.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        rows = [
            p for p in Profile.objects.exclude(avatar_url='')
            if p.avatar_url.startswith('data:') and not p.avatar_thumb_url
        ]
        inline = sum(len(p.avatar_url) for p in rows)
        self.stdout.write(
            f'{len(rows)} avatar(s) without a file copy, {inline/1024:.0f} KB of base64'
        )
        if options['dry_run']:
            return

        done = failed = 0
        for profile in rows:
            url = store_thumb_avatar(profile.avatar_url)
            if not url:
                failed += 1
                continue
            profile.avatar_thumb_url = url
            profile.save(update_fields=['avatar_thumb_url'])
            done += 1
        self.stdout.write(f'done: {done} stored, {failed} failed')
