import logging

from django.core.cache import cache

logger = logging.getLogger(__name__)


def client_ip(request):
    """nginx (the only path to gunicorn) always sets X-Real-IP; REMOTE_ADDR
    is just a safety fallback for local/manage.py runserver testing."""
    return request.META.get('HTTP_X_REAL_IP') or request.META.get('REMOTE_ADDR') or 'unknown'


def rate_limited(key, limit, window_seconds):
    """True if `key` has already hit `limit` within the last `window_seconds`
    (and records this call either way). Uses the shared cache so the count is
    correct across all gunicorn worker processes rather than per-process.

    Fails open when the cache backend itself is unreachable. That case used to
    be unreachable in practice: the cache lived in the database, so a cache
    that was not answering meant a database that was not answering, and the
    request was already lost. On Redis the cache can fail on its own, and every
    caller here is an auth endpoint — letting the error escape would turn a
    Redis blip into 500s on login and signup, which is far worse than a brief
    unthrottled window.
    """
    full_key = f'ratelimit:{key}'
    try:
        try:
            count = cache.incr(full_key)
        except ValueError:
            cache.set(full_key, 1, timeout=window_seconds)
            count = 1
    except Exception:
        logger.warning('rate limit failing open for %s', full_key, exc_info=True)
        return False
    return count > limit
