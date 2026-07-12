from django.core.cache import cache


def client_ip(request):
    """nginx (the only path to gunicorn) always sets X-Real-IP; REMOTE_ADDR
    is just a safety fallback for local/manage.py runserver testing."""
    return request.META.get('HTTP_X_REAL_IP') or request.META.get('REMOTE_ADDR') or 'unknown'


def rate_limited(key, limit, window_seconds):
    """True if `key` has already hit `limit` within the last `window_seconds`
    (and records this call either way). Uses the DB-backed cache so the count
    is shared correctly across all gunicorn worker processes."""
    full_key = f'ratelimit:{key}'
    try:
        count = cache.incr(full_key)
    except ValueError:
        cache.set(full_key, 1, timeout=window_seconds)
        count = 1
    return count > limit
