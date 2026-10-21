"""Whether a credited signup is a real user yet.

Four tests, chosen because each one costs a fraudster something different:

* **A city.** Finishing setup. The cheapest of the four, and on its own proof
  of nothing much.
* **A post.** Content, which needs a person with something to say.
* **Two interactions with other people.** A like, a follow, a comment — any
  two, on somebody else's account. This is the one that separates a person
  from a shell: an account can be filled in and made to post by one person
  working alone, but touching *other* people's content is behaviour, and a
  farm of accounts talking only to itself does not produce it.
* **Day three.** Still opening the app 72 hours later. The expensive one — a
  farm has to be *tended* for three days to pass it, which is the whole point
  of measuring retention rather than registrations.

All four, or it is not payable. And clearing them only moves a row to
`qualified`: a human still has to approve it. That separation is deliberate —
these rules should be able to be wrong without money moving.
"""

from django.utils import timezone

#: How long after signing up the account must still be in use.
RETENTION_DAYS = 3
#: How many posts count as "said something".
MIN_POSTS = 1
#: How many touches of other people's accounts count as "joined in". Two,
#: because one is an accident and two is a habit.
MIN_INTERACTIONS = 2


def measure(user):
    """The four signals for one account, as of right now."""
    from accounts.models import AppSession, Follow
    from posts.models import Post, PostComment, PostLike

    profile = getattr(user, 'profile', None)
    has_city = bool(getattr(profile, 'city', '') or '')
    post_count = Post.objects.filter(user=user).count()

    # Only what lands on somebody else: liking your own post, or the follow
    # the database already forbids, is not joining in. Counted rather than
    # merely detected, so a reviewer can see whether an account did the
    # minimum twice or is genuinely active.
    interactions = (
        PostLike.objects.filter(user=user).exclude(post__user=user).count()
        + PostComment.objects.filter(user=user).exclude(post__user=user).count()
        + Follow.objects.filter(follower=user).exclude(following=user).count()
    )

    # Deliberately "a session on or after day three", not "last_active is
    # three days old": last_active is a single overwritten timestamp, so an
    # account opened once and never again would pass it three days later.
    # A session row is evidence of the app being open at that moment.
    day3 = user.date_joined + timezone.timedelta(days=RETENTION_DAYS)
    retained = AppSession.objects.filter(user=user, last_seen__gte=day3).exists()

    return {
        'has_city': has_city,
        'post_count': post_count,
        'interaction_count': interactions,
        'retained_day3': retained,
        'qualifies': (
            has_city
            and post_count >= MIN_POSTS
            and interactions >= MIN_INTERACTIONS
            and retained
        ),
    }


def refresh(signup):
    """Re-measure one signup and move it along if it now qualifies.

    Never touches a row a human has settled, and never demotes: an account
    that qualified and then deleted its only post stays qualified, because the
    ambassador did their part and the decision was already made about them.
    """
    from .models import AmbassadorSignup

    if signup.status in AmbassadorSignup.FINAL:
        return signup

    m = measure(signup.user)
    signup.has_city = m['has_city']
    signup.post_count = m['post_count']
    signup.interaction_count = m['interaction_count']
    signup.retained_day3 = m['retained_day3']

    fields = ['has_city', 'post_count', 'interaction_count', 'retained_day3']
    # A flagged row still gets its measurements refreshed so a reviewer sees
    # the truth, but it does not climb the ladder on its own — that is what
    # being flagged means.
    if m['qualifies'] and signup.status == AmbassadorSignup.PENDING:
        signup.status = AmbassadorSignup.QUALIFIED
        signup.qualified_at = timezone.now()
        fields += ['status', 'qualified_at']

    signup.save(update_fields=fields)
    return signup


def refresh_all(queryset=None):
    """Re-measure everything still in play. Cheap at this scale; if it ever
    stops being cheap, this is the thing to move to a cron."""
    from .models import AmbassadorSignup

    if queryset is None:
        queryset = AmbassadorSignup.objects.exclude(status__in=AmbassadorSignup.FINAL)
    count = 0
    for signup in queryset.select_related('user', 'user__profile'):
        refresh(signup)
        count += 1
    return count
