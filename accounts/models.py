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
    last_active = models.DateTimeField(null=True, blank=True)
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

        return {
            'id': self.id,
            'recipientId': self.recipient_id,
            'actor': self.actor.username,
            'verb': self.verb,
            'targetType': self.target_type,
            'targetId': self.target_id,
            'targetText': self.target_text,
            'isRead': self.is_read,
            'created': self.created.isoformat(),
            'imageUrl': image_url,
            'videoUrl': video_url,
        }
