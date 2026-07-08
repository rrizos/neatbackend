import json

from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from accounts.auth import require_authenticated_user

from .models import DeviceToken


def _cors_json(response):
    response['Access-Control-Allow-Origin'] = '*'
    response['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response['Access-Control-Allow-Methods'] = 'POST,OPTIONS'
    response['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response['Pragma'] = 'no-cache'
    response['Expires'] = '0'
    return response


def _unauthorized():
    return _cors_json(JsonResponse({'error': 'Authentication required'}, status=401))


def _bad_request(message):
    return _cors_json(JsonResponse({'error': message}, status=400))


def _json_body(request):
    try:
        return json.loads(request.body.decode('utf-8') or '{}')
    except Exception:
        return None


@csrf_exempt
@require_http_methods(['POST', 'OPTIONS'])
def register_device(request):
    if request.method == 'OPTIONS':
        return _cors_json(HttpResponse())

    viewer = require_authenticated_user(request)
    if viewer is None:
        return _unauthorized()

    body = _json_body(request)
    if body is None:
        return _bad_request('Invalid JSON')

    token = (body.get('token') or '').strip()
    platform = (body.get('platform') or '').strip()
    if not token or platform not in ('ios', 'android'):
        return _bad_request('token and a valid platform are required')

    DeviceToken.objects.update_or_create(
        token=token,
        defaults={'user': viewer, 'platform': platform},
    )
    return _cors_json(JsonResponse({'ok': True}))


@csrf_exempt
@require_http_methods(['POST', 'OPTIONS'])
def unregister_device(request):
    if request.method == 'OPTIONS':
        return _cors_json(HttpResponse())

    viewer = require_authenticated_user(request)
    if viewer is None:
        return _unauthorized()

    body = _json_body(request)
    if body is None:
        return _bad_request('Invalid JSON')

    token = (body.get('token') or '').strip()
    if not token:
        return _bad_request('token is required')

    DeviceToken.objects.filter(token=token, user=viewer).delete()
    return _cors_json(JsonResponse({'ok': True}))
