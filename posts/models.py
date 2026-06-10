from django.db import models
from django.utils import timezone
import json


class Post(models.Model):
    author = models.CharField(max_length=150, default='Anonymous')
    text = models.TextField()
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
            'author': self.author,
            'text': self.text,
            'created': self.created.isoformat(),
            'minutesAgo': minutes_ago,
            'likes': self.likes,
            'comments': comments,
        }
