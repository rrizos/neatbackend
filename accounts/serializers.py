from django.conf import settings
from .avatars import avatar_for
from .models import Block, Follow, Profile


def _city_lock_fields(city, is_self):
    """Return cityLocked/cityThreshold/cityMemberCount for the account owner.

    Only meaningful for the owner (is_self=True); everyone else gets the safe
    defaults so the lock state of a city is never leaked through another
    account's profile response.
    """
    if not is_self or not getattr(settings, 'LOCKED_CITIES_ENABLED', False) or not city:
        return {'cityLocked': False, 'cityThreshold': 0, 'cityMemberCount': 0}
    unlocked = getattr(settings, 'LOCKED_CITIES_UNLOCKED', set())
    if city in unlocked:
        return {'cityLocked': False, 'cityThreshold': 0, 'cityMemberCount': 0}
    try:
        from posts.models import CityConfig
        cfg = CityConfig.objects.get(name=city)
        return {
            'cityLocked': cfg.is_locked,
            'cityThreshold': cfg.threshold,
            'cityMemberCount': cfg.member_count_cache,
        }
    except Exception:
        # No CityConfig row → treat city as open.
        return {'cityLocked': False, 'cityThreshold': 0, 'cityMemberCount': 0}


def ensure_profile(user):
    profile, _ = Profile.objects.get_or_create(user=user)
    return profile


def _post_count(user, city=''):
    try:
        from posts.models import Post
        # Only city-scope posts made while the user was in their current city.
        # Filtering by city means moving to a new city starts the count at 0,
        # and returning to an old city restores the original count (posts stay).
        if city:
            return Post.objects.filter(user=user, scope='city', city=city).count()
        return Post.objects.filter(user=user, scope='city').count()
    except Exception:
        return 0


def user_to_dict(user, viewer=None):
    profile = ensure_profile(user)
    user_city = profile.city or ''
    # Followers and following are scoped to the user's current city so that
    # moving cities presents a blank-slate social graph. The underlying Follow
    # rows are never deleted — returning to a previous city restores the counts.
    if user_city:
        followers = Follow.objects.filter(
            following=user, follower__profile__city=user_city
        ).count()
        following = Follow.objects.filter(
            follower=user, following__profile__city=user_city
        ).count()
    else:
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
        # When the home city may next be changed. null means "right now",
        # which is also what a brand new account with no city yet sees.
        'canChangeCity': profile.can_change_city() if is_self_or_admin else False,
        'cityChangeAllowedAt': (
            (profile.city_change_allowed_at() or None) and
            profile.city_change_allowed_at().isoformat()
        ) if is_self_or_admin else None,
        # Whether this account can be signed into with a password at all.
        # False for one created through Apple or Google that has not set one,
        # which is what the settings screen offers to fix.
        'hasPassword': user.has_usable_password() if is_self_or_admin else False,
        'avatarUrl': avatar_for(profile),
        # Only the enlarged-avatar screens fetch this; everything else draws
        # the inline copy above. Empty for anyone who has not saved a picture
        # since the two-copy split shipped.
        'avatarFullUrl': profile.avatar_full_url,
        'postCount': _post_count(user, city=user_city),
        'followers': followers,
        'following': following,
        'isFollowing': is_following,
        'isMutual': is_mutual,
        'isVerified': profile.is_verified,
        'isAdmin': profile.is_admin,
        'canCreateOfficialEvents': profile.can_create_official_events,
        'isBlocked': is_blocked,
        'hasBlockedYou': has_blocked_viewer,
        **_city_lock_fields(profile.city, is_self_or_admin),
    }


def auth_payload(user, token):
    return {
        'token': token.key,
        'user': user_to_dict(user, viewer=user),
    }
