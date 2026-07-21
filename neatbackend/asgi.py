"""
ASGI config for neatbackend project.

Routes plain HTTP to the regular Django app, and WebSocket connections
(currently just the DM real-time channel) to Channels via a custom token-auth
middleware — see dm_messages/ws_auth.py for why this isn't Channels' built-in
AuthMiddlewareStack.
"""

import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'neatbackend.settings')

import django

django.setup()

from channels.routing import ProtocolTypeRouter, URLRouter
from django.core.asgi import get_asgi_application

django_asgi_app = get_asgi_application()

from dm_messages.routing import websocket_urlpatterns
from dm_messages.ws_auth import TokenAuthMiddleware

application = ProtocolTypeRouter(
    {
        'http': django_asgi_app,
        'websocket': TokenAuthMiddleware(URLRouter(websocket_urlpatterns)),
    }
)
