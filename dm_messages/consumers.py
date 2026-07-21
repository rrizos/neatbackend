import asyncio
import logging

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.core.cache import cache
from django.utils import timezone

from .models import ConversationMember
from .realtime import apush_to_user
from .ws_auth import resolve_token

logger = logging.getLogger(__name__)

# How long a newly-connected socket has to send {"action": "auth", ...}
# before it gets closed.
AUTH_TIMEOUT_SECONDS = 5

# Absorbs normal mobile network blips / brief backgrounding: a disconnect
# only turns into a broadcast "offline" if the same user hasn't reconnected
# (from any device) within this window.
PRESENCE_OFFLINE_GRACE_SECONDS = 3


def _online_key(user_id):
    return f'ws_online_{user_id}'


class MessagingConsumer(AsyncJsonWebsocketConsumer):
    """One consumer instance per connected socket. A user's app opens exactly
    one of these for the whole logged-in session; it multiplexes every one of
    their conversations (new messages/edits/deletes/reactions, typing, read
    receipts, presence) rather than being scoped to a single conversation."""

    async def connect(self):
        await self.accept()
        self.user = None
        self.group_name = None
        self._auth_deadline_task = asyncio.ensure_future(self._enforce_auth_deadline())

    async def _enforce_auth_deadline(self):
        try:
            await asyncio.sleep(AUTH_TIMEOUT_SECONDS)
            if self.user is None:
                await self.close(code=4001)
        except asyncio.CancelledError:
            pass

    async def disconnect(self, close_code):
        task = getattr(self, '_auth_deadline_task', None)
        if task:
            task.cancel()
        if self.group_name:
            await self.channel_layer.group_discard(self.group_name, self.channel_name)
            await self._handle_going_offline()

    async def receive_json(self, content, **kwargs):
        action = content.get('action')
        if action == 'auth':
            await self._handle_auth(content)
            return
        if self.user is None:
            return  # ignore everything else until authenticated
        if action == 'typing':
            await self._handle_typing(content)
        elif action == 'mark_read':
            await self._handle_mark_read(content)
        elif action == 'ping':
            await self.send_json({'event': 'pong', 'payload': {}})

    async def push_event(self, event):
        """Dispatched via channel_layer.group_send({'type': 'push.event', ...})."""
        await self.send_json({'event': event['event'], 'payload': event['payload']})

    # ---- auth ----------------------------------------------------------

    async def _handle_auth(self, content):
        if self.user is not None:
            return
        user = await resolve_token(content.get('token'))
        if user is None:
            await self.send_json({'event': 'auth_error', 'payload': {}})
            await self.close(code=4001)
            return
        task = getattr(self, '_auth_deadline_task', None)
        if task:
            task.cancel()
        self.user = user
        self.group_name = f'user_{user.id}'
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.send_json({'event': 'authenticated', 'payload': {}})
        await self._handle_going_online()

    # ---- presence -------------------------------------------------------

    async def _handle_going_online(self):
        last_active = await self._touch_last_active()
        await database_sync_to_async(self._incr_online_count)()
        partner_ids = await self._conversation_partner_ids()
        payload = {
            'username': self.user.username,
            'online': True,
            'last_active': last_active.isoformat(),
        }
        for user_id in partner_ids:
            await apush_to_user(user_id, 'presence', payload)

    async def _handle_going_offline(self):
        still_online = await database_sync_to_async(self._decr_online_count)()
        await asyncio.sleep(PRESENCE_OFFLINE_GRACE_SECONDS)
        if still_online:
            return  # another device for this user is (or was) still connected
        # Re-check after the grace period in case a reconnect landed during it.
        reconnected = await database_sync_to_async(
            lambda: cache.get(_online_key(self.user.id), 0) > 0
        )()
        if reconnected:
            return
        last_active = await self._touch_last_active()
        partner_ids = await self._conversation_partner_ids()
        payload = {
            'username': self.user.username,
            'online': False,
            'last_active': last_active.isoformat(),
        }
        for user_id in partner_ids:
            await apush_to_user(user_id, 'presence', payload)

    def _incr_online_count(self):
        key = _online_key(self.user.id)
        try:
            cache.incr(key)
        except ValueError:
            cache.set(key, 1, timeout=None)

    def _decr_online_count(self):
        """Returns True if at least one other connection for this user is
        still registered after decrementing for this one."""
        key = _online_key(self.user.id)
        try:
            remaining = cache.decr(key)
        except ValueError:
            return False
        if remaining <= 0:
            cache.delete(key)
            return False
        return True

    @database_sync_to_async
    def _touch_last_active(self):
        now = timezone.now()
        profile = getattr(self.user, 'profile', None)
        if profile is not None:
            profile.last_active = now
            profile.save(update_fields=['last_active'])
        return now

    @database_sync_to_async
    def _conversation_partner_ids(self):
        return list(
            set(
                ConversationMember.objects.filter(conversation__members__user=self.user)
                .exclude(user=self.user)
                .values_list('user_id', flat=True)
            )
        )

    # ---- typing ----------------------------------------------------------

    async def _handle_typing(self, content):
        conversation_id = content.get('conversation_id')
        typing = bool(content.get('typing'))
        member = await self._get_membership(conversation_id)
        if member is None:
            return
        await self._set_typing_at(member, typing)
        payload = {
            'conversation_id': conversation_id,
            'typing': typing,
            'from': self.user.username,
        }
        member_ids = await self._other_conversation_member_ids(conversation_id)
        for user_id in member_ids:
            await apush_to_user(user_id, 'typing', payload)

    @database_sync_to_async
    def _set_typing_at(self, member, typing):
        member.typing_at = timezone.now() if typing else None
        member.save(update_fields=['typing_at'])

    # ---- read receipts ----------------------------------------------------

    async def _handle_mark_read(self, content):
        conversation_id = content.get('conversation_id')
        member = await self._get_membership(conversation_id)
        if member is None:
            return
        read_at = await self._set_last_read(member)
        payload = {
            'conversation_id': conversation_id,
            'reader': self.user.username,
            'read_at': read_at.isoformat(),
        }
        # Broadcast to every member (including the reader's other devices)
        # so read state stays in sync across all connected clients.
        member_ids = await self._all_conversation_member_ids(conversation_id)
        for user_id in member_ids:
            await apush_to_user(user_id, 'read_receipt', payload)

    @database_sync_to_async
    def _set_last_read(self, member):
        now = timezone.now()
        member.last_read_at = now
        member.save(update_fields=['last_read_at'])
        return now

    # ---- shared membership helpers -----------------------------------------

    @database_sync_to_async
    def _get_membership(self, conversation_id):
        return ConversationMember.objects.filter(
            conversation_id=conversation_id, user=self.user
        ).first()

    @database_sync_to_async
    def _other_conversation_member_ids(self, conversation_id):
        return list(
            ConversationMember.objects.filter(conversation_id=conversation_id)
            .exclude(user=self.user)
            .values_list('user_id', flat=True)
        )

    @database_sync_to_async
    def _all_conversation_member_ids(self, conversation_id):
        return list(
            ConversationMember.objects.filter(
                conversation_id=conversation_id
            ).values_list('user_id', flat=True)
        )
