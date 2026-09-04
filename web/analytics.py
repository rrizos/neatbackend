"""The numbers behind /analytics.

Three kinds of metric live here, and the page says which is which:

**Measured.** Signups, activity, content, cities, the social graph — derived
from timestamps the database has kept since the beginning, so their history is
real.

**Only from now on.** Session length, sessions per person and the power-user
curve need to know when somebody opened the app. Nothing recorded that until
`AppSession` existed, so they start the day tracking did and cannot be
reconstructed backwards.

**Proxied.** `Profile.last_active` is a single timestamp that gets overwritten,
so it can answer "seen since when" but never "how often". Retention derived
from it undercounts (it cannot see a same-day return) and is labelled as an
estimate wherever it appears. Once there are enough sessions, the real cohort
table below it becomes the answer.

Benchmarks quoted on the page come from published cross-industry bands for
consumer-social apps — D1 >40%, D7 >20%, D30 >10% is top-decile; the broad
middle sits nearer D1 25%, D7 10%, D30 5%. They are direction, not a target:
your own cohorts always beat a published figure for deciding anything.
"""

from collections import Counter
from contextvars import ContextVar
from datetime import datetime

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Avg, Count, Min, Q
from django.db.models.functions import TruncDate
from django.utils import timezone

from accounts.models import (
    AppSession, Follow, Notification, Profile, SocialAccount,
)
from dm_messages.models import Conversation, Message
from events.models import Event
from posts.models import Post, PostComment, PostLike
from push.models import DeviceToken

User = get_user_model()

#: Published bands for consumer-social apps. Shown as context, never as a goal.
BENCHMARKS = {'d1': 40, 'd7': 20, 'd30': 10, 'stickiness': 20}


# ── Launch scope ────────────────────────────────────────────────────────────
#
# Everything below is scoped to the launch by default. The accounts and posts
# that predate it are the WordPress import and our own testing, and they do not
# behave like real users: they never opened the app, so they sink D1/D7/D30 and
# every step of the activation funnel while adding nothing true. A launch is
# judged on those two numbers, so the page must not quietly poison them.
#
# The scope is a contextvar rather than an argument threaded through twenty-odd
# functions — the same shape accounts/client_version.py already uses, and for
# the same reason. `?all=1` on the page turns it off when the whole history is
# genuinely what you want.

_launch_scoped = ContextVar('neat_analytics_launch_scoped', default=True)


def launch_date():
    """Configured launch, as an aware datetime at local midnight. A value that
    cannot be parsed disables scoping rather than raising: a broken date should
    cost you a filter, not the whole page."""
    raw = (getattr(settings, 'NEAT_LAUNCH_DATE', '') or '').strip()
    if not raw:
        return None
    try:
        naive = datetime.strptime(raw, '%Y-%m-%d')
    except ValueError:
        return None
    return timezone.make_aware(naive, timezone.get_current_timezone())


def set_launch_scoped(value):
    _launch_scoped.set(bool(value))


def _cutoff():
    return launch_date() if _launch_scoped.get() else None


def _scoped(qs, field):
    cut = _cutoff()
    return qs.filter(**{f'{field}__gte': cut}) if cut else qs


# One helper per model, so no call site has to remember which timestamp marks
# an account or a row as belonging to the launch.
def _users():          return _scoped(User.objects.all(), 'date_joined')
def _profiles():       return _scoped(Profile.objects.all(), 'user__date_joined')
def _sessions():       return _scoped(AppSession.objects.all(), 'started')
def _follows():        return _scoped(Follow.objects.all(), 'created')
def _notifications():  return _scoped(Notification.objects.all(), 'created')
def _socials():        return _scoped(SocialAccount.objects.all(), 'created')
def _conversations():  return _scoped(Conversation.objects.all(), 'created')
def _messages():       return _scoped(Message.objects.all(), 'created')
def _events():         return _scoped(Event.objects.all(), 'created')
def _posts():          return _scoped(Post.objects.all(), 'created')
def _comments():       return _scoped(PostComment.objects.all(), 'created')
def _likes():          return _scoped(PostLike.objects.all(), 'created')


