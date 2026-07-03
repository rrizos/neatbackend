import json
import logging
import random

from django.contrib.auth import authenticate, get_user_model
from django.core.mail import send_mail
from django.db import IntegrityError
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .auth import require_authenticated_user
from .models import AuthToken, Block, Follow, Notification, PasswordResetCode, SearchHistory, blocked_user_ids, is_blocked
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
@require_http_methods(['GET', 'PATCH', 'DELETE', 'OPTIONS'])
def me(request):
    try:
        if request.method == 'OPTIONS':
            return _cors_json(HttpResponse())

        user = require_authenticated_user(request)
        if user is None:
            return _unauthorized()

        if request.method == 'DELETE':
            user.delete()
            return _cors_json(JsonResponse({'ok': True}))

        profile = ensure_profile(user)
        if request.method == 'PATCH':
            body = _json_body(request)
            if body is None:
                return _bad_request('Invalid JSON')
            new_username = (body.get('username') or user.username).strip()
            if new_username != user.username:
                if not new_username:
                    return _bad_request('Username cannot be empty')
                if User.objects.exclude(pk=user.pk).filter(username=new_username).exists():
                    return _bad_request('Username is already taken')
                user.username = new_username
            user.email = (body.get('email') or user.email).strip()
            user.save(update_fields=['username', 'email'])
            profile.full_name = (body.get('fullName') or body.get('full_name') or profile.full_name).strip()
            profile.bio = (body.get('bio') if body.get('bio') is not None else profile.bio).strip()
            profile.city = (body.get('city') or profile.city).strip()
            profile.avatar_url = (body.get('avatarUrl') or body.get('avatar_url') or profile.avatar_url).strip()
            profile.save(update_fields=['full_name', 'bio', 'city', 'avatar_url'])

        return _cors_json(JsonResponse({'user': user_to_dict(user, viewer=user)}))
    except Exception as exc:
        return _handle_exception('me', exc)


