"""
One-time broadcast push for the Ελλάδα (Greece) national feed launch.

Usage
-----
Send to everyone:
    python manage.py broadcast_greece_launch

Test on a single user first:
    python manage.py broadcast_greece_launch --username jim

Dry run (print who would receive it, send nothing):
    python manage.py broadcast_greece_launch --dry-run
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from push.models import DeviceToken
from push.senders import _send_to_user

User = get_user_model()

TITLE = "Νέο: Feed Ελλάδας 🇬🇷"
BODY  = "Μπορείς τώρα να δημοσιεύεις στο εθνικό feed της Ελλάδας και να βλέπεις posts από όλες τις πόλεις!"


class Command(BaseCommand):
    help = "Send a one-time push notification announcing the Greece national feed."

    def add_arguments(self, parser):
        parser.add_argument(
            '--username',
            type=str,
            default=None,
            help='Send only to this username (for testing).',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            default=False,
            help='Print recipients without sending anything.',
        )

    def handle(self, *args, **options):
        username = options['username']
        dry_run  = options['dry_run']

        # Find users who have at least one registered device token.
        user_ids_with_token = (
            DeviceToken.objects
            .values_list('user_id', flat=True)
            .distinct()
        )
        qs = User.objects.filter(id__in=user_ids_with_token)
        if username:
            qs = qs.filter(username=username)

        users = list(qs)
        self.stdout.write(f"Recipients: {len(users)}")

        if dry_run:
            for u in users:
                self.stdout.write(f"  {u.username}")
            self.stdout.write("Dry run — nothing sent.")
            return

        sent = 0
        failed = 0
        for user in users:
            try:
                _send_to_user(
                    user,
                    title=TITLE,
                    body=BODY,
                    data={'type': 'greece_launch'},
                    silent=False,
                )
                sent += 1
                if sent % 100 == 0:
                    self.stdout.write(f"  {sent}/{len(users)} sent…")
            except Exception as exc:
                failed += 1
                self.stderr.write(f"  Failed for {user.username}: {exc}")

        self.stdout.write(self.style.SUCCESS(
            f"Done. Sent: {sent}, failed: {failed}."
        ))
