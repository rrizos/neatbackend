"""The public, crawlable web pages — everything a shared link needs.

These used to be Netlify edge functions (netlify/edge-functions/post-meta.js
and og-image.js) that fetched our own API over the network and injected meta
tags into the Flutter shell. Rendering here instead means one request, no
outbound hop, and a page that shows the post itself rather than booting a
SPA to do it.
"""

import base64
import json
import logging
import os
import re
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.mail import send_mail
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.cache import cache_control
from django.views.decorators.http import require_http_methods

from accounts.ratelimit import client_ip, rate_limited
from posts.views import _get_post_or_404, _post_to_dict

logger = logging.getLogger(__name__)

# Where tools/deploy_web.sh drops the landing page and its brand assets.
WEB_ROOT = os.environ.get('NEAT_WEB_ROOT', '/home/bitnami/neat-web')
DEFAULT_CARD = os.path.join(WEB_ROOT, 'brand', 'og-default.png')

APP_STORE_URL = (
    'https://apps.apple.com/gr/app/'
    'neat-%CF%83%CF%85%CE%BD%CE%B4%CE%AD%CF%83%CE%BF%CF%85-'
    '%CE%BC%CE%B5-%CF%84%CE%B7%CE%BD-%CF%80%CF%8C%CE%BB%CE%B7-'
    '%CF%83%CE%BF%CF%85/id6748038152?l=el'
)
PLAY_STORE_URL = 'https://play.google.com/store/apps/details?id=gr.app.neat'


def _post_payload(post_id):
    post = _get_post_or_404(post_id)
    if post is None:
        raise Http404('Post not found')
    # viewer=None: a share link is read by logged-out strangers and crawlers,
    # so `liked`/`saved` are always False here and the page renders read-only.
    return post, _post_to_dict(post, viewer=None, with_link_preview=True)


def _social_text(data, post_id):
    """Title and description for the share card.

    Ported from post-meta.js. A post that is just a pasted link used to
    preview as its own URL, which told the recipient nothing — so prefer what
    the link actually points at ("Zach King · TikTok") over the raw text.
    """
    raw = (data.get('text') or '').strip()
    link = data.get('link_preview') or None
    by = ''
    site = ''
    if link:
        by = (link.get('author_name') or link.get('author_handle') or '').strip()
        site = (link.get('site_name') or '').strip()

    link_title = (link.get('title') or '').strip() if link else ''
    byline = f'{by} on {site}' if by and site else (by or site)
    fallback = (raw[:97] + '…') if len(raw) > 100 else (raw or f"Post by @{data.get('author')}")
    title = link_title or byline or fallback

    city = data.get('city')
    description = f"@{data.get('author')} on Neat" + (f' · {city}' if city else '')
    if link:
        source = f'{by} · {site}' if by and site else (by or site)
        if source:
            description = f"{source} — shared by @{data.get('author')}"

    return title, description


def _decorate_media(data, card_url):
    """Give every media item what the page needs to draw it.

    `bgUrl` is the blurred plate behind a photo that does not fill the frame —
    the trick Instagram uses so a portrait shot in a landscape slot still looks
    deliberate. A base64 item points its plate at the og-image endpoint
    instead: repeating the same data: URI in a CSS background would send the
    whole picture down the wire twice.
    """
    for index, item in enumerate(data.get('media') or []):
        if item.get('type') == 'video':
            source = item.get('thumbUrl') or ''   # a video's plate is its poster
        else:
            source = item.get('url') or ''
        if source.startswith('data:'):
            source = card_url if index == 0 else ''
        item['bgUrl'] = source
    return data.get('media') or []


def _structured_data(data, title, image, url):
    """Schema.org for the post, so search and chat apps read it as a post."""
    payload = {
        '@context': 'https://schema.org',
        '@type': 'SocialMediaPosting',
        '@id': url,
        'url': url,
        'headline': title[:110],
        'datePublished': data.get('created') or '',
        'author': {'@type': 'Person', 'name': f"@{data.get('author') or ''}"},
        'image': image,
        'publisher': {'@type': 'Organization', 'name': 'Neat'},
        'interactionStatistic': [
            {
                '@type': 'InteractionCounter',
                'interactionType': 'https://schema.org/LikeAction',
                'userInteractionCount': data.get('likes') or 0,
            },
            {
                '@type': 'InteractionCounter',
                'interactionType': 'https://schema.org/CommentAction',
                'userInteractionCount': data.get('comment_count') or 0,
            },
        ],
    }
    if data.get('text'):
        payload['articleBody'] = data['text'][:600]
    # </script> inside a JSON-LD block would end the block early.
    return json.dumps(payload, ensure_ascii=False).replace('<', r'\u003c')


