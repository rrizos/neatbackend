"""Apply a retention window to the security audit trail.

AuditLog rows carry an IP address and a user agent, which are personal data.
The model is deliberately append-only — instance .save()/.delete() both raise —
so nothing in the request path can quietly rewrite history. That is right for
tamper-evidence and wrong for GDPR storage limitation on its own: without this
job the IPs would be kept forever, and the Privacy Policy could not state a
retention period.

Rows are removed whole rather than having their IP blanked. Each row's
entry_hash covers its own body and its stored prev_hash, so a row verifies
independently (see security/audit.py); dropping the oldest rows leaves every
surviving row verifiable, whereas rewriting a field would not.

Run daily, e.g.:
    0 4 * * *  cd /srv/neat && python manage.py purge_audit_logs
"""

import datetime

from django.core.management.base import BaseCommand
from django.utils import timezone

from security.models import AuditLog

DEFAULT_RETENTION_DAYS = 365


class Command(BaseCommand):
    help = 'Delete security audit rows older than the retention window.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=DEFAULT_RETENTION_DAYS,
            help=f'Retention window in days (default: {DEFAULT_RETENTION_DAYS}).',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Report what would be deleted without deleting it.',
        )

    def handle(self, *args, **options):
        days = options['days']
        if days < 1:
            self.stderr.write('--days must be at least 1')
            return

        cutoff = timezone.now() - datetime.timedelta(days=days)
        # Queryset .delete() is a bulk operation and does not call the model's
        # instance delete(), which is the only reason this can run at all.
        stale = AuditLog.objects.filter(created__lt=cutoff)
        count = stale.count()

        if options['dry_run']:
            self.stdout.write(
                f'{count} audit rows older than {cutoff.isoformat()} would be deleted'
            )
            return

        if count:
            stale.delete()
        self.stdout.write(
            self.style.SUCCESS(
                f'Deleted {count} audit rows older than {days} days '
                f'(cutoff {cutoff.isoformat()})'
            )
        )
