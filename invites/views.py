"""The invite link's two ends: the page it opens, and the numbers it leaves.

The page is rendered here rather than shipped as a static file because it
knows three things a static file cannot: who is inviting, which city, and how
close that city is to opening. It is also the one page on the site whose
audience definitionally does not have the app — so it is a landing page with
store buttons, not a shell that boots one.
"""

import json
import secrets

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, Max, Q
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.cache import cache_control
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_http_methods

from accounts.auth import require_authenticated_user
from accounts.ratelimit import client_ip, rate_limited
from .cities import city_slug
from ambassadors.models import ip_hash as ambassador_ip_hash

from .models import InviteEvent, visitor_hash

User = get_user_model()

#: Above this many people still missing, the page and the app both stop
#: naming the number. "234 to go" reads as a wall rather than an invitation —
#: it tells someone their share will not matter, which is the opposite of what
#: an invite is for. Under it, the number is the argument.
REVEAL_REMAINING_AT = 25

#: An account older than this cannot be somebody's new signup, whatever the
#: app reports. Generous on purpose: a person can download the app on Friday
#: and finish signing up on Sunday.
JOIN_ATTRIBUTION_WINDOW = timezone.timedelta(days=3)

#: How long a click token stays claimable. Matches the ambassador one.
TOKEN_MAX_AGE = timezone.timedelta(days=14)


# ── Recording ────────────────────────────────────────────────────────────────

def record_open(request, inviter, city):
    """One row per visitor per inviter per day, and the token that row hands
    out. See InviteEvent's docstring for why the dedup happens here rather
    than in a constraint.

    The token is refreshed on every read of an unspent row, so the person
    looking at the page right now is the one holding a live token — rather
    than whoever looked first this morning.
    """
    visitor = visitor_hash(
        client_ip(request),
        request.headers.get('User-Agent', '')[:400],
    )
    fresh = {
        'city': city or '',
        'token': secrets.token_urlsafe(32),
        'ip_hash': ambassador_ip_hash(client_ip(request)),
    }
    bucket = {
        'kind': InviteEvent.OPENED,
        'inviter': inviter,
        'visitor': visitor,
        'day': timezone.localdate(),
    }

    # Not get_or_create: a visitor can end up with more than one row a day
    # (see below), and get_or_create raises on the second lookup when that
    # happens. The newest row is the one that matters.
    event = InviteEvent.objects.filter(**bucket).order_by('-created').first()

    if event is None:
        event = InviteEvent.objects.create(**bucket, **fresh)
    elif event.used_at is None:
        # The address is refreshed alongside the token rather than only
        # written at creation: the row is reused by a second visit, and rows
        # predating this field carry an empty hash that the network match
        # could never pair with anything.
        event.token = fresh['token']
        event.ip_hash = fresh['ip_hash']
        event.save(update_fields=['token', 'ip_hash'])
    else:
        # Already spent on somebody's signup. A second row rather than a dead
        # end: one row per visitor per day is the right shape for *counting*
        # opens and the wrong shape for handing out claims — two people on one
        # school wifi with the same phone model hash to the same visitor, and
        # the second of them would otherwise be uncountable. The slight
        # inflation of "opens" only happens after a visitor has already
        # produced a signup, which is a visit worth counting anyway.
        event = InviteEvent.objects.create(**bucket, **fresh)

    return event.token


@csrf_exempt
@require_http_methods(['POST'])
def invite_sent(request):
    """The app calls this when someone copies their link."""
    user = require_authenticated_user(request)
    if user is None:
        return JsonResponse({'error': 'Authentication required'}, status=401)

    # A copy button is a button: somebody will lean on it. The cap is per
    # person per hour and is about keeping one enthusiast from dominating the
    # dashboard, not about stopping abuse.
    if rate_limited(f'invite-sent:{user.id}', limit=60, window_seconds=3600):
        return JsonResponse({'ok': True, 'recorded': False})

    try:
        body = json.loads(request.body or b'{}')
    except ValueError:
        body = {}

    InviteEvent.objects.create(
        kind=InviteEvent.SENT,
        inviter=user,
        city=(body.get('city') or '')[:120],
    )
    return JsonResponse({'ok': True, 'recorded': True})