def post_page(request, post_id):
    post, data = _post_payload(post_id)
    title, description = _social_text(data, post_id)

    origin = f"{request.scheme}://{request.get_host()}"
    page_url = f'{origin}/post/{post_id}'
    card_url = f'{page_url}/og-image'
    media = _decorate_media(data, card_url)

    return render(request, 'web/post.html', {
        'post': data,
        'post_id': post_id,
        'og_title': title,
        'og_description': description,
        'og_image': card_url,
        'og_url': page_url,
        'app_store_url': APP_STORE_URL,
        'play_store_url': PLAY_STORE_URL,
        # The custom scheme, not the page's own https URL: a universal link
        # followed from a page already on that domain is ignored by iOS, so
        # tapping "open in app" would have done nothing at all.
        'deep_link': f'neat://post/{post_id}',
        'media_count': len(media),
        'has_video': any(m.get('type') == 'video' for m in media),
        'jsonld': _structured_data(data, title, card_url, page_url),
    })


def _default_card():
    if os.path.exists(DEFAULT_CARD):
        return FileResponse(open(DEFAULT_CARD, 'rb'), content_type='image/png')
    return HttpResponse('No image', status=404)


@cache_control(public=True, max_age=3600)
def og_image(request, post_id):
    """The post's own first image, so a shared link never previews blank.

    Media arrives in three shapes: an inline data: URI (older posts), a
    /media/ path on our own disk, or an absolute third-party URL (Giphy, link
    thumbnails).
    """
    try:
        _post, data = _post_payload(post_id)
    except Http404:
        return _default_card()

    media = data.get('media') or []
    link = data.get('link_preview') or {}
    first_image = next((m.get('url') for m in media if m.get('type') == 'image'), '')
    source = first_image or link.get('image_url') or data.get('avatarUrl') or ''

    if source.startswith('data:'):
        try:
            header, b64 = source.split(',', 1)
            mime = header[5:header.index(';')]
            return HttpResponse(base64.b64decode(b64), content_type=mime)
        except Exception:
            return _default_card()

    if source.startswith('/media/'):
        # Read straight off disk rather than looping back through nginx.
        relative = source[len('/media/'):]
        path = os.path.normpath(os.path.join(settings.MEDIA_ROOT, relative))
        # normpath first, so a crafted ../ can't escape MEDIA_ROOT.
        if path.startswith(str(settings.MEDIA_ROOT)) and os.path.isfile(path):
            return FileResponse(open(path, 'rb'))
        return _default_card()

    if source.startswith('http://') or source.startswith('https://'):
        try:
            host = urlparse(source).hostname or ''
            # Only ever fetch public hosts; never let a stored URL make the
            # server read its own metadata service or anything on the LAN.
            if host in ('localhost', '127.0.0.1', '169.254.169.254') or host.startswith('10.'):
                return _default_card()
            req = Request(source, headers={'User-Agent': 'neat-og/1.0'})
            with urlopen(req, timeout=5) as res:
                ctype = res.headers.get('Content-Type', '')
                if not ctype.startswith('image/'):
                    return _default_card()
                return HttpResponse(res.read(), content_type=ctype)
        except Exception:
            return _default_card()

    return _default_card()


# ---------------------------------------------------------------------------
# Safety portal and account deletion
# ---------------------------------------------------------------------------

# Where deletion requests land. Deliberately the same mailbox the app already
# sends from (EMAIL_HOST_USER), so there is one inbox to watch.
DELETION_INBOX = 'neatgreece@gmail.com'

# Good enough to reject nonsense without rejecting real addresses; the actual
# check is whether the address matches an account, which happens by hand.
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$')


def safety_portal(request):
    return render(request, 'web/safety_portal.html')


