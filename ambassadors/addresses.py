"""One account per address, and what it costs to enforce that.

A paid referral programme's commonest fraud is one person signing up a dozen
accounts from their own phone or their own wifi. The plainest answer is to
refuse the second account that appears at an address a credited account is
already at — which is this.

It is a blunt instrument, deliberately, and worth knowing where it is blunt:

* **Carrier networks share addresses.** A Greek mobile carrier can put a whole
  city behind one address, so two genuine strangers can look identical from
  here. Under this rule the second of them is rejected.
* **So do households, schools and cafés.** Two flatmates joining from the same
  wifi is one rejection, and a class of students is many.

Because of that, a rejection here is recorded with its reason, and can be
turned back by hand on the review page like any other. Nothing is deleted and
nobody is blocked from using Neat — only the *payment* for that signup is
refused.
"""

import logging

from django.utils import timezone

logger = logging.getLogger(__name__)

#: Whether one address may be credited to different ambassadors. Global means
#: no: once an address has produced a credited account, the next account from
#: it is refused whoever brought it. Per-ambassador would let two ambassadors
#: each claim a person from the same café — likelier to be honest, and also
#: the shape two accomplices would use.
GLOBAL = True


def address_for(user, request_ip=''):
    """The best address known for an account, for crediting and for display."""
    from .models import SignupAddress

    if request_ip:
        return request_ip
    row = SignupAddress.objects.filter(user=user).values_list('ip_address', flat=True).first()
    return row or ''


def already_credited(ip_address, ambassador=None, exclude_user=None):
    """The account already credited at this address, if there is one.

    Rejected credits do not count: a signup somebody has already refused is
    not evidence against the next one.
    """
    from .models import AmbassadorSignup

    if not ip_address:
        return None
    others = (AmbassadorSignup.objects
              .filter(ip_address=ip_address)
              .exclude(status=AmbassadorSignup.REJECTED)
              .select_related('user', 'ambassador'))
    if exclude_user is not None:
        others = others.exclude(user=exclude_user)
    if not GLOBAL and ambassador is not None:
        others = others.filter(ambassador=ambassador)
    return others.order_by('created').first()


def verdict(ip_address, ambassador, user):
    """(status, flag) for a credit about to be written at this address."""
    from .models import AmbassadorSignup

    clash = already_credited(ip_address, ambassador, exclude_user=user)
    if clash is None:
        return None, ''
    return AmbassadorSignup.REJECTED, (
        f'rejected automatically: @{clash.user.username} was already credited '
        f'from this address ({ip_address}) on '
        f'{timezone.localtime(clash.created):%d %b %H:%M}. Shared wifi and '
        f'mobile networks look like this too — reverse it here if this one is real.'
    )


def purge_expired():
    """Drop addresses older than the retention window."""
    from .models import SignupAddress

    cutoff = timezone.now() - SignupAddress.RETENTION
    deleted, _ = SignupAddress.objects.filter(created__lt=cutoff).delete()
    return deleted
