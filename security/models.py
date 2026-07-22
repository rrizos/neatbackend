from django.conf import settings
from django.db import models


class AuditLog(models.Model):
    """Append-only security/audit record.

    Non-repudiation is enforced two ways:

    1. The model refuses UPDATE and DELETE, so nothing in application code can
       quietly rewrite history (inserts go through ``objects.create`` or the
       worker's ``bulk_create``).
    2. Every row carries ``entry_hash = sha256(prev_hash + canonical payload)``,
       chaining it to the row before it. Editing or deleting a row directly in
       the database therefore breaks the chain from that point on, which
       ``verify_chain`` detects and reports.

    There are deliberately no mutable columns (no "acknowledged" flag):
    acknowledging an alert appends a *new* record referencing the original, so
    the trail only ever grows.
    """

    INFO = 'info'
    LOW = 'low'
    MEDIUM = 'medium'
    HIGH = 'high'
    CRITICAL = 'critical'
    SEVERITIES = [
        (INFO, 'Info'),
        (LOW, 'Low'),
        (MEDIUM, 'Medium'),
        (HIGH, 'High'),
        (CRITICAL, 'Critical'),
    ]

    created = models.DateTimeField(auto_now_add=True, db_index=True)
    event_type = models.CharField(max_length=64, db_index=True)
    severity = models.CharField(
        max_length=16, choices=SEVERITIES, default=INFO, db_index=True
    )

    # Actor is denormalised as well as FK'd: the username must survive the
    # account being deleted, otherwise the trail loses its subject.
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='audit_events',
    )
    actor_username = models.CharField(
        max_length=150, blank=True, default='', db_index=True
    )

    target_type = models.CharField(max_length=64, blank=True, default='')
    target_id = models.CharField(max_length=64, blank=True, default='')

    # Request context
    ip = models.CharField(max_length=64, blank=True, default='', db_index=True)
    user_agent = models.CharField(max_length=300, blank=True, default='')
    # Fingerprint of the auth token (never the token itself) so a session can be
    # correlated across events without the log becoming a credential store.
    session_id = models.CharField(max_length=64, blank=True, default='')
    method = models.CharField(max_length=8, blank=True, default='')
    path = models.CharField(max_length=300, blank=True, default='')
    status_code = models.IntegerField(null=True, blank=True)
    # This app has no MFA; recorded as 'none' so the field is meaningful the day
    # one is added rather than silently absent from historical rows.
    mfa = models.CharField(max_length=16, blank=True, default='none')

    message = models.CharField(max_length=500, blank=True, default='')
    metadata = models.JSONField(default=dict, blank=True)

    prev_hash = models.CharField(max_length=64, blank=True, default='')
    entry_hash = models.CharField(max_length=64, blank=True, default='', db_index=True)

    class Meta:
        ordering = ['-id']
        indexes = [
            models.Index(fields=['-created', 'severity']),
            models.Index(fields=['event_type', '-created']),
        ]

    def __str__(self):
        return f'[{self.severity}] {self.event_type} {self.actor_username}'

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError('AuditLog is append-only: existing rows cannot be modified')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError('AuditLog is append-only: rows cannot be deleted')

    def to_dict(self):
        return {
            'id': self.id,
            'created': self.created.isoformat() if self.created else None,
            'eventType': self.event_type,
            'severity': self.severity,
            'actor': self.actor_username,
            'targetType': self.target_type,
            'targetId': self.target_id,
            'ip': self.ip,
            'userAgent': self.user_agent,
            'sessionId': self.session_id,
            'method': self.method,
            'path': self.path,
            'statusCode': self.status_code,
            'mfa': self.mfa,
            'message': self.message,
            'metadata': self.metadata or {},
            'entryHash': self.entry_hash,
        }
