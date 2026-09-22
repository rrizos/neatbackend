"""The numbers behind /ambassadorsstats.

Every figure here answers one of six questions, and each is defined where it
is computed so the page and the code cannot quietly mean different things:

1. **Is the programme worth it?**    spend, cost per approved person, funnel
2. **Are we paying for real people?** the people ambassadors bring, measured
                                      against people who joined on their own
                                      over the same period
3. **Who is doing well?**             per-ambassador leaderboard
4. **How much is evidence?**          how credits arrived, and how often each
                                      kind survives review
5. **What content works?**            platforms, cities, individual posts
6. **What looks wrong?**              flag reasons, device clusters, rejection
                                      rates, and a review queue going stale

Two rules run through all of it.

*Only judge what has had time to happen.* A person who joined yesterday cannot
have been retained for a week, or cleared a day-three bar. Every rate that
depends on elapsed time counts only people old enough to have had the chance —
otherwise a busy week makes an ambassador look worse, simply for being recent.

*Retention means what it means on /analytics.* Day N counts, among people who
joined at least N days ago, the share with a session starting on or after day
N. Same rule, same data, so the two pages agree.
"""

from collections import Counter, defaultdict
from decimal import Decimal
from statistics import median

from django.contrib.auth import get_user_model
from django.db.models import Count, F, Max, Q
from django.utils import timezone

from . import qualify
from .models import Ambassador, AmbassadorClick, AmbassadorContent, AmbassadorSignup

S = AmbassadorSignup

#: Credits that rest on evidence a link was followed, as opposed to a guess.
LINK_METHODS = {S.TOKEN, S.REFERRER, S.CLIPBOARD}
#: Accounts that cleared the quality bar, whatever happened after.
REAL = {S.QUALIFIED, S.APPROVED, S.PAID}
#: Accounts that cost money: approved and owed, or already paid.
PAID_FOR = {S.APPROVED, S.PAID}

TREND_DAYS = 30
#: The organic comparison group is capped to the recent past, so a programme
#: running for a year is not compared with people who joined before it began.
BASELINE_MAX_DAYS = 90

METHOD_LABELS = {
    S.REFERRER: ('Play referrer', 'evidence'),
    S.CLIPBOARD: ('Clipboard', 'evidence'),
    S.TOKEN: ('Link re-opened', 'evidence'),
    S.NETWORK: ('Same network', 'guess'),
    S.CONTENT: ('Post window', 'guess'),
}

#: What each flag sentence means, for counting them. Matched by substring so a
#: reworded flag still lands in the right bucket as long as its key phrase stays.
FLAG_REASONS = [
    ('rejected automatically', 'Auto-rejected: address already credited'),
    ('already credited from this device', 'Same device as another credited signup'),
    ('claim IP differs', 'Claimed from a different network than the click'),
    ('over the daily cap', 'Over the ambassador’s daily cap'),
    ('network and time only', 'Matched by network and time only'),
]


def _pct(part, whole):
    return round(part * 100 / whole, 1) if whole else None


# ── The people ambassadors bring, and the people who came on their own ───────