def _mutuals_for(viewer, target):
    """Return (preview_list, total_count) of users viewer follows who also follow target."""
    if viewer is None or not viewer.is_authenticated or viewer == target:
        return [], 0
    viewer_following_ids = set(
        Follow.objects.filter(follower=viewer).values_list('following_id', flat=True)
    )
    target_follower_ids = set(
        Follow.objects.filter(following=target).values_list('follower_id', flat=True)
    )
    mutual_ids = viewer_following_ids & target_follower_ids
    total = len(mutual_ids)
    preview_users = (
        User.objects
        .filter(id__in=mutual_ids)
        .select_related('profile')
        .order_by('username')[:3]
    )
    preview = []
    for u in preview_users:
        p = getattr(u, 'profile', None)
        preview.append({
            'username': u.username,
            'fullName': getattr(p, 'full_name', '') if p else '',
            'avatarUrl': getattr(p, 'avatar_url', '') if p else '',
        })
    return preview, total


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

        user_dict = user_to_dict(user, viewer=viewer)
        mutuals_preview, mutuals_total = _mutuals_for(viewer, user)
        user_dict['mutuals'] = mutuals_preview
        user_dict['mutualsCount'] = mutuals_total
        return _cors_json(JsonResponse({'user': user_dict}))
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
        hidden_ids = blocked_user_ids(viewer)
        users = User.objects.filter(id__in=follower_ids).exclude(id__in=hidden_ids).order_by('username')
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
        hidden_ids = blocked_user_ids(viewer)
        users = User.objects.filter(id__in=following_ids).exclude(id__in=hidden_ids).order_by('username')
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
        hidden_ids = blocked_user_ids(viewer)
        candidate_ids = [
            user
            for user in User.objects.exclude(id=viewer.id).exclude(id__in=following_ids).exclude(id__in=hidden_ids).order_by('username')
            if ensure_profile(user).city == viewer_city
        ]
        users = candidate_ids[:10]
        if not users:
            users = [
                user
                for user in User.objects.exclude(id=viewer.id).exclude(id__in=hidden_ids).order_by('username')
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
        hidden_ids = blocked_user_ids(viewer)
        users_qs = User.objects.exclude(id=viewer.id).exclude(id__in=hidden_ids).order_by('username')
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
        if is_blocked(viewer, target):
            return _bad_request('You cannot follow this user')
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
@require_http_methods(['POST', 'OPTIONS'])
def block_toggle(request, username):
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
            return _bad_request('You cannot block yourself')

        existing = Block.objects.filter(blocker=viewer, blocked=target)
        if existing.exists():
            existing.delete()
        else:
            Block.objects.create(blocker=viewer, blocked=target)
            # Blocking severs any existing follow relationship in both directions,
            # matching Instagram's behavior.
            Follow.objects.filter(follower=viewer, following=target).delete()
            Follow.objects.filter(follower=target, following=viewer).delete()

        return _cors_json(
            JsonResponse(
                {
                    'user': user_to_dict(target, viewer=viewer),
                    'viewer': user_to_dict(viewer, viewer=viewer),
                }
            )
        )
    except Exception as exc:
        return _handle_exception('block_toggle', exc)


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


@csrf_exempt
@require_http_methods(['POST', 'OPTIONS'])
def forgot_password(request):
    try:
        if request.method == 'OPTIONS':
            return _cors_json(HttpResponse())

        body = _json_body(request)
        if body is None:
            return _bad_request('Invalid JSON')

        identifier = (body.get('email') or '').strip()
        if not identifier:
            return _bad_request('Email or username is required')

        user = None
        if '@' in identifier:
            try:
                user = User.objects.get(email__iexact=identifier)
            except User.DoesNotExist:
                pass
        else:
            try:
                user = User.objects.get(username__iexact=identifier)
            except User.DoesNotExist:
                pass

        # Always respond ok — never reveal whether an email/username exists
        if user is None:
            return _cors_json(JsonResponse({'ok': True}))

        email = (user.email or '').strip()
        if not email:
            return _cors_json(JsonResponse({'ok': True}))

        code = f'{random.randint(0, 999999):06d}'
        PasswordResetCode.objects.filter(user=user, used=False).delete()
        PasswordResetCode.objects.create(user=user, email=email, code=code)

        html = f'''
        <div style="font-family:system-ui,-apple-system,BlinkMacSystemFont,sans-serif;background:#0a0a0a;padding:48px 24px;min-height:100vh">
          <div style="max-width:400px;margin:0 auto">
            <div style="text-align:center;margin-bottom:32px">
              <div style="display:inline-block;width:56px;height:56px;background:#1e1e1e;border-radius:14px;line-height:56px;font-size:28px">🔑</div>
            </div>
            <h2 style="color:#fff;font-size:22px;font-weight:800;margin:0 0 8px;text-align:center">Reset your password</h2>
            <p style="color:#a9a9a9;font-size:14px;margin:0 0 32px;text-align:center;line-height:1.5">Enter this code in the Neat app to continue</p>
            <div style="background:#1e1e1e;border-radius:18px;padding:28px;text-align:center;margin-bottom:24px">
              <div style="font-size:42px;font-weight:800;color:#fff;letter-spacing:14px;font-family:monospace">{code}</div>
              <p style="color:#666;font-size:12px;margin:12px 0 0">Expires in 15 minutes</p>
            </div>
            <p style="color:#555;font-size:12px;text-align:center;line-height:1.6">If you didn't request a password reset, you can safely ignore this email. Your password won't change.</p>
          </div>
        </div>
        '''

        try:
            send_mail(
                subject='Your Neat verification code',
                message=f'Your Neat password reset code is: {code}\n\nThis code expires in 15 minutes.',
                from_email=None,
                recipient_list=[email],
                html_message=html,
                fail_silently=False,
            )
        except Exception:
            logger.exception('Failed to send reset email to %s', email)
            return _server_error('Could not send email. Please check your address and try again.')

        return _cors_json(JsonResponse({'ok': True}))
    except Exception as exc:
        return _handle_exception('forgot_password', exc)


@csrf_exempt
@require_http_methods(['POST', 'OPTIONS'])
def reset_password(request):
    try:
        if request.method == 'OPTIONS':
            return _cors_json(HttpResponse())

        body = _json_body(request)
        if body is None:
            return _bad_request('Invalid JSON')

        identifier = (body.get('email') or '').strip().lower()
        code = (body.get('code') or '').strip()
        new_password = body.get('newPassword') or ''

        if not identifier or not code or not new_password:
            return _bad_request('Email, code, and new password are required')
        if len(new_password) < 8:
            return _bad_request('Password must be at least 8 characters')

        reset = None
        try:
            reset = PasswordResetCode.objects.filter(
                email__iexact=identifier,
                code=code,
                used=False,
            ).latest('created_at')
        except PasswordResetCode.DoesNotExist:
            # Identifier might be a username — look up the user's email
            try:
                user_obj = User.objects.get(username__iexact=identifier)
                reset = PasswordResetCode.objects.filter(
                    user=user_obj,
                    code=code,
                    used=False,
                ).latest('created_at')
            except (User.DoesNotExist, PasswordResetCode.DoesNotExist):
                pass

        if reset is None:
            return _bad_request('Invalid or expired code')

        if reset.is_expired():
            return _bad_request('Code has expired. Please request a new one.')

        user = reset.user
        user.set_password(new_password)
        user.save()

        reset.used = True
        reset.save(update_fields=['used'])

        ensure_profile(user)
        token = AuthToken.create_for_user(user)
        return _cors_json(JsonResponse(auth_payload(user, token)))
    except Exception as exc:
        return _handle_exception('reset_password', exc)


@csrf_exempt
@require_http_methods(['GET', 'POST', 'DELETE', 'OPTIONS'])
def search_history(request, query=None):
    try:
        if request.method == 'OPTIONS':
            return _cors_json(HttpResponse())

        viewer = require_authenticated_user(request)
        if viewer is None:
            return _unauthorized()

        if request.method == 'DELETE':
            if query:
                SearchHistory.objects.filter(user=viewer, query=query).delete()
            else:
                SearchHistory.objects.filter(user=viewer).delete()
            return _cors_json(JsonResponse({'ok': True}))

        if request.method == 'POST':
            body = _json_body(request) or {}
            q = (body.get('query') or '').strip()
            if not q:
                return _bad_request('query is required')
            # upsert: delete old entry so the new one sorts to top
            SearchHistory.objects.filter(user=viewer, query=q).delete()
            SearchHistory.objects.create(user=viewer, query=q)
            # keep at most 8 entries per user
            old_ids = list(
                SearchHistory.objects.filter(user=viewer)
                .values_list('id', flat=True)[8:]
            )
            if old_ids:
                SearchHistory.objects.filter(id__in=old_ids).delete()
            return _cors_json(JsonResponse({'ok': True}))

        # GET
        qs = SearchHistory.objects.filter(user=viewer)[:8]
        return _cors_json(JsonResponse({'queries': [h.query for h in qs]}))
    except Exception as exc:
        return _handle_exception('search_history', exc)
