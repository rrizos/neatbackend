import json
import logging

from django.contrib.auth import authenticate, get_user_model
from django.db import IntegrityError
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .auth import require_authenticated_user
from .models import AuthToken, Follow, Notification
from .serializers import auth_payload, ensure_profile, user_to_dict


User = get_user_model()
logger = logging.getLogger(__name__)


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


def _bad_request(message):
    return _cors_json(JsonResponse({'error': message}, status=400))


def _server_error(message='Internal server error'):
    return _cors_json(JsonResponse({'error': message}, status=500))


def _handle_exception(context, exc):
    logger.exception('%s failed', context)
    detail = str(exc).strip()
    if detail:
        return _server_error(detail)
    return _server_error()


def _user_list_response(users, viewer):
    return _cors_json(
        JsonResponse(
            {
                'users': [user_to_dict(user, viewer=viewer) for user in users],
            }
        )
    )


def _notify(recipient, actor, verb, target_type='', target_id='', target_text=''):
    if recipient == actor:
        return
    Notification.objects.create(
        recipient=recipient,
        actor=actor,
        verb=verb,
        target_type=target_type,
        target_id=str(target_id or ''),
        target_text=target_text[:255],
    )


@csrf_exempt
@require_http_methods(['GET', 'OPTIONS'])
def health(request):
    if request.method == 'OPTIONS':
        return _cors_json(HttpResponse())
    return _cors_json(JsonResponse({'ok': True, 'service': 'auth'}))


@csrf_exempt
@require_http_methods(['POST', 'OPTIONS'])
def signup(request):
    try:
        if request.method == 'OPTIONS':
            return _cors_json(HttpResponse())

        body = _json_body(request)
        if body is None:
            return _bad_request('Invalid JSON')

        username = (body.get('username') or '').strip()
        email = (body.get('email') or '').strip()
        password = body.get('password') or ''
        full_name = (body.get('fullName') or body.get('full_name') or '').strip()
        city = (body.get('city') or '').strip()

        if not username or not password:
            return _bad_request('Username and password are required')
        if len(password) < 8:
            return _bad_request('Password must be at least 8 characters')

        try:
            user = User.objects.create_user(username=username, email=email, password=password)
        except IntegrityError:
            return _bad_request('Username is already taken')

        profile = ensure_profile(user)
        profile.full_name = full_name
        profile.bio = (body.get('bio') or '').strip()
        profile.city = city
        profile.save(update_fields=['full_name', 'bio', 'city'])
        token = AuthToken.create_for_user(user)
        return _cors_json(JsonResponse(auth_payload(user, token), status=201))
    except Exception as exc:
        return _handle_exception('signup', exc)


@csrf_exempt
@require_http_methods(['POST', 'OPTIONS'])
def login(request):
    try:
        if request.method == 'OPTIONS':
            return _cors_json(HttpResponse())

        body = _json_body(request)
        if body is None:
            return _bad_request('Invalid JSON')

        username = (body.get('username') or body.get('email') or '').strip()
        password = body.get('password') or ''
        user = authenticate(username=username, password=password)
        if user is None:
            return _bad_request('Invalid username or password')

        ensure_profile(user)
        token = AuthToken.create_for_user(user)
        return _cors_json(JsonResponse(auth_payload(user, token)))
    except Exception as exc:
        return _handle_exception('login', exc)


@csrf_exempt
@require_http_methods(['POST', 'OPTIONS'])
def logout(request):
    try:
        if request.method == 'OPTIONS':
            return _cors_json(HttpResponse())

        header = request.headers.get('Authorization', '')
        if header.startswith('Token '):
            AuthToken.objects.filter(key=header.removeprefix('Token ').strip()).delete()
        return _cors_json(JsonResponse({'ok': True}))
    except Exception as exc:
        return _handle_exception('logout', exc)


@csrf_exempt
@require_http_methods(['GET', 'PATCH', 'OPTIONS'])
def me(request):
    try:
        if request.method == 'OPTIONS':
            return _cors_json(HttpResponse())

        user = require_authenticated_user(request)
        if user is None:
            return _unauthorized()

        profile = ensure_profile(user)
        if request.method == 'PATCH':
            body = _json_body(request)
            if body is None:
                return _bad_request('Invalid JSON')
            user.email = (body.get('email') or user.email).strip()
            user.save(update_fields=['email'])
            profile.full_name = (body.get('fullName') or body.get('full_name') or profile.full_name).strip()
            profile.bio = (body.get('bio') if body.get('bio') is not None else profile.bio).strip()
            profile.city = (body.get('city') or profile.city).strip()
            profile.avatar_url = (body.get('avatarUrl') or body.get('avatar_url') or profile.avatar_url).strip()
            profile.save(update_fields=['full_name', 'bio', 'city', 'avatar_url'])

        return _cors_json(JsonResponse({'user': user_to_dict(user, viewer=user)}))
    except Exception as exc:
        return _handle_exception('me', exc)


@csrf_exempt
@require_http_methods(['GET', 'OPTIONS'])
def profile_detail(request, username):
    try:
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
    except Exception as exc:
        return _handle_exception('profile_detail', exc)


@csrf_exempt
@require_http_methods(['GET', 'OPTIONS'])
def followers_list(request, username):
    try:
        if request.method == 'OPTIONS':
            return _cors_json(HttpResponse())

        viewer = require_authenticated_user(request)
        if viewer is None:
            return _unauthorized()

        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            return _cors_json(JsonResponse({'error': 'Profile not found'}, status=404))

        follower_ids = Follow.objects.filter(following=user).values_list('follower_id', flat=True)
        users = User.objects.filter(id__in=follower_ids).order_by('username')
        return _user_list_response(users, viewer)
    except Exception as exc:
        return _handle_exception('followers_list', exc)