def _cohort_quality(people, now):
    """How a group of accounts behaves. [people] is [(user_id, date_joined)].

    Returns size, D1/D7 retention (mature-only), share active in the last week,
    share who posted, share who reached the interaction bar, and posts per
    person.
    """
    from accounts.models import AppSession, Follow
    from posts.models import Post, PostComment, PostLike

    ids = [uid for uid, _ in people]
    result = {'size': len(ids)}
    if not ids:
        return result

    sessions = defaultdict(list)
    last_seen = {}
    for uid, started, seen in AppSession.objects.filter(user_id__in=ids).values_list(
            'user_id', 'started', 'last_seen'):
        sessions[uid].append(started)
        if uid not in last_seen or seen > last_seen[uid]:
            last_seen[uid] = seen

    for label, day in (('d1', 1), ('d7', 7)):
        offset = timezone.timedelta(days=day)
        mature = [(uid, joined) for uid, joined in people if joined + offset <= now]
        kept = sum(
            1 for uid, joined in mature
            if any(s >= joined + offset for s in sessions.get(uid, ()))
        )
        result[label] = _pct(kept, len(mature))
        result[f'{label}_base'] = len(mature)

    week_ago = now - timezone.timedelta(days=7)
    result['active_7d'] = _pct(
        sum(1 for uid in ids if last_seen.get(uid) and last_seen[uid] >= week_ago), len(ids))

    posts = dict(
        Post.objects.filter(user_id__in=ids).values('user_id')
        .annotate(n=Count('id')).values_list('user_id', 'n'))
    # Interactions are only ever the ones aimed at somebody else, exactly as
    # the quality bar counts them.
    interactions = Counter()
    for model, owner in ((PostLike, 'user_id'), (PostComment, 'user_id')):
        for uid, n in (model.objects.filter(user_id__in=ids)
                       .exclude(post__user_id=F('user_id'))
                       .values(owner).annotate(n=Count('id')).values_list(owner, 'n')):
            interactions[uid] += n
    for uid, n in (Follow.objects.filter(follower_id__in=ids)
                   .exclude(following_id=F('follower_id'))
                   .values('follower_id').annotate(n=Count('id'))
                   .values_list('follower_id', 'n')):
        interactions[uid] += n

    result['posted'] = _pct(sum(1 for uid in ids if posts.get(uid)), len(ids))
    result['interacted'] = _pct(
        sum(1 for uid in ids if interactions[uid] >= qualify.MIN_INTERACTIONS), len(ids))
    result['posts_per_person'] = round(sum(posts.values()) / len(ids), 2)
    return result


def _quality_comparison(signups, now):
    """Recruited people next to organic people from the same stretch of time.

    Rejected credits are left out of the recruited group: a reviewer has
    already decided those were not real, and counting them would describe the
    fraud rather than the people the programme is actually paying for.
    """
    User = get_user_model()
    recruited = [(s.user_id, s.user.date_joined) for s in signups if s.status != S.REJECTED]

    credited_ids = {s.user_id for s in signups}
    if signups:
        start = min(s.user.date_joined for s in signups)
    else:
        start = now - timezone.timedelta(days=30)
    start = max(start, now - timezone.timedelta(days=BASELINE_MAX_DAYS))

    organic = list(
        User.objects.filter(date_joined__gte=start)
        .exclude(id__in=credited_ids)
        .values_list('id', 'date_joined'))

    recruited_q = _cohort_quality(recruited, now)
    organic_q = _cohort_quality(organic, now)

    rows = []
    for key, label, note in (
        ('d1', 'Came back on day 1', 'of people at least a day old'),
        ('d7', 'Came back on day 7', 'of people at least a week old'),
        ('active_7d', 'Active in the last 7 days', 'any session this week'),
        ('posted', 'Posted at least once', ''),
        ('interacted', f'Interacted with {qualify.MIN_INTERACTIONS}+ other people', 'likes, comments, follows'),
        ('posts_per_person', 'Posts per person', ''),
    ):
        a, b = recruited_q.get(key), organic_q.get(key)
        delta = round(a - b, 1) if a is not None and b is not None else None
        rows.append({
            'label': label, 'note': note,
            'recruited': a, 'organic': b, 'delta': delta,
            'percent': key != 'posts_per_person',
            'recruited_base': recruited_q.get(f'{key}_base'),
            'organic_base': organic_q.get(f'{key}_base'),
        })

    return {
        'since': start,
        'recruited': recruited_q,
        'organic': organic_q,
        'rows': rows,
    }


# ── Over time ─────────────────────────────────────────────────────────────────

