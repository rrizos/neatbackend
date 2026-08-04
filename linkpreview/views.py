from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from accounts.auth import require_authenticated_user
from accounts.ratelimit import client_ip, rate_limited

from .fetcher import UnsafeUrl, fetch_preview
from .models import LinkPreview, url_fingerprint

MAX_URL_LENGTH = 2048
# Per-viewer ceiling on *misses*. Cache hits are free and deliberately not
# counted — scrolling a feed full of already-known links must never trip this.
FETCH_LIMIT = 30
FETCH_WINDOW = 60


def _cors_json(response):
    response['Access-Control-Allow-Origin'] = '*'
    response['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response['Access-Control-Allow-Methods'] = 'GET,OPTIONS'
    return response


def _unauthorized():
    return _cors_json(JsonResponse({'error': 'Authentication required'}, status=401))


def _no_preview(status=200):
    """A link we can't describe. 200 with preview:null keeps the client simple —
    it renders the URL as a plain tappable link and moves on."""
    return _cors_json(JsonResponse({'preview': None}, status=status))


@csrf_exempt
@require_http_methods(['GET', 'OPTIONS'])
def link_preview(request):
    if request.method == 'OPTIONS':
        return _cors_json(JsonResponse({}))

    user = require_authenticated_user(request)
    if user is None:
        return _unauthorized()

    url = (request.GET.get('url') or '').strip()
    if not url or len(url) > MAX_URL_LENGTH:
        return _no_preview(status=400)

    fingerprint = url_fingerprint(url)
    row = LinkPreview.objects.filter(url_hash=fingerprint).first()
    if row is not None and not row.is_stale:
        return _cors_json(JsonResponse({'preview': row.to_dict() if row.ok else None}))

    # Only an actual outbound fetch is rate limited. The counter lives in the
    # DB-backed cache, so a locked/unavailable cache table fails open rather
    # than 500-ing — a preview card is never worth breaking a feed render.
    try:
        throttled = rate_limited(
            f'linkpreview:{client_ip(request)}', FETCH_LIMIT, FETCH_WINDOW)
    except Exception:
        throttled = False
    if throttled:
        # Serve a stale card rather than nothing if we have one.
        if row is not None and row.ok:
            return _cors_json(JsonResponse({'preview': row.to_dict()}))
        return _no_preview(status=429)

    try:
        data = fetch_preview(url)
    except UnsafeUrl:
        LinkPreview.objects.update_or_create(
            url_hash=fingerprint,
            defaults={'url': url, 'ok': False, 'fetched_at': timezone.now()},
        )
        return _no_preview()
    except Exception:
        # Timeout, TLS failure, connection reset — all "no card", all cached
        # briefly so one bad link doesn't cost every viewer a 6s wait.
        LinkPreview.objects.update_or_create(
            url_hash=fingerprint,
            defaults={'url': url, 'ok': False, 'fetched_at': timezone.now()},
        )
        return _no_preview()

    # A page with neither a title nor an image has nothing worth rendering.
    if not data['title'] and not data['image_url']:
        LinkPreview.objects.update_or_create(
            url_hash=fingerprint,
            defaults={'url': url, 'ok': False, 'fetched_at': timezone.now()},
        )
        return _no_preview()

    row, _ = LinkPreview.objects.update_or_create(
        url_hash=fingerprint,
        defaults={
            'url': url,
            'resolved_url': data['url'],
            'title': data['title'],
            'description': data['description'],
            'image_url': data['image_url'],
            'site_name': data['site_name'],
            'ok': True,
            'fetched_at': timezone.now(),
        },
    )
    return _cors_json(JsonResponse({'preview': row.to_dict()}))
