"""The ambassador programme: the link, the claim, and the two admin pages.

Read `models.py` first — it says what is being defended and why. This file is
where those rules are enforced, and the shape to keep in mind is that there
are exactly two ways into the numbers:

    /a/<code>                     a person opens the link      (recorded)
    POST /api/ambassadors/claim/  a new account claims a token (checked hard)

Nothing else writes. The admin pages only move rows between statuses, and
every move records who did it.
"""

import json
import secrets
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.cache import cache_control
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_http_methods

from accounts.auth import require_authenticated_user
from accounts.ratelimit import client_ip, rate_limited

from . import qualify
from .models import (
    Ambassador, AmbassadorClick, AmbassadorSignup, ip_hash, new_code, visitor_hash,
)

User = get_user_model()

#: An account older than this cannot be a referral, whatever token it presents.
MAX_ACCOUNT_AGE = timezone.timedelta(days=3)
#: Tolerance for an account that looks a moment older than the click that is
#: supposed to have produced it — clock skew, not time travel.
CLOCK_SKEW = timezone.timedelta(minutes=10)


# ── Admin gate ───────────────────────────────────────────────────────────────

def _is_admin(request):
    from web.views import _analytics_admin
    return _analytics_admin(request)


def _login_or_none(request, template):
    """Shared sign-in for both ambassador pages. Returns a response when the
    caller is not (yet) an admin, or None when they are."""
    from django.contrib.auth import authenticate

    from accounts.serializers import ensure_profile
    from web.views import ANALYTICS_SESSION_KEY

    error = ''
    if request.method == 'POST' and not _is_admin(request):
        # These pages hold payout figures. Brute force is worth slowing down.
        if rate_limited(f'ambassadors:{client_ip(request)}', limit=8, window_seconds=900):
            error = 'Too many attempts. Try again later.'
        else:
            user = authenticate(
                username=(request.POST.get('username') or '').strip(),
                password=request.POST.get('password') or '',
            )
            if user is not None and ensure_profile(user).is_admin:
                request.session[ANALYTICS_SESSION_KEY] = True
                # Money moves from these pages, so every approval has to carry
                # a name. The analytics session only stores a flag, which is
                # why anything that writes here re-checks for this and sends
                # the caller back through the form when it is missing.
                request.session[ADMIN_USER_KEY] = user.username
                request.session.set_expiry(60 * 60 * 8)
                return redirect(request.path)
            error = 'Those details are not valid here.'

    if not _is_admin(request):
        return render(request, template, {'error': error}, status=200)
    return None


#: Set by this app's own sign-in. A session started at /analytics has the
#: admin flag but no name, which is not enough to attribute a payout decision.
ADMIN_USER_KEY = 'neat_ambassadors_admin_user'


def _admin_user(request):
    """The signed-in admin, for the audit fields, or None when the session
    cannot say who they are."""
    username = request.session.get(ADMIN_USER_KEY, '')
    if not username:
        return None
    return User.objects.filter(username=username).first()


def _require_named_admin(request):
    """An unattributable session is sent back through the form before it can
    change anything. Read-only viewing is unaffected."""
    from web.views import ANALYTICS_SESSION_KEY

    if _admin_user(request) is not None:
        return None
    request.session.pop(ANALYTICS_SESSION_KEY, None)
    return redirect(request.path)


# ── The link ─────────────────────────────────────────────────────────────────

