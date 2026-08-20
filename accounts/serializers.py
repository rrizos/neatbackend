from .avatars import avatar_for
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
        # Only the owner is told to pick a username; to anyone else the
        # generated one is simply their username.
        'usernamePending': profile.username_pending if is_self_or_admin else False,
        # When the home city may next be changed, so the app can say so
        # instead of letting somebody pick one and be refused.
        'canChangeCity': profile.can_change_city() if is_self_or_admin else False,
        'cityChangeAllowedAt': (
            profile.city_change_allowed_at().isoformat() if is_self_or_admin else None
        ),
        # Whether this account can be signed into with a password at all.
        # False for one created through Apple or Google that has not set one,
        # which is what the settings screen offers to fix.
        'hasPassword': user.has_usable_password() if is_self_or_admin else False,
        'avatarUrl': avatar_for(profile),
        # Only the enlarged-avatar screens fetch this; everything else draws
        # the inline copy above. Empty for anyone who has not saved a picture
        # since the two-copy split shipped.
        'avatarFullUrl': profile.avatar_full_url,
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
