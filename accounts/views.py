import json

from django.contrib.auth import authenticate, get_user_model
from django.db import IntegrityError
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .auth import require_authenticated_user
from .models import AuthToken, Follow
from .serializers import auth_payload, ensure_profile, user_to_dict


User = get_user_model()


def _cors_json(response):
    response['Access-Control-Allow-Origin'] = '*'
    response['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response['Access-Control-Allow-Methods'] = 'GET,POST,PATCH,DELETE,OPTIONS'
    response['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response['Pragma'] = 'no-cache'
    response['Expires'] = '0'
    return response


def _json_body(request):
    try:
        return json.loads(request.body.decode('utf-8') or '{}')
    except Exception:
        return None


def _unauthorized():
    return _cors_json(JsonResponse({'error': 'Authentication required'}, status=401))


@csrf_exempt
@require_http_methods(['POST', 'OPTIONS'])
def signup(request):
    if request.method == 'OPTIONS':
        return _cors_json(HttpResponse())

    body = _json_body(request)
    if body is None:
        return _cors_json(JsonResponse({'error': 'Invalid JSON'}, status=400))

    username = (body.get('username') or '').strip()
    email = (body.get('email') or '').strip()
    password = body.get('password') or ''
    full_name = (body.get('fullName') or body.get('full_name') or '').strip()

    if not username or not password:
        return _cors_json(JsonResponse({'error': 'Username and password are required'}, status=400))
    if len(password) < 8:
        return _cors_json(JsonResponse({'error': 'Password must be at least 8 characters'}, status=400))

    try:
        user = User.objects.create_user(username=username, email=email, password=password)
    except IntegrityError:
        return _cors_json(JsonResponse({'error': 'Username is already taken'}, status=400))

    profile = ensure_profile(user)
    profile.full_name = full_name
    profile.bio = (body.get('bio') or '').strip()
    profile.save(update_fields=['full_name', 'bio'])
    token = AuthToken.create_for_user(user)
    return _cors_json(JsonResponse(auth_payload(user, token), status=201))


@csrf_exempt
@require_http_methods(['POST', 'OPTIONS'])
def login(request):
    if request.method == 'OPTIONS':
        return _cors_json(HttpResponse())

    body = _json_body(request)
    if body is None:
        return _cors_json(JsonResponse({'error': 'Invalid JSON'}, status=400))

    username = (body.get('username') or body.get('email') or '').strip()
    password = body.get('password') or ''
    user = authenticate(username=username, password=password)
    if user is None:
        return _cors_json(JsonResponse({'error': 'Invalid username or password'}, status=400))

    ensure_profile(user)
    token = AuthToken.create_for_user(user)
    return _cors_json(JsonResponse(auth_payload(user, token)))


@csrf_exempt
@require_http_methods(['POST', 'OPTIONS'])
def logout(request):
    if request.method == 'OPTIONS':
        return _cors_json(HttpResponse())

    header = request.headers.get('Authorization', '')
    if header.startswith('Token '):
        AuthToken.objects.filter(key=header.removeprefix('Token ').strip()).delete()
    return _cors_json(JsonResponse({'ok': True}))


@csrf_exempt
@require_http_methods(['GET', 'PATCH', 'OPTIONS'])
def me(request):
    if request.method == 'OPTIONS':
        return _cors_json(HttpResponse())

    user = require_authenticated_user(request)
    if user is None:
        return _unauthorized()

    profile = ensure_profile(user)
    if request.method == 'PATCH':
        body = _json_body(request)
        if body is None:
            return _cors_json(JsonResponse({'error': 'Invalid JSON'}, status=400))
        user.email = (body.get('email') or user.email).strip()
        user.save(update_fields=['email'])
        profile.full_name = (body.get('fullName') or body.get('full_name') or profile.full_name).strip()
        profile.bio = (body.get('bio') if body.get('bio') is not None else profile.bio).strip()
        profile.avatar_url = (body.get('avatarUrl') or body.get('avatar_url') or profile.avatar_url).strip()
        profile.save(update_fields=['full_name', 'bio', 'avatar_url'])

    return _cors_json(JsonResponse({'user': user_to_dict(user, viewer=user)}))


@csrf_exempt
@require_http_methods(['GET', 'OPTIONS'])
def profile_detail(request, username):
    if request.method == 'OPTIONS':
        return _cors_json(HttpResponse())

    viewer = require_authenticated_user(request)
    if viewer is None:
        return _unauthorized()

    try:
        user = User.objects.get(username=username)
    except User.DoesNotExist:
        return _cors_json(JsonResponse({'error': 'Profile not found'}, status=404))

    return _cors_json(JsonResponse({'user': user_to_dict(user, viewer=viewer)}))


@csrf_exempt
@require_http_methods(['POST', 'OPTIONS'])
def follow_toggle(request, username):
    if request.method == 'OPTIONS':
        return _cors_json(HttpResponse())

    viewer = require_authenticated_user(request)
    if viewer is None:
        return _unauthorized()

    try:
        target = User.objects.get(username=username)
    except User.DoesNotExist:
        return _cors_json(JsonResponse({'error': 'Profile not found'}, status=404))
    if target == viewer:
        return _cors_json(JsonResponse({'error': 'You cannot follow yourself'}, status=400))

    body = _json_body(request) or {}
    should_follow = body.get('follow')
    existing = Follow.objects.filter(follower=viewer, following=target)
    if should_follow is False:
        existing.delete()
    elif existing.exists():
        existing.delete()
    else:
        Follow.objects.create(follower=viewer, following=target)

    return _cors_json(JsonResponse({'user': user_to_dict(target, viewer=viewer)}))
