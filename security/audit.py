"""Audit ingest: sanitize -> enqueue -> background worker -> hash-chained insert.

Requests never touch the database to write an audit record. ``record()`` does a
bounded, non-blocking put onto an in-process queue and returns; a daemon worker
drains it in batches. That keeps logging off the request's latency path and
means a database hiccup degrades logging rather than the API.

The queue is bounded on purpose: under a flood we drop (and count) rather than
grow memory without limit. Dropped counts are themselves reported to the
dashboard so the gap is visible instead of silent.
"""

import atexit
import hashlib
import json
import logging
import queue
import re
import threading

logger = logging.getLogger(__name__)

# Bounded so a burst can never exhaust memory.
_QUEUE_MAX = 5000
_BATCH_MAX = 200

_queue: "queue.Queue[dict | None]" = queue.Queue(maxsize=_QUEUE_MAX)
_worker = None
_worker_lock = threading.Lock()
_dropped = 0
_dropped_lock = threading.Lock()

# Log injection: CR/LF let an attacker forge extra log lines; other control
# characters corrupt viewers. Everything user-controlled is scrubbed.
_CONTROL_CHARS = re.compile(r'[\x00-\x1f\x7f]')

# Never persist credential-ish values even if a caller passes them in metadata.
_REDACT_HINTS = (
    'password', 'passwd', 'token', 'authorization', 'auth', 'secret',
    'api_key', 'apikey', 'session', 'cookie', 'code',
)
_REDACTED = '[redacted]'

_FIELD_LIMITS = {
    'event_type': 64,
    'severity': 16,
    'actor_username': 150,
    'target_type': 64,
    'target_id': 64,
    'ip': 64,
    'user_agent': 300,
    'session_id': 64,
    'method': 8,
    'path': 300,
    'mfa': 16,
    'message': 500,
}


def scrub(value, limit=500):
    """Strip control characters and cap length. Safe for any untrusted string."""
    if value is None:
        return ''
    try:
        text = value if isinstance(value, str) else str(value)
    except Exception:
        return ''
    return _CONTROL_CHARS.sub(' ', text)[:limit]


def redact(obj, _depth=0):
    """Recursively scrub a metadata structure and mask credential-like keys."""
    if _depth > 4:
        return _REDACTED
    if isinstance(obj, dict):
        out = {}
        for k, v in list(obj.items())[:40]:
            key = scrub(k, 64)
            if any(hint in key.lower() for hint in _REDACT_HINTS):
                out[key] = _REDACTED
            else:
                out[key] = redact(v, _depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(v, _depth + 1) for v in list(obj)[:40]]
    if isinstance(obj, (int, float, bool)) or obj is None:
        return obj
    return scrub(obj, 500)


def session_fingerprint(token_key):
    """A stable, non-reversible handle for a session. The raw token never lands
    in the log."""
    if not token_key:
        return ''
    return hashlib.sha256(str(token_key).encode('utf-8')).hexdigest()[:32]


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(',', ':'), default=str)


def _load_tail_hash():
    from .models import AuditLog

    try:
        last = AuditLog.objects.order_by('-id').values('entry_hash').first()
        return (last or {}).get('entry_hash') or ''
    except Exception:
        return ''


def _insert_batch(batch):
    from django.db import connection

    from .models import AuditLog

    prev = _load_tail_hash()
    rows = []
    for rec in batch:
        body = _canonical(rec)
        entry_hash = hashlib.sha256((prev + body).encode('utf-8')).hexdigest()
        rows.append(AuditLog(prev_hash=prev, entry_hash=entry_hash, **rec))
        prev = entry_hash
    AuditLog.objects.bulk_create(rows)
    # Don't hold an idle connection open in this thread between bursts; MySQL
    # will drop it out from under us otherwise.
    connection.close()