def _trend(signups, now):
    """Last thirty days, by local calendar day: opens, and credits by kind."""
    today = timezone.localtime(now).date()
    days = [today - timezone.timedelta(days=offset) for offset in range(TREND_DAYS - 1, -1, -1)]
    buckets = {d: {'opens': 0, 'link': 0, 'guess': 0, 'approved': 0} for d in days}
    start = now - timezone.timedelta(days=TREND_DAYS)

    for created in AmbassadorClick.objects.filter(
            created__gte=start, source=AmbassadorClick.WEB).values_list('created', flat=True):
        day = timezone.localtime(created).date()
        if day in buckets:
            buckets[day]['opens'] += 1

    for s in signups:
        day = timezone.localtime(s.created).date()
        if day in buckets:
            buckets[day]['link' if s.claim_method in LINK_METHODS else 'guess'] += 1
        if s.status in PAID_FOR and s.reviewed_at:
            reviewed = timezone.localtime(s.reviewed_at).date()
            if reviewed in buckets:
                buckets[reviewed]['approved'] += 1

    rows = [{'day': d, **buckets[d]} for d in days]
    return {
        'rows': rows,
        'opens': _bars(rows, ['opens'], ['#8b949e']),
        'signups': _bars(rows, ['link', 'guess'], ['#2f81f7', '#d29922']),
        'totals': {
            'opens': sum(r['opens'] for r in rows),
            'link': sum(r['link'] for r in rows),
            'guess': sum(r['guess'] for r in rows),
            'approved': sum(r['approved'] for r in rows),
        },
    }


def _bars(rows, keys, colours, width=720, height=120):
    """Geometry for a stacked bar chart, so the template only draws rects."""
    peak = max((sum(r[k] for k in keys) for r in rows), default=0)
    slot = width / max(len(rows), 1)
    bar = max(slot * 0.72, 1)
    shapes, labels = [], []
    for i, r in enumerate(rows):
        x = i * slot + (slot - bar) / 2
        y = height
        for key, colour in zip(keys, colours):
            h = (r[key] / peak * (height - 4)) if peak else 0
            if h:
                y -= h
                shapes.append({'x': round(x, 2), 'y': round(y, 2), 'w': round(bar, 2),
                               'h': round(h, 2), 'fill': colour,
                               'title': f"{r['day']:%d %b}: {r[key]} {key}"})
        if i % 5 == 0 or i == len(rows) - 1:
            labels.append({'x': round(x + bar / 2, 2), 'text': f"{r['day']:%d/%m}"})
    return {'shapes': shapes, 'labels': labels, 'peak': peak, 'width': width, 'height': height}


# ── Per ambassador ────────────────────────────────────────────────────────────

def _leaderboard(signups, now):
    mature_cut = now - timezone.timedelta(days=qualify.RETENTION_DAYS)
    week_ago = now - timezone.timedelta(days=7)

    clicks = {
        row['ambassador_id']: row
        for row in AmbassadorClick.objects.values('ambassador_id').annotate(
            opens=Count('id', filter=Q(source=AmbassadorClick.WEB)),
            opens_7d=Count('id', filter=Q(source=AmbassadorClick.WEB, created__gte=week_ago)),
            visitors=Count('visitor', filter=Q(source=AmbassadorClick.WEB), distinct=True),
            last_open=Max('created', filter=Q(source=AmbassadorClick.WEB)),
        )
    }
    posts = {
        row['ambassador_id']: row
        for row in AmbassadorContent.objects.values('ambassador_id').annotate(
            verified=Count('id', filter=Q(status=AmbassadorContent.APPROVED)),
            running=Count('id', filter=Q(status=AmbassadorContent.APPROVED,
                                         stopped_at__isnull=True)),
            waiting=Count('id', filter=Q(status=AmbassadorContent.PENDING)),
        )
    }

    by_ambassador = defaultdict(list)
    for s in signups:
        by_ambassador[s.ambassador_id].append(s)

    rows = []
    for amb in Ambassador.objects.order_by('name'):
        mine = by_ambassador.get(amb.id, [])
        c = clicks.get(amb.id, {})
        p = posts.get(amb.id, {})
        status = Counter(s.status for s in mine)

        link = sum(1 for s in mine if s.claim_method in LINK_METHODS)
        network = sum(1 for s in mine if s.claim_method == S.NETWORK)
        content = sum(1 for s in mine if s.claim_method == S.CONTENT)

        # Quality: of the credits old enough to have cleared the day-three bar,
        # the share that did. Rejected ones stay in the denominator — they are
        # the failures this number exists to show.
        mature = [s for s in mine if s.user.date_joined <= mature_cut]
        real_mature = sum(1 for s in mature if s.status in REAL)

        decided = status[S.APPROVED] + status[S.PAID] + status[S.REJECTED]
        owed = sum((s.amount for s in mine if s.status == S.APPROVED), Decimal('0'))
        paid = sum((s.amount for s in mine if s.status == S.PAID), Decimal('0'))
        paid_for = status[S.APPROVED] + status[S.PAID]

        retention = _cohort_quality(
            [(s.user_id, s.user.date_joined) for s in mine if s.status != S.REJECTED], now)

        rows.append({
            'ambassador': amb,
            'opens': c.get('opens', 0),
            'opens_7d': c.get('opens_7d', 0),
            'visitors': c.get('visitors', 0),
            'last_open': c.get('last_open'),
            'signups': len(mine),
            'signups_7d': sum(1 for s in mine if s.created >= week_ago),
            'link': link,
            'network': network,
            'content': content,
            'visit_to_signup': _pct(link, c.get('visitors', 0)),
            'pending': status[S.PENDING],
            'qualified': status[S.QUALIFIED],
            'approved': status[S.APPROVED],
            'paid': status[S.PAID],
            'flagged': status[S.FLAGGED],
            'rejected': status[S.REJECTED],
            'quality': _pct(real_mature, len(mature)),
            'quality_base': len(mature),
            'rejection_rate': _pct(status[S.REJECTED], decided),
            'd7': retention.get('d7'),
            'd7_base': retention.get('d7_base', 0),
            'owed': owed,
            'settled': paid,
            'spend': owed + paid,
            'cost_per_person': (round((owed + paid) / paid_for, 2) if paid_for else None),
            'posts_verified': p.get('verified', 0),
            'posts_running': p.get('running', 0),
            'posts_waiting': p.get('waiting', 0),
            'last_signup': max((s.created for s in mine), default=None),
        })

    rows.sort(key=lambda r: (r['approved'] + r['paid'], r['qualified'], r['signups']),
              reverse=True)
    return rows


