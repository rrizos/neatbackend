from .models import Follow, Profile


def ensure_profile(user):
    profile, _ = Profile.objects.get_or_create(user=user)
    return profile


def user_to_dict(user, viewer=None):
    profile = ensure_profile(user)
    followers = Follow.objects.filter(following=user).count()
    following = Follow.objects.filter(follower=user).count()
    is_following = False
    if viewer and viewer.is_authenticated and viewer != user:
        is_following = Follow.objects.filter(follower=viewer, following=user).exists()

    return {
        'id': user.id,
        'username': user.username,
        'email': user.email,
        'fullName': profile.full_name,
        'bio': profile.bio,
        'avatarUrl': profile.avatar_url,
        'followers': followers,
        'following': following,
        'isFollowing': is_following,
    }


def auth_payload(user, token):
    return {
        'token': token.key,
        'user': user_to_dict(user, viewer=user),
    }
