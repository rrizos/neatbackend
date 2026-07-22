"""Request-level security context, payload probing, and access-denial logging.

Deliberately does NOT log every request — that would be volume without signal
and would dwarf the real events. It records only:

  * requests carrying SQLi/XSS/traversal-looking payloads,
  * denied access (401/403),
  * an account exceeding the sustained request-volume threshold.

Ordinary successful traffic is covered by the explicit audit hooks on the
sensitive operations themselves. Every step is wrapped so that a fault in
security logging can never break a request.
"""

import logging

from accounts.ratelimit import client_ip

from . import audit, detectors

logger = logging.getLogger(__name__)

# Auth endpoints log their own outcomes with far better context, so skip them
# here to avoid double-recording.
_SKIP_DENIAL_PATHS = ('/api/auth/login/', '/api/auth/signup/')


class SecurityAuditMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            self._attach_context(request)
            self._scan_payload(request)
        except Exception:
            logger.exception('security middleware: pre-request step failed')

        response = self.get_response(request)

        try:
            self._inspect_response(request, response)
        except Exception:
            logger.exception('security middleware: post-response step failed')
        return response

    # ── context ──────────────────────────────────────────────────────────────

    def _attach_context(self, request):
        header = request.headers.get('Authorization', '')
        token_key = ''
        if header.startswith('Token '):
            token_key = header.removeprefix('Token ').strip()
        request.security_context = {
            'ip': client_ip(request),
            'user_agent': request.META.get('HTTP_USER_AGENT', ''),
            # Fingerprint only — the raw token is never stored.
            'session_id': audit.session_fingerprint(token_key),
        }

    # ── probing ──────────────────────────────────────────────────────────────

    def _scan_payload(self, request):
        query = request.META.get('QUERY_STRING', '')
        candidate = f'{request.path}?{query}' if query else request.path
        label = detectors.classify_payload(candidate)
        if not label:
            return
        ctx = request.security_context
        audit.record(
            f'threat.{label}',
            severity=audit_severity_for(label),
            request=request,
            message=f'Suspicious {label.upper()} pattern in request URL',
            metadata={'query': audit.scrub(query, 400), 'path': request.path},
        )
        # A probe is also a signal about the source address.
        logger.warning('security: %s probe from %s on %s', label, ctx.get('ip'), request.path)

    # ── responses ────────────────────────────────────────────────────────────

    def _inspect_response(self, request, response):
        status = getattr(response, 'status_code', 0)
        path = request.path

        if status in (401, 403) and not path.startswith(_SKIP_DENIAL_PATHS):
            actor = getattr(request, 'audit_actor', None)
            audit.record(
                'access.denied',
                severity='medium' if status == 403 else 'low',
                actor=actor,
                request=request,
                status_code=status,
                message=(
                    'Forbidden — authenticated but not permitted'
                    if status == 403
                    else 'Unauthorized — missing or invalid credentials'
                ),
            )
            return

        # Sustained volume from one account (scraping / mass export shape).
        user = getattr(request, 'audit_actor', None)
        user_id = getattr(user, 'id', None)
        if user_id and status < 400:
            count = detectors.note_request_volume(user_id)
            if count > detectors.VOLUME_LIMIT and detectors.volume_alert_once(user_id):
                audit.record(
                    'threat.high_volume',
                    severity='high',
                    actor=user,
                    request=request,
                    status_code=status,
                    message=(
                        f'{count} requests in {detectors.VOLUME_BUCKET}s '
                        f'(threshold {detectors.VOLUME_LIMIT}) — possible scraping or bulk export'
                    ),
                    metadata={'requests': count, 'window_seconds': detectors.VOLUME_BUCKET},
                )


def audit_severity_for(label):
    return {
        'sqli': 'critical',
        'xss': 'high',
        'path_traversal': 'high',
    }.get(label, 'medium')