# ── Attribution, content, risk, queue ─────────────────────────────────────────

def _attribution(signups):
    rows = []
    for method, (label, kind) in METHOD_LABELS.items():
        mine = [s for s in signups if s.claim_method == method]
        decided = [s for s in mine if s.status in PAID_FOR | {S.REJECTED}]
        rows.append({
            'label': label,
            'kind': kind,
            'count': len(mine),
            'share': _pct(len(mine), len(signups)),
            'real': sum(1 for s in mine if s.status in REAL),
            'approved': sum(1 for s in mine if s.status in PAID_FOR),
            'rejected': sum(1 for s in mine if s.status == S.REJECTED),
            'approval_rate': _pct(sum(1 for s in decided if s.status in PAID_FOR), len(decided)),
        })
    evidence = sum(r['count'] for r in rows if r['kind'] == 'evidence')
    return {'rows': rows, 'evidence_share': _pct(evidence, len(signups))}


def _content(signups, now):
    platforms = dict(AmbassadorContent.PLATFORMS)
    posts = list(AmbassadorContent.objects.select_related('ambassador'))
    credits = defaultdict(list)
    for s in signups:
        if s.content_id:
            credits[s.content_id].append(s)

    by_platform = defaultdict(lambda: {'posts': 0, 'credited': 0, 'real': 0, 'approved': 0})
    by_city = defaultdict(lambda: {'posts': 0, 'credited': 0, 'real': 0, 'approved': 0})
    post_rows = []
    for post in posts:
        if post.status != AmbassadorContent.APPROVED:
            continue
        mine = credits.get(post.id, [])
        real = sum(1 for s in mine if s.status in REAL)
        approved = sum(1 for s in mine if s.status in PAID_FOR)
        for bucket in (by_platform[post.platform], by_city[post.city]):
            bucket['posts'] += 1
            bucket['credited'] += len(mine)
            bucket['real'] += real
            bucket['approved'] += approved
        end = post.stopped_at or now
        hours = max((end - post.uploaded_at).total_seconds() / 3600, 0)
        post_rows.append({
            'post': post,
            'platform': platforms.get(post.platform, post.platform),
            'credited': len(mine),
            'real': real,
            'approved': approved,
            'hours': int(hours),
            # People per day the post ran: a fair way to compare a post that
            # ran for a weekend with one that ran for a fortnight.
            'per_day': round(len(mine) / (hours / 24), 2) if hours >= 1 else None,
        })

    def table(source, names=None):
        out = []
        for key, v in source.items():
            out.append({
                'label': (names or {}).get(key, key), **v,
                'per_post': round(v['credited'] / v['posts'], 2) if v['posts'] else None,
                'real_rate': _pct(v['real'], v['credited']),
            })
        return sorted(out, key=lambda r: r['credited'], reverse=True)

    return {
        'platforms': table(by_platform, platforms),
        'cities': table(by_city),
        'top_posts': sorted(post_rows, key=lambda r: r['credited'], reverse=True)[:10],
    }


