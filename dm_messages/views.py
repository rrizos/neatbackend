import json
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.db.models import Q
from django.db.models.functions import Right, Substr

from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from accounts.auth import require_authenticated_user
from accounts.models import Block, is_blocked
from linkpreview import service as linkpreview_service

from .models import (
    Conversation,
    ConversationMember,
    Message,
    MessageOpen,
    MessageReaction,
    MessageReport,
)
from .realtime import broadcast_to_conversation, push_to_user

User = get_user_model()

# A "typing" signal is only re-sent by the client on the false->true transition (see
# messages_page.dart _onComposerChanged), so it can legitimately stay true for as long as the
# user keeps typing without a 4s pause. This TTL is purely a safety net against a stuck indicator
# if the client never sends the "stopped typing" signal (app killed, network drop, etc).
TYPING_TTL = timedelta(seconds=30)

# A message can only be edited within this window of being sent, mirroring
# the client's "Edit" affordance — kept in sync with the 15-minute copy shown
# in the app. The server is the source of truth since the client doesn't
# currently gate the "Edit" menu item on message age itself.
MESSAGE_EDIT_WINDOW = timedelta(minutes=15)

# Non-text payloads are encoded inline in Message.text with these prefixes
# (see messages_page.dart). Editing only makes sense for plain text.
_NON_TEXT_PREFIXES = ('__neat_post__:', '__neat_image__:', '__neat_voice__:', '__neat_reply__:')

# How much of a message the inbox needs. The client turns anything prefixed
# into a fixed line ("sent a photo"), so for those the prefix alone is the
# whole preview -- and sending more meant every inbox refresh carried the
# base64 of the newest photo in every conversation.
_INBOX_PREVIEW_CHARS = 200

# How many messages a thread request returns. Enough that most conversations
# arrive whole and nobody notices the paging; small enough that the ones full
# of photos open in one short request instead of one long one.
_THREAD_PAGE_SIZE = 40
_THREAD_PAGE_MAX = 200

# Just enough of a message to recognise its prefix, and just enough of its end
# to read a voice note's "|<seconds>" — so a thread can be listed without the
# base64 in between ever leaving the database.
_MEDIA_HEAD_CHARS = 24
_VOICE_TAIL_CHARS = 8


# Clients that know how to fetch a photo or voice note on demand say so with
# this header. Everything without it is an older build that still expects the
# base64 inline in `text`, and must keep getting it — a lean payload would
# render as a broken bubble there.
def _wants_lean_media(request):
    try:
        return int(request.headers.get('X-Neat-Client', '1')) >= 2
    except (TypeError, ValueError):
        return False


def _strip_media(text):
    """A media payload with its bytes removed but its shape intact.

    The client keeps the prefix (so it still knows this is a photo, or a voice
    note of a given length) and asks `message_media` for the rest, once, when
    it actually needs to draw or play it.
    """
    if text.startswith('__neat_image__:'):
        return '__neat_image__:'
    if text.startswith('__neat_voice__:'):
        # Duration lives after the final '|' and is what the bubble draws
        # before anyone presses play, so it stays.
        separator = text.rfind('|')
        return '__neat_voice__:' + text[separator:] if separator >= 0 else '__neat_voice__:'
    return text


