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
        }