@require_http_methods(['GET', 'HEAD'])
@cache_control(no_store=True)
def ambassador_landing(request, code):
    """neatapp.gr/a/<code> — the page an ambassador's audience lands on.

    An unknown code renders the ordinary invite page rather than a 404: a
    mistyped link should still sell the app, and a scraper should not be able
    to tell a real code from a wrong one by the status line.
    """
    from invites.views import _avatar_for_page, _stores_for

    ambassador = Ambassador.objects.filter(code=code, is_active=True).first()

    # The token minted here is the whole mechanism. It rides out on the Play
    # URL (Android hands it back on first launch) and onto the clipboard (iOS
    # has nothing else), and if neither survives, the click it belongs to is
    # still what the network fallback matches against.
    click_token = ''
    if ambassador is not None and request.method == 'GET':
        if not rate_limited(f'amb-click:{client_ip(request)}', limit=60, window_seconds=3600):
            click = AmbassadorClick.objects.create(
                ambassador=ambassador,
                source=AmbassadorClick.WEB,
                token=secrets.token_urlsafe(32),
                visitor=visitor_hash(client_ip(request), request.headers.get('User-Agent', '')[:400]),
                ip=ip_hash(client_ip(request)),
                user_agent=request.headers.get('User-Agent', '')[:300],
            )
            click_token = click.token

    # No city, deliberately. An ambassador posts one link to everyone, and the
    # people who follow it live anywhere — plenty of them in Αθήνα or
    # Θεσσαλονίκη, which are already open. Telling those readers they are
    # helping unlock a city is at best irrelevant and at worst wrong, so this
    # page sells the app and says nothing about locks.
    who = ambassador.name if ambassador else 'Ένας φίλος σου'
    lede = ('Η Neat χωρίζει την Ελλάδα σε πόλεις — κάθε πόλη με το δικό της '
            'feed, με ό,τι συμβαίνει δίπλα σου.')
    description = lede

    # The same page the invite links use — one look for one product.
    return render(request, 'invites/invite.html', {
        'inviter': '',
        'avatar': _avatar_for_page(ambassador.user, request) if ambassador and ambassador.user else '',
        'initial': (who[:1].upper() or 'N'),
        'stores': _stores_for(request.headers.get('User-Agent', ''), click_token),
        # Written to the pasteboard when the download button is pressed, and
        # read back by the app on first launch. Only ever a click token, which
        # is single-use and worth nothing to anyone else.
        'clipboard_token': f'neat_ct={click_token}' if click_token else '',
        'who': who,
        'title': f'{who} σε προσκαλεί στη Neat',
        'description': description,
        'city': None,
        'lede': lede,
        # Left off for the same reason as the city: it is an answer to
        # "when does my city open", which this page no longer raises.
        'show_greece_note': False,
        'canonical': request.build_absolute_uri(request.path),
    })


# ── The claim ────────────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(['POST'])
def mint_token(request):
    """An installed app asks for something it can claim with.

    Unauthenticated because it happens before the account exists. The token it
    returns is worth nothing on its own: it buys one attempt at a claim, by an
    account that does not exist yet, within fourteen days, and the claim is
    where the checks are.
    """
    ip = client_ip(request)
    if rate_limited(f'amb-mint:{ip}', limit=20, window_seconds=3600):
        return JsonResponse({'error': 'Too many requests'}, status=429)

    try:
        body = json.loads(request.body or b'{}')
    except ValueError:
        body = {}

    code = (body.get('code') or '').strip()
    ambassador = Ambassador.objects.filter(code=code, is_active=True).first()
    if ambassador is None:
        # Same shape of answer as success, so the endpoint cannot be used to
        # test which codes exist.
        return JsonResponse({'token': ''})

    click = AmbassadorClick.objects.create(
        ambassador=ambassador,
        source=AmbassadorClick.APP,
        token=secrets.token_urlsafe(32),
        visitor=visitor_hash(ip, request.headers.get('User-Agent', '')[:400]),
        ip=ip_hash(ip),
        user_agent=request.headers.get('User-Agent', '')[:300],
    )
    return JsonResponse({'token': click.token})


