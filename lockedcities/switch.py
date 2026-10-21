"""Turning the locked-cities feature off, and putting it back.

Off means every city reports `locked: false` to every client, including the
copies already on people's phones. It is not a settings flag and not a code
change, because neither reaches an installed app: the app asks the server
which cities are locked, so the server simply has to stop saying any are.

Two things make this exactly reversible:

* the lock state of every existing city is written down before anything moves
* rows the switch had to invent are remembered, and deleted on the way back

Nothing here touches LOCKED_CITIES_ENABLED. Setting that to False empties the
API response, and an empty response means *locked* on the phone — see
cities.py. It is a trap, not a switch.
"""

import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .cities import APP_CITIES
from .models import LockedCitiesState

logger = logging.getLogger(__name__)


def _threshold_for(name):
    thresholds = getattr(settings, 'LOCKED_CITIES_THRESHOLDS', {}) or {}
    return thresholds.get(name, getattr(settings, 'LOCKED_CITIES_DEFAULT_THRESHOLD', 100))


@transaction.atomic
def turn_off(admin=None):
    """Open every city, everywhere, now."""
    from posts.models import CityConfig

    state = LockedCitiesState.get()
    if not state.enabled:
        return state

    state.snapshot = {
        c.name: c.is_locked for c in CityConfig.objects.all()
    }

    # A city the app knows but the server has never heard of is locked on the
    # phone by default, so silence is not good enough — each one needs a row
    # that says otherwise.
    created = []
    for name in APP_CITIES:
        _, was_created = CityConfig.objects.get_or_create(
            name=name,
            defaults={
                'threshold': _threshold_for(name),
                'member_count_cache': 0,
                'is_locked': False,
            },
        )
        if was_created:
            created.append(name)
    state.created = created

    CityConfig.objects.update(is_locked=False)

    state.enabled = False
    state.changed_by = admin
    state.save()
    logger.warning('locked cities switched OFF by %s', getattr(admin, 'username', 'unknown'))
    return state


@transaction.atomic
def turn_on(admin=None):
    """Put every city back exactly as it was when the switch was thrown."""
    from posts.models import CityConfig

    state = LockedCitiesState.get()
    if state.enabled:
        return state

    # Rows that only exist because the switch created them go away again, so
    # the app falls back to its own default for those cities rather than
    # inheriting a row nobody chose.
    invented = state.created
    if invented:
        CityConfig.objects.filter(name__in=invented).delete()

    for name, was_locked in state.snapshot.items():
        CityConfig.objects.filter(name=name).update(is_locked=was_locked)

    state.enabled = True
    state.snapshot = {}
    state.created = []
    state.changed_by = admin
    state.save()
    logger.warning('locked cities switched back ON by %s', getattr(admin, 'username', 'unknown'))
    return state


def status():
    """What the page needs to show, and what a check would want to assert."""
    from posts.models import CityConfig

    state = LockedCitiesState.get()
    rows = CityConfig.objects.all()
    return {
        'state': state,
        'enabled': state.enabled,
        'rows': rows.count(),
        'locked': rows.filter(is_locked=True).count(),
        'open': rows.filter(is_locked=False).count(),
        'app_cities': len(APP_CITIES),
        'missing': sorted(set(APP_CITIES) - set(rows.values_list('name', flat=True))),
        'changed_at': state.changed_at if state.pk else None,
        'changed_by': getattr(state.changed_by, 'username', ''),
        'now': timezone.now(),
    }
