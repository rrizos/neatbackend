from .models import AuthToken


def get_authenticated_user(request):
    header = request.headers.get('Authorization', '')
    if not header.startswith('Token '):
        return None

    key = header.removeprefix('Token ').strip()
    if not key:
        return None

    try:
        token = AuthToken.objects.select_related('user', 'user__profile').get(key=key)
    except AuthToken.DoesNotExist:
        return None

    token.mark_used()
    # Stash the resolved user for the security middleware so it can attribute
    # denials/volume without repeating this lookup on every request.
    try:
        request.audit_actor = token.user
    except Exception:
        pass
    return token.user


def require_authenticated_user(request):
    user = get_authenticated_user(request)
    if user is None:
        return None
    if not user.is_active:
        return None
    return user
