"""The ambassador programme: the link, the claim, and the two admin pages.

Read `models.py` first — it says what is being defended and why. This file is
where those rules are enforced, and the shape to keep in mind is that there
are exactly two ways into the numbers:

    /a/<code>                     a person opens the link      (recorded)
    POST /api/ambassadors/claim/  a new account claims a token (checked hard)

Nothing else writes. The admin pages only move rows between statuses, and
every move records who did it.
"""

import hashlib
import json
import os
import random
import secrets
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape
from django.views.decorators.cache import cache_control
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_http_methods

from accounts.auth import require_authenticated_user
from accounts.ratelimit import client_ip, rate_limited
from lockedcities.cities import APP_CITIES

from . import addresses
from . import content as content_matching
from . import qr as qr_module
from . import qualify
from .models import (
    Ambassador, AmbassadorClick, AmbassadorContent, AmbassadorSignup, ip_hash,
    new_code, visitor_hash,
)

User = get_user_model()

#: An account older than this cannot be a referral, whatever token it presents.
MAX_ACCOUNT_AGE = timezone.timedelta(days=3)

#: Codes that would collide with something else on the site if /a/ ever moved,
#: or that simply read as a mistake on a poster.
RESERVED_CODES = {
    'invite', 'invites', 'post', 'posts', 'api', 'admin', 'analytics',
    'creator', 'ambassadors', 'ambassadorsstats', 'stoplocked', 'privacy',
    'terms', 'health', 'runbook', 'media', 'static', 'app', 'www',
}
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

#: The landing page as tools/deploy_web.sh last published it. Re-read only when
#: the file changes: it is ~1.7 MB of inlined screenshots.
_landing = {'key': None, 'html': None}


def _landing_html():
    """neatapp.gr's own index.html, or None where it has not been published
    (a dev checkout, a test run)."""
    from web.views import WEB_ROOT

    path = os.path.join(WEB_ROOT, 'index.html')
    try:
        key = (path, os.stat(path).st_mtime_ns)
        if _landing['key'] != key:
            with open(path, encoding='utf-8') as f:
                _landing['html'] = f.read()
            _landing['key'] = key
    except OSError:
        return None
    return _landing['html']


def _with_click_token(html, token):
    """The landing page, with this click's token where the app looks for it.

    Nothing the reader can see changes. The Play badge carries the token as
    the install referrer, and pressing either badge puts it on the pasteboard
    for iOS — the same two routes the invite page used.
    """
    from urllib.parse import quote

    from web.views import PLAY_STORE_URL

    play = f'{PLAY_STORE_URL}&referrer={quote(f"neat_ct={token}", safe="")}'
    html = html.replace(f'href="{PLAY_STORE_URL}"', f'href="{escape(play)}"')

    script = (
        '<script>\n'
        # Says a browser rendered this, which is what separates a reader from
        # a link preview. Sent before anything else so a reader who leaves
        # immediately still counts.
        f'(function () {{ var b = JSON.stringify({{token: {json.dumps(token)}}});\n'
        '  try { if (navigator.sendBeacon) { navigator.sendBeacon("/api/ambassadors/seen/", b); }\n'
        '        else { fetch("/api/ambassadors/seen/", {method: "POST", body: b, keepalive: true}); }\n'
        '  } catch (e) {} })();\n'
        'document.querySelectorAll(".store-badge").forEach(function (a) {\n'
        '  a.addEventListener("click", function () {\n'
        f'    try {{ navigator.clipboard.writeText({json.dumps(f"neat_ct={token}")}); }} catch (e) {{}}\n'
        '  });\n'
        '});\n'
        '</script>\n'
    )
    head, body_end, tail = html.rpartition('</body>')
    if not body_end:
        return html + script
    return head + script + body_end + tail


