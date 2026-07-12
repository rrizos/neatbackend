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
    is_self_or_admin = bool(viewer and viewer.is_authenticated and viewer == user)
    if viewer and viewer.is_authenticated and viewer != user:
        is_following = Follow.objects.filter(follower=viewer, following=user).exists()
        is_mutual = is_following and Follow.objects.filter(follower=user, following=viewer).exists()
        is_blocked = Block.objects.filter(blocker=viewer, blocked=user).exists()
        has_blocked_viewer = Block.objects.filter(blocker=user, blocked=viewer).exists()
        is_self_or_admin = ensure_profile(viewer).is_admin

    return {
        'id': user.id,
        'username': user.username,
        # Only the account owner (or an admin, for moderation) ever sees the
        # email — this was previously returned to any authenticated viewer
        # via profile/search/likers/attendees lookups, letting any user scrape
        # the whole user base's email addresses.
        'email': user.email if is_self_or_admin else '',
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
        'canCreateOfficialEvents': profile.can_create_official_events,
        'isBlocked': is_blocked,
        'hasBlockedYou': has_blocked_viewer,
    }


def auth_payload(user, token):
    return {
        'token': token.key,
        'user': user_to_dict(user, viewer=user),
    }
