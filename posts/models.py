from django.db import models
from django.conf import settings
from django.utils import timezone
import json


class Post(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='posts',
    )
    city = models.CharField(max_length=120, blank=True, default='')
    author = models.CharField(max_length=150, default='Anonymous')
    text = models.TextField()
    image_url = models.TextField(blank=True, default='')
    created = models.DateTimeField(auto_now_add=True)
    likes = models.IntegerField(default=0)
    comments = models.TextField(blank=True, default='[]')

    def __str__(self):
        return f"{self.author}: {self.text[:40]}"

    def to_dict(self):
        try:
            comments = json.loads(self.comments or '[]')
        except Exception:
            comments = []
        minutes_ago = int((timezone.now() - self.created).total_seconds() // 60)
        return {
            'id': self.id,
            'author': self.user.username if self.user_id else self.author,
            'authorId': self.user_id,
            'avatarUrl': getattr(getattr(self.user, 'profile', None), 'avatar_url', ''),
            'city': self.city,
            'text': self.text,
            'imageUrl': self.image_url,
            'created': self.created.isoformat(),
            'minutesAgo': minutes_ago,
            'likes': self.likes,
            'comments': comments,
        }


class PostLike(models.Model):
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name='like_rows')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='post_likes')
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['post', 'user'], name='unique_post_like'),
        ]


class PostSave(models.Model):
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name='save_rows')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='post_saves')
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['post', 'user'], name='unique_post_save'),
        ]


class PostComment(models.Model):
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name='comment_rows')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='post_comments')
    parent = models.ForeignKey('self', null=True, blank=True, on_delete=models.CASCADE, related_name='replies')
    text = models.TextField()
    image_url = models.TextField(blank=True, default='')
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created']

    def to_dict(self, viewer=None):
        liked = False
        if viewer and viewer.is_authenticated:
            liked = self.comment_likes.filter(user=viewer).exists()
        replies = []
        if not self.parent_id:
            for r in self.replies.select_related('user').prefetch_related('comment_likes').order_by('created'):
                replies.append(r.to_dict(viewer=viewer))
        return {
            'id': self.id,
            'author': self.user.username,
            'text': self.text,
            'imageUrl': self.image_url,
            'parentId': self.parent_id,
            'created': self.created.isoformat(),
            'avatarUrl': getattr(getattr(self.user, 'profile', None), 'avatar_url', ''),
            'likes': self.comment_likes.count(),
            'liked': liked,
            'replies': replies,
        }


class CommentLike(models.Model):
    comment = models.ForeignKey(PostComment, on_delete=models.CASCADE, related_name='comment_likes')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='comment_likes')
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['comment', 'user'], name='unique_comment_like'),
        ]


class PostMedia(models.Model):
    TYPES = [('image', 'Image'), ('video', 'Video')]
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name='media_items')
    media_type = models.CharField(max_length=10, choices=TYPES, default='image')
    url = models.TextField()
    duration = models.FloatField(null=True, blank=True)
    order = models.IntegerField(default=0)

    class Meta:
        ordering = ['order']