def _recruit_cities(signups):
    """Where the people ambassadors bring actually live, whatever brought them."""
    by_city = defaultdict(lambda: {'credited': 0, 'real': 0})
    for s in signups:
        if s.status == S.REJECTED:
            continue
        city = getattr(getattr(s.user, 'profile', None), 'city', '') or '—'
        by_city[city]['credited'] += 1
        if s.status in REAL:
            by_city[city]['real'] += 1
    rows = [{'city': k, **v, 'real_rate': _pct(v['real'], v['credited'])}
            for k, v in by_city.items()]
    return sorted(rows, key=lambda r: r['credited'], reverse=True)[:15]


def _risk(signups, leaderboard):
    reasons = Counter()
    for s in signups:
        for needle, label in FLAG_REASONS:
            if needle in (s.flags or ''):
                reasons[label] += 1

    # Devices that more than one credited account came from. A household is a
    # reasonable explanation for two; five is not.
    by_device = defaultdict(set)
    owners = {}
    for s in signups:
        if s.click_id and s.click and s.click.visitor:
            by_device[s.click.visitor].add(s.user_id)
            owners.setdefault(s.click.visitor, set()).add(s.ambassador.name)
    clusters = sorted(
        ({'accounts': len(users), 'ambassadors': ', '.join(sorted(owners[v]))}
         for v, users in by_device.items() if len(users) >= 2),
        key=lambda c: c['accounts'], reverse=True)

    # Addresses, shown rather than hashed: an argument about a payout is
    # settled by looking at where the signups came from.
    by_address = defaultdict(list)
    for s in signups:
        if s.ip_address:
            by_address[s.ip_address].append(s)
    address_rows = []
    for ip, mine in by_address.items():
        if len(mine) < 2:
            continue
        address_rows.append({
            'ip': ip,
            'accounts': len(mine),
            'live': sum(1 for x in mine if x.status != S.REJECTED),
            'rejected': sum(1 for x in mine if x.status == S.REJECTED),
            'ambassadors': ', '.join(sorted({x.ambassador.name for x in mine})),
            'users': ', '.join(f'@{x.user.username}' for x in mine[:8]),
            'last': max(x.created for x in mine),
        })
    address_rows.sort(key=lambda r: r['accounts'], reverse=True)

    watch = [r for r in leaderboard
             if (r['rejection_rate'] or 0) >= 30 or r['flagged'] >= 3]
    return {
        'reasons': [{'label': label, 'count': reasons.get(label, 0)}
                    for _, label in FLAG_REASONS],
        'addresses': address_rows[:15],
        'shared_addresses': len(address_rows),
        'clusters': clusters[:10],
        'cluster_accounts': sum(c['accounts'] for c in clusters),
        'watch': watch,
    }


def _queue(now):
    qualified = S.objects.filter(status=S.QUALIFIED)
    oldest_qualified = qualified.order_by('qualified_at').values_list('qualified_at', flat=True).first()
    flagged = S.objects.filter(status=S.FLAGGED)
    oldest_flagged = flagged.order_by('created').values_list('created', flat=True).first()

    waits = [
        (q - c).total_seconds() / 3600
        for c, q in S.objects.filter(qualified_at__isnull=False).values_list('created', 'qualified_at')
    ]
    posts_waiting = AmbassadorContent.objects.filter(status=AmbassadorContent.PENDING)
    oldest_post = posts_waiting.order_by('created').first()
    # Stories vanish after a day; one logged more than 20 hours ago may already
    # be impossible to verify.
    expiring = posts_waiting.filter(
        platform='instagram_story', uploaded_at__lte=now - timezone.timedelta(hours=20)).count()

    def age(dt):
        return round((now - dt).total_seconds() / 3600, 1) if dt else None

    return {
        'pending': S.objects.filter(status=S.PENDING).count(),
        'qualified': qualified.count(),
        'flagged': flagged.count(),
        'oldest_qualified_hours': age(oldest_qualified),
        'oldest_flagged_hours': age(oldest_flagged),
        'median_hours_to_qualify': round(median(waits), 1) if waits else None,
        'posts_waiting': posts_waiting.count(),
        'oldest_post_hours': age(oldest_post.created) if oldest_post else None,
        'stories_expiring': expiring,
    }


