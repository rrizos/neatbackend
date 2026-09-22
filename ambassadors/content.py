"""Crediting creators with the people who joined after their posts.

A creator logs a post, the city it targeted, and when it went up. Once
someone has verified the post is real, every account that joins that city
from then on is credited to them — alongside the people their link brought
in, not instead of them — until somebody presses Stop on the post in
/ambassadorsstats. There is no fixed window, because how long a video keeps
working is a judgement for the person watching the numbers, not a constant.

Read the limits before trusting the numbers this produces:

* **It is correlation.** Everyone who joins the city in the window counts:
  people a friend invited, people who found the app on their own, people who
  never saw the post. In a busy city that is most of them.
* **Links win.** A person who arrived by a creator's link, a Play referrer or
  the clipboard is already credited by evidence; a window never overrides
  that. The reverse does happen: a token arriving later replaces a window
  credit, the same way it replaces a network guess.
* **One credit per person.** When two posts' windows overlap, the most recent
  upload before the signup gets it — the post that was freshest in the feed.
* **Nothing here is payable.** A window credit enters the same ladder as every
  other — city, a post, two interactions with other people, still there on
  day three — and cannot be approved until it clears all four.
* **A forgotten post keeps counting.** Nothing closes a window but the button,
  so a post nobody stops credits every new account in its city indefinitely.
  The stats page shows how long each one has been running for that reason.
"""

import logging

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from . import addresses

logger = logging.getLogger(__name__)

#: How long after a post is stopped it is still scanned. A person can sign up
#: while a post is running and only choose their city days later, so a stopped
#: post can still gain people who joined before the button was pressed; this
#: bounds how long that stays possible. It also bounds how old a post a creator
#: may log in the first place.
LOOKBACK = timezone.timedelta(days=14)


def _candidates_for(post):
    """Accounts that joined this post's city during its window and are not yet
    credited to anybody."""
    from .models import AmbassadorSignup

    User = get_user_model()
    start = post.uploaded_at
    # Running posts reach to now; stopped ones end where they were stopped, so
    # nobody who joins after the button was pressed is ever credited to them.
    end = post.stopped_at or timezone.now()
    credited = AmbassadorSignup.objects.values_list('user_id', flat=True)
    users = (
        User.objects
        .filter(date_joined__gte=start, date_joined__lte=end,
                profile__city=post.city)
        .exclude(id__in=credited)
    )
    if post.ambassador.user_id:
        users = users.exclude(id=post.ambassador.user_id)
    return users


def match(ambassador=None):
    """Credit every uncredited account that fell inside a verified post's
    window. Safe to call as often as a page loads; it only ever adds.

    With [ambassador], only that creator's posts are considered — enough for
    their own dashboard, and cheaper than the whole table.
    """
    from .models import AmbassadorContent, AmbassadorSignup

    from django.db.models import Q

    now = timezone.now()
    posts = (
        AmbassadorContent.objects
        .filter(status=AmbassadorContent.APPROVED,
                uploaded_at__lte=now,
                ambassador__is_active=True)
        .filter(Q(stopped_at__isnull=True) | Q(stopped_at__gte=now - LOOKBACK))
        .select_related('ambassador')
    )
    if ambassador is not None:
        posts = posts.filter(ambassador=ambassador)

    # Resolve overlaps before writing anything: for each person, the post whose
    # upload is most recent without being after they joined.
    best = {}
    for post in posts:
        for user in _candidates_for(post):
            current = best.get(user.id)
            if current is None or post.uploaded_at > current[1].uploaded_at:
                best[user.id] = (user, post)

    created = 0
    for user, post in best.values():
        try:
            with transaction.atomic():
                # Re-checked inside the transaction: a link claim may have
                # landed between the scan and this write.
                if AmbassadorSignup.objects.filter(user=user).exists():
                    continue
                day_ago = now - timezone.timedelta(days=1)
                recent = AmbassadorSignup.objects.filter(
                    ambassador=post.ambassador, created__gte=day_ago).count()
                over_cap = recent >= post.ambassador.daily_cap

                # The address this account signed up from, recorded when it was
                # created — there is no request here to read one from.
                address = addresses.address_for(user)
                auto_status, auto_flag = addresses.verdict(address, post.ambassador, user)

                flags = []
                if auto_flag:
                    flags.append(auto_flag)
                if over_cap:
                    flags.append(f'over the daily cap of {post.ambassador.daily_cap}')

                AmbassadorSignup.objects.create(
                    ambassador=post.ambassador,
                    click=None,
                    content=post,
                    user=user,
                    claim_method=AmbassadorSignup.CONTENT,
                    status=auto_status or (AmbassadorSignup.FLAGGED if over_cap
                                           else AmbassadorSignup.PENDING),
                    ip_address=address,
                    flags='\n'.join(flags),
                )
                created += 1
        except Exception:
            # One bad row must not stop the rest being credited.
            logger.exception('content window credit failed for user %s', user.id)
    return created
