"""Push events to connected DM WebSocket clients (see consumers.py).

Two call sites need this:
  - The existing REST views in views.py (plain sync Django views) call the
    sync entrypoints (push_to_user/broadcast_to_conversation) right after a
    successful mutation, purely to notify anyone connected -- the REST
    response shape is completely unchanged.
  - MessagingConsumer itself (already running inside an async event loop)
    calls the `a`-prefixed async versions directly -- asgiref's
    async_to_sync() raises if called from a thread that's already running an
    event loop, so the sync wrappers below must never be used from consumer
    code.

Every path is wrapped so a Channels/Redis hiccup can never raise out of an
HTTP view that has already committed its database write and is about to
return 200/201.
"""

import logging

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.layers import get_channel_layer

logger = logging.getLogger(__name__)


async def _group_send(group_name, event, payload):
    layer = get_channel_layer()
    if layer is None:
        return
    try:
        await layer.group_send(
            group_name,
            {'type': 'push.event', 'event': event, 'payload': payload},
        )
    except Exception:
        logger.exception('Failed to push %s to group %s', event, group_name)


async def apush_to_user(user_id, event, payload):
    await _group_send(f'user_{user_id}', event, payload)


async def abroadcast_to_conversation(conversation, event, payload, member_ids=None):
    """Push `event` to every member of `conversation` (all of their devices).

    `member_ids` lets a caller target a subset without an extra query when it
    already knows who should get it -- e.g. conversation_delete is "delete
    for me" and must only reach the deleter, not the other participant.
    """
    ids = member_ids
    if ids is None:
        ids = await database_sync_to_async(list)(
            conversation.members.values_list('user_id', flat=True)
        )
    for user_id in ids:
        await apush_to_user(user_id, event, payload)


def push_to_user(user_id, event, payload):
    """Sync entrypoint — call this from regular (non-async) Django views."""
    async_to_sync(apush_to_user)(user_id, event, payload)


def broadcast_to_conversation(conversation, event, payload, member_ids=None):
    """Sync entrypoint — call this from regular (non-async) Django views."""
    async_to_sync(abroadcast_to_conversation)(
        conversation, event, payload, member_ids=member_ids
    )