@csrf_exempt
@require_http_methods(['POST'])
def claim(request):
    """A freshly created account presents a token.

    Everything here is a refusal waiting to happen, and that is the design.
    The happy path writes exactly one row, inside a transaction that locks the
    click, so two simultaneous claims cannot both win it.
    """
    user = require_authenticated_user(request)
    if user is None:
        return JsonResponse({'error': 'Authentication required'}, status=401)

    if rate_limited(f'amb-claim:{user.id}', limit=10, window_seconds=3600):
        return JsonResponse({'error': 'Too many requests'}, status=429)

    try:
        body = json.loads(request.body or b'{}')
    except ValueError:
        body = {}
    token = (body.get('token') or '').strip()
    if not token:
        return JsonResponse({'error': 'token required'}, status=400)

    now = timezone.now()

    # Refusals that are nobody's fault and cannot be fixed by retrying get a
    # plain ok:false — an error would only make the app try again forever.
    if now - user.date_joined > MAX_ACCOUNT_AGE:
        return JsonResponse({'ok': True, 'credited': False, 'reason': 'account_not_new'})
    existing = AmbassadorSignup.objects.filter(user=user).first()
    # A network match is a guess the server made in the absence of anything
    # better. A token is the better thing, so it replaces the guess — unless a
    # human has already ruled on it, in which case their decision stands.
    replaceable = (
        existing is not None
        and existing.claim_method == AmbassadorSignup.NETWORK
        and existing.status not in AmbassadorSignup.FINAL
    )
    if existing is not None and not replaceable:
        return JsonResponse({'ok': True, 'credited': False, 'reason': 'already_credited'})

    with transaction.atomic():
        # Any unused click the server minted, web or app: a token that left
        # on the Play URL or the clipboard belongs to a click recorded when
        # somebody actually read the page, which is no weaker than one minted
        # by the app itself.
        click = (
            AmbassadorClick.objects
            .select_for_update()
            .select_related('ambassador')
            .filter(token=token, used_at__isnull=True)
            .first()
        )
        if click is None:
            return JsonResponse({'ok': True, 'credited': False, 'reason': 'token_unusable'})
        if click.expired:
            return JsonResponse({'ok': True, 'credited': False, 'reason': 'token_expired'})

        ambassador = click.ambassador
        if not ambassador.is_active:
            return JsonResponse({'ok': True, 'credited': False, 'reason': 'inactive'})

        # An ambassador cannot be their own recruit.
        if ambassador.user_id and ambassador.user_id == user.id:
            return JsonResponse({'ok': True, 'credited': False, 'reason': 'self_referral'})

        # The account must have been created after the click that claims to
        # have produced it.
        if user.date_joined < click.created - CLOCK_SKEW:
            return JsonResponse({'ok': True, 'credited': False, 'reason': 'predates_click'})

        flags = []
        here = ip_hash(client_ip(request))
        if here != click.ip:
            # Not damning on its own — wifi at the shop, cellular at home —
            # but worth a human glance before it is worth money.
            flags.append('claim IP differs from the IP that asked for the token')

        if AmbassadorClick.objects.filter(
            visitor=click.visitor, signup__isnull=False,
        ).exclude(id=click.id).exclude(signup__user_id=user.id).exists():
            # One phone, two *different* accounts. Legitimate occasionally (a
            # shared family iPad); the commonest shape of farming otherwise.
            #
            # Excluding this account's own earlier clicks matters for the
            # upgrade path: replacing a network guess with a real token means
            # the row's previous click is still sitting on this device, and
            # without this it would flag the account for colliding with itself.
            flags.append('another signup was already credited from this device')

        day = now - timezone.timedelta(days=1)
        recent = AmbassadorSignup.objects.filter(
            ambassador=ambassador, created__gte=day,
        ).count()
        if recent >= ambassador.daily_cap:
            flags.append(f'over the daily cap of {ambassador.daily_cap}')

        method = (body.get('method') or '').strip()
        if method not in dict(AmbassadorSignup.METHODS):
            method = AmbassadorSignup.TOKEN

        if replaceable:
            # Free the click the guess was holding, so it is not left looking
            # spent, and re-point the row at the evidence that actually arrived.
            old_click = existing.click
            existing.ambassador = ambassador
            existing.click = click
            existing.claim_method = method
            existing.status = AmbassadorSignup.FLAGGED if flags else AmbassadorSignup.PENDING
            existing.flags = '\n'.join(flags)
            existing.save(update_fields=['ambassador', 'click', 'claim_method',
                                         'status', 'flags'])
            signup = existing
            AmbassadorClick.objects.filter(id=old_click.id).update(used_at=None)
        else:
            signup = AmbassadorSignup.objects.create(
                ambassador=ambassador,
                click=click,
                user=user,
                status=AmbassadorSignup.FLAGGED if flags else AmbassadorSignup.PENDING,
                claim_method=method,
                flags='\n'.join(flags),
            )
        click.used_at = now
        click.save(update_fields=['used_at'])

    # Measured immediately so a reviewer never sees an empty row, even though
    # a brand-new account cannot possibly pass the day-three test yet.
    qualify.refresh(signup)
    return JsonResponse({'ok': True, 'credited': True})


