import json
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection

from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from accounts.auth import require_authenticated_user
from accounts.models import Block, is_blocked

from .models import Conversation, ConversationMember, Message, MessageReaction, MessageReport

User = get_user_model()

# A "typing" signal is only re-sent by the client on the false->true transition (see
# messages_page.dart _onComposerChanged), so it can legitimately stay true for as long as the
# user keeps typing without a 4s pause. This TTL is purely a safety net against a stuck indicator
# if the client never sends the "stopped typing" signal (app killed, network drop, etc).
TYPING_TTL = timedelta(seconds=30)


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


def _ensure_messages_tables():
    with connection.cursor() as cursor:
        table_names = set(connection.introspection.table_names(cursor))

    models_to_create = []
    for model in [Conversation, ConversationMember, Message, MessageReaction, MessageReport]:
        if model._meta.db_table not in table_names:
            models_to_create.append(model)

    if not models_to_create:
        return

    with connection.schema_editor() as schema_editor:
        for model in models_to_create:
            schema_editor.create_model(model)


def _message_to_dict(message):
    reactions = {}
    for reaction in message.reactions.select_related('user').all():
        reactions.setdefault(reaction.emoji, []).append(reaction.user.username)
    return {
        'id': message.id,
        'sender': message.sender.username,
        'text': message.text,
        'created': message.created.isoformat(),
        'reactions': reactions,
    }


def _is_typing(member):
    return bool(member and member.typing_at and timezone.now() - member.typing_at < TYPING_TTL)


