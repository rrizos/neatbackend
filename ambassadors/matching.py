"""The fallback: pairing a new account with a recent click on the same network.

This exists because the two honest mechanisms cannot cover everyone. Google
Play hands the app its install referrer, and iOS hands it nothing at all — so
for an iPhone whose owner did not grant the clipboard read, the only remaining
evidence that they came from an ambassador's link is that they signed up from
the same address, minutes later.

That is evidence, not proof. Two strangers behind one carrier NAT look
identical from here. So nothing found this way is ever credited quietly:
it arrives flagged, with the distance in minutes written on it, for a person
to judge. The flag is the feature.
"""

import logging

from django.utils import timezone

logger = logging.getLogger(__name__)

#: How long after a click a signup can still plausibly be the same person.
#: Long enough to cover downloading over a slow connection and finishing later
#: the same evening; short enough that it is not simply "someone, once".
WINDOW = timezone.timedelta(hours=6)


def _recent_ambassador_click(user, ip):
    """The newest unspent ambassador click from this address, if any."""
    from .models import AmbassadorClick, ip_hash

    click = (
        AmbassadorClick.objects
        .filter(
            ip=ip_hash(ip),
            created__gte=timezone.now() - WINDOW,
            used_at__isnull=True,
            ambassador__is_active=True,
        )
        .select_related('ambassador')
        .order_by('-created')
        .first()
    )
    # An ambassador signing up on their own network is not a recruit.
    if click and click.ambassador.user_id and click.ambassador.user_id == user.id:
        return None
    return click


def _recent_invite_click(user, ip):
    """The newest unspent invite open from this address, if any."""
    from invites.models import InviteEvent

    from .models import ip_hash

    click = (
        InviteEvent.objects
        .filter(
            kind=InviteEvent.OPENED,
            ip_hash=ip_hash(ip),
            created__gte=timezone.now() - WINDOW,
            used_at__isnull=True,
        )
        .select_related('inviter')
        .order_by('-created')
        .first()
    )
    if click and click.inviter_id == user.id:
        return None
    return click


def _credit_ambassador(user, click):
    from . import addresses
    from .models import AmbassadorClick, AmbassadorSignup

    minutes = max(int((timezone.now() - click.created).total_seconds() // 60), 0)
    address = addresses.address_for(user) or click.ip_address
    auto_status, auto_flag = addresses.verdict(address, click.ambassador, user)
    flags = [
        'matched by network and time only, with no token: this account '
        f'signed up {minutes} minutes after a click from the same address. '
        'Shared mobile networks produce this too — check before approving.'
    ]
    if auto_flag:
        flags.insert(0, auto_flag)
    signup = AmbassadorSignup.objects.create(
        ambassador=click.ambassador,
        click=click,
        user=user,
        status=auto_status or AmbassadorSignup.FLAGGED,
        claim_method=AmbassadorSignup.NETWORK,
        ip_address=address,
        flags='\n'.join(flags),
    )
    AmbassadorClick.objects.filter(id=click.id).update(used_at=timezone.now())
    return signup


def _credit_invite(user, click):
    # Invites are not paid, so the one-account-per-address rule does not apply
    # to them: nothing is lost by counting two flatmates.
    from invites.models import InviteEvent

    event = InviteEvent.objects.create(
        kind=InviteEvent.JOINED,
        inviter=click.inviter,
        city=click.city,
        joined_user=user,
    )
    InviteEvent.objects.filter(id=click.id).update(used_at=timezone.now())
    return event


def match_on_network(user):
    """Pair a new account with whichever link was opened most recently from
    the same address — ambassador or invite.

    The two are not in competition, because only one of them pays. An invite
    is recorded whenever an invite link was opened from this address — it is a
    record of who is bringing people in, and nothing is spent on it. An
    ambassador credit is money, so it is withheld whenever an invite link was
    opened *more recently*: that is the likelier explanation of the signup,
    and the cost of guessing wrong is a payment for somebody a friend brought.

    An earlier version made them exclusive and tried ambassadors first. On a
    shared network that collapses: one stale ambassador click absorbed every
    signup from a whole school's wifi while the invite somebody actually
    followed a minute earlier counted for nobody.

    Best effort throughout: called from a post_save signal inside the signup
    transaction, so it must never raise. Nobody should fail to join Neat
    because a referral count broke.
    """
    from invites.models import InviteEvent

    from .middleware import current_ip
    from .models import AmbassadorSignup

    ip = current_ip()
    if not ip:
        return None

    ambassador_click = (
        None if AmbassadorSignup.objects.filter(user=user).exists()
        else _recent_ambassador_click(user, ip)
    )
    invite_click = (
        None if InviteEvent.objects.filter(joined_user=user).exists()
        else _recent_invite_click(user, ip)
    )

    # The ambassador credit is the one that costs money, so it is the one that
    # has to lose ties: if an invite link was opened from this address more
    # recently, that is the likelier explanation and nobody gets paid for it.
    if (ambassador_click and invite_click
            and invite_click.created > ambassador_click.created):
        ambassador_click = None

    result = None
    if ambassador_click is not None:
        result = _credit_ambassador(user, ambassador_click)
    # The invite is recorded either way, including alongside an ambassador
    # credit. It pays nobody — it is a record of who is bringing people in —
    # so there is nothing to protect by making the two exclusive, and making
    # them exclusive is precisely what left /invites reading zero.
    if invite_click is not None:
        invite = _credit_invite(user, invite_click)
        result = result or invite
    return result
