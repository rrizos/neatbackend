import datetime
import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone


class Profile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='profile')
    city = models.CharField(max_length=120, blank=True, default='')
    full_name = models.CharField(max_length=150, blank=True)
    bio = models.TextField(blank=True)
    avatar_url = models.TextField(blank=True, default='')
    # Media URL of the full-resolution avatar on disk. The small inline copy
    # above is what every payload carries; this is fetched only where an
    # avatar is shown large. See accounts/avatars.py.
    avatar_full_url = models.CharField(max_length=255, blank=True, default='')
    # Media URL of the small avatar as a *file*. `avatar_url` above is the same
    # picture as base64 and is kept only so builds released before this still
    # work — it is what made every payload naming a person expensive, since a
    # data URL cannot be cached by the client, cannot be fetched in parallel
    # with the list that mentions it, and is re-sent in full every time.
    # Clients that announce X-Neat-Client >= 3 are given this instead.
    avatar_thumb_url = models.CharField(max_length=255, blank=True, default='')
    last_active = models.DateTimeField(null=True, blank=True)
    #: True while the account is still carrying the username we invented for
    #: it. Only social sign-ups have one: they never pass through a form with a
    #: username field, so the server has to put *something* there to create the
    #: account, and the person then gets to replace it. Kept on the profile
    #: rather than inferred, so the prompt survives the app being killed
    #: mid-sign-up in exactly the way the empty-city check does.
    username_pending = models.BooleanField(default=False)
    #: When the home city was last set, so it cannot be changed again for a
    #: month. The whole app is scoped to one city — feed, posting rights,
    #: events, who can see you — so somebody hopping between them every day
    #: would be in every local feed at once while belonging to none of them.
    #: Null on accounts that predate this; they are measured from sign-up.
    city_changed_at = models.DateTimeField(null=True, blank=True)

    #: How long a city has to be kept before it can be changed again.
    CITY_CHANGE_INTERVAL = datetime.timedelta(days=30)

    def city_change_allowed_at(self):
        """When this account may next change city.

        Measured from the last change, or from sign-up for anyone who has
        never changed one — so a brand new account cannot pick a city, look
        around a second one, and settle in a third on its first evening.
        """
        anchor = self.city_changed_at or self.created
        return anchor + self.CITY_CHANGE_INTERVAL

    def can_change_city(self, now=None):
        return (now or timezone.now()) >= self.city_change_allowed_at()
    is_verified = models.BooleanField(default=False)
    is_admin = models.BooleanField(default=False)
    can_create_official_events = models.BooleanField(default=False)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.user.username


class Follow(models.Model):
    follower = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='following_links')
    following = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='follower_links')
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['follower', 'following'], name='unique_follow_relationship'),
            models.CheckConstraint(condition=~models.Q(follower=models.F('following')), name='prevent_self_follow'),
        ]

    def __str__(self):
        return f'{self.follower} follows {self.following}'


class Block(models.Model):
    blocker = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='blocking')
    blocked = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='blocked_by')
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['blocker', 'blocked'], name='unique_block_relationship'),
            models.CheckConstraint(condition=~models.Q(blocker=models.F('blocked')), name='prevent_self_block'),
        ]

    def __str__(self):
        return f'{self.blocker} blocked {self.blocked}'


def is_blocked(user_a, user_b):
    """True if either user has blocked the other."""
    if user_a is None or user_b is None or user_a == user_b:
        return False
    return Block.objects.filter(
        models.Q(blocker=user_a, blocked=user_b) | models.Q(blocker=user_b, blocked=user_a)
    ).exists()


def blocked_user_ids(user):
    """IDs of users `user` has blocked, or that have blocked `user`."""
    if user is None or not user.is_authenticated:
        return set()
    blocked_by_me = set(Block.objects.filter(blocker=user).values_list('blocked_id', flat=True))
    blocking_me = set(Block.objects.filter(blocked=user).values_list('blocker_id', flat=True))
    return blocked_by_me | blocking_me


class AuthToken(models.Model):
    key = models.CharField(max_length=64, primary_key=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='auth_tokens')
    created = models.DateTimeField(auto_now_add=True)
    last_used = models.DateTimeField(null=True, blank=True)

    @classmethod
    def create_for_user(cls, user):
        return cls.objects.create(user=user, key=secrets.token_urlsafe(48))

    def mark_used(self):
        self.last_used = timezone.now()
        self.save(update_fields=['last_used'])


class SearchHistory(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='search_history',
    )
    query = models.CharField(max_length=200)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created']
        unique_together = [('user', 'query')]

    def __str__(self):
        return f'{self.user.username}: {self.query}'


class PasswordResetCode(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='reset_codes',
    )
    email = models.EmailField()
    code = models.CharField(max_length=6)
    created_at = models.DateTimeField(auto_now_add=True)
    used = models.BooleanField(default=False)

    class Meta:
        ordering = ['-created_at']

    def is_expired(self):
        return (timezone.now() - self.created_at).total_seconds() > 900


