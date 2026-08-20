from django.conf import settings
from django.db import models


class DeviceToken(models.Model):
    PLATFORMS = [
        ('ios', 'iOS'),
        ('android', 'Android'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='device_tokens',
    )
    token = models.CharField(max_length=255, unique=True)
    # A stable id for the install this token belongs to.
    #
    # FCM hands out a new token on reinstall (and sometimes on its own), and
    # nothing tied the new one to the old, so every reinstall left another live
    # row behind — one account here had four, and every notification was
    # delivered four times. Registration now clears any other token carrying
    # the same device id, so a phone can only ever have one.
    device_id = models.CharField(max_length=64, blank=True, default='', db_index=True)
    platform = models.CharField(max_length=16, choices=PLATFORMS)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.user.username} ({self.platform})'
