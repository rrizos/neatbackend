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
    platform = models.CharField(max_length=16, choices=PLATFORMS)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.user.username} ({self.platform})'
