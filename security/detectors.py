"""Stateless-ish threat heuristics backed by the shared cache.

The cache is the DB-backed one the rate limiter already uses, so counters are
correct across all gunicorn workers rather than per-process.
"""

import re
import time

from django.core.cache import cache

# Rapid login failure (credential stuffing / brute force)
LOGIN_FAIL_LIMIT = 5
LOGIN_FAIL_WINDOW = 600  # seconds

# Sustained high request volume from one account (scraping / mass export)
VOLUME_LIMIT = 300  # requests per bucket
VOLUME_BUCKET = 60  # seconds

_LAST_IP_TTL = 60 * 60 * 24 * 30


def _incr(key, window):
    try:
        return cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=window)
        return 1


def note_login_failure(ip, username):
    """Count a failed login for this ip+username window; returns the count."""
    return _incr(f'sec:loginfail:{ip}:{username}', LOGIN_FAIL_WINDOW)


def clear_login_failures(ip, username):
    try:
        cache.delete(f'sec:loginfail:{ip}:{username}')
    except Exception:
        pass


def note_ip_for_user(user_id, ip):
    """Track the last IP seen for a user. Returns the previous IP when it
    changed (a session moving between addresses), else None."""
    if not user_id or not ip:
        return None
    key = f'sec:lastip:{user_id}'
    try:
        previous = cache.get(key)
        cache.set(key, ip, timeout=_LAST_IP_TTL)
    except Exception:
        return None
    if previous and previous != ip:
        return previous
    return None


def note_request_volume(user_id):
    """Returns the request count in the current bucket for this user."""
    if not user_id:
        return 0
    bucket = int(time.time() // VOLUME_BUCKET)
    return _incr(f'sec:vol:{user_id}:{bucket}', VOLUME_BUCKET * 2)


def volume_alert_once(user_id):
    """True only the first time a user trips the volume threshold in a bucket,
    so one burst produces one alert rather than hundreds."""
    bucket = int(time.time() // VOLUME_BUCKET)
    key = f'sec:volalert:{user_id}:{bucket}'
    try:
        return cache.add(key, 1, timeout=VOLUME_BUCKET * 2)
    except Exception:
        return False


# ── Payload probing ──────────────────────────────────────────────────────────
# Scanned against the URL path and query string only. Request *bodies* are
# deliberately excluded: post/comment text legitimately contains things like
# "<script" when people discuss code, and flagging those would bury real
# signals in false positives.

_SQLI_PATTERNS = [
    r"(?:'|%27)\s*(?:or|and)\s*(?:'|%27)?\d",
    r'\bunion\s+(?:all\s+)?select\b',
    r'\binformation_schema\b',
    r'\bsleep\s*\(',
    r'\bbenchmark\s*\(',
    r'\bwaitfor\s+delay\b',
    r'\bdrop\s+table\b',
    r'\bxp_cmdshell\b',
    r'(?:--|#)\s*$',
    r'/\*.*\*/',
]

_XSS_PATTERNS = [
    r'<\s*script',
    r'javascript\s*:',
    r'\bon(?:error|load|click|mouseover)\s*=',
    r'<\s*iframe',
    r'<\s*img[^>]+src\s*=',
    r'document\.cookie',
]

_PATH_TRAVERSAL_PATTERNS = [
    r'\.\./',
    r'%2e%2e[/%]',
    r'/etc/passwd',
]

_SQLI_RE = re.compile('|'.join(_SQLI_PATTERNS), re.IGNORECASE)
_XSS_RE = re.compile('|'.join(_XSS_PATTERNS), re.IGNORECASE)
_TRAVERSAL_RE = re.compile('|'.join(_PATH_TRAVERSAL_PATTERNS), re.IGNORECASE)


def classify_payload(candidate):
    """Return a threat label for a suspicious URL/query, or None."""
    if not candidate:
        return None
    text = candidate[:2000]
    try:
        from urllib.parse import unquote

        decoded = unquote(text)
    except Exception:
        decoded = text
    for probe in (text, decoded):
        if _SQLI_RE.search(probe):
            return 'sqli'
        if _XSS_RE.search(probe):
            return 'xss'
        if _TRAVERSAL_RE.search(probe):
            return 'path_traversal'
    return None