# ── /ambassadors ─────────────────────────────────────────────────────────────

@csrf_protect
@require_http_methods(['GET', 'POST'])
def manage(request):
    gate = _login_or_none(request, 'ambassadors/login.html')
    if gate is not None:
        return gate

    if request.GET.get('logout'):
        from web.views import ANALYTICS_SESSION_KEY
        request.session.pop(ANALYTICS_SESSION_KEY, None)
        return redirect('ambassadors_manage')

    error = ''
    if request.method == 'POST':
        anonymous = _require_named_admin(request)
        if anonymous is not None:
            return anonymous
        action = request.POST.get('action') or 'create'
        if action == 'create':
            name = (request.POST.get('name') or '').strip()
            if not name:
                error = 'A name is required.'
            else:
                raw_code = (request.POST.get('code') or '').strip().lower()
                if raw_code:
                    # A hand-written code still has to be unguessable-ish and
                    # url-safe; short or taken is refused rather than mangled.
                    ok = (len(raw_code) >= 6 and
                          all(c.isalnum() or c in '-_' for c in raw_code) and
                          not Ambassador.objects.filter(code=raw_code).exists())
                    if not ok:
                        error = 'That code is too short, has odd characters, or is taken.'
                code = raw_code or new_code(name)
                if not error:
                    try:
                        rate = Decimal(request.POST.get('rate') or '0')
                    except InvalidOperation:
                        rate = Decimal('0')
                    linked = User.objects.filter(
                        username__iexact=(request.POST.get('linked') or '').strip(),
                    ).first()
                    Ambassador.objects.create(
                        name=name,
                        code=code,
                        contact=(request.POST.get('contact') or '').strip()[:200],
                        note=(request.POST.get('note') or '').strip(),
                        user=linked,
                        payout_per_signup=max(rate, Decimal('0')),
                        daily_cap=max(int(request.POST.get('cap') or 25), 1),
                        created_by=_admin_user(request),
                    )
                    return redirect('ambassadors_manage')
        elif action in ('deactivate', 'activate'):
            Ambassador.objects.filter(id=request.POST.get('id') or 0).update(
                is_active=(action == 'activate'),
            )
            return redirect('ambassadors_manage')

    rows = (
        Ambassador.objects
        .annotate(
            # Not `clicks`/`signups`: those names are already the reverse
            # accessors for the two related models, and Django refuses an
            # annotation that would shadow one.
            click_count=Count('clicks', distinct=True),
            signup_count=Count('signups', distinct=True),
            approved=Count('signups', filter=Q(signups__status__in=[
                AmbassadorSignup.APPROVED, AmbassadorSignup.PAID]), distinct=True),
        )
        .order_by('-is_active', '-created')
    )
    return render(request, 'ambassadors/manage.html', {'rows': rows, 'error': error})


# ── /ambassadorsstats ────────────────────────────────────────────────────────

