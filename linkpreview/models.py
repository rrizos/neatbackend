import hashlib

from django.db import models
from django.utils import timezone

# A link that resolved keeps its card for a week; one that failed is retried
# after an hour. The negative cache matters as much as the positive one — a
# dead link pasted into a busy thread would otherwise mean a 6s timeout on
# every render for every viewer.
GOOD_TTL = timezone.timedelta(days=7)
BAD_TTL = timezone.timedelta(hours=1)


def url_fingerprint(url):
    return hashlib.sha256(url.encode('utf-8')).hexdigest()


class LinkPreview(models.Model):
    """One cached Open Graph card, keyed by the exact URL that was pasted."""

    url_hash = models.CharField(max_length=64, unique=True, db_index=True)
    url = models.TextField()
    # Where we ended up after redirects; what the card actually describes.
    resolved_url = models.TextField(blank=True, default='')
    title = models.CharField(max_length=200, blank=True, default='')
    description = models.CharField(max_length=400, blank=True, default='')
    image_url = models.TextField(blank=True, default='')
    # 0 when the source didn't say. Short-form video is portrait, so the card
    # needs the real shape to avoid cropping a 9:16 frame into a letterbox.
    image_width = models.IntegerField(default=0)
    image_height = models.IntegerField(default=0)
    site_name = models.CharField(max_length=100, blank=True, default='')
    # Who posted it, when the link points at one person's content rather than
    # at a site. Empty for a plain article or homepage, which is what keeps a
    # shared newspaper link looking like a newspaper link.
    author_name = models.CharField(max_length=100, blank=True, default='')
    author_handle = models.CharField(max_length=100, blank=True, default='')
    author_url = models.CharField(max_length=500, blank=True, default='')
    # '', 'video', 'article' or 'website'. Drives the play badge on the card.
    kind = models.CharField(max_length=20, blank=True, default='')
    # False = the fetch failed; row exists purely so we stop retrying for BAD_TTL.
    ok = models.BooleanField(default=True)
    fetched_at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [models.Index(fields=['fetched_at'])]

    def __str__(self):
        return f'{"ok" if self.ok else "fail"}: {self.url[:60]}'

    @property
    def is_stale(self):
        return timezone.now() - self.fetched_at > (GOOD_TTL if self.ok else BAD_TTL)

    def to_dict(self):
        return {
            'url': self.url,
            'resolved_url': self.resolved_url or self.url,
            'title': self.title,
            'description': self.description,
            'image_url': self.image_url,
            'image_width': self.image_width,
            'image_height': self.image_height,
            'site_name': self.site_name,
            'author_name': self.author_name,
            'author_handle': self.author_handle,
            'author_url': self.author_url,
            'kind': self.kind,
        }
