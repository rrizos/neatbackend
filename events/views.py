import json
import re

from django.contrib.auth import get_user_model
from django.db import connection
from django.http import HttpResponse, JsonResponse
from datetime import datetime

from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from accounts.auth import require_authenticated_user
from accounts.models import Notification, blocked_user_ids, is_blocked
from accounts.serializers import user_to_dict

from .models import Event, EventAttendance, EventComment, EventCommentLike, EventCommentReport, EventReport

User = get_user_model()


def _cors_json(response):
    response['Access-Control-Allow-Origin'] = '*'
    response['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response['Access-Control-Allow-Methods'] = 'GET,POST,DELETE,OPTIONS'
    response['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response['Pragma'] = 'no-cache'
    response['Expires'] = '0'
    return response


def _bad_request(message):
    return _cors_json(JsonResponse({'error': message}, status=400))


def _unauthorized():
    return _cors_json(JsonResponse({'error': 'Authentication required'}, status=401))


def _json_body(request):
    try:
        return json.loads(request.body.decode('utf-8') or '{}')
    except Exception:
        return None


def _ensure_tables():
    table_names = set()
    with connection.cursor() as cursor:
        table_names = set(connection.introspection.table_names(cursor))
    models_to_create = []
    for model in [Event, EventAttendance, EventComment, EventReport, EventCommentReport, EventCommentLike]:
        if model._meta.db_table not in table_names:
            models_to_create.append(model)
    if not models_to_create:
        return
    with connection.schema_editor() as schema_editor:
        for model in models_to_create:
            schema_editor.create_model(model)


def _viewer_city(user):
    return getattr(getattr(user, 'profile', None), 'city', '') or ''


def _boolish(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on'}


def _parse_event_datetime(value):
    """Accepts a full ISO datetime (date + time) or a bare date string.
    Bare dates are treated as midnight. Naive datetimes are stamped with the
    default timezone as-is (no conversion) so the wall-clock time an
    organizer picks is exactly what gets stored and shown back to everyone.
    """
    value = (value or '').strip()
    if not value:
        return None
    dt = parse_datetime(value)
    if dt is None:
        d = parse_date(value)
        if d is None:
            return None
        dt = datetime.combine(d, datetime.min.time())
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_default_timezone())
    return dt


def _can_create_official_events(user):
    profile = getattr(user, 'profile', None)
    return bool(getattr(profile, 'can_create_official_events', False) or getattr(profile, 'is_admin', False))


def _notify(recipient, actor, verb, target_type='', target_id='', target_text=''):
    if recipient is None or recipient == actor:
        return
    Notification.objects.create(
        recipient=recipient,
        actor=actor,
        verb=verb,
        target_type=target_type,
        target_id=str(target_id or ''),
        target_text=target_text[:255],
    )


_MENTION_RE = re.compile(r'@([\w.]+)')


def _notify_mentions(text, actor, city, event, verb='mentioned you in a comment'):
    """Notify @mentioned users, restricted to people in the same city as the
    event they're being tagged into — mentioning is a hyperlocal-only action.
    """
    usernames = set(_MENTION_RE.findall(text or ''))
    if not usernames:
        return
    mentioned = User.objects.filter(username__in=usernames).select_related('profile').exclude(pk=actor.pk)
    for user in mentioned:
        if getattr(getattr(user, 'profile', None), 'city', '') != city:
            continue
        Notification.objects.create(
            recipient=user,
            actor=actor,
            verb=verb,
            target_type='event',
            target_id=str(event.id),
            target_text=text[:255],
        )


@csrf_exempt
@require_http_methods(['GET', 'POST', 'OPTIONS'])
def events_list(request):
    if request.method == 'OPTIONS':
        return _cors_json(HttpResponse())
    _ensure_tables()
    viewer = require_authenticated_user(request)
    if viewer is None:
        return _unauthorized()
    city = (request.GET.get('city') or _viewer_city(viewer)).strip()
    event_type = (request.GET.get('type') or '').strip()
    events = Event.objects.all()
    if city:
        events = events.filter(city=city)
    if event_type in {Event.OFFICIAL, Event.COMMUNITY}:
        events = events.filter(event_type=event_type)
    if request.method == 'GET':
        return _cors_json(JsonResponse({'events': [event.to_dict() for event in events]}))

    body = _json_body(request)
    if body is None:
        return _bad_request('Invalid JSON')
    title = (body.get('title') or '').strip()
    if not title:
        return _bad_request('Title is required')

    event_type = body.get('eventType') or Event.COMMUNITY
    if event_type == Event.OFFICIAL and not _can_create_official_events(viewer):
        return _cors_json(JsonResponse(
            {'error': 'You are not eligible to create official events'},
            status=403,
        ))

    event = Event.objects.create(
        city=city or _viewer_city(viewer),
        event_type=event_type,
        title=title,
        description=(body.get('description') or '').strip(),
        location=(body.get('location') or '').strip(),
        image_url=(body.get('imageUrl') or '').strip(),
        category=(body.get('category') or '').strip(),
        date=_parse_event_datetime(body.get('date')),
        creator=viewer,
        organizer=(body.get('organizer') or viewer.username).strip(),
        has_tickets=_boolish(body.get('hasTickets')),
        tickets_url=(body.get('ticketsUrl') or '').strip(),
    )
    _notify(event.creator, viewer, 'created an event', 'event', event.id, event.title)
    return _cors_json(JsonResponse({'event': event.to_dict()}, status=201))


@csrf_exempt
@require_http_methods(['POST', 'DELETE', 'OPTIONS'])
def event_attend(request, event_id):
    if request.method == 'OPTIONS':
        return _cors_json(HttpResponse())
    _ensure_tables()
    viewer = require_authenticated_user(request)
    if viewer is None:
        return _unauthorized()
    try:
        event = Event.objects.get(pk=event_id)
    except Event.DoesNotExist:
        return _cors_json(JsonResponse({'error': 'Event not found'}, status=404))
    if event.city != _viewer_city(viewer):
        return _bad_request('You can only attend events in your city')
    existing = EventAttendance.objects.filter(event=event, user=viewer)
    if existing.exists():
        existing.delete()
        event.attendees = max(0, event.attendance_rows.count())
    else:
        EventAttendance.objects.create(event=event, user=viewer)
        event.attendees = event.attendance_rows.count()
        _notify(event.creator or viewer, viewer, 'is attending your event', 'event', event.id, event.title)
    event.save(update_fields=['attendees', 'updated'])
    return _cors_json(JsonResponse({'event': event.to_dict()}))


@csrf_exempt
@require_http_methods(['GET', 'OPTIONS'])
def event_attendees(request, event_id):
    if request.method == 'OPTIONS':
        return _cors_json(HttpResponse())
    _ensure_tables()
    viewer = require_authenticated_user(request)
    if viewer is None:
        return _unauthorized()
    try:
        event = Event.objects.get(pk=event_id)
    except Event.DoesNotExist:
        return _cors_json(JsonResponse({'error': 'Event not found'}, status=404))
    attendee_ids = event.attendance_rows.values_list('user_id', flat=True)
    hidden_ids = blocked_user_ids(viewer)
    users = User.objects.filter(id__in=attendee_ids).exclude(id__in=hidden_ids).order_by('username')
    return _cors_json(
        JsonResponse({'attendees': [user_to_dict(user, viewer=viewer) for user in users]})
    )


@csrf_exempt
@require_http_methods(['DELETE', 'OPTIONS'])
def event_delete(request, event_id):
    if request.method == 'OPTIONS':
        return _cors_json(HttpResponse())
    _ensure_tables()
    viewer = require_authenticated_user(request)
    if viewer is None:
        return _unauthorized()
    try:
        event = Event.objects.get(pk=event_id)
    except Event.DoesNotExist:
        return _cors_json(JsonResponse({'error': 'Event not found'}, status=404))
    is_admin = getattr(getattr(viewer, 'profile', None), 'is_admin', False)
    if event.creator_id != viewer.id and not is_admin:
        return _cors_json(JsonResponse({'error': 'You can only delete your own event'}, status=403))
    event.delete()
    return _cors_json(JsonResponse({'ok': True}))


@csrf_exempt
@require_http_methods(['POST', 'PATCH', 'OPTIONS'])
def event_update(request, event_id):
    if request.method == 'OPTIONS':
        return _cors_json(HttpResponse())
    _ensure_tables()
    viewer = require_authenticated_user(request)
    if viewer is None:
        return _unauthorized()
    try:
        event = Event.objects.get(pk=event_id)
    except Event.DoesNotExist:
        return _cors_json(JsonResponse({'error': 'Event not found'}, status=404))
    is_admin = getattr(getattr(viewer, 'profile', None), 'is_admin', False)
    if event.creator_id != viewer.id and not is_admin:
        return _cors_json(JsonResponse({'error': 'You can only edit your own event'}, status=403))

    body = _json_body(request)
    if body is None:
        return _bad_request('Invalid JSON')

    update_fields = []
    if 'title' in body:
        title = (body.get('title') or '').strip()
        if not title:
            return _bad_request('Title is required')
        event.title = title
        update_fields.append('title')
    if 'description' in body:
        event.description = (body.get('description') or '').strip()
        update_fields.append('description')
    if 'location' in body:
        event.location = (body.get('location') or '').strip()
        update_fields.append('location')
    if 'imageUrl' in body:
        event.image_url = (body.get('imageUrl') or '').strip()
        update_fields.append('image_url')
    if 'category' in body:
        event.category = (body.get('category') or '').strip()
        update_fields.append('category')
    if 'date' in body:
        event.date = _parse_event_datetime(body.get('date'))
        update_fields.append('date')
    if 'organizer' in body:
        event.organizer = (body.get('organizer') or '').strip()
        update_fields.append('organizer')
    if 'hasTickets' in body:
        event.has_tickets = _boolish(body.get('hasTickets'))
        update_fields.append('has_tickets')
    if 'ticketsUrl' in body:
        event.tickets_url = (body.get('ticketsUrl') or '').strip()
        update_fields.append('tickets_url')

    if update_fields:
        event.save(update_fields=update_fields + ['updated'])

    return _cors_json(JsonResponse({'event': event.to_dict()}))


@csrf_exempt
@require_http_methods(['GET', 'POST', 'DELETE', 'OPTIONS'])
def event_comments(request, event_id):
    if request.method == 'OPTIONS':
        return _cors_json(HttpResponse())
    _ensure_tables()
    viewer = require_authenticated_user(request)
    if viewer is None:
        return _unauthorized()
    try:
        event = Event.objects.get(pk=event_id)
    except Event.DoesNotExist:
        return _cors_json(JsonResponse({'error': 'Event not found'}, status=404))
    if event.city != _viewer_city(viewer):
        return _bad_request('You can only interact in your city')

    if request.method == 'GET':
        comments = event.comment_rows.select_related('user').prefetch_related('like_rows').order_by('-pinned', 'created')
        return _cors_json(JsonResponse({'comments': [comment.to_dict(viewer=viewer, owner_id=event.creator_id) for comment in comments]}))

    body = _json_body(request)
    if body is None:
        return _bad_request('Invalid JSON')

    if request.method == 'DELETE':
        comment_id = body.get('commentId') or body.get('id')
        if not comment_id:
            return _bad_request('commentId required')
        try:
            comment = EventComment.objects.get(pk=int(comment_id), event=event)
        except (EventComment.DoesNotExist, ValueError):
            return _cors_json(JsonResponse({'error': 'Comment not found'}, status=404))
        is_admin = getattr(getattr(viewer, 'profile', None), 'is_admin', False)
        if comment.user_id != viewer.id and not is_admin:
            return _cors_json(JsonResponse({'error': "Cannot delete other user's comment"}, status=403))
        comment.delete()
        comments = event.comment_rows.select_related('user').prefetch_related('like_rows').order_by('-pinned', 'created')
        return _cors_json(JsonResponse({'comments': [c.to_dict(viewer=viewer, owner_id=event.creator_id) for c in comments]}))

    text = (body.get('text') or '').strip()
    if not text:
        return _bad_request('Comment text is required')
    comment = EventComment.objects.create(event=event, user=viewer, text=text)
    _notify(event.creator or viewer, viewer, 'commented on your event', 'event', event.id, event.title)
    _notify_mentions(text, viewer, event.city, event)
    return _cors_json(JsonResponse({'comment': comment.to_dict(viewer=viewer, owner_id=event.creator_id)}, status=201))


@csrf_exempt
@require_http_methods(['POST', 'OPTIONS'])
def event_comment_report(request, comment_id):
    if request.method == 'OPTIONS':
        return _cors_json(HttpResponse())
    _ensure_tables()
    viewer = require_authenticated_user(request)
    if viewer is None:
        return _unauthorized()
    try:
        comment = EventComment.objects.get(pk=comment_id)
    except EventComment.DoesNotExist:
        return _cors_json(JsonResponse({'error': 'Comment not found'}, status=404))
    if comment.user_id == viewer.id:
        return _bad_request('You cannot report your own comment')

    body = _json_body(request) or {}
    reason = (body.get('reason') or 'other').strip()
    valid_reasons = {r[0] for r in EventCommentReport.REASONS}
    if reason not in valid_reasons:
        reason = 'other'

    EventCommentReport.objects.get_or_create(
        comment=comment,
        reporter=viewer,
        defaults={'reason': reason},
    )
    return _cors_json(JsonResponse({'ok': True}))


@csrf_exempt
@require_http_methods(['POST', 'OPTIONS'])
def event_comment_pin(request, comment_id):
    if request.method == 'OPTIONS':
        return _cors_json(HttpResponse())
    _ensure_tables()
    viewer = require_authenticated_user(request)
    if viewer is None:
        return _unauthorized()
    try:
        comment = EventComment.objects.select_related('event').get(pk=comment_id)
    except EventComment.DoesNotExist:
        return _cors_json(JsonResponse({'error': 'Comment not found'}, status=404))

    event = comment.event
    is_admin = getattr(getattr(viewer, 'profile', None), 'is_admin', False)
    if event.creator_id != viewer.id and not is_admin:
        return _cors_json(JsonResponse({'error': 'Only the event owner can pin comments'}, status=403))

    body = _json_body(request) or {}
    if body.get('pinned', True):
        EventComment.objects.filter(event=event, pinned=True).exclude(pk=comment.pk).update(pinned=False)
        comment.pinned = True
    else:
        comment.pinned = False
    comment.save(update_fields=['pinned'])

    comments = event.comment_rows.select_related('user').prefetch_related('like_rows').order_by('-pinned', 'created')
    return _cors_json(JsonResponse({'comments': [c.to_dict(viewer=viewer, owner_id=event.creator_id) for c in comments]}))


@csrf_exempt
@require_http_methods(['POST', 'OPTIONS'])
def event_comment_like(request, comment_id):
    if request.method == 'OPTIONS':
        return _cors_json(HttpResponse())
    _ensure_tables()
    viewer = require_authenticated_user(request)
    if viewer is None:
        return _unauthorized()
    try:
        comment = EventComment.objects.select_related('event').get(pk=comment_id)
    except EventComment.DoesNotExist:
        return _cors_json(JsonResponse({'error': 'Comment not found'}, status=404))

    if comment.event.creator_id and is_blocked(viewer, comment.event.creator):
        return _cors_json(JsonResponse({'error': 'Comment not found'}, status=404))

    if comment.event.city != _viewer_city(viewer):
        return _cors_json(JsonResponse(
            {'error': 'You can only like comments on events in your city'},
            status=403,
        ))

    body = _json_body(request) or {}
    if body.get('liked', True):
        _, created = EventCommentLike.objects.get_or_create(comment=comment, user=viewer)
        if created:
            _notify(comment.user, viewer, 'liked your comment', 'event', comment.event_id, comment.text)
    else:
        EventCommentLike.objects.filter(comment=comment, user=viewer).delete()

    return _cors_json(JsonResponse({
        'likes': comment.like_rows.count(),
        'liked': EventCommentLike.objects.filter(comment=comment, user=viewer).exists(),
    }))


@csrf_exempt
@require_http_methods(['POST', 'OPTIONS'])
def event_report(request, event_id):
    if request.method == 'OPTIONS':
        return _cors_json(HttpResponse())
    _ensure_tables()
    viewer = require_authenticated_user(request)
    if viewer is None:
        return _unauthorized()
    try:
        event = Event.objects.get(pk=event_id)
    except Event.DoesNotExist:
        return _cors_json(JsonResponse({'error': 'Event not found'}, status=404))
    if event.creator_id == viewer.id:
        return _bad_request('You cannot report your own event')

    body = _json_body(request) or {}
    reason = (body.get('reason') or 'other').strip()
    valid_reasons = {r[0] for r in EventReport.REASONS}
    if reason not in valid_reasons:
        reason = 'other'

    EventReport.objects.get_or_create(
        event=event,
        reporter=viewer,
        defaults={'reason': reason},
    )
    return _cors_json(JsonResponse({'ok': True}))