@csrf_protect
@require_http_methods(['GET', 'POST'])
def stats(request):
    gate = _login_or_none(request, 'ambassadors/login.html')
    if gate is not None:
        return gate

    if request.GET.get('logout'):
        from web.views import ANALYTICS_SESSION_KEY
        request.session.pop(ANALYTICS_SESSION_KEY, None)
        return redirect('ambassadors_stats')

    if request.method == 'POST':
        anonymous = _require_named_admin(request)
        if anonymous is not None:
            return anonymous
        action = request.POST.get('action')
        signup = AmbassadorSignup.objects.filter(
            id=request.POST.get('id') or 0,
        ).select_related('ambassador').first()
        if signup is not None:
            admin = _admin_user(request)
            if action == 'approve' and signup.status in (
                AmbassadorSignup.QUALIFIED, AmbassadorSignup.FLAGGED,
            ):
                # The rate is copied now, so a later change to the ambassador's
                # rate cannot rewrite what was already owed.
                signup.status = AmbassadorSignup.APPROVED
                signup.amount = signup.ambassador.payout_per_signup
                signup.reviewed_by = admin
                signup.reviewed_at = timezone.now()
                signup.save(update_fields=['status', 'amount', 'reviewed_by', 'reviewed_at'])
            elif action == 'reject' and signup.status != AmbassadorSignup.PAID:
                signup.status = AmbassadorSignup.REJECTED
                signup.reviewed_by = admin
                signup.reviewed_at = timezone.now()
                signup.save(update_fields=['status', 'reviewed_by', 'reviewed_at'])
            elif action == 'paid' and signup.status == AmbassadorSignup.APPROVED:
                signup.status = AmbassadorSignup.PAID
                signup.paid_at = timezone.now()
                signup.save(update_fields=['status', 'paid_at'])
        return redirect('ambassadors_stats')

    # Every row still in play is re-measured on load. At this volume that is
    # cheaper than a scheduled job and impossible to forget to run.
    qualify.refresh_all()

    rows = (
        Ambassador.objects
        .annotate(
            click_count=Count('clicks', distinct=True),
            app_clicks=Count('clicks', filter=Q(clicks__source=AmbassadorClick.APP), distinct=True),
            signup_count=Count('signups', distinct=True),
            pending=Count('signups', filter=Q(signups__status=AmbassadorSignup.PENDING), distinct=True),
            qualified=Count('signups', filter=Q(signups__status=AmbassadorSignup.QUALIFIED), distinct=True),
            approved=Count('signups', filter=Q(signups__status=AmbassadorSignup.APPROVED), distinct=True),
            paid=Count('signups', filter=Q(signups__status=AmbassadorSignup.PAID), distinct=True),
            flagged=Count('signups', filter=Q(signups__status=AmbassadorSignup.FLAGGED), distinct=True),
            rejected=Count('signups', filter=Q(signups__status=AmbassadorSignup.REJECTED), distinct=True),
            owed=Sum('signups__amount', filter=Q(signups__status=AmbassadorSignup.APPROVED)),
            settled=Sum('signups__amount', filter=Q(signups__status=AmbassadorSignup.PAID)),
        )
        .order_by('-approved', '-qualified', '-signup_count')
    )

    review = (
        AmbassadorSignup.objects
        .exclude(status__in=[AmbassadorSignup.PAID, AmbassadorSignup.REJECTED])
        .select_related('ambassador', 'user')
        .order_by('-created')[:60]
    )

    totals = {
        'ambassadors': len(rows),
        'signups': sum(r.signup_count for r in rows),
        'qualified': sum(r.qualified for r in rows),
        'approved': sum(r.approved for r in rows),
        'owed': sum((r.owed or 0) for r in rows),
        'settled': sum((r.settled or 0) for r in rows),
    }

    return render(request, 'ambassadors/stats.html', {
        'rows': rows,
        'review': review,
        'totals': totals,
        'retention_days': qualify.RETENTION_DAYS,
        'min_posts': qualify.MIN_POSTS,
        'min_interactions': qualify.MIN_INTERACTIONS,
    })
