import datetime
import json

from django.contrib.auth import get_user_model
from django.db.models import Count, Q
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from accounts.admin_views import _cors_json, _require_admin

from . import audit
from .models import AuditLog

User = get_user_model()

_MAX_LIMIT = 300


@csrf_exempt
@require_http_methods(["GET", "OPTIONS"])
def security_logs(request):
    """Filterable audit trail. Admin-only."""
    if request.method == "OPTIONS":
        return _cors_json(HttpResponse())
    _admin, error = _require_admin(request)
    if error:
        return error

    qs = AuditLog.objects.all()

    severity = (request.GET.get('severity') or '').strip().lower()
    if severity and severity != 'all':
        if severity == 'alerts':  # anything worth a human looking at
            qs = qs.filter(severity__in=[AuditLog.HIGH, AuditLog.CRITICAL])
        else:
            qs = qs.filter(severity=severity)

    event_type = (request.GET.get('event_type') or '').strip()
    if event_type and event_type != 'all':
        qs = qs.filter(event_type__startswith=event_type)

    actor = (request.GET.get('actor') or '').strip()
    if actor:
        qs = qs.filter(actor_username__icontains=actor)

    q = (request.GET.get('q') or '').strip()
    if q:
        qs = qs.filter(
            Q(message__icontains=q)
            | Q(path__icontains=q)
            | Q(ip__icontains=q)
            | Q(actor_username__icontains=q)
            | Q(event_type__icontains=q)
        )

    try:
        hours = int(request.GET.get('hours') or 0)
    except ValueError:
        hours = 0
    if hours > 0:
        qs = qs.filter(created__gte=timezone.now() - datetime.timedelta(hours=hours))

    try:
        limit = min(int(request.GET.get('limit') or 100), _MAX_LIMIT)
    except ValueError:
        limit = 100

    try:
        before = int(request.GET.get('before') or 0)
    except ValueError:
        before = 0
    if before > 0:
        qs = qs.filter(id__lt=before)

    rows = list(qs.order_by('-id')[:limit])
    return _cors_json(
        JsonResponse(
            {
                'logs': [r.to_dict() for r in rows],
                'nextBefore': rows[-1].id if len(rows) == limit else None,
            }
        )
    )


@csrf_exempt
@require_http_methods(["GET", "OPTIONS"])
def security_summary(request):
    """Dashboard header: severity counts, top signals, and chain integrity."""
    if request.method == "OPTIONS":
        return _cors_json(HttpResponse())
    _admin, error = _require_admin(request)
    if error:
        return error

    now = timezone.now()
    day_ago = now - datetime.timedelta(hours=24)
    week_ago = now - datetime.timedelta(days=7)

    def by_severity(since):
        rows = (
            AuditLog.objects.filter(created__gte=since)
            .values('severity')
            .annotate(c=Count('id'))
        )
        out = {s: 0 for s, _ in AuditLog.SEVERITIES}
        for r in rows:
            out[r['severity']] = r['c']
        return out

    top_events = list(
        AuditLog.objects.filter(created__gte=week_ago)
        .values('event_type')
        .annotate(c=Count('id'))
        .order_by('-c')[:8]
    )
    top_ips = list(
        AuditLog.objects.filter(
            created__gte=week_ago,
            severity__in=[AuditLog.MEDIUM, AuditLog.HIGH, AuditLog.CRITICAL],
        )
        .exclude(ip='')
        .values('ip')
        .annotate(c=Count('id'))
        .order_by('-c')[:8]
    )

    ok, checked, bad_id = audit.verify_chain(limit=2000)

    return _cors_json(
        JsonResponse(
            {
                'generatedAt': now.isoformat(),
                'last24h': by_severity(day_ago),
                'last7d': by_severity(week_ago),
                'totalEvents': AuditLog.objects.count(),
                'topEvents': [{'eventType': r['event_type'], 'count': r['c']} for r in top_events],
                'topIps': [{'ip': r['ip'], 'count': r['c']} for r in top_ips],
                'lockedAccounts': User.objects.filter(is_active=False).count(),
                'integrity': {
                    'ok': ok,
                    'checked': checked,
                    'firstBadId': bad_id,
                },
                'droppedRecords': audit.dropped_count(),
            }
        )
    )


@csrf_exempt
@require_http_methods(["POST", "OPTIONS"])
def security_action(request):
    """Quick actions. Every one appends its own audit record naming the admin
    who did it, so remediation is itself part of the trail."""
    if request.method == "OPTIONS":
        return _cors_json(HttpResponse())
    admin_user, error = _require_admin(request)
    if error:
        return error

    try:
        body = json.loads(request.body.decode('utf-8') or '{}')
    except Exception:
        return _cors_json(JsonResponse({'error': 'Invalid JSON'}, status=400))

    action = (body.get('action') or '').strip()
    username = (body.get('username') or '').strip()
    if not username:
        return _cors_json(JsonResponse({'error': 'username is required'}, status=400))

    try:
        target = User.objects.get(username=username)
    except User.DoesNotExist:
        return _cors_json(JsonResponse({'error': 'User not found'}, status=404))

    if target.id == admin_user.id and action in ('lock_account', 'revoke_sessions'):
        return _cors_json(
            JsonResponse({'error': 'Refusing to lock out or revoke your own admin session'}, status=400)
        )

    from accounts.models import AuthToken

    if action == 'revoke_sessions':
        # Tokens are this app's API credentials, so revoking them is both a
        # force-logout and an API-key revocation.
        revoked, _ = AuthToken.objects.filter(user=target).delete()
        audit.record(
            'admin.sessions_revoked',
            severity='high',
            actor=admin_user,
            target_type='user',
            target_id=str(target.id),
            request=request,
            message=f'Revoked all sessions/API tokens for {target.username}',
            metadata={'revoked': revoked, 'target': target.username},
        )
        return _cors_json(JsonResponse({'ok': True, 'revoked': revoked}))

    if action == 'lock_account':
        target.is_active = False
        target.save(update_fields=['is_active'])
        revoked, _ = AuthToken.objects.filter(user=target).delete()
        audit.record(
            'admin.account_locked',
            severity='critical',
            actor=admin_user,
            target_type='user',
            target_id=str(target.id),
            request=request,
            message=f'Locked account {target.username} and revoked {revoked} session(s)',
            metadata={'target': target.username, 'revoked': revoked},
        )
        return _cors_json(JsonResponse({'ok': True, 'locked': True, 'revoked': revoked}))

    if action == 'unlock_account':
        target.is_active = True
        target.save(update_fields=['is_active'])
        audit.record(
            'admin.account_unlocked',
            severity='high',
            actor=admin_user,
            target_type='user',
            target_id=str(target.id),
            request=request,
            message=f'Unlocked account {target.username}',
            metadata={'target': target.username},
        )
        return _cors_json(JsonResponse({'ok': True, 'locked': False}))

    if action == 'acknowledge':
        # Append-only: acknowledging appends a new row rather than mutating the
        # alert it refers to.
        ref = str(body.get('logId') or '')
        audit.record(
            'alert.acknowledged',
            severity='info',
            actor=admin_user,
            target_type='audit_log',
            target_id=ref,
            request=request,
            message=f'Alert {ref} acknowledged',
        )
        return _cors_json(JsonResponse({'ok': True}))

    return _cors_json(JsonResponse({'error': 'Unknown action'}, status=400))