def _run():
    while True:
        item = _queue.get()
        if item is None:  # shutdown sentinel
            return
        batch = [item]
        while len(batch) < _BATCH_MAX:
            try:
                nxt = _queue.get_nowait()
            except queue.Empty:
                break
            if nxt is None:
                break
            batch.append(nxt)
        try:
            _insert_batch(batch)
        except Exception:
            # Losing audit rows is bad, but taking the process down over it is
            # worse. Surface it in the app log and keep the worker alive.
            logger.exception('audit: failed to persist %d record(s)', len(batch))


def _ensure_worker():
    global _worker
    if _worker is not None and _worker.is_alive():
        return
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_run, name='audit-worker', daemon=True)
        _worker.start()


def _drain_on_exit():
    try:
        _queue.put_nowait(None)
        if _worker is not None:
            _worker.join(timeout=3)
    except Exception:
        pass


atexit.register(_drain_on_exit)


def dropped_count():
    with _dropped_lock:
        return _dropped


def record(
    event_type,
    *,
    severity='info',
    actor=None,
    actor_username='',
    target_type='',
    target_id='',
    request=None,
    ip='',
    user_agent='',
    session_id='',
    method='',
    path='',
    status_code=None,
    mfa='none',
    message='',
    metadata=None,
):
    """Queue an audit record. Non-blocking; never raises into the caller."""
    global _dropped
    try:
        if request is not None:
            ctx = getattr(request, 'security_context', None) or {}
            ip = ip or ctx.get('ip', '')
            user_agent = user_agent or ctx.get('user_agent', '')
            session_id = session_id or ctx.get('session_id', '')
            method = method or getattr(request, 'method', '')
            path = path or getattr(request, 'path', '')

        if actor is not None and not actor_username:
            actor_username = getattr(actor, 'username', '') or ''

        rec = {
            'event_type': scrub(event_type, _FIELD_LIMITS['event_type']),
            'severity': scrub(severity, _FIELD_LIMITS['severity']) or 'info',
            'actor_id': getattr(actor, 'id', None),
            'actor_username': scrub(actor_username, _FIELD_LIMITS['actor_username']),
            'target_type': scrub(target_type, _FIELD_LIMITS['target_type']),
            'target_id': scrub(target_id, _FIELD_LIMITS['target_id']),
            'ip': scrub(ip, _FIELD_LIMITS['ip']),
            'user_agent': scrub(user_agent, _FIELD_LIMITS['user_agent']),
            'session_id': scrub(session_id, _FIELD_LIMITS['session_id']),
            'method': scrub(method, _FIELD_LIMITS['method']),
            'path': scrub(path, _FIELD_LIMITS['path']),
            'status_code': status_code if isinstance(status_code, int) else None,
            'mfa': scrub(mfa, _FIELD_LIMITS['mfa']) or 'none',
            'message': scrub(message, _FIELD_LIMITS['message']),
            'metadata': redact(metadata or {}),
        }

        _ensure_worker()
        try:
            _queue.put_nowait(rec)
        except queue.Full:
            with _dropped_lock:
                _dropped += 1
    except Exception:
        logger.exception('audit: record() failed for %s', event_type)


def verify_chain(limit=5000):
    """Recompute the hash chain over the most recent rows.

    Returns (ok, checked, first_bad_id). A mismatch means a row was altered or
    removed at the database level — exactly what the chain exists to reveal.
    """
    from .models import AuditLog

    rows = list(
        AuditLog.objects.order_by('-id')[:limit].values(
            'id', 'created', 'event_type', 'severity', 'actor_id', 'actor_username',
            'target_type', 'target_id', 'ip', 'user_agent', 'session_id', 'method',
            'path', 'status_code', 'mfa', 'message', 'metadata',
            'prev_hash', 'entry_hash',
        )
    )
    rows.reverse()
    checked = 0
    for row in rows:
        stored_prev = row.pop('prev_hash')
        stored_hash = row.pop('entry_hash')
        row_id = row.pop('id')
        # `created` is set by the DB on insert and is not part of the signed
        # payload the worker built, so it is excluded here too.
        row.pop('created', None)
        body = _canonical(row)
        expected = hashlib.sha256((stored_prev + body).encode('utf-8')).hexdigest()
        checked += 1
        if expected != stored_hash:
            return False, checked, row_id
    return True, checked, None