def scope_summary():
    """What the page is showing, and what it is leaving out — stated on the
    page itself, because a filtered number that looks unfiltered is worse than
    no number."""
    cut = _cutoff()
    if cut is None:
        return {'scoped': False, 'launch': launch_date(),
                'excluded_users': 0, 'excluded_posts': 0}
    return {
        'scoped': True,
        'launch': cut,
        'live': timezone.now() >= cut,
        'excluded_users': User.objects.filter(date_joined__lt=cut).count(),
        'excluded_posts': Post.objects.filter(created__lt=cut).count(),
    }


def _since(days):
    return timezone.now() - timezone.timedelta(days=days)


def _pct(part, whole, places=1):
    return round(part / whole * 100, places) if whole else 0.0


# ── Headline ────────────────────────────────────────────────────────────────

def headline():
    total = _users().count()
    return {
        'total_users': total,
        'new_today': _users().filter(date_joined__gte=_since(1)).count(),
        'new_7d': _users().filter(date_joined__gte=_since(7)).count(),
        'new_30d': _users().filter(date_joined__gte=_since(30)).count(),
        'dau': _profiles().filter(last_active__gte=_since(1)).count(),
        'wau': _profiles().filter(last_active__gte=_since(7)).count(),
        'mau': _profiles().filter(last_active__gte=_since(30)).count(),
        'posts': _posts().count(),
        'comments': _comments().count(),
        'messages': _messages().count(),
        'events': _events().count(),
        'first_signup': _users().aggregate(d=Min('date_joined'))['d'],
        'now': timezone.now(),
    }


def growth(head):
    """Week-on-week, because a raw signup count says nothing on its own."""
    this_week = head['new_7d']
    last_week = _users().filter(
        date_joined__gte=_since(14), date_joined__lt=_since(7)
    ).count()
    return {
        'this_week': this_week,
        'last_week': last_week,
        'change': _pct(this_week - last_week, last_week) if last_week else None,
        'stickiness': _pct(head['dau'], head['mau']),
        # The share of everyone who ever registered that still shows up.
        # Total signups flatter; this is the number that is actually true.
        'mau_share': _pct(head['mau'], head['total_users']),
        'stickiness_target': BENCHMARKS['stickiness'],
    }


# ── Activation: the funnel that says where people fall out ──────────────────

def activation():
    """What share of signups ever reach each step.

    The most useful thing on this page. A social app dies at whichever of these
    steps people stop at — an account with no city sees an empty feed, one
    following nobody has nothing to read, and one that never posted has given
    the place no reason to keep them.
    """
    total = _users().count()
    if not total:
        return {'total': 0, 'steps': []}

    with_city = _profiles().exclude(city='').count()
    with_avatar = _profiles().exclude(
        Q(avatar_url='') & Q(avatar_thumb_url='')
    ).count()
    following_someone = _follows().values('follower_id').distinct().count()
    posted = _posts().exclude(user=None).values('user_id').distinct().count()
    messaged = _messages().values('sender_id').distinct().count()

    # Deliberately *not* a funnel. These are independent milestones — somebody
    # can post without ever following anyone — so nesting them would invent a
    # sequence that does not exist and produce negative drop-offs. Ordered by
    # how many people reach them, which is the thing worth seeing.
    steps = sorted(
        [
            ('Signed up', total),
            ('Chose a city', with_city),
            ('Added a photo', with_avatar),
            ('Followed someone', following_someone),
            ('Sent a message', messaged),
            ('Posted', posted),
        ],
        key=lambda s: -s[1],
    )
    out = []
    previous = None
    for label, n in steps:
        out.append({
            'label': label,
            'n': n,
            'of_total': _pct(n, total),
            # Fall from the next-most-reached milestone: the steepest one is
            # where the most people stop short.
            'drop': _pct(previous - n, previous) if previous else 0.0,
        })
        previous = n
    return {'total': total, 'steps': out}


