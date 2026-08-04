from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from accounts.auth import require_authenticated_user
from accounts.ratelimit import client_ip, rate_limited

from . import service
from .fetcher import normalise_url

MAX_URL_LENGTH = 2048
# Per-IP ceiling on *misses*. Cache hits are free and deliberately not counted
# — scrolling a feed full of already-known links must never trip this.
#
# Counted per IP rather than per account, and phone networks put whole cities
# behind a handful of addresses, so this is a shared allowance rather than a
# personal one. At 30 it was tripping on ordinary use: a feed of unseen links
# would exhaust it, and the 429s that followed are what made thumbnails need
# several refreshes to appear. Misses are the only thing that reaches here and
# each one is cached afterwards, so the real ceiling is distinct new links per
# minute, which no amount of normal scrolling approaches.
FETCH_LIMIT = 120
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

    url = normalise_url(url)

    row = service.cached_row(url)
    if row is not None:
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
        stale = service.stale_row(url)
        if stale is not None and stale.ok:
            return _cors_json(JsonResponse({'preview': stale.to_dict()}))
        return _no_preview(status=429)

    row = service.resolve_and_store(url)
    if row is None:
        return _no_preview()
    return _cors_json(JsonResponse({'preview': row.to_dict()}))