def _load_thread_window(queryset, limit, lean):
    """The newest `limit` messages, and whether older ones exist.

    For a client that fetches media on demand, the base64 never leaves the
    database: `text` is deferred and only the head (enough to recognise a
    prefix) and tail (a voice note's duration) come back, with the full column
    fetched in one follow-up query for the plain messages that need it. The
    database is on the other side of a network hop, so a thread of photos was
    several megabytes crossing it on every open and every poll.
    """
    queryset = queryset.prefetch_related('opens_rows')
    if not lean:
        window = list(queryset.order_by('-created', '-id')[: limit + 1])
        has_more = len(window) > limit
        window = window[:limit]
        window.reverse()
        return window, has_more

    window = list(
        queryset.defer('text')
        .annotate(head=Substr('text', 1, _MEDIA_HEAD_CHARS), tail=Right('text', _VOICE_TAIL_CHARS))
        .order_by('-created', '-id')[: limit + 1]
    )
    has_more = len(window) > limit
    window = window[:limit]
    window.reverse()

    plain = []
    for message in window:
        head = message.head or ''
        if message.photo_mode:
            # Withheld from every payload anyway; give it a value so nothing
            # below triggers a fetch of the deferred column.
            message.text = ''
        elif head.startswith('__neat_image__:'):
            message.text = '__neat_image__:'
            message.media_withheld = True
        elif head.startswith('__neat_voice__:'):
            separator = (message.tail or '').rfind('|')
            message.text = '__neat_voice__:' + (message.tail[separator:] if separator >= 0 else '')
            message.media_withheld = True
        else:
            plain.append(message)

    if plain:
        texts = dict(
            Message.objects.filter(id__in=[m.id for m in plain]).values_list('id', 'text')
        )
        for message in plain:
            message.text = texts.get(message.id, '')

    return window, has_more


def _page_limit(request):
    try:
        limit = int(request.GET.get('limit', _THREAD_PAGE_SIZE))
    except (TypeError, ValueError):
        return _THREAD_PAGE_SIZE
    return max(1, min(limit, _THREAD_PAGE_MAX))


def _inbox_preview(message):
    """The inbox line for a message, from its first few hundred characters.

    Reads `preview_text` — the truncated copy the query annotates — and never
    `message.text`, which is deferred: touching that would fetch the whole
    column back from the database and undo the point of this.
    """
    if message is None:
        return ''
    if message.photo_mode:
        return '__neat_image__:'
    text = getattr(message, 'preview_text', '') or ''
    for prefix in _NON_TEXT_PREFIXES:
        if text.startswith(prefix):
            return prefix
    return text


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


def _message_to_dict(message, preview_map=None, lean=False, viewer=None):
    reactions = {}
    for reaction in message.reactions.select_related('user').all():
        reactions.setdefault(reaction.emoji, []).append(reaction.user.username)
    data = {
        'id': message.id,
        'sender': message.sender.username,
        # A temporary photo's bytes never ride along with the thread — not to
        # the recipient, who would then hold a copy of a picture they have not
        # opened yet, and not to the sender either, which is what makes "view
        # once" mean anything at all. `message_open` is the only way to them,
        # and it is also what spends the viewing.
        'text': '' if message.photo_mode else (
            _strip_media(message.text) if lean else message.text
        ),
        'created': message.created.isoformat(),
        'reactions': reactions,
        'edited': message.edited,
    }
    if message.photo_mode:
        data['photo_mode'] = message.photo_mode
        # Per person: how many viewings *you* have left, and whether anyone
        # else has opened it (which is what the sender's "Opened" means).
        # Without a viewer — a broadcast at send time, when nobody has opened
        # anything — everyone still has the full budget, so that is the honest
        # answer for both sides.
        viewer_id = getattr(viewer, 'id', None)
        data['opens_left'] = (
            message.opens_left_for(viewer_id) if viewer_id else message.open_budget
        )
        data['opened_by_other'] = any(
            row.count > 0 for row in message.opens_rows.all() if row.user_id != viewer_id
        )
    elif lean and (getattr(message, 'media_withheld', False) or data['text'] != message.text):
        # Says "there are bytes behind this one" so the client can tell a
        # withheld payload from an empty message.
        data['media'] = True
    # Cards the server already holds ride along with the thread, so a chat
    # opens with its thumbnails rather than filling them in one request at a
    # time. Absent for a link nobody has resolved yet — the client asks for
    # that one itself.
    if preview_map:
        url = linkpreview_service.first_url(message.text or '')
        if url:
            preview = preview_map.get(linkpreview_service.normalise_url(url))
            if preview:
                data['link_preview'] = preview
    return data


