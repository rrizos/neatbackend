"""Which client is asking, available anywhere without threading `request`.

Avatars are serialised in a dozen places — the feed, the inbox, notifications,
comments, search, followers, mutuals — and most of those functions are model
methods or plain helpers that never see a request. Passing one down to all of
them would touch far more code than the decision is worth, and would be easy to
forget at a new call site.

A context variable set once by middleware answers it instead. Defaults to 1,
the oldest behaviour, so anything that runs outside a request (a management
command, the transcode worker) serialises exactly as it always did.
"""

from contextvars import ContextVar

_client_version: ContextVar[int] = ContextVar('neat_client_version', default=1)

# Builds from this version on are given avatar *URLs* rather than base64.
AVATAR_URL_CLIENT = 3


def set_client_version(value):
    _client_version.set(value)


def client_version():
    return _client_version.get()


def wants_url_avatars():
    return _client_version.get() >= AVATAR_URL_CLIENT


class ClientVersionMiddleware:
    """Reads X-Neat-Client once per request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            version = int(request.headers.get('X-Neat-Client', '1'))
        except (TypeError, ValueError):
            version = 1
        token = _client_version.set(version)
        try:
            return self.get_response(request)
        finally:
            # Workers are reused across requests; leaving this set would let one
            # client's capabilities leak into the next request on that thread.
            _client_version.reset(token)