@csrf_exempt
@require_http_methods(['POST'])
def invite_joined(request):
    """The app calls this once, after a signup that came from an invite.

    Everything about the claim is checked here rather than trusted: who the
    inviter is, that they are not the caller, that the caller is actually new,
    and that nobody has already been credited with them.
    """
    user = require_authenticated_user(request)
    if user is None:
        return JsonResponse({'error': 'Authentication required'}, status=401)

    try:
        body = json.loads(request.body or b'{}')
    except ValueError:
        body = {}

    username = (body.get('inviter') or '').strip()
    if not username:
        return JsonResponse({'error': 'inviter required'}, status=400)

    inviter = User.objects.filter(username__iexact=username).first()
    if inviter is None or inviter.id == user.id:
        # Silent success: the app cannot fix either of these, and an error
        # would only make it retry a claim that will never be valid.
        return JsonResponse({'ok': True, 'recorded': False})

    if timezone.now() - user.date_joined > JOIN_ATTRIBUTION_WINDOW:
        return JsonResponse({'ok': True, 'recorded': False})

    _, created = InviteEvent.objects.get_or_create(
        joined_user=user,
        defaults={
            'kind': InviteEvent.JOINED,
            'inviter': inviter,
            'city': (body.get('city') or '')[:120],
        },
    )
    return JsonResponse({'ok': True, 'recorded': created})


@csrf_exempt
@require_http_methods(['POST'])
def invite_claim(request):
    """A new account presents the token its installation carried in.

    The same shape as the ambassador claim and for the same reason — nobody
    taps a link twice — but without the fraud apparatus, because nothing here
    is paid. What it still refuses: a spent token, a stale one, an account
    that is not new, an account already credited, and crediting yourself.
    """
    user = require_authenticated_user(request)
    if user is None:
        return JsonResponse({'error': 'Authentication required'}, status=401)

    try:
        body = json.loads(request.body or b'{}')
    except ValueError:
        body = {}
    token = (body.get('token') or '').strip()
    if not token:
        return JsonResponse({'error': 'token required'}, status=400)

    if timezone.now() - user.date_joined > JOIN_ATTRIBUTION_WINDOW:
        return JsonResponse({'ok': True, 'recorded': False, 'reason': 'account_not_new'})
    if InviteEvent.objects.filter(joined_user=user).exists():
        return JsonResponse({'ok': True, 'recorded': False, 'reason': 'already_credited'})

    with transaction.atomic():
        click = (
            InviteEvent.objects
            .select_for_update()
            .select_related('inviter')
            .filter(kind=InviteEvent.OPENED, token=token, used_at__isnull=True)
            .first()
        )
        if click is None:
            return JsonResponse({'ok': True, 'recorded': False, 'reason': 'token_unusable'})
        if timezone.now() - click.created > TOKEN_MAX_AGE:
            return JsonResponse({'ok': True, 'recorded': False, 'reason': 'token_expired'})
        if click.inviter_id == user.id:
            return JsonResponse({'ok': True, 'recorded': False, 'reason': 'self_invite'})

        InviteEvent.objects.create(
            kind=InviteEvent.JOINED,
            inviter=click.inviter,
            city=click.city,
            joined_user=user,
        )
        click.used_at = timezone.now()
        click.save(update_fields=['used_at'])

    return JsonResponse({'ok': True, 'recorded': True})


# ── The page an invite opens ─────────────────────────────────────────────────

def _avatar_for_page(user, request):
    """The inviter's real face, because an invitation from a stranger is not
    one. Prefers the file on disk over the base64 copy: a browser can cache a
    URL, and the data URL would be inlined into the HTML on every load."""
    profile = getattr(user, 'profile', None)
    if profile is None:
        return ''
    thumb = getattr(profile, 'avatar_thumb_url', '') or ''
    if thumb:
        return request.build_absolute_uri(thumb) if thumb.startswith('/') else thumb
    # Already a data: URL, so it goes straight into the <img>.
    return getattr(profile, 'avatar_url', '') or ''


