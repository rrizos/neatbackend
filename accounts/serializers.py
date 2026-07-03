from .models import Block, Follow, Profile


def ensure_profile(user):
    profile, _ = Profile.objects.get_or_create(user=user)
    return profile


def user_to_dict(user, viewer=None):
    profile = ensure_profile(user)
    followers = Follow.objects.filter(following=user).count()
    following = Follow.objects.filter(follower=user).count()
    is_following = False
    is_mutual = False
    is_blocked = False
    has_blocked_viewer = False
    if viewer and viewer.is_authenticated and viewer != user:
        is_following = Follow.objects.filter(follower=viewer, following=user).exists()
        is_mutual = is_following and Follow.objects.filter(follower=user, following=viewer).exists()
        is_blocked = Block.objects.filter(blocker=viewer, blocked=user).exists()
        has_blocked_viewer = Block.objects.filter(blocker=user, blocked=viewer).exists()

    return {
        'id': user.id,
        'username': user.username,
        'email': user.email,
        'fullName': profile.full_name,
        'bio': profile.bio,
        'city': profile.city,
        'avatarUrl': profile.avatar_url,
        'followers': followers,
        'following': following,
        'isFollowing': is_following,
        'isMutual': is_mutual,
        'isVerified': profile.is_verified,
        'isAdmin': profile.is_admin,
        'isBlocked': is_blocked,
        'hasBlockedYou': has_blocked_viewer,
    }


def auth_payload(user, token):
    return {
        'token': token.key,
        'user': user_to_dict(user, viewer=user),
    }
