import json

from django.contrib.auth import get_user_model
from django.db import connection
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from accounts.auth import require_authenticated_user

from .models import Conversation, ConversationMember, Message

User = get_user_model()


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


def _ensure_messages_tables():
    table_names = set()
    with connection.cursor() as cursor:
        table_names = set(connection.introspection.table_names(cursor))

    models_to_create = []
    if Conversation._meta.db_table not in table_names:
        models_to_create.append(Conversation)
    if ConversationMember._meta.db_table not in table_names:
        models_to_create.append(ConversationMember)
    if Message._meta.db_table not in table_names:
        models_to_create.append(Message)

    if not models_to_create:
        return

    with connection.schema_editor() as schema_editor:
        for model in models_to_create:
            schema_editor.create_model(model)


def _message_to_dict(message):
    return {
        'id': message.id,
        'sender': message.sender.username,
        'text': message.text,
        'created': message.created.isoformat(),
    }


def _conversation_to_dict(conversation, viewer):
    members = list(conversation.members.select_related('user').all())
    other_members = [m.user for m in members if m.user_id != viewer.id]
    other = other_members[0] if other_members else viewer
    last_message = conversation.messages.select_related('sender').last()
    unread = conversation.messages.exclude(sender=viewer)
    member = next((m for m in members if m.user_id == viewer.id), None)
    return {
        'id': conversation.id,
        'otherUser': other.username,
        'otherFullName': getattr(other.profile, 'full_name', '') if hasattr(other, 'profile') else '',
        'lastMessage': last_message.text if last_message else '',
        'lastSender': last_message.sender.username if last_message else '',
        'updated': conversation.updated.isoformat(),
        'unreadCount': unread.count(),
        'lastReadAt': member.last_read_at.isoformat() if member and member.last_read_at else '',
    }


def _get_or_create_direct_conversation(user_a, user_b):
    conversations_a = Conversation.objects.filter(members__user=user_a)
    conversations_b = Conversation.objects.filter(members__user=user_b)
    conversation = conversations_a.filter(id__in=conversations_b.values('id')).first()
    if conversation:
        return conversation
    conversation = Conversation.objects.create()
    ConversationMember.objects.create(conversation=conversation, user=user_a)
    ConversationMember.objects.create(conversation=conversation, user=user_b)
    return conversation


def _same_city(user_a, user_b):
    city_a = getattr(getattr(user_a, 'profile', None), 'city', '') or ''
    city_b = getattr(getattr(user_b, 'profile', None), 'city', '') or ''
    return bool(city_a) and city_a == city_b


@csrf_exempt
@require_http_methods(['GET', 'OPTIONS'])
def inbox(request):
    if request.method == 'OPTIONS':
        return _cors_json(HttpResponse())

    _ensure_messages_tables()
    viewer = require_authenticated_user(request)
    if viewer is None:
        return _unauthorized()

    conversations = (
        Conversation.objects.filter(members__user=viewer)
        .prefetch_related('members__user', 'messages__sender')
        .order_by('-updated')
    )
    data = [_conversation_to_dict(conversation, viewer) for conversation in conversations]
    return _cors_json(JsonResponse({'conversations': data}))


@csrf_exempt
@require_http_methods(['GET', 'POST', 'OPTIONS'])
def conversation_detail(request, conversation_id):
    if request.method == 'OPTIONS':
        return _cors_json(HttpResponse())

    _ensure_messages_tables()
    viewer = require_authenticated_user(request)
    if viewer is None:
        return _unauthorized()

    try:
        conversation = Conversation.objects.prefetch_related('members__user', 'messages__sender').get(
            pk=conversation_id,
            members__user=viewer,
        )
    except Conversation.DoesNotExist:
        return _cors_json(JsonResponse({'error': 'Conversation not found'}, status=404))

    if request.method == 'GET':
        messages = conversation.messages.select_related('sender').all()
        member = ConversationMember.objects.filter(conversation=conversation, user=viewer).first()
        if member:
            member.last_read_at = timezone.now()
            member.save(update_fields=['last_read_at'])
        return _cors_json(
            JsonResponse(
                {
                    'conversation': _conversation_to_dict(conversation, viewer),
                    'messages': [_message_to_dict(message) for message in messages],
                }
            )
        )

    body = _json_body(request)
    if body is None:
        return _bad_request('Invalid JSON')
    members = list(conversation.members.select_related('user').all())
    other_members = [m.user for m in members if m.user_id != viewer.id]
    if other_members and not _same_city(viewer, other_members[0]):
        return _bad_request('You can only message people in your city')
    text = (body.get('text') or '').strip()
    if not text:
        return _bad_request('Message text is required')
    message = Message.objects.create(conversation=conversation, sender=viewer, text=text)
    conversation.save(update_fields=['updated'])
    return _cors_json(JsonResponse(_message_to_dict(message), status=201))


@csrf_exempt
@require_http_methods(['POST', 'OPTIONS'])
def start_conversation(request):
    if request.method == 'OPTIONS':
        return _cors_json(HttpResponse())

    _ensure_messages_tables()
    viewer = require_authenticated_user(request)
    if viewer is None:
        return _unauthorized()

    body = _json_body(request)
    if body is None:
        return _bad_request('Invalid JSON')

    username = (body.get('username') or body.get('recipient') or '').strip().lstrip('@')
    if not username:
        return _bad_request('Username is required')

    try:
        other = User.objects.get(username__iexact=username)
    except User.DoesNotExist:
        return _cors_json(JsonResponse({'error': 'User not found'}, status=404))

    if other == viewer:
        return _bad_request('You cannot message yourself')
    if not _same_city(viewer, other):
        return _bad_request('You can only message people in your city')

    conversation = _get_or_create_direct_conversation(viewer, other)
    return _cors_json(
        JsonResponse(
            {
                'conversation': _conversation_to_dict(conversation, viewer),
            },
            status=201,
        )
    )
