"""The state of the locked-cities kill switch.

One row, ever. It holds enough to put the world back exactly as it was: what
every city's lock state was at the moment the switch was thrown, and which
rows the switch itself had to invent.
"""

import json

from django.conf import settings
from django.db import models


class LockedCitiesState(models.Model):
    #: True when the feature is running normally.
    enabled = models.BooleanField(default=True)
    #: {city name: was_locked} captured the moment the switch was thrown.
    #: TextField rather than JSONField purely to avoid depending on the
    #: database's JSON support for something this small.
    snapshot_json = models.TextField(blank=True, default='{}')
    #: Cities that had no row at all until the switch created one. Restoring
    #: deletes them again, so the app returns to treating them as locked by
    #: default — which is what it did before.
    created_json = models.TextField(blank=True, default='[]')
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+',
    )
    changed_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Locked cities switch'

    @classmethod
    def get(cls):
        state, _ = cls.objects.get_or_create(pk=1)
        return state

    @property
    def snapshot(self):
        try:
            return json.loads(self.snapshot_json or '{}')
        except ValueError:
            return {}

    @snapshot.setter
    def snapshot(self, value):
        self.snapshot_json = json.dumps(value, ensure_ascii=False)

    @property
    def created(self):
        try:
            return json.loads(self.created_json or '[]')
        except ValueError:
            return []

    @created.setter
    def created(self, value):
        self.created_json = json.dumps(value, ensure_ascii=False)

    def __str__(self):
        return f'locked cities: {"on" if self.enabled else "OFF"}'