@csrf_exempt
@require_http_methods(['GET', 'OPTIONS'])
def following_list(request, username):
    try:
        if request.method == 'OPTIONS':
            return _cors_json(HttpResponse())

        viewer = require_authenticated_user(request)
        if viewer is None:
            return _unauthorized()

        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            return _cors_json(JsonResponse({'error': 'Profile not found'}, status=404))

        following_ids = Follow.objects.filter(follower=user).values_list('following_id', flat=True)
        users = User.objects.filter(id__in=following_ids).order_by('username')
        return _user_list_response(users, viewer)
    except Exception as exc:
        return _handle_exception('following_list', exc)


@csrf_exempt
@require_http_methods(['GET', 'OPTIONS'])
def suggestions(request):
    try:
        if request.method == 'OPTIONS':
            return _cors_json(HttpResponse())

        viewer = require_authenticated_user(request)
        if viewer is None:
            return _unauthorized()

        viewer_city = ensure_profile(viewer).city
        following_ids = Follow.objects.filter(follower=viewer).values_list('following_id', flat=True)
        candidate_ids = [
            user
            for user in User.objects.exclude(id=viewer.id).exclude(id__in=following_ids).order_by('username')
            if ensure_profile(user).city == viewer_city
        ]
        users = candidate_ids[:10]
        if not users:
            users = [
                user
                for user in User.objects.exclude(id=viewer.id).order_by('username')
                if ensure_profile(user).city == viewer_city
            ][:10]
        return _user_list_response(users, viewer)
    except Exception as exc:
        return _handle_exception('suggestions', exc)


@csrf_exempt
@require_http_methods(['GET', 'OPTIONS'])
def search_users(request):
    try:
        if request.method == 'OPTIONS':
            return _cors_json(HttpResponse())

        viewer = require_authenticated_user(request)
        if viewer is None:
            return _unauthorized()

        query = (request.GET.get('q') or '').strip().lower()
        viewer_city = ensure_profile(viewer).city
        users = []
        for user in User.objects.exclude(id=viewer.id).order_by('username'):
            profile = ensure_profile(user)
            if viewer_city and profile.city != viewer_city:
                continue
            if query and query not in user.username.lower() and query not in profile.full_name.lower() and query not in profile.bio.lower():
                continue
            users.append(user)
            if len(users) >= 25:
                break
        return _user_list_response(users, viewer)
    except Exception as exc:
        return _handle_exception('search_users', exc)


@csrf_exempt
@require_http_methods(['GET', 'OPTIONS'])
def search_users(request):
    try:
        if request.method == 'OPTIONS':
            return _cors_json(HttpResponse())

        viewer = require_authenticated_user(request)
        if viewer is None:
            return _unauthorized()

        query = (request.GET.get('q') or '').strip().lower()
        viewer_city = ensure_profile(viewer).city
        users_qs = User.objects.exclude(id=viewer.id).order_by('username')
        if viewer_city:
            users_qs = [
                user for user in users_qs if ensure_profile(user).city == viewer_city
            ]
        else:
            users_qs = list(users_qs)

        if query:
            users_qs = [
                user
                for user in users_qs
                if query in user.username.lower()
                or query in ensure_profile(user).full_name.lower()
                or query in ensure_profile(user).bio.lower()
            ]

        return _user_list_response(users_qs[:25], viewer)
    except Exception as exc:
        return _handle_exception('search_users', exc)


@csrf_exempt
@require_http_methods(['POST', 'OPTIONS'])
def follow_toggle(request, username):
    try:
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
            return _bad_request('You cannot follow yourself')
        if ensure_profile(target).city != ensure_profile(viewer).city:
            return _bad_request('You can only follow people in your city')

        body = _json_body(request) or {}
        should_follow = body.get('follow')
        existing = Follow.objects.filter(follower=viewer, following=target)
        if should_follow is False:
            existing.delete()
        elif existing.exists():
            existing.delete()
        else:
            Follow.objects.create(follower=viewer, following=target)
            _notify(target, viewer, 'followed you', 'user', target.id, viewer.username)

        return _cors_json(
            JsonResponse(
                {
                    'user': user_to_dict(target, viewer=viewer),
                    'viewer': user_to_dict(viewer, viewer=viewer),
                }
            )
        )
    except Exception as exc:
        return _handle_exception('follow_toggle', exc)


@csrf_exempt
@require_http_methods(['GET', 'POST', 'OPTIONS'])
def notifications(request):
    try:
        if request.method == 'OPTIONS':
            return _cors_json(HttpResponse())

        viewer = require_authenticated_user(request)
        if viewer is None:
            return _unauthorized()

        if request.method == 'POST':
            body = _json_body(request) or {}
            ids = body.get('ids') or []
            Notification.objects.filter(recipient=viewer, id__in=ids).update(is_read=True)
            return _cors_json(JsonResponse({'ok': True}))

        qs = Notification.objects.select_related('actor').filter(recipient=viewer)
        data = [item.to_dict() for item in qs[:50]]
        unread = Notification.objects.filter(recipient=viewer, is_read=False).count()
        return _cors_json(JsonResponse({'notifications': data, 'unread': unread}))
    except Exception as exc:
        return _handle_exception('notifications', exc)