@require_http_methods(['GET', 'POST'])
def delete_account(request):
    """Takes a deletion request from someone who can no longer sign in.

    The app itself deletes an account outright (DELETE /api/auth/me/), so this
    page exists for the case that flow cannot cover — a lost password, a
    deleted app — which is also the route the app stores require to be
    reachable from the web.

    Nothing is deleted here. The request is emailed to the team and actioned by
    hand, which is why the confirmation promises a follow-up rather than
    claiming the account is already gone.
    """
    if request.method == 'GET':
        return render(request, 'web/delete_account.html', {'email_value': ''})

    email = (request.POST.get('email') or '').strip()

    # Hidden field: a human never sees it, so anything in it is a bot. Answer
    # exactly as if it had worked — telling a bot it failed only teaches it.
    if (request.POST.get('website') or '').strip():
        logger.info('delete_account: honeypot tripped from %s', client_ip(request))
        return render(request, 'web/delete_account.html', {'sent': True})

    if not EMAIL_RE.match(email) or len(email) > 254:
        return render(request, 'web/delete_account.html', {
            'error': True,
            'email_value': email[:254],
            'error_el': 'Δώσε μια έγκυρη διεύθυνση email.',
            'error_en': 'Please enter a valid email address.',
        })

    # This form puts mail in a person's inbox, so it needs a ceiling. Per IP
    # rather than per address: the address is attacker-controlled, the IP is
    # the thing that actually costs something to change.
    if rate_limited('delete-account:%s' % client_ip(request), limit=5, window_seconds=3600):
        return render(request, 'web/delete_account.html', {
            'error': True,
            'email_value': email,
            'error_el': 'Πολλά αιτήματα από αυτό το δίκτυο. Δοκίμασε ξανά αργότερα ή '
                        'στείλε μας email στο %s.' % DELETION_INBOX,
            'error_en': 'Too many requests from this network. Try again later, or '
                        'email us at %s.' % DELETION_INBOX,
        })

    body = (
        'Account deletion request from neatapp.gr/deleteaccount\n\n'
        'Account email : %s\n'
        'Requested from: %s\n'
        'User agent    : %s\n\n'
        'Find the user by this address and delete them from the admin panel '
        '(Users -> delete), which removes their posts, events and any '
        'conversation left with nobody in it.\n'
    ) % (email, client_ip(request), (request.META.get('HTTP_USER_AGENT') or '')[:300])

    try:
        send_mail(
            subject='Neat — account deletion request: %s' % email,
            message=body,
            from_email=None,          # falls back to DEFAULT_FROM_EMAIL
            recipient_list=[DELETION_INBOX],
            fail_silently=False,
        )
    except Exception:
        # Never answer "we got it" when the mail did not leave: a deletion
        # request that quietly vanishes is the one failure mode this page
        # cannot have. Logged with the address so it can still be honoured.
        logger.exception('delete_account: could not email request for %s', email)
        return render(request, 'web/delete_account.html', {
            'error': True,
            'email_value': email,
            'error_el': 'Δεν μπορέσαμε να στείλουμε το αίτημα. Στείλε μας email '
                        'απευθείας στο %s και θα το χειριστούμε.' % DELETION_INBOX,
            'error_en': 'We could not send your request. Please email us directly '
                        'at %s and we will take care of it.' % DELETION_INBOX,
        })

    logger.info('delete_account: request received for %s', email)
    return render(request, 'web/delete_account.html', {'sent': True})


# ── Analytics ────────────────────────────────────────────────────────────────
#
# Behind a login, because the page lists real accounts and when they were last
# seen. It authenticates against the app's own credentials and requires
# `Profile.is_admin` — there is no Django superuser on this deployment, and
# inventing a second set of credentials to protect one page is worse than
# reusing the one that already decides who is an admin in the app.

ANALYTICS_SESSION_KEY = 'neat_analytics_admin'


def _analytics_admin(request):
    return bool(request.session.get(ANALYTICS_SESSION_KEY))


@csrf_protect
@require_http_methods(['GET', 'POST'])
def analytics(request):
    from django.contrib.auth import authenticate

    from accounts.ratelimit import client_ip, rate_limited
    from accounts.serializers import ensure_profile
    from web import analytics as metrics

    error = ''
    if request.method == 'POST' and not _analytics_admin(request):
        # Brute force on a page that lists your whole user base is worth
        # slowing down properly.
        if rate_limited(f'analytics:{client_ip(request)}', limit=8, window_seconds=900):
            error = 'Too many attempts. Try again later.'
        else:
            user = authenticate(
                username=(request.POST.get('username') or '').strip(),
                password=request.POST.get('password') or '',
            )
            if user is not None and ensure_profile(user).is_admin:
                request.session[ANALYTICS_SESSION_KEY] = True
                request.session.set_expiry(60 * 60 * 8)
                return redirect('analytics')
            # Deliberately one message for both "no such user" and "not an
            # admin": which of the two it was is not the visitor's business.
            error = 'Those details are not valid here.'

    if not _analytics_admin(request):
        return render(request, 'web/analytics_login.html', {'error': error}, status=200)

    if request.GET.get('logout'):
        request.session.pop(ANALYTICS_SESSION_KEY, None)
        return redirect('analytics')

    return render(request, 'web/analytics.html', metrics.collect())