class Notification(models.Model):
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='notifications',
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='actor_notifications',
    )
    verb = models.CharField(max_length=32)
    target_type = models.CharField(max_length=32, blank=True, default='')
    target_id = models.CharField(max_length=64, blank=True, default='')
    # For comment-related notifications: the specific comment/reply the
    # interaction concerns, so the client can scroll the comment panel to the
    # exact position instead of guessing by author.
    target_comment_id = models.CharField(max_length=64, blank=True, default='')
    target_text = models.CharField(max_length=255, blank=True, default='')
    is_read = models.BooleanField(default=False)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created']

    def to_dict(self):
        image_url = ''
        video_url = ''
        if self.target_type == 'post' and self.target_id:
            from posts.models import Post

            try:
                post = Post.objects.prefetch_related('media_items').get(pk=int(self.target_id))
            except (Post.DoesNotExist, ValueError):
                post = None
            if post is not None:
                first_media = post.media_items.first()
                if first_media is not None:
                    if first_media.media_type == 'video':
                        video_url = first_media.url
                    else:
                        image_url = first_media.url
                elif post.image_url:
                    image_url = post.image_url

        from .avatars import avatar_for as _avatar_for

        actor_profile = getattr(self.actor, 'profile', None)

        return {
            'id': self.id,
            'recipientId': self.recipient_id,
            'actor': self.actor.username,
            'actorAvatarUrl': _avatar_for(actor_profile),
            'verb': self.verb,
            'targetType': self.target_type,
            'targetId': self.target_id,
            'targetCommentId': self.target_comment_id,
            'targetText': self.target_text,
            'isRead': self.is_read,
            'created': self.created.isoformat(),
            'imageUrl': image_url,
            'videoUrl': video_url,
        }


class AppSession(models.Model):
    """One continuous stretch of somebody using the app.

    Nothing recorded how long people stayed or whether they came back — the
    only signal was `Profile.last_active`, a single timestamp that is
    overwritten every time, so it can say "active recently" and nothing else.
    Retention and time-in-app cannot be reconstructed from it after the fact.

    The client pings while the app is in the foreground and the server decides
    where one session ends and the next begins: a ping within
    `SESSION_GAP_MINUTES` of the last one extends the current session, anything
    later starts a new one. Sessionising on the server rather than the client
    means a build that is killed mid-session, or loses its network, still
    produces sensible data instead of a session that never closes.
    """

    #: Longer than a glance at a notification, shorter than a lunch break.
    #: A gap larger than this is treated as having left and come back.
    SESSION_GAP_MINUTES = 30

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='app_sessions'
    )
    started = models.DateTimeField(default=timezone.now)
    last_seen = models.DateTimeField(default=timezone.now)
    platform = models.CharField(max_length=16, blank=True, default='')

    class Meta:
        indexes = [
            models.Index(fields=['user', '-last_seen']),
            models.Index(fields=['started']),
        ]

    @property
    def duration_seconds(self):
        return max(0, int((self.last_seen - self.started).total_seconds()))

    def __str__(self):
        return f'{self.user_id}: {self.started:%Y-%m-%d %H:%M} (+{self.duration_seconds}s)'

    @classmethod
    def record_ping(cls, user, platform=''):
        """Extend the current session, or open a new one. Returns the session."""
        cutoff = timezone.now() - timezone.timedelta(minutes=cls.SESSION_GAP_MINUTES)
        current = (
            cls.objects.filter(user=user, last_seen__gte=cutoff)
            .order_by('-last_seen')
            .first()
        )
        if current is not None:
            current.last_seen = timezone.now()
            fields = ['last_seen']
            if platform and not current.platform:
                current.platform = platform
                fields.append('platform')
            current.save(update_fields=fields)
            return current
        return cls.objects.create(user=user, platform=platform)


class SocialAccount(models.Model):
    """A provider identity (Apple, Google) that may sign in as [user].

    Kept in its own table rather than as columns on the profile so one account
    can be reached through several providers, and so the *subject* — the
    provider's own opaque user id — is what identifies a returning user.
    Matching on email instead would be wrong in both directions: Apple hands
    out per-app relay addresses that change if the user disconnects the app,
    and an email address can be reassigned by whoever runs the domain.
    """

    APPLE = 'apple'
    GOOGLE = 'google'
    PROVIDERS = [(APPLE, 'Apple'), (GOOGLE, 'Google')]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='social_accounts',
    )
    provider = models.CharField(max_length=16, choices=PROVIDERS)
    #: The provider's `sub` claim. Stable for the lifetime of the account.
    subject = models.CharField(max_length=255)
    #: Only ever what the provider told us, kept for support rather than login.
    email = models.CharField(max_length=254, blank=True, default='')
    created = models.DateTimeField(auto_now_add=True)
    last_used = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['provider', 'subject'],
                name='unique_social_identity',
            ),
        ]
        indexes = [models.Index(fields=['user', 'provider'])]

    def __str__(self):
        return f'{self.provider}:{self.subject} -> {self.user_id}'
