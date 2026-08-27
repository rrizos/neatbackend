"""Signing in with Apple or Google.

One endpoint for both, and for both signing up and signing back in: the app
cannot know whether the person behind a provider identity is new here, and
asking it to guess would mean a wrong guess every time somebody reinstalls.

The account it returns has **no city**, exactly like a fresh email sign-up, so
the client's usual "sign-up is not finished" path sends the user to the map.
"""

import re
import secrets

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from security import audit as security_audit
from .models import AuthToken, SocialAccount
from .ratelimit import client_ip, rate_limited
from .serializers import auth_payload, ensure_profile
from .social_auth import VERIFIERS, SocialAuthError

User = get_user_model()


def _cors_json(response):
    from .views import _cors_json as base
    return base(response)


def _bad_request(message, status=400):
    return _cors_json(JsonResponse({'error': message}, status=status))


def _audiences(provider):
    from django.conf import settings
    if provider == SocialAccount.APPLE:
        return settings.APPLE_CLIENT_IDS
    return settings.GOOGLE_CLIENT_IDS


_USERNAME_SAFE = re.compile(r'[^a-z0-9_.]+')


def _username_from(email, name):
    """A username nobody has to think up, derived from what the provider gave.

    Social sign-up has no username field, but the rest of the app is built
    around one being present and visible, so it has to come from somewhere.
    The local part of the email is closest to what someone would have picked;
    a name is the fallback, and a random suffix keeps it unique. Deliberately
    ASCII-only: usernames appear in URLs and @-mentions.
    """
    base = ''
    if email and '@' in email:
        base = email.split('@', 1)[0]
    if not base and name:
        base = name
    base = _USERNAME_SAFE.sub('', base.strip().lower().replace(' ', '_'))
    base = base.strip('._')[:20]
    if len(base) < 3:
        base = 'neat'

    if not User.objects.filter(username=base).exists():
        return base
    for _ in range(12):
        candidate = f'{base}{secrets.randbelow(9000) + 1000}'
        if not User.objects.filter(username=candidate).exists():
            return candidate
    # Practically unreachable; a collision here would be a genuine surprise.
    return f'{base}{secrets.token_hex(4)}'


@csrf_exempt
@require_http_methods(['POST', 'OPTIONS'])
def social_login(request):
    if request.method == 'OPTIONS':
        return _cors_json(HttpResponse())

    from .views import _json_body

    ip = client_ip(request)
    if rate_limited(f'social:{ip}', limit=20, window_seconds=900):
        return _bad_request('Too many attempts. Please try again later.', status=429)

    body = _json_body(request)
    if body is None:
        return _bad_request('Invalid JSON')

    provider = (body.get('provider') or '').strip().lower()
    verify = VERIFIERS.get(provider)
    if verify is None:
        return _bad_request('Unknown sign-in provider.')

    try:
        claims = verify(
            body.get('idToken') or body.get('id_token') or '',
            _audiences(provider),
            (body.get('nonce') or '').strip(),
        )
    except SocialAuthError as exc:
        security_audit.record(
            'auth.social_rejected',
            severity='medium',
            request=request,
            status_code=400,
            message=f'{provider} sign-in rejected: {exc}',
            metadata={'provider': provider},
        )
        return _bad_request(str(exc))

    subject = claims['subject']
    email = claims['email']
    # Apple puts the name outside the token and only on the first authorisation,
    # so the client forwards it. It is a display hint, never used to identify.
    display_name = (body.get('fullName') or body.get('full_name')
                    or claims.get('name') or '').strip()[:150]

    created = False
    with transaction.atomic():
        link = (SocialAccount.objects
                .select_related('user')
                .filter(provider=provider, subject=subject)
                .first())

        if link is not None:
            user = link.user
        else:
            user = None
            # Linking to an existing account by email is only safe when the
            # provider states it verified the address — otherwise anyone able
            # to claim an address at a provider could take over the account
            # that already uses it here.
            if email and claims['email_verified']:
                user = User.objects.filter(email__iexact=email).first()

            if user is None:
                created = True
                username = _username_from(email, display_name)
                try:
                    user = User.objects.create_user(username=username, email=email)
                except IntegrityError:
                    return _bad_request('Could not create an account. Please try again.')
                # No usable password: this account is reachable only through
                # the provider until its owner sets one.
                user.set_unusable_password()
                user.save(update_fields=['password'])

                profile = ensure_profile(user)
                if display_name:
                    profile.full_name = display_name
                # The username above was invented, not chosen. Flagging it is
                # what makes the client ask for a real one before letting the
                # account loose — see Profile.username_pending.
                profile.username_pending = True
                # City is deliberately left empty — the client treats that as
                # "sign-up unfinished" and takes them to the map.
                profile.save(update_fields=['full_name', 'username_pending'])

            link = SocialAccount.objects.create(
                user=user, provider=provider, subject=subject, email=email,
            )

        link.last_used = timezone.now()
        link.save(update_fields=['last_used'])

        if not user.is_active:
            return _bad_request('This account is locked.', status=403)

        token = AuthToken.create_for_user(user)

    security_audit.record(
        'auth.social_signup' if created else 'auth.social_login',
        severity='info',
        actor=user,
        request=request,
        status_code=201 if created else 200,
        session_id=security_audit.session_fingerprint(token.key),
        message=f'{user.username} {"created via" if created else "signed in with"} {provider}',
        metadata={'provider': provider, 'created': created},
    )
    return _cors_json(JsonResponse(auth_payload(user, token),
                                   status=201 if created else 200))