# ── Everything ────────────────────────────────────────────────────────────────

def collect():
    now = timezone.now()
    week_ago = now - timezone.timedelta(days=7)
    signups = list(
        S.objects.select_related('user', 'user__profile', 'ambassador', 'content', 'click'))

    leaderboard = _leaderboard(signups, now)
    status = Counter(s.status for s in signups)
    link_signups = sum(1 for s in signups if s.claim_method in LINK_METHODS)
    visitors = (AmbassadorClick.objects.filter(source=AmbassadorClick.WEB)
                .exclude(visitor='').values('visitor').distinct().count())

    owed = sum((r['owed'] for r in leaderboard), Decimal('0'))
    settled = sum((r['settled'] for r in leaderboard), Decimal('0'))
    paid_for = status[S.APPROVED] + status[S.PAID]
    real = sum(status[s] for s in REAL)

    mature_cut = now - timezone.timedelta(days=qualify.RETENTION_DAYS)
    mature = [s for s in signups if s.user.date_joined <= mature_cut]

    overview = {
        'ambassadors': len(leaderboard),
        'active': sum(1 for r in leaderboard if r['ambassador'].is_active),
        'opens': sum(r['opens'] for r in leaderboard),
        'opens_7d': sum(r['opens_7d'] for r in leaderboard),
        'visitors': visitors,
        'signups': len(signups),
        'signups_7d': sum(1 for s in signups if s.created >= week_ago),
        'real': real,
        'approved': paid_for,
        'owed': owed,
        'settled': settled,
        'spend': owed + settled,
        'cost_per_person': round((owed + settled) / paid_for, 2) if paid_for else None,
        'quality': _pct(sum(1 for s in mature if s.status in REAL), len(mature)),
        'quality_base': len(mature),
        'rejection_rate': _pct(status[S.REJECTED],
                               status[S.APPROVED] + status[S.PAID] + status[S.REJECTED]),
    }

    guessed = len(signups) - link_signups
    funnel = [
        {'label': 'Link opens', 'value': overview['opens'], 'note': 'every read of an /a/ page'},
        {'label': 'Unique visitors', 'value': visitors, 'note': 'distinct devices', 'of_previous': True},
        {'label': 'Signups by link', 'value': link_signups,
         'note': 'referrer, clipboard or re-opened link', 'of_previous': True},
        # Not a share of the step above: this adds people who never opened a
        # link at all, so a percentage here would read as a conversion rate
        # and be meaningless — 350% of the step above, in one real case.
        {'label': 'All credited signups', 'value': len(signups),
         'note': f'the {link_signups} above, plus {guessed} matched by network or a post window'},
        {'label': 'Real users', 'value': real, 'note': 'cleared the full bar', 'of_previous': True},
        {'label': 'Approved', 'value': paid_for, 'note': 'approved or paid', 'of_previous': True},
        {'label': 'Paid', 'value': status[S.PAID], 'note': 'settled', 'of_previous': True},
    ]
    for i, step in enumerate(funnel):
        step['from_previous'] = (
            _pct(step['value'], funnel[i - 1]['value']) if i and step.get('of_previous') else None)

    return {
        'generated': now,
        'overview': overview,
        'funnel': funnel,
        'quality': _quality_comparison(signups, now),
        'trend': _trend(signups, now),
        'leaderboard': leaderboard,
        'attribution': _attribution(signups),
        'content': _content(signups, now),
        'recruit_cities': _recruit_cities(signups),
        'risk': _risk(signups, leaderboard),
        'queue': _queue(now),
    }