def _conversation_to_dict(conversation, viewer):
    members = list(conversation.members.select_related('user__profile').all())
    member = next((m for m in members if m.user_id == viewer.id), None)
    other_member = next((m for m in members if m.user_id != viewer.id), None)
    other = other_member.user if other_member else viewer
    last_message = conversation.messages.select_related('sender').last()

    unread_qs = conversation.messages.exclude(sender=viewer)
    if member and member.last_read_at:
        unread_qs = unread_qs.filter(created__gt=member.last_read_at)

    other_profile = getattr(other, 'profile', None)
    other_last_active = getattr(other_profile, 'last_active', None) if other_profile else None
    return {
        'id': conversation.id,
        'otherUser': other.username,
        'otherFullName': getattr(other_profile, 'full_name', '') if other_profile else '',
        'otherAvatarUrl': getattr(other_profile, 'avatar_url', '') if other_profile else '',
        'otherLastActive': other_last_active.isoformat() if other_last_active else '',
        'lastMessage': last_message.text if last_message else '',
        'lastSender': last_message.sender.username if last_message else '',
        'updated': conversation.updated.isoformat(),
        'unreadCount': unread_qs.count(),
        'lastReadAt': member.last_read_at.isoformat() if member and member.last_read_at else '',
        'otherLastReadAt': other_member.last_read_at.isoformat() if other_member and other_member.last_read_at else '',
        'otherIsTyping': _is_typing(other_member),
        'viewerBlockedOther': Block.objects.filter(blocker=viewer, blocked=other).exists() if other != viewer else False,
        'otherBlockedViewer': Block.objects.filter(blocker=other, blocked=viewer).exists() if other != viewer else False,
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


def _conversation_not_found():
    return _cors_json(JsonResponse({'error': 'Conversation not found'}, status=404))


def _get_conversation_for_viewer(conversation_id, viewer):
    """Returns (conversation, other_user, error_response). On failure, conversation is None
    and error_response is set. If the OTHER member has blocked the viewer, the conversation is
    hidden entirely (Instagram-style: the blocker's side vanishes for the blocked party). If the
    viewer is the one who blocked the other member, the thread stays visible (read-only) so they
    can still unblock — callers that send new messages must check that case separately."""
    try:
        conversation = Conversation.objects.prefetch_related('members__user', 'messages__sender').get(
            pk=conversation_id,
            members__user=viewer,
        )
    except Conversation.DoesNotExist:
        return None, None, _conversation_not_found()

    members = list(conversation.members.all())
    other_members = [m.user for m in members if m.user_id != viewer.id]
    other = other_members[0] if other_members else None
    if other is not None and Block.objects.filter(blocker=other, blocked=viewer).exists():
        return None, None, _conversation_not_found()
    return conversation, other, None


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
    data = []
    for conversation in conversations:
        other_members = [m.user for m in conversation.members.all() if m.user_id != viewer.id]
        if other_members and Block.objects.filter(blocker=other_members[0], blocked=viewer).exists():
            continue
        data.append(_conversation_to_dict(conversation, viewer))
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

    conversation, other, error = _get_conversation_for_viewer(conversation_id, viewer)
    if error:
        return error

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
    if other is not None and Block.objects.filter(blocker=viewer, blocked=other).exists():
        return _bad_request('You have blocked this user')
    if other is not None and not _same_city(viewer, other):
        return _bad_request('You can only message people in your city')
    text = (body.get('text') or '').strip()
    if not text:
        return _bad_request('Message text is required')
    message = Message.objects.create(conversation=conversation, sender=viewer, text=text)
    conversation.save(update_fields=['updated'])
    return _cors_json(JsonResponse(_message_to_dict(message), status=201))


@csrf_exempt
@require_http_methods(['POST', 'OPTIONS'])
def update_presence(request):
    """Heartbeat: updates the authenticated user's last_active timestamp."""
    if request.method == 'OPTIONS':
        return _cors_json(HttpResponse())

    viewer = require_authenticated_user(request)
    if viewer is None:
        return _unauthorized()

    profile = getattr(viewer, 'profile', None)
    if profile is not None:
        profile.last_active = timezone.now()
        profile.save(update_fields=['last_active'])

    return _cors_json(JsonResponse({'ok': True}))


@csrf_exempt
@require_http_methods(['GET', 'POST', 'OPTIONS'])
def conversation_typing(request, conversation_id):
    """GET returns whether the other member of the conversation is currently typing.
    POST sets/clears the authenticated user's own typing signal for this conversation."""
    if request.method == 'OPTIONS':
        return _cors_json(HttpResponse())

    viewer = require_authenticated_user(request)
    if viewer is None:
        return _unauthorized()

    conversation, _other, error = _get_conversation_for_viewer(conversation_id, viewer)
    if error:
        return error

    if request.method == 'POST':
        body = _json_body(request)
        if body is None:
            return _bad_request('Invalid JSON')
        member = ConversationMember.objects.filter(conversation=conversation, user=viewer).first()
        if member:
            member.typing_at = timezone.now() if body.get('typing') else None
            member.save(update_fields=['typing_at'])
        return _cors_json(JsonResponse({'ok': True}))

    other_member = conversation.members.exclude(user=viewer).first()
    return _cors_json(JsonResponse({'otherIsTyping': _is_typing(other_member)}))


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
    if is_blocked(viewer, other):
        return _bad_request('You cannot message this user')
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


@csrf_exempt
@require_http_methods(['DELETE', 'OPTIONS'])
def message_delete(request, conversation_id, message_id):
    if request.method == 'OPTIONS':
        return _cors_json(HttpResponse())

    _ensure_messages_tables()
    viewer = require_authenticated_user(request)
    if viewer is None:
        return _unauthorized()

    conversation, _other, error = _get_conversation_for_viewer(conversation_id, viewer)
    if error:
        return error

    try:
        message = Message.objects.get(pk=message_id, conversation=conversation, sender=viewer)
    except Message.DoesNotExist:
        return _cors_json(JsonResponse({'error': 'Message not found'}, status=404))

    message.delete()
    return _cors_json(JsonResponse({'ok': True}))


@csrf_exempt
@require_http_methods(['POST', 'OPTIONS'])
def message_react(request, conversation_id, message_id):
    if request.method == 'OPTIONS':
        return _cors_json(HttpResponse())

    _ensure_messages_tables()
    viewer = require_authenticated_user(request)
    if viewer is None:
        return _unauthorized()

    conversation, _other, error = _get_conversation_for_viewer(conversation_id, viewer)
    if error:
        return error

    try:
        message = Message.objects.get(pk=message_id, conversation=conversation)
    except Message.DoesNotExist:
        return _cors_json(JsonResponse({'error': 'Message not found'}, status=404))

    body = _json_body(request)
    if body is None:
        return _bad_request('Invalid JSON')
    emoji = (body.get('emoji') or '').strip()
    if not emoji:
        return _bad_request('Emoji is required')

    existing = MessageReaction.objects.filter(message=message, user=viewer).first()
    if existing and existing.emoji == emoji:
        existing.delete()
    else:
        MessageReaction.objects.update_or_create(
            message=message,
            user=viewer,
            defaults={'emoji': emoji},
        )
    return _cors_json(JsonResponse({'message': _message_to_dict(message)}))


@csrf_exempt
@require_http_methods(['POST', 'OPTIONS'])
def message_report(request, conversation_id, message_id):
    if request.method == 'OPTIONS':
        return _cors_json(HttpResponse())

    _ensure_messages_tables()
    viewer = require_authenticated_user(request)
    if viewer is None:
        return _unauthorized()

    conversation, _other, error = _get_conversation_for_viewer(conversation_id, viewer)
    if error:
        return error

    try:
        message = Message.objects.get(pk=message_id, conversation=conversation)
    except Message.DoesNotExist:
        return _cors_json(JsonResponse({'error': 'Message not found'}, status=404))

    if message.sender == viewer:
        return _bad_request('You cannot report your own message')

    body = _json_body(request) or {}
    reason = body.get('reason', 'other').strip()
    valid_reasons = {r[0] for r in MessageReport.REASONS}
    if reason not in valid_reasons:
        reason = 'other'

    MessageReport.objects.get_or_create(
        message=message,
        reporter=viewer,
        defaults={'reason': reason},
    )
    return _cors_json(JsonResponse({'ok': True}))
