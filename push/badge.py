"""The number iOS paints on the app icon.

iOS only shows the red bubble if the APNs payload carries `aps.badge`, and it
is an absolute value rather than an increment — so every push has to state the
recipient's whole outstanding count, not "one more than before". That also
means the number is only ever as correct as the last push or the last time the
app set it itself (see /api/push/badge/ and PushService.refreshBadge on the
client), which is why both paths share this one function.

"Outstanding" is defined exactly as the two red counts already shown inside the
app: unread direct messages plus unread activity notifications.
"""

import logging

from django.db.models import Q

logger = logging.getLogger(__name__)


def badge_count(user):
    """Unread DMs + unread notifications for `user`, or None if it can't be
    determined — callers omit the badge entirely rather than send a wrong one,
    since a stale number on the icon is worse than no number."""
    try:
        return _unread_messages(user) + _unread_notifications(user)
    except Exception:
        logger.exception('badge_count failed for user %s', getattr(user, 'username', user))
        return None


def _unread_notifications(user):
    from accounts.models import Notification

    return Notification.objects.filter(recipient=user, is_read=False).count()


def _unread_messages(user):
    """Mirrors the per-conversation `unreadCount` in dm_messages/views.py:
    messages someone else sent after this member last read the conversation,
    with "delete for me" (hidden_at) acting as a floor of its own.

    Two queries rather than one per conversation: the read floor differs per
    membership, so the floors are collected first and then folded into a single
    OR'd count.
    """
    from dm_messages.models import ConversationMember, Message

    members = ConversationMember.objects.filter(user=user).values_list(
        'conversation_id', 'last_read_at', 'hidden_at'
    )

    clauses = []
    for conversation_id, last_read_at, hidden_at in members:
        floor = last_read_at
        if hidden_at and (floor is None or hidden_at > floor):
            floor = hidden_at
        if floor:
            clauses.append(Q(conversation_id=conversation_id, created__gt=floor))
        else:
            clauses.append(Q(conversation_id=conversation_id))

    if not clauses:
        return 0

    # Guarded above because an empty Q() matches everything — folding nothing
    # would count every message on the service.
    condition = clauses[0]
    for clause in clauses[1:]:
        condition |= clause

    return Message.objects.filter(condition).exclude(sender=user).count()