def _push_message(conversation, event, message, members=None):
    """Pushes a message update, per member when their views of it differ.

    A temporary photo's payload says how many viewings *you* have left, so one
    shared broadcast would hand each side the other's state — and a reaction
    on a photo the recipient had already opened would quietly tell them their
    viewing was back. Anything else is the same for everybody and goes out as
    one broadcast.
    """
    if not message.photo_mode:
        broadcast_to_conversation(
            conversation,
            event,
            {'conversation_id': conversation.id, 'message': _message_to_dict(message)},
        )
        return
    if members is None:
        members = list(conversation.members.select_related('user').all())
    for member in members:
        push_to_user(
            member.user_id,
            event,
            {
                'conversation_id': conversation.id,
                'message': _message_to_dict(message, viewer=member.user),
            },
        )


def _preview_map_for_messages(messages):
    """Already-resolved cards for a page of messages, in one query, no fetch."""
    try:
        return linkpreview_service.previews_for_texts(
            [(m.text or '') for m in messages]
        )
    except Exception:
        # Previews are decoration; a conversation must render without them.
        return {}


def _is_typing(member):
    return bool(member and member.typing_at and timezone.now() - member.typing_at < TYPING_TTL)


def _conversation_to_dict(conversation, viewer):
    members = list(conversation.members.select_related('user__profile').all())
    member = next((m for m in members if m.user_id == viewer.id), None)
    other_member = next((m for m in members if m.user_id != viewer.id), None)
    other = other_member.user if other_member else viewer

    # "Delete for me" clears this viewer's own history up to that point --
    # the other participant's ConversationMember row (and therefore their own
    # view) is untouched, so only the deleter loses the old messages.
    messages_qs = conversation.messages.all()
    if member and member.hidden_at:
        messages_qs = messages_qs.filter(created__gt=member.hidden_at)

    # `.only()` keeps the base64 in `text` out of the row entirely: the
    # preview is computed from a truncated copy the database makes for us.
    last_message = (
        messages_qs.select_related('sender')
        .defer('text')
        .annotate(preview_text=Substr('text', 1, _INBOX_PREVIEW_CHARS))
        .last()
    )

    unread_qs = messages_qs.exclude(sender=viewer)
    read_floor = member.last_read_at if member else None
    if member and member.hidden_at and (read_floor is None or member.hidden_at > read_floor):
        read_floor = member.hidden_at
    if read_floor:
        unread_qs = unread_qs.filter(created__gt=read_floor)

    other_profile = getattr(other, 'profile', None)
    other_last_active = getattr(other_profile, 'last_active', None) if other_profile else None
    return {
        'id': conversation.id,
        'otherUser': other.username,
        'otherFullName': getattr(other_profile, 'full_name', '') if other_profile else '',
        'otherAvatarUrl': getattr(other_profile, 'avatar_url', '') if other_profile else '',
        'otherLastActive': other_last_active.isoformat() if other_last_active else '',
        # Never the bytes: a photo reads as "sent a photo" here, and for a
        # temporary one handing them over would skip the opening entirely.
        'lastMessage': _inbox_preview(last_message),
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
        # Members only. Prefetching 'messages__sender' here pulled every
        # message of the thread — base64 photos and all — into memory before
        # doing anything, on every send, react, delete and open.
        conversation = Conversation.objects.prefetch_related('members__user').get(
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
        .prefetch_related('members__user')
        .order_by('-updated')
    )
    data = []
    for conversation in conversations:
        members = list(conversation.members.all())
        viewer_member = next((m for m in members if m.user_id == viewer.id), None)
        # "Delete" hides the thread from just this viewer's inbox (their side
        # only — the other participant keeps it). Any activity since the hide
        # (a new incoming message bumps `conversation.updated`) surfaces it
        # again automatically, matching WhatsApp/iMessage-style "delete for me".
        if viewer_member and viewer_member.hidden_at and viewer_member.hidden_at >= conversation.updated:
            continue
        other_members = [m.user for m in members if m.user_id != viewer.id]
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
        member = ConversationMember.objects.filter(conversation=conversation, user=viewer).first()
        messages = conversation.messages.select_related('sender').all()
        if member and member.hidden_at:
            # "Delete for me" — this viewer shouldn't see messages from
            # before they cleared the chat, even though the other
            # participant's copy of the conversation is untouched.
            messages = messages.filter(created__gt=member.hidden_at)
        if member:
            member.last_read_at = timezone.now()
            member.save(update_fields=['last_read_at'])

        # The newest page, not the whole history.
        #
        # Photos and voice notes live inline in `text` as base64, so a thread's
        # size grows with its media rather than its message count: sending the
        # lot meant opening a chat downloaded every picture ever exchanged in
        # it before the first bubble could be drawn, and the safety-net poll
        # did it again every 25 seconds. The client asks for older pages with
        # `before` as you scroll back.
        limit = _page_limit(request)
        before = request.GET.get('before')
        if before:
            # Cursor on the sort key rather than the id: the two normally
            # agree here, but a thread is ordered by `created` and paging by
            # id would quietly overlap or skip if they ever diverged (as they
            # do for imported posts — see posts/views.py).
            try:
                anchor = Message.objects.filter(pk=int(before)).values('created', 'id').first()
            except (TypeError, ValueError):
                return _bad_request('Invalid before')
            if anchor:
                messages = messages.filter(
                    Q(created__lt=anchor['created'])
                    | Q(created=anchor['created'], id__lt=anchor['id'])
                )

        lean = _wants_lean_media(request)
        window, has_more = _load_thread_window(messages, limit, lean)
        preview_map = _preview_map_for_messages(window)
        return _cors_json(
            JsonResponse(
                {
                    'conversation': _conversation_to_dict(conversation, viewer),
                    'messages': [
                        _message_to_dict(
                            message, preview_map=preview_map, lean=lean, viewer=viewer
                        )
                        for message in window
                    ],
                    'has_more': has_more,
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
    # Only a photo can be temporary: the mode is meaningless on anything the
    # recipient doesn't "open", and honouring it on, say, a text message would
    # hide the text behind a tap for no reason.
    photo_mode = (body.get('photo_mode') or '').strip()
    if photo_mode not in dict(Message.PHOTO_MODES) or not text.startswith('__neat_image__:'):
        photo_mode = ''
    message = Message.objects.create(
        conversation=conversation, sender=viewer, text=text, photo_mode=photo_mode
    )
    conversation.save(update_fields=['updated'])
    message_dict = _message_to_dict(message)
    broadcast_to_conversation(
        conversation, 'message.new', {'conversation_id': conversation.id, 'message': message_dict}
    )
    return _cors_json(JsonResponse(message_dict, status=201))


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
    # Let the recipient learn about a brand-new thread instantly instead of
    # waiting for their next inbox poll. Built from `other`'s point of view
    # (their "otherUser" is the viewer), not the viewer's own dict.
    push_to_user(other.id, 'conversation.new', _conversation_to_dict(conversation, other))
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
def conversation_delete(request, conversation_id):
    if request.method == 'OPTIONS':
        return _cors_json(HttpResponse())

    _ensure_messages_tables()
    viewer = require_authenticated_user(request)
    if viewer is None:
        return _unauthorized()

    conversation, _other, error = _get_conversation_for_viewer(conversation_id, viewer)
    if error:
        return error

    member = ConversationMember.objects.filter(conversation=conversation, user=viewer).first()
    if member:
        member.hidden_at = timezone.now()
        member.save(update_fields=['hidden_at'])
    # "Delete for me" — only the deleter's other devices need to know.
    push_to_user(viewer.id, 'conversation.deleted', {'conversation_id': conversation.id})
    return _cors_json(JsonResponse({'ok': True}))


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
    broadcast_to_conversation(
        conversation, 'message.deleted', {'conversation_id': conversation.id, 'message_id': message_id}
    )
    return _cors_json(JsonResponse({'ok': True}))


@csrf_exempt
@require_http_methods(['PATCH', 'OPTIONS'])
def message_edit(request, conversation_id, message_id):
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

    if message.text.startswith(_NON_TEXT_PREFIXES):
        return _bad_request('This message cannot be edited')

    if timezone.now() - message.created > MESSAGE_EDIT_WINDOW:
        return _cors_json(JsonResponse({'error': 'This message can no longer be edited'}, status=403))

    body = _json_body(request)
    if body is None:
        return _bad_request('Invalid JSON')
    text = (body.get('text') or '').strip()
    if not text:
        return _bad_request('Message text is required')

    message.text = text
    message.edited = True
    message.save(update_fields=['text', 'edited'])
    message_dict = _message_to_dict(message)
    broadcast_to_conversation(
        conversation, 'message.edited', {'conversation_id': conversation.id, 'message': message_dict}
    )
    return _cors_json(JsonResponse({'message': message_dict}))


@require_http_methods(['GET', 'OPTIONS'])
def message_media(request, conversation_id, message_id):
    """The bytes of one photo or voice note, fetched when the client needs them.

    Threads no longer carry media inline for clients that understand this
    route (see `_strip_media`): a conversation's payload is now its text, and
    a picture is downloaded once, when it is about to be drawn, and kept on
    the device from then on.

    Temporary photos are deliberately not served here — opening one costs a
    viewing, so it goes through `message_open` instead.
    """
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

    if message.photo_mode:
        return _cors_json(JsonResponse({'error': 'Use the open endpoint'}, status=403))
    if not message.text.startswith(('__neat_image__:', '__neat_voice__:')):
        return _bad_request('Message has no media')

    response = _cors_json(JsonResponse({'text': message.text}))
    # A message's media never changes, so let the client keep it. This is the
    # one DM response worth caching, which is why it overrides the no-store
    # default every other endpoint here sets.
    response['Cache-Control'] = 'private, max-age=31536000, immutable'
    del response['Pragma']
    del response['Expires']
    return response


@csrf_exempt
@require_http_methods(['POST', 'OPTIONS'])
def message_open(request, conversation_id, message_id):
    """Spends one viewing of a temporary photo and hands back its bytes.

    This is the only route to a "view once" / "allow replay" picture — the
    thread itself never carries one (see `_message_to_dict`). Opening is
    therefore an act, not a read: the count goes up, and on the last allowed
    viewing the bytes are deleted from the row, so nothing can serve them
    again to anybody.

    Everyone in the conversation has their own viewings, the sender included:
    spending yours is your business and must not touch theirs. The picture is
    only deleted once nobody has any left.
    """
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

    if not message.photo_mode:
        return _bad_request('Message is not a temporary photo')
    if message.is_spent_for(viewer.id):
        return _cors_json(JsonResponse({'error': 'Photo is no longer available'}, status=410))

    photo = message.text
    if not photo:
        return _cors_json(JsonResponse({'error': 'Photo is no longer available'}, status=410))

    row, _created = MessageOpen.objects.get_or_create(message=message, user=viewer)
    row.count += 1
    row.save(update_fields=['count', 'updated'])
    message.opens += 1
    fields = ['opens']

    members = list(conversation.members.select_related('user').all())
    spent = dict(MessageOpen.objects.filter(message=message).values_list('user_id', 'count'))
    # Only once *nobody* has a viewing left does the picture go: deleting it
    # when the first person finishes theirs would take the other's with it.
    if all(spent.get(m.user_id, 0) >= message.open_budget for m in members):
        message.text = ''
        fields.append('text')
    message.save(update_fields=fields)

    # Reloaded with its open rows so the payloads below can be built without a
    # query each.
    message = Message.objects.prefetch_related('opens_rows').get(pk=message.pk)

    _push_message(conversation, 'message.edited', message, members=members)

    return _cors_json(
        JsonResponse({'message': _message_to_dict(message, viewer=viewer), 'photo': photo})
    )


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
    _push_message(conversation, 'message.reaction', message)
    message_dict = _message_to_dict(message, viewer=viewer)
    return _cors_json(JsonResponse({'message': message_dict}))


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
