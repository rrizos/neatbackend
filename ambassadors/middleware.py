"""The requesting address, where a signal can reach it.

Django signals do not get the request, and the one thing the network fallback
needs is exactly the thing only the request knows. A contextvar is how the rest
of this codebase already solves that (accounts/client_version.py,
web/analytics.py), so it is how this solves it too.
"""

import contextvars

_client_ip = contextvars.ContextVar('neat_ambassador_client_ip', default='')


def current_ip():
    return _client_ip.get()


class ClientIPMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from accounts.ratelimit import client_ip

        token = _client_ip.set(client_ip(request))
        try:
            return self.get_response(request)
        finally:
            # Reset rather than leave it set: gunicorn threads are reused, and
            # a stale address would attribute the next signup to the last
            # visitor's network.
            _client_ip.reset(token)