def _stores_for(user_agent, referrer_token='', prefix='neat_ct'):
    """One download button, pointed at the store this phone actually has.

    Two badges side by side ask the reader to make a choice they have already
    made by owning a phone. The other platform stays reachable underneath for
    the case this guess is wrong.

    [referrer_token] is threaded onto the Play URL, where it survives the
    install: Google hands the referrer string to the app on first launch, so
    an Android referral needs nothing from the user at all. The App Store has
    no equivalent — Apple passes an app nothing about how it was installed —
    which is why iOS falls back to the clipboard and, failing that, to the
    network match.
    """
    from urllib.parse import quote

    from web.views import APP_STORE_URL, PLAY_STORE_URL

    play = PLAY_STORE_URL
    if referrer_token:
        play = f'{PLAY_STORE_URL}&referrer={quote(f"{prefix}={referrer_token}", safe="")}'

    ua = (user_agent or '').lower()
    ios = any(t in ua for t in ('iphone', 'ipad', 'ipod', 'mac os x'))
    if ios:
        return {'primary_url': APP_STORE_URL, 'primary_label': 'App Store',
                'other_url': play, 'other_label': 'Android'}
    return {'primary_url': play, 'primary_label': 'Google Play',
            'other_url': APP_STORE_URL, 'other_label': 'iPhone'}


def _city_state(slug):
    """The city an invite names, as the page needs to talk about it."""
    if not slug:
        return None
    # Imported here, not at module load: this checkout and the box disagree
    # about which posts/models.py is newest, and an invite page is not worth
    # taking the whole URLconf down over an import that resolves at call time.
    from posts.models import CityConfig

    for config in CityConfig.objects.all():
        if city_slug(config.name) != slug:
            continue
        remaining = max(config.threshold - config.member_count_cache, 0)
        return {
            'name': config.name,
            'threshold': config.threshold,
            'members': config.member_count_cache,
            'remaining': remaining,
            'open': (not config.is_locked) or remaining == 0,
            # The counter is shown only when it argues for joining. See
            # REVEAL_REMAINING_AT.
            'show_numbers': remaining <= REVEAL_REMAINING_AT,
            'progress': (
                min(round(config.member_count_cache * 100 / config.threshold), 100)
                if config.threshold > 0 else 0
            ),
        }
    return None


@require_http_methods(['GET', 'HEAD'])
@cache_control(no_store=True)
def invite_page(request, username=''):
    inviter = None
    if username:
        inviter = User.objects.filter(
            username__iexact=username, is_active=True,
        ).first()
        # A link naming nobody is still a working invitation to the app, and
        # a 404 would be a worse answer to a mistyped or renamed handle.

    city = _city_state((request.GET.get('city') or '').strip().lower())

    click_token = ''
    if inviter is not None and request.method == 'GET':
        click_token = record_open(request, inviter, city['name'] if city else '')

    # The handle, not the display name: on Neat people know each other by
    # @username, and a full name that happens to be "haha" tells the reader
    # nothing about who is inviting them.
    who = f'@{inviter.username}' if inviter else 'Ένας φίλος σου'
    if city and city['open']:
        description = (
            f'Το feed της πόλης {city["name"]} είναι ανοιχτό — '
            'κατέβασε τη Neat και μπες.'
        )
    elif city and city['show_numbers']:
        description = (
            f'Λείπουν {city["remaining"]} άτομα για να ανοίξει το feed της '
            f'πόλης {city["name"]}. Μπες κι εσύ.'
        )
    elif city:
        description = (
            f'Μπες στη Neat και φέρε το feed της πόλης {city["name"]} '
            'πιο κοντά στο άνοιγμα.'
        )
    else:
        description = 'Μπες στη Neat και φέρε την πόλη σου πιο κοντά στο άνοιγμα.'

    return render(request, 'invites/invite.html', {
        'inviter': inviter.username if inviter else '',
        'avatar': _avatar_for_page(inviter, request) if inviter else '',
        # Strip the @ before taking the letter, or a handle-only inviter gets
        # a monogram of 'N' instead of their own first letter.
        'initial': (who.lstrip('@')[:1].upper() or 'N'),
        # `neat_it=` rather than the ambassador's `neat_ct=`, so the app knows
        # which claim this token belongs to without having to try both.
        'stores': _stores_for(request.headers.get('User-Agent', ''),
                              click_token, prefix='neat_it'),
        'clipboard_token': f'neat_it={click_token}' if click_token else '',
        'who': who,
        'title': f'{who} σε προσκαλεί στη Neat',
        'description': description,
        'city': city,
        # An invite is about one locked city, so the reassurance that the
        # national feed is already open answers the obvious next question.
        'show_greece_note': True,
        'canonical': request.build_absolute_uri(request.path),
    })


