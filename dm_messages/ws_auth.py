from channels.db import database_sync_to_async
from django.contrib.auth.models import AnonymousUser

from accounts.models import AuthToken


class TokenAuthMiddleware:
    """ASGI middleware for the DM WebSocket route.

    Unlike Channels' built-in AuthMiddlewareStack (which authenticates via a
    Django session cookie), every HTTP request in this app is authenticated
    with a static bearer token (accounts.models.AuthToken) sent as an
    Authorization header -- and there's no cookie to read at the WebSocket
    handshake either. Putting that same token in the wss:// URL as a
    ?token= query string would work, but query strings land in access logs
    and proxy logs, and the token never expires/rotates. So this middleware
    doesn't authenticate at connect time at all: it just seeds an
    AnonymousUser placeholder, and MessagingConsumer.connect() requires an
    explicit {"action": "auth", "token": ...} as the first message instead,
    resolved via resolve_token() below.
    """

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        scope['user'] = AnonymousUser()
        return await self.inner(scope, receive, send)


@database_sync_to_async
def resolve_token(token_key):
    if not token_key:
        return None
    try:
        return AuthToken.objects.select_related('user', 'user__profile').get(key=token_key).user
    except AuthToken.DoesNotExist:
        return None