@require_http_methods(['GET', 'HEAD'])
@cache_control(no_store=True)
def ambassador_landing(request, code):
    """neatapp.gr/a/<code> — the page an ambassador's audience lands on.

    It is the ordinary landing page, not a page about the ambassador: the link
    counts the visit and hands out a click token, and the reader sees the same
    neatapp.gr as anyone else. An unknown code gets the same page with no
    token, so a scraper cannot tell a real code from a wrong one.
    """
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
                ip_address=client_ip(request)[:45],
                user_agent=request.headers.get('User-Agent', '')[:300],
            )
            click_token = click.token

    html = _landing_html()
    if html is None:
        # Nothing published here to show. The click is already counted, and
        # the network fallback can still match it.
        return redirect('/')
    if click_token:
        html = _with_click_token(html, click_token)
    return HttpResponse(html)


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
        confirmed=True,
        token=secrets.token_urlsafe(32),
        visitor=visitor_hash(ip, request.headers.get('User-Agent', '')[:400]),
        ip=ip_hash(ip),
        ip_address=ip[:45],
        user_agent=request.headers.get('User-Agent', '')[:300],
    )
    return JsonResponse({'token': click.token})


@csrf_exempt
@require_http_methods(['POST'])
def confirm_open(request):
    """The landing page reporting that a browser rendered it.

    Unauthenticated, like the other two: it happens before any account exists.
    It is worth nothing to forge — the most a made-up token could do is mark a
    click that already happened as having been seen, which buys the sender no
    money and no attribution, since a claim is settled by the token itself.
    """
    ip = client_ip(request)
    if rate_limited(f'amb-seen:{ip}', limit=120, window_seconds=3600):
        return JsonResponse({})

    try:
        body = json.loads(request.body or b'{}')
    except ValueError:
        body = {}

    token = (body.get('token') or '').strip()
    if token:
        # An update, not a fetch-then-save: the same page can fire this twice
        # (a reload, a restored tab), and a flag being set again is nothing.
        AmbassadorClick.objects.filter(
            token=token, source=AmbassadorClick.WEB).update(confirmed=True)
    # The same empty answer either way, so this cannot be used to test which
    # tokens exist.
    return JsonResponse({})


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
        and existing.claim_method in AmbassadorSignup.REPLACEABLE
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
        address = addresses.address_for(user, client_ip(request))
        auto_status, auto_flag = addresses.verdict(address, ambassador, user)

        here = ip_hash(client_ip(request))
        if here != click.ip:
            # Not damning on its own — wifi at the shop, cellular at home —
            # but worth a human glance before it is worth money.
            flags.append('claim IP differs from the IP that asked for the token')

        if click.visitor and AmbassadorClick.objects.filter(
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

        if auto_flag:
            flags.insert(0, auto_flag)

        if replaceable:
            # Free the click the guess was holding, so it is not left looking
            # spent, and re-point the row at the evidence that actually arrived.
            # A content credit holds no click at all, only a post.
            old_click_id = existing.click_id
            existing.ambassador = ambassador
            existing.click = click
            existing.content = None
            existing.claim_method = method
            existing.status = auto_status or (
                AmbassadorSignup.FLAGGED if flags else AmbassadorSignup.PENDING)
            existing.flags = '\n'.join(flags)
            existing.ip_address = address
            existing.save(update_fields=['ambassador', 'click', 'content', 'claim_method',
                                         'status', 'flags', 'ip_address'])
            signup = existing
            if old_click_id:
                AmbassadorClick.objects.filter(id=old_click_id).update(used_at=None)
        else:
            signup = AmbassadorSignup.objects.create(
                ambassador=ambassador,
                click=click,
                user=user,
                status=auto_status or (
                    AmbassadorSignup.FLAGGED if flags else AmbassadorSignup.PENDING),
                claim_method=method,
                flags='\n'.join(flags),
                ip_address=address,
            )
        click.used_at = now
        click.save(update_fields=['used_at'])

    # Measured immediately so a reviewer never sees an empty row, even though
    # a brand-new account cannot possibly pass the day-three test yet.
    qualify.refresh(signup)
    return JsonResponse({'ok': True, 'credited': True})


# ── The creator's own dashboard ───────────────────────────────────────────────

def _creator_or_404(key):
    from django.http import Http404

    ambassador = Ambassador.objects.filter(dashboard_key=key).first()
    if ambassador is None:
        raise Http404('No such dashboard')
    return ambassador


def _log_content(ambassador, data):
    """Validate and store a post a creator logged. Returns an error, or ''."""
    from datetime import datetime

    platform = (data.get('platform') or '').strip()
    if platform not in dict(AmbassadorContent.PLATFORMS):
        return 'Διάλεξε πλατφόρμα.'

    url = (data.get('url') or '').strip()
    if not (url.startswith('https://') or url.startswith('http://')) or len(url) > 500:
        return 'Βάλε το link της ανάρτησης — χωρίς αυτό δεν μπορεί να ελεγχθεί.'

    city = (data.get('city') or '').strip()
    if city not in APP_CITIES:
        return 'Διάλεξε πόλη από τη λίστα.'

    try:
        naive = datetime.strptime((data.get('uploaded_at') or '').strip(), '%Y-%m-%dT%H:%M')
    except ValueError:
        return 'Βάλε ημερομηνία και ώρα ανάρτησης.'
    uploaded_at = timezone.make_aware(naive, timezone.get_current_timezone())

    now = timezone.now()
    # A post from the future is a typo at best. One from long ago is a request
    # to be credited with people who joined before anyone was keeping track.
    if uploaded_at > now + timezone.timedelta(minutes=10):
        return 'Η ώρα ανάρτησης δεν μπορεί να είναι στο μέλλον.'
    if uploaded_at < now - content_matching.LOOKBACK:
        return 'Η ανάρτηση είναι πολύ παλιά για να καταχωρηθεί.'

    if AmbassadorContent.objects.filter(ambassador=ambassador, url=url).exists():
        return 'Αυτή η ανάρτηση έχει ήδη καταχωρηθεί.'

    AmbassadorContent.objects.create(
        ambassador=ambassador,
        platform=platform,
        url=url,
        city=city,
        uploaded_at=uploaded_at,
        note=(data.get('note') or '').strip()[:300],
    )
    return ''


@csrf_protect
@require_http_methods(['GET', 'HEAD', 'POST'])
@cache_control(no_store=True)
def creator_dashboard(request, key):
    """What an ambassador sees about their own work.

    No login, because most ambassadors have no Neat account to log in with —
    the key in the URL is the credential. It is unguessable, the page is
    noindex, and it shows one person's own figures and nothing else.

    Money is reported exactly as the payout dashboard reports it, from the
    same rows: `amount` is copied onto a signup at approval, so what a creator
    is told they are owed cannot drift from what you approved.
    """
    ambassador = _creator_or_404(key)

    # Two things a creator may do here: change the line on their own poster,
    # and log a post they put up. Everything else on this page is a report.
    content_error = ''
    if request.method == 'POST':
        action = request.POST.get('action') or 'slogan'

        if action == 'content':
            if rate_limited(f'creator-content:{ambassador.id}', limit=40, window_seconds=86400):
                content_error = 'Πολλές καταχωρήσεις για σήμερα. Δοκίμασε αύριο.'
            else:
                content_error = _log_content(ambassador, request.POST)
                if not content_error:
                    return redirect(
                        reverse('creator_dashboard', kwargs={'key': key}) + '#content')
        else:
            if rate_limited(f'creator-slogan:{ambassador.id}', limit=30, window_seconds=3600):
                return redirect('creator_dashboard', key=key)
            ambassador.custom_slogan = (request.POST.get('slogan') or '').strip()[:60]
            ambassador.save(update_fields=['custom_slogan'])
            # Back to the tab they were on, not to the first one.
            return redirect(
                reverse('creator_dashboard', kwargs={'key': key}) + '?tab=custom')

    # Credit anyone who has joined inside one of this creator's verified post
    # windows since the page was last opened.
    content_matching.match(ambassador)

    # Re-measure anything still in play, so a creator refreshing this page sees
    # today's answer rather than the one from when someone last looked.
    qualify.refresh_all(
        AmbassadorSignup.objects
        .filter(ambassador=ambassador)
        .exclude(status__in=AmbassadorSignup.FINAL)
    )

    # A different loud line every time this page is opened, so a creator can
    # print a mix instead of a hundred identical stickers. The index travels
    # in the image and download URLs, so what they see is what they get.
    loud = random.randrange(len(qr_module.AGGRESSIVE_SLOGANS))
    tab = request.GET.get('tab')
    if tab not in qr_module.STYLES:
        tab = qr_module.BASIC
    custom_rev = hashlib.sha256(
        ambassador.custom_slogan.encode('utf-8')).hexdigest()[:8]

    signups = (
        AmbassadorSignup.objects
        .filter(ambassador=ambassador)
        .select_related('user')
        .order_by('-created')
    )
    # Approved and paid only. A creator is never shown a person who has not
    # been approved — not as a name, not as a count, not as a hint that
    # something is pending. Anything else is a promise the review step has not
    # made yet, and an argument waiting to happen when it is refused.
    approved = [s for s in signups
                if s.status in (AmbassadorSignup.APPROVED, AmbassadorSignup.PAID)]

    owed = sum((s.amount for s in signups if s.status == AmbassadorSignup.APPROVED),
               Decimal('0'))
    paid = sum((s.amount for s in signups if s.status == AmbassadorSignup.PAID),
               Decimal('0'))

    return render(request, 'ambassadors/creator.html', {
        'ambassador': ambassador,
        'approved': approved,
        'owed': owed,
        'paid': paid,
        'earned': owed + paid,
        'rate': ambassador.payout_per_signup,
        'opens': AmbassadorClick.objects.filter(
            ambassador=ambassador, confirmed=True).count(),
        'qr_version': qr_module.DESIGN_VERSION,
        # Every style, so the page can show what each one actually looks like
        # rather than describing it.
        'styles': [
            {'id': qr_module.BASIC, 'name': 'Βασικό', 'index': '',
             'line': qr_module.slogan_for(qr_module.BASIC)},
            {'id': qr_module.AGGRESSIVE, 'name': 'Δυνατό', 'index': loud,
             'line': qr_module.slogan_for(qr_module.AGGRESSIVE, ambassador.code, '', loud)},
            {'id': qr_module.CUSTOM, 'name': 'Δικό σου', 'index': '',
             'line': ambassador.custom_slogan or qr_module.BASIC_SLOGAN},
        ],
        # Which tab to open on. Saving a slogan comes back here, and coming
        # back to the wrong tab is how you lose the thing you just did.
        'tab': tab,
        # Changes whenever the saved line does, so the new poster is fetched
        # rather than read out of the browser's cache.
        'custom_rev': custom_rev,
        'custom_slogan': ambassador.custom_slogan,
        'slogan_max': 60,
        'content_error': content_error,
        'content_form': request.POST if content_error else {},
        'platforms': AmbassadorContent.PLATFORMS,
        'cities': APP_CITIES,
        'posts': [
            {
                'post': post,
                # Approved people only, for the same reason the list below
                # shows nobody unapproved: a count is a promise too.
                'brought': post.signups.filter(status__in=[
                    AmbassadorSignup.APPROVED, AmbassadorSignup.PAID]).count(),
            }
            for post in AmbassadorContent.objects.filter(ambassador=ambassador)[:30]
        ],
        'retention_days': qualify.RETENTION_DAYS,
        'min_posts': qualify.MIN_POSTS,
        'min_interactions': qualify.MIN_INTERACTIONS,
    })


@require_http_methods(['GET', 'HEAD'])
def creator_qr(request, key, fmt):
    """The same link as something printable. `?download=1` to save it."""
    from django.http import HttpResponse

    from . import qr

    ambassador = _creator_or_404(key)
    download = request.GET.get('download') == '1'

    style = request.GET.get('style') or qr.BASIC
    if style not in qr.STYLES:
        style = qr.BASIC

    try:
        index = int(request.GET.get('i')) if request.GET.get('i') else None
    except ValueError:
        index = None

    # `text` previews a line the creator is still typing, before they save it.
    # Only reachable with their own key, and never stored from here.
    live = (request.GET.get('text') or '').strip()[:60] if style == qr.CUSTOM else ''
    slogan = qr.slogan_for(style, ambassador.code,
                           live or ambassador.custom_slogan, index)
    stem = f'neat-{ambassador.code}-{style}'

    if fmt == 'png':
        body, content_type, name = qr.png(ambassador.link), 'image/png', f'{stem}.png'
    elif fmt == 'pdf':
        body, content_type, name = (qr.pdf(ambassador.link, slogan),
                                    'application/pdf', f'{stem}.pdf')
    else:
        body, content_type, name = (qr.svg(ambassador.link, slogan),
                                    'image/svg+xml', f'{stem}.svg')

    response = HttpResponse(body, content_type=content_type)
    if download:
        response['Content-Disposition'] = f'attachment; filename="{name}"'
    elif fmt == 'pdf':
        # Inline, so it opens in the browser's PDF viewer ready to print —
        # with a real file name if they save it from there instead.
        response['Content-Disposition'] = f'inline; filename="{name}"'
    # Short, and deliberately so. The link inside rarely changes but the
    # poster around it does, and a day-long cache meant a redesign was
    # invisible to the person it was for. ?v= retires old copies outright;
    # this keeps even an un-versioned request honest.
    response['Cache-Control'] = 'public, max-age=300'
    return response


# ── Public posters ───────────────────────────────────────────────────────────
#
# Posters live under the public link, /a/<code>/…, and never under the
# creator's private dashboard key. Everything on a poster is public already —
# the link it encodes and the line printed under it — so nothing is lost by
# that, and one thing is gained: a browser prints the page address in its
# footer, and a print page under /creator/<key>/ put the private key on paper,
# stuck to a wall, for anyone to read.

def _poster_for(code, request):
    """The ambassador and the line a poster request is asking for."""
    from django.http import Http404

    from . import qr

    # Inactive ambassadors included. Their dashboard still shows them their
    # own code, so the print button there has to work; and the link on the
    # poster is harmless while they are inactive — it opens the ordinary
    # invite page and credits nobody.
    ambassador = Ambassador.objects.filter(code=code).first()
    if ambassador is None:
        raise Http404('No such poster')
    style = request.GET.get('style') or qr.BASIC
    if style not in qr.STYLES:
        style = qr.BASIC
    try:
        index = int(request.GET['i']) if request.GET.get('i') else None
    except ValueError:
        index = None
    # Only the saved custom line: this URL is public, and a `text` parameter
    # here would let anyone print anything under a creator's name.
    slogan = qr.slogan_for(style, ambassador.code, ambassador.custom_slogan, index)
    return ambassador, style, index, slogan


@require_http_methods(['GET', 'HEAD'])
def ambassador_poster(request, code, fmt):
    from django.http import HttpResponse

    from . import qr

    ambassador, style, index, slogan = _poster_for(code, request)
    name = f'neat-{ambassador.code}-{style}.{fmt}'
    if fmt == 'pdf':
        response = HttpResponse(qr.pdf(ambassador.link, slogan), content_type='application/pdf')
        response['Content-Disposition'] = f'inline; filename="{name}"'
    else:
        response = HttpResponse(qr.poster_png(ambassador.link, slogan), content_type='image/png')
    response['Cache-Control'] = 'public, max-age=300'
    return response


@require_http_methods(['GET', 'HEAD'])
@cache_control(no_store=True)
def ambassador_print(request, code):
    """A page whose only job is to print one poster on one sheet."""
    from urllib.parse import urlencode

    from . import qr

    ambassador, style, index, slogan = _poster_for(code, request)
    params = {'style': style, 'v': qr.DESIGN_VERSION}
    if index is not None:
        params['i'] = index
    if style == qr.CUSTOM:
        # Changes with the saved line, so a fresh one is never read from cache.
        params['r'] = hashlib.sha256(ambassador.custom_slogan.encode('utf-8')).hexdigest()[:8]
    query = urlencode(params)
    return render(request, 'ambassadors/print.html', {
        'png_url': f'/a/{ambassador.code}/poster.png?{query}',
        'pdf_url': f'/a/{ambassador.code}/poster.pdf?{query}',
        'width_mm': qr.PDF_POSTER_MM,
    })


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
                    # Three characters is enough now that codes are names
                    # rather than secrets. The character class is what keeps a
                    # code from dressing itself up as another route.
                    ok = (3 <= len(raw_code) <= 40 and
                          all(c.isalnum() or c in '-_' for c in raw_code) and
                          raw_code not in RESERVED_CODES and
                          not Ambassador.objects.filter(code=raw_code).exists())
                    if not ok:
                        error = ('That code is too short or long, has odd '
                                 'characters, is reserved, or is taken.')
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

        if action == 'stop_content':
            post = AmbassadorContent.objects.filter(id=request.POST.get('id') or 0).first()
            if post is not None and post.running:
                # Credit everyone who joined up to this moment before closing
                # the window, so pressing Stop never loses someone who had
                # already arrived.
                content_matching.match(post.ambassador)
                post.stopped_at = timezone.now()
                post.stopped_by = _admin_user(request)
                post.save(update_fields=['stopped_at', 'stopped_by'])
            return redirect(reverse('ambassadors_stats') + '#content')

        if action in ('approve_content', 'reject_content'):
            post = AmbassadorContent.objects.filter(id=request.POST.get('id') or 0).first()
            if post is not None and post.status == AmbassadorContent.PENDING:
                post.status = (AmbassadorContent.APPROVED if action == 'approve_content'
                               else AmbassadorContent.REJECTED)
                post.reviewed_by = _admin_user(request)
                post.reviewed_at = timezone.now()
                post.save(update_fields=['status', 'reviewed_by', 'reviewed_at'])
                if post.status == AmbassadorContent.APPROVED:
                    # Credit its window straight away rather than on the next
                    # page load, so the result of approving is visible now.
                    content_matching.match(post.ambassador)
            return redirect(reverse('ambassadors_stats') + '#content')

        signup = AmbassadorSignup.objects.filter(
            id=request.POST.get('id') or 0,
        ).select_related('ambassador').first()
        if signup is not None:
            admin = _admin_user(request)
            # Measured again at the moment of approval rather than trusted
            # from the last page load, and required of every credit however it
            # arrived. A flagged row used to be approvable as it stood — and
            # flagged rows never climb to qualified on their own — so an
            # account with no posts and no day-three session could be paid for
            # simply by being flagged. The bar is the bar.
            meets_bar = qualify.measure(signup.user)['qualifies']
            if action == 'approve' and meets_bar and signup.status in (
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

    # Credit anyone who joined inside a verified post's window, then re-measure
    # every row still in play. At this volume both are cheaper than a scheduled
    # job, and impossible to forget to run.
    content_matching.match()
    qualify.refresh_all()
    # Addresses are kept only as long as they can settle a payout dispute.
    addresses.purge_expired()

    review = list(
        AmbassadorSignup.objects
        .exclude(status__in=[AmbassadorSignup.PAID, AmbassadorSignup.REJECTED])
        .select_related('ambassador', 'user', 'content')
        .order_by('-created')[:60]
    )
    # What each row still lacks, from the measurements refresh_all just took.
    # Approval re-measures on its own; this is so the page never offers a
    # button that will refuse.
    for signup in review:
        missing = []
        if not signup.has_city:
            missing.append('city')
        if signup.post_count < qualify.MIN_POSTS:
            missing.append('post')
        if signup.interaction_count < qualify.MIN_INTERACTIONS:
            missing.append(f'{qualify.MIN_INTERACTIONS - signup.interaction_count} interactions')
        if not signup.retained_day3:
            missing.append(f'day {qualify.RETENTION_DAYS}')
        signup.missing = missing

    pending_posts = (
        AmbassadorContent.objects
        .filter(status=AmbassadorContent.PENDING)
        .select_related('ambassador')
        .order_by('uploaded_at')
    )
    running_posts = list(
        AmbassadorContent.objects
        .filter(status=AmbassadorContent.APPROVED, stopped_at__isnull=True)
        .select_related('ambassador')
        .annotate(credited=Count('signups'))
        .order_by('uploaded_at')
    )
    for post in running_posts:
        post.running_hours = int((timezone.now() - post.uploaded_at).total_seconds() // 3600)

    recent_posts = (
        AmbassadorContent.objects
        .exclude(status=AmbassadorContent.PENDING)
        .exclude(status=AmbassadorContent.APPROVED, stopped_at__isnull=True)
        .select_related('ambassador', 'reviewed_by', 'stopped_by')
        .annotate(credited=Count('signups'))
        .order_by('-uploaded_at')[:20]
    )


    from . import analytics

    return render(request, 'ambassadors/stats.html', {
        # Everything the analytics sections show. Collected after matching and
        # re-measuring above, so the numbers describe the state the review
        # queues below are in, not the one before this page load.
        'a': analytics.collect(),
        'review': review,
        'retention_days': qualify.RETENTION_DAYS,
        'min_posts': qualify.MIN_POSTS,
        'min_interactions': qualify.MIN_INTERACTIONS,
        'pending_posts': pending_posts,
        'running_posts': running_posts,
        'recent_posts': recent_posts,
    })