# ── The dashboard ────────────────────────────────────────────────────────────

@csrf_protect
@require_http_methods(['GET', 'POST'])
def invites_dashboard(request):
    """Who is inviting, and what came of it.

    Behind the same admin login as /analytics, and sharing its session: this
    names individual users and counts their friends, which is not something to
    hand to whoever asks.
    """
    from django.contrib.auth import authenticate

    from accounts.serializers import ensure_profile
    from web.views import ANALYTICS_SESSION_KEY, _analytics_admin

    error = ''
    if request.method == 'POST' and not _analytics_admin(request):
        if rate_limited(f'invites:{client_ip(request)}', limit=8, window_seconds=900):
            error = 'Too many attempts. Try again later.'
        else:
            user = authenticate(
                username=(request.POST.get('username') or '').strip(),
                password=request.POST.get('password') or '',
            )
            if user is not None and ensure_profile(user).is_admin:
                request.session[ANALYTICS_SESSION_KEY] = True
                request.session.set_expiry(60 * 60 * 8)
                return redirect('invites_dashboard')
            error = 'Those details are not valid here.'

    if not _analytics_admin(request):
        return render(request, 'invites/login.html', {'error': error}, status=200)

    if request.GET.get('logout'):
        request.session.pop(ANALYTICS_SESSION_KEY, None)
        return redirect('invites_dashboard')

    # One grouped query rather than three plus a loop: the dashboard is read
    # by one person occasionally, but it reads the whole event table, and that
    # table only grows.
    rows = (
        InviteEvent.objects
        .values('inviter__username')
        .annotate(
            copies=Count('id', filter=Q(kind=InviteEvent.SENT)),
            opens=Count('id', filter=Q(kind=InviteEvent.OPENED)),
            joins=Count('id', filter=Q(kind=InviteEvent.JOINED)),
            last=Max('created'),
        )
        .order_by('-joins', '-opens', '-copies')
    )
    rows = list(rows)

    # A join is an account that exists; a *real* one is an account that behaves
    # like a person. Measured with the same four tests the ambassador payouts
    # use, so the two pages cannot quietly disagree about what a user is —
    # city, a post, two interactions with other people, still there on day
    # three. Nothing is paid here, so there is no approval step: the number is
    # just the honest version of "joins".
    from ambassadors import qualify

    real = {}
    joined = (
        InviteEvent.objects
        .filter(kind=InviteEvent.JOINED, joined_user__isnull=False)
        .select_related('inviter', 'joined_user', 'joined_user__profile')
    )
    for event in joined:
        if qualify.measure(event.joined_user)['qualifies']:
            real[event.inviter.username] = real.get(event.inviter.username, 0) + 1
    for row in rows:
        row['real'] = real.get(row['inviter__username'], 0)

    totals = {
        'people': len(rows),
        'copies': sum(r['copies'] for r in rows),
        'opens': sum(r['opens'] for r in rows),
        'joins': sum(r['joins'] for r in rows),
    }
    # The one number the page exists to answer: of everyone who opened an
    # invite, how many actually signed up.
    totals['conversion'] = (
        round(totals['joins'] * 100 / totals['opens'], 1) if totals['opens'] else 0
    )
    totals['real'] = sum(r['real'] for r in rows)

    recent = (
        InviteEvent.objects
        .filter(kind=InviteEvent.JOINED)
        .select_related('inviter', 'joined_user')
        .order_by('-created')[:25]
    )

    return render(request, 'invites/dashboard.html', {
        'rows': rows,
        'totals': totals,
        'recent': recent,
        'reveal_at': REVEAL_REMAINING_AT,
        'retention_days': qualify.RETENTION_DAYS,
        'min_posts': qualify.MIN_POSTS,
        'min_interactions': qualify.MIN_INTERACTIONS,
    })