def time_to_first_post():
    """How long a new account takes to post, if it ever does.

    Fast is healthy: the longer the gap, the more likely the answer is never.
    """
    rows = (
        _posts().exclude(user=None)
        .values('user_id')
        .annotate(first=Min('created'))
    )
    joined = dict(_users().values_list('id', 'date_joined'))
    hours = []
    for row in rows:
        start = joined.get(row['user_id'])
        if start and row['first']:
            delta = (row['first'] - start).total_seconds() / 3600
            if delta >= 0:
                hours.append(delta)
    if not hours:
        return None
    hours.sort()
    return {
        'median_hours': round(hours[len(hours) // 2], 1),
        'within_24h': _pct(sum(1 for h in hours if h <= 24), len(hours)),
        'people': len(hours),
    }


# ── Retention ───────────────────────────────────────────────────────────────

def retention_estimate():
    """D1/D7/D30 from the only history there is.

    An account counts as retained at D-n if it was still being seen n days
    after it was created. `last_active` is one overwritten timestamp, so this
    can only ask "was the last sighting at least this far after signup" — which
    is a floor, not the real number. Cohorts from real sessions replace it.
    """
    rows = list(
        _profiles().exclude(last_active=None)
        .values_list('user__date_joined', 'last_active')
    )
    if not rows:
        return None
    out = {'checked': len(rows)}
    for label, days in (('d1', 1), ('d7', 7), ('d30', 30)):
        eligible = [
            (joined, seen) for joined, seen in rows
            if joined and joined <= timezone.now() - timezone.timedelta(days=days)
        ]
        if not eligible:
            out[label] = None
            continue
        kept = sum(
            1 for joined, seen in eligible
            if seen >= joined + timezone.timedelta(days=days)
        )
        out[label] = _pct(kept, len(eligible))
        out[f'{label}_of'] = len(eligible)
    out['benchmarks'] = BENCHMARKS
    return out


def retention_cohorts(weeks=6):
    """True retention, for the signups that session tracking has covered.

    Unlike the estimate above this counts an actual return visit, so it is the
    number to trust — but it can only speak for people who joined after
    tracking began, which is why it starts nearly empty and fills in weekly.
    """
    first_session = _sessions().aggregate(d=Min('started'))['d']
    if not first_session:
        return None

    rows = []
    now = timezone.now()
    for week in range(weeks):
        start = now - timezone.timedelta(days=7 * (week + 1))
        end = now - timezone.timedelta(days=7 * week)
        if end < first_session:
            continue
        cohort = list(
            _users().filter(date_joined__gte=max(start, first_session),
                                date_joined__lt=end)
            .values_list('id', 'date_joined')
        )
        if not cohort:
            continue
        ids = [c[0] for c in cohort]
        joined_at = dict(cohort)
        seen = {}
        for user_id, started in _sessions().filter(
            user_id__in=ids
        ).values_list('user_id', 'started'):
            seen.setdefault(user_id, []).append(started)

        entry = {'label': f'{start:%d %b}', 'size': len(cohort)}
        for label, day in (('d1', 1), ('d7', 7), ('d30', 30)):
            # Only ask about a day the cohort has actually lived through.
            mature = [
                uid for uid in ids
                if joined_at[uid] + timezone.timedelta(days=day) <= now
            ]
            if not mature:
                entry[label] = None
                continue
            kept = sum(
                1 for uid in mature
                if any(s >= joined_at[uid] + timezone.timedelta(days=day)
                       for s in seen.get(uid, []))
            )
            entry[label] = _pct(kept, len(mature))
        rows.append(entry)
    return {'rows': rows, 'since': first_session} if rows else None


def dormant():
    """Accounts that have gone quiet — the churn nobody notices."""
    total = _users().count()
    never = _profiles().filter(last_active=None).count()
    gone_30 = _profiles().filter(last_active__lt=_since(30)).count()
    gone_90 = _profiles().filter(last_active__lt=_since(90)).count()
    return {
        'never_seen': never,
        'never_seen_pct': _pct(never, total),
        'quiet_30d': gone_30,
        'quiet_30d_pct': _pct(gone_30, total),
        'quiet_90d': gone_90,
        'quiet_90d_pct': _pct(gone_90, total),
    }


def power_user_curve(days=28):
    """How many days out of the last 28 each active person showed up.

    a16z's L28. A healthy social app's histogram "smiles" — a bump at 1-2 days
    and another at 25-28, because the people who stay come nearly every day. A
    curve that only falls away to the right is an app nobody has a habit for.
    Needs real sessions; empty until tracking has run long enough.
    """
    if not _sessions().exists():
        return None
    rows = _sessions().filter(started__gte=_since(days)).values_list(
        'user_id', 'started'
    )
    per_user = {}
    for user_id, started in rows:
        per_user.setdefault(user_id, set()).add(started.date())
    if not per_user:
        return None
    histogram = Counter(len(v) for v in per_user.values())
    buckets = [{'days': d, 'n': histogram.get(d, 0)} for d in range(1, days + 1)]
    peak = max([b['n'] for b in buckets] + [1])
    return {
        'buckets': buckets,
        'peak': peak,
        'people': len(per_user),
        'heavy': sum(n for d, n in histogram.items() if d >= days * 0.75),
    }


def session_metrics():
    total = _sessions().count()
    if not total:
        return {'tracking': False}
    durations = sorted(
        s.duration_seconds for s in _sessions().only('started', 'last_seen')
    )
    durations = [d for d in durations if d > 0]
    people = _sessions().values('user_id').distinct().count()
    return {
        'tracking': True,
        'since': _sessions().aggregate(d=Min('started'))['d'],
        'sessions': total,
        'sessions_7d': _sessions().filter(started__gte=_since(7)).count(),
        'avg_seconds': int(sum(durations) / len(durations)) if durations else 0,
        'median_seconds': durations[len(durations) // 2] if durations else 0,
        'people': people,
        'per_user': round(total / people, 1) if people else 0,
    }


# ── Content and the graph ───────────────────────────────────────────────────

def creator_split():
    """The 1-9-90 shape: who makes, who reacts, who only reads.

    Lurking is normal and not a problem in itself — but if almost nobody
    creates, there is nothing for the rest to come back for.
    """
    total = _users().count()
    creators = _posts().exclude(user=None).values('user_id').distinct().count()
    reactors = set(_likes().values_list('user_id', flat=True)) | set(
        _comments().values_list('user_id', flat=True)
    )
    reactor_only = len(reactors - set(
        _posts().exclude(user=None).values_list('user_id', flat=True)
    ))
    lurkers = total - creators - reactor_only
    return {
        'creators': creators, 'creators_pct': _pct(creators, total),
        'reactors': reactor_only, 'reactors_pct': _pct(reactor_only, total),
        'lurkers': max(0, lurkers), 'lurkers_pct': _pct(max(0, lurkers), total),
    }


def graph_health():
    """Whether people are connected to anyone.

    An account following nobody opens an empty feed, and an empty feed is the
    single most reliable predictor that somebody will not come back.
    """
    total = _users().count()
    following = _follows().values('follower_id').distinct().count()
    followed = _follows().values('following_id').distinct().count()
    pairs = _follows().count()
    return {
        'isolated': total - following,
        'isolated_pct': _pct(total - following, total),
        'nobody_follows_them': total - followed,
        'nobody_follows_them_pct': _pct(total - followed, total),
        'avg_following': round(pairs / total, 1) if total else 0,
        'follows_7d': _follows().filter(created__gte=_since(7)).count(),
    }


def content_health():
    total = _users().count()
    posters_30d = _posts().filter(created__gte=_since(30)).values('user_id').distinct().count()
    posts = _posts().count()
    engaged = _posts().annotate(
        likes_n=Count('like_rows', distinct=True),
        comments_n=Count('comment_rows', distinct=True),
    ).filter(Q(likes_n__gt=0) | Q(comments_n__gt=0)).count()
    return {
        'posters_30d': posters_30d,
        'posting_share': _pct(posters_30d, total),
        'posts_7d': _posts().filter(created__gte=_since(7)).count(),
        'comments_7d': _comments().filter(created__gte=_since(7)).count(),
        'messages_7d': _messages().filter(created__gte=_since(7)).count(),
        'likes_7d': _likes().filter(created__gte=_since(7)).count(),
        # A post nobody touched is a post that discouraged its author.
        'no_engagement': posts - engaged,
        'no_engagement_pct': _pct(posts - engaged, posts),
        'avg_likes': round(_likes().count() / posts, 1) if posts else 0,
        'avg_comments': round(_comments().count() / posts, 1) if posts else 0,
        'conversations': _conversations().count(),
        'unread_notifications': _notifications().filter(is_read=False).count(),
    }


# ── How people get in ───────────────────────────────────────────────────────

#: A provider identity recorded this close to the account's creation was the
#: thing that created it. Anything later is somebody linking a second way in to
#: an account they already had.
_SIGNUP_WINDOW = timezone.timedelta(minutes=2)


def signup_methods():
    """Which route people actually take to create an account.

    Worth watching rather than assuming: a provider button that nobody presses
    is dead weight on the busiest screen in the app, and one that everybody
    presses means the email form is the thing to stop maintaining. The split
    also decides how much the lockout risk below matters.
    """
    joined = dict(_users().values_list('id', 'date_joined'))
    total = len(joined)
    if not total:
        return None

    at_signup = {}          # user_id -> provider that created the account
    linked_later = 0
    for user_id, provider, created in _socials().values_list(
        'user_id', 'provider', 'created'
    ):
        start = joined.get(user_id)
        if start is None:
            continue
        if created - start <= _SIGNUP_WINDOW:
            at_signup[user_id] = provider
        else:
            linked_later += 1

    counts = Counter(at_signup.values())
    email = total - len(at_signup)

    rows = [
        ('Email', email),
        ('Google', counts.get(SocialAccount.GOOGLE, 0)),
        ('Apple', counts.get(SocialAccount.APPLE, 0)),
    ]

    # The same split for recent signups only, so a method added partway through
    # is not judged against every account that predates it.
    recent_cut = _since(30)
    recent_ids = {uid for uid, when in joined.items() if when >= recent_cut}
    recent_total = len(recent_ids)
    recent_counts = Counter(
        p for uid, p in at_signup.items() if uid in recent_ids
    )
    recent = [
        ('Email', recent_total - sum(
            1 for uid in at_signup if uid in recent_ids)),
        ('Google', recent_counts.get(SocialAccount.GOOGLE, 0)),
        ('Apple', recent_counts.get(SocialAccount.APPLE, 0)),
    ]

    # Somebody whose only way in is a provider loses the account with it, so
    # this is the number that says how exposed the base is.
    provider_only = _users().filter(
        id__in=list(at_signup), password__startswith='!'
    ).count()

    return {
        'total': total,
        'rows': [
            {'label': label, 'n': n, 'pct': _pct(n, total)}
            for label, n in rows
        ],
        'recent': [
            {'label': label, 'n': n, 'pct': _pct(n, recent_total)}
            for label, n in recent
        ],
        'recent_total': recent_total,
        'social_total': len(at_signup),
        'social_pct': _pct(len(at_signup), total),
        'linked_later': linked_later,
        'provider_only': provider_only,
        'provider_only_pct': _pct(provider_only, len(at_signup)) if at_signup else 0.0,
    }


def push_reach():
    """What share of signups the app can still reach.

    Push is the only channel that brings somebody back who has not thought
    about the app today, so this quietly sets the ceiling on every retention
    number below. A user who declined the permission is not lost, but they can
    only return on their own initiative, and most do not.
    """
    total = _users().count()
    if not total:
        return {'total': 0, 'reachable': 0, 'pct': 0.0, 'platforms': []}

    rows = DeviceToken.objects.filter(user__in=_users()).values_list('user_id', 'platform')
    by_user = {}
    for user_id, platform in rows:
        by_user.setdefault(user_id, set()).add(platform or 'unknown')

    counts = Counter(p for platforms in by_user.values() for p in platforms)
    reachable = len(by_user)
    return {
        'total': total,
        'reachable': reachable,
        'pct': _pct(reachable, total),
        'unreachable': total - reachable,
        'platforms': sorted(counts.items(), key=lambda kv: -kv[1]),
    }


# ── Cities ──────────────────────────────────────────────────────────────────

def city_breakdown(limit=15):
    """People, activity and content per city, side by side.

    A city with users and no posts is a room full of people staring at a blank
    wall — it needs seeding, not more signups.
    """
    counts = {
        r['city']: r['n'] for r in
        _profiles().exclude(city='').values('city').annotate(n=Count('id'))
    }
    active = {
        r['city']: r['n'] for r in
        _profiles().exclude(city='').filter(last_active__gte=_since(30))
        .values('city').annotate(n=Count('id'))
    }
    posts = {
        r['city']: r['n'] for r in
        _posts().exclude(city='').values('city').annotate(n=Count('id'))
    }
    posts_30 = {
        r['city']: r['n'] for r in
        _posts().exclude(city='').filter(created__gte=_since(30))
        .values('city').annotate(n=Count('id'))
    }
    rows = []
    for city, n in counts.items():
        rows.append({
            'city': city,
            'users': n,
            'active': active.get(city, 0),
            'active_pct': _pct(active.get(city, 0), n),
            'posts': posts.get(city, 0),
            'posts_30d': posts_30.get(city, 0),
            'posts_per_user': round(posts.get(city, 0) / n, 2),
            # The diagnosis, not just the numbers.
            'verdict': (
                'dead' if posts_30.get(city, 0) == 0 and n >= 5
                else 'quiet' if posts_30.get(city, 0) < n * 0.05
                else 'healthy'
            ),
        })
    rows.sort(key=lambda r: -r['users'])
    return rows[:limit]


# ── When people are here ────────────────────────────────────────────────────

def activity_by_hour():
    """Which hours the app is actually used — when to post and when to notify."""
    hours = Counter()
    for created in _posts().filter(created__gte=_since(60)).values_list('created', flat=True):
        hours[timezone.localtime(created).hour] += 1
    for created in _messages().filter(created__gte=_since(60)).values_list('created', flat=True):
        hours[timezone.localtime(created).hour] += 1
    peak = max(list(hours.values()) + [1])
    return {
        'hours': [{'hour': h, 'n': hours.get(h, 0)} for h in range(24)],
        'peak': peak,
        'busiest': max(range(24), key=lambda h: hours.get(h, 0)) if hours else None,
    }


def signups_by_day(days=30):
    rows = (
        _users().filter(date_joined__gte=_since(days))
        .annotate(day=TruncDate('date_joined')).values('day').annotate(n=Count('id'))
    )
    counts = {r['day']: r['n'] for r in rows}
    today = timezone.now().date()
    return [
        {'day': today - timezone.timedelta(days=o), 'n': counts.get(today - timezone.timedelta(days=o), 0)}
        for o in range(days - 1, -1, -1)
    ]


def activity_by_day(days=30):
    def per_day(qs):
        rows = (
            qs.filter(created__gte=_since(days))
            .annotate(day=TruncDate('created')).values('day').annotate(n=Count('id'))
        )
        return {r['day']: r['n'] for r in rows}
    posts, comments, messages = per_day(_posts()), per_day(_comments()), per_day(_messages())
    today = timezone.now().date()
    out = []
    for o in range(days - 1, -1, -1):
        day = today - timezone.timedelta(days=o)
        out.append({
            'day': day, 'posts': posts.get(day, 0),
            'comments': comments.get(day, 0), 'messages': messages.get(day, 0),
            'total': posts.get(day, 0) + comments.get(day, 0) + messages.get(day, 0),
        })
    return out


def most_active(limit=12):
    return list(
        _users().annotate(
            post_count=Count('posts', distinct=True),
            comment_count=Count('post_comments', distinct=True),
        )
        .filter(Q(post_count__gt=0) | Q(comment_count__gt=0))
        .order_by('-post_count', '-comment_count')[:limit]
        .values('username', 'post_count', 'comment_count', 'date_joined')
    )


def recent_users(limit=100):
    rows = _users().select_related('profile').order_by('-date_joined')[:limit]
    out = []
    for user in rows:
        profile = getattr(user, 'profile', None)
        last_active = getattr(profile, 'last_active', None)
        out.append({
            'username': user.username,
            'city': getattr(profile, 'city', '') or '—',
            'joined': user.date_joined,
            'last_active': last_active,
            'returned': bool(
                last_active and user.date_joined
                and last_active.date() > user.date_joined.date()
            ),
            'verified': getattr(profile, 'is_verified', False),
        })
    return out


# ── The read-out ────────────────────────────────────────────────────────────

def diagnosis(data):
    """Turn the numbers above into a ranked list of what is actually wrong.

    Everything else on the page reports; this decides. A dashboard nobody knows
    how to read changes nothing, so each finding names the number, says why it
    matters and gives the one action that would move it. Ordered by severity,
    because attention is the scarce thing.
    """
    out = []

    def add(level, title, detail, action):
        out.append({'level': level, 'title': title, 'detail': detail,
                    'action': action})

    push = data.get('push') or {}
    if push.get('total') and push['pct'] < 50:
        add('critical' if push['pct'] < 30 else 'warning',
            'Most signups cannot be reached',
            f"{push['unreachable']} of {push['total']} accounts "
            f"({100 - push['pct']:.0f}%) have no push token, so nothing can "
            "bring them back except their own initiative.",
            'Ask for the notification permission after the first real moment of '
            'value — a first like or follow — rather than on the launch screen, '
            'where it is declined most often.')

    graph = data['graph']
    if graph['isolated_pct'] >= 30:
        add('critical', 'Most people follow nobody',
            f"{graph['isolated']} accounts ({graph['isolated_pct']}%) follow no "
            "one, so their feed is empty every time they open it.",
            'Suggest accounts to follow during signup, before the feed is ever '
            'shown — an empty first feed is the most reliable reason people '
            'never come back.')
    elif graph['isolated_pct'] >= 15:
        add('warn', 'A lot of empty feeds',
            f"{graph['isolated_pct']}% of accounts follow nobody.",
            'Put a "people in your city" list in front of them on first open.')

    health = data['health']
    if health['no_engagement_pct'] >= 40:
        add('critical', 'Posts are landing in silence',
            f"{health['no_engagement']} posts ({health['no_engagement_pct']}%) "
            'have never received a like or a comment.',
            'Somebody who posts into silence does not post twice. Widen who '
            'sees a new post, or notify the author\'s city when they post.')
    elif health['no_engagement_pct'] >= 20:
        add('warn', 'Some posts get no reaction',
            f"{health['no_engagement_pct']}% of posts have no like or comment.",
            'Check whether these are from new accounts with no followers yet.')

    creators = data['creators']
    if creators['creators_pct'] < 5:
        add('warn', 'Very few people post',
            f"{creators['creators_pct']}% of accounts have ever posted; "
            f"{creators['lurkers_pct']}% have never posted, liked or commented.",
            'Lurking is normal, but this is low even for social. Try prompting '
            'a first post directly after signup, while intent is highest.')

    ret = data['retention']
    if ret and ret.get('d1') is not None and ret['d1'] < BENCHMARKS['d1']:
        add('warn' if ret['d1'] >= 20 else 'critical',
            'Day-1 return is below the band',
            f"About {ret['d1']}% came back the day after signing up, against "
            f"{BENCHMARKS['d1']}% for a strong consumer-social app. This is a "
            'floor — the real figure is somewhat higher.',
            'Day 1 is an onboarding problem, not a content problem: what people '
            'see in their first minute decides it.')

    dorm = data['dormant']
    if dorm['never_seen_pct'] >= 20:
        add('warn', 'Accounts that never opened the app again',
            f"{dorm['never_seen']} accounts ({dorm['never_seen_pct']}%) have no "
            'recorded activity at all after signing up.',
            'Worth checking these are real signups and not abandoned or '
            'automated registrations.')
    if dorm['quiet_30d_pct'] >= 50:
        add('warn', 'Half the base has gone quiet',
            f"{dorm['quiet_30d_pct']}% have not been seen in 30 days.",
            'A push worth opening — something that happened in their city — is '
            'the cheapest thing to try before spending on new signups.')

    head, grow = data['head'], data['growth']
    if head['total_users'] and head['mau'] < head['total_users'] * 0.1:
        add('critical', 'Almost nobody registered is still using it',
            f"{head['mau']} of {head['total_users']} accounts opened the app in "
            f"the last 30 days ({_pct(head['mau'], head['total_users'])}%). "
            f"{head['dau']} today.",
            'The registrations already exist — winning back even a tenth of them '
            'is far cheaper than finding new ones. Treat this as the headline '
            'number, not total signups.')

    if grow['stickiness'] < BENCHMARKS['stickiness']:
        add('warn' if grow['stickiness'] >= 10 else 'critical',
            'People are not coming back daily',
            f"Stickiness is {grow['stickiness']}% (daily ÷ monthly actives); "
            f"{BENCHMARKS['stickiness']}%+ is what a social app needs to compound.",
            'Something has to be different each day for a daily open to make '
            'sense — new posts nearby is the usual answer.')

    steps = data['activation']['steps']
    if len(steps) > 1:
        worst = max(steps[1:], key=lambda s: s['drop'])
        if worst['drop'] >= 25:
            add('info', f"Steepest fall: {worst['label'].lower()}",
                f"Only {worst['n']} people ({worst['of_total']}%) ever did this "
                f"— {worst['drop']}% fewer than the next most common milestone.",
                'The milestones are independent, so this is not a funnel step '
                'people fall out of — it is simply the thing fewest people '
                'reach, and the cheapest one to make easier.')

    dead = [c for c in data['cities'] if c['verdict'] == 'dead']
    if dead:
        names = ', '.join(c['city'] for c in dead[:4])
        add('warn', f"{len(dead)} cities have people but no posts",
            f"{names}{' and others' if len(dead) > 4 else ''} — accounts signed "
            'up there, nothing posted in the last 30 days.',
            'A city with no content cannot retain anybody. Either seed it or '
            'let those users see a wider area until it fills.')

    grow = data['growth']
    if grow['change'] is not None and grow['change'] <= -25:
        add('warn', 'Signups are slowing',
            f"{grow['this_week']} this week against {grow['last_week']} last "
            f"({grow['change']}%).",
            'Worth knowing whether the earlier week was a one-off push.')

    if not out:
        add('good', 'Nothing is obviously broken',
            'None of the health checks crossed a threshold worth acting on.',
            'Watch the power-user curve as it fills in — that is the earliest '
            'honest signal of whether a habit is forming.')

    order = {'critical': 0, 'warn': 1, 'info': 2, 'good': 3}
    out.sort(key=lambda f: order[f['level']])
    return out


def collect(launch_scoped=True):
    """Everything the page shows. Scoped to the launch unless asked otherwise —
    see the launch-scope block above for why that is the default."""
    set_launch_scoped(launch_scoped)
    head = headline()
    signups = signups_by_day()
    activity = activity_by_day()
    data = {
        'head': head,
        'growth': growth(head),
        'activation': activation(),
        'first_post': time_to_first_post(),
        'retention': retention_estimate(),
        'cohorts': retention_cohorts(),
        'dormant': dormant(),
        'power_curve': power_user_curve(),
        'sessions': session_metrics(),
        'creators': creator_split(),
        'graph': graph_health(),
        'health': content_health(),
        'signup_methods': signup_methods(),
        'push': push_reach(),
        'cities': city_breakdown(),
        'hours': activity_by_hour(),
        'signups': signups,
        'max_signup': max([d['n'] for d in signups] + [1]),
        'activity': activity,
        'max_activity': max([d['total'] for d in activity] + [1]),
        'active_users': most_active(),
        'users': recent_users(),
    }
    data['scope'] = scope_summary()
    data['diagnosis'] = diagnosis(data)
    return data
