import json

from django.db import connection
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from accounts.auth import require_authenticated_user
from accounts.models import Notification

from .models import Event, EventAttendance, EventComment


def _cors_json(response):
    response['Access-Control-Allow-Origin'] = '*'
    response['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response['Access-Control-Allow-Methods'] = 'GET,POST,OPTIONS'
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
    if Event._meta.db_table not in table_names:
        models_to_create.append(Event)
    if EventAttendance._meta.db_table not in table_names:
        models_to_create.append(EventAttendance)
    if EventComment._meta.db_table not in table_names:
        models_to_create.append(EventComment)
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
    event = Event.objects.create(
        city=city or _viewer_city(viewer),
        event_type=(body.get('eventType') or Event.COMMUNITY),
        title=title,
        description=(body.get('description') or '').strip(),
        location=(body.get('location') or '').strip(),
        image_url=(body.get('imageUrl') or '').strip(),
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
    if event.creator_id != viewer.id:
        return _cors_json(JsonResponse({'error': 'You can only delete your own event'}, status=403))
    event.delete()
    return _cors_json(JsonResponse({'ok': True}))


@csrf_exempt
@require_http_methods(['GET', 'POST', 'OPTIONS'])
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
        comments = event.comment_rows.select_related('user').all()
        return _cors_json(JsonResponse({'comments': [comment.to_dict() for comment in comments]}))

    body = _json_body(request)
    if body is None:
        return _bad_request('Invalid JSON')
    text = (body.get('text') or '').strip()
    if not text:
        return _bad_request('Comment text is required')
    comment = EventComment.objects.create(event=event, user=viewer, text=text)
    _notify(event.creator or viewer, viewer, 'commented on your event', 'event', event.id, event.title)
    return _cors_json(JsonResponse({'comment': comment.to_dict()}, status=201))
