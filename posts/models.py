from django.db import models
from django.conf import settings

from accounts.avatars import avatar_for as _avatar_for
from django.utils import timezone
import json
import uuid
from neatbackend.timefmt import local_iso


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
    shares = models.IntegerField(default=0)
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
            'avatarUrl': _avatar_for(getattr(self.user, 'profile', None)),
            'city': self.city,
            'text': self.text,
            'imageUrl': self.image_url,
            'created': local_iso(self.created),
            'minutesAgo': minutes_ago,
            'likes': self.likes,
            'shares': self.shares,
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
    pinned = models.BooleanField(default=False)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created']
        constraints = [
            # Only one comment per post may be pinned at a time.
            models.UniqueConstraint(
                fields=['post'],
                condition=models.Q(pinned=True),
                name='unique_pinned_comment_per_post',
            ),
        ]

    def to_dict(self, viewer=None, owner_id=None):
        if owner_id is None:
            owner_id = self.post.user_id
        liked = False
        if viewer and viewer.is_authenticated:
            liked = self.comment_likes.filter(user=viewer).exists()
        liked_by_owner = bool(owner_id) and self.comment_likes.filter(user_id=owner_id).exists()
        replies = []
        if not self.parent_id:
            for r in self.replies.select_related('user').prefetch_related('comment_likes').order_by('created'):
                replies.append(r.to_dict(viewer=viewer, owner_id=owner_id))
        return {
            'id': self.id,
            'author': self.user.username,
            'text': self.text,
            'imageUrl': self.image_url,
            'parentId': self.parent_id,
            'created': local_iso(self.created),
            'avatarUrl': _avatar_for(getattr(self.user, 'profile', None)),
            'likes': self.comment_likes.count(),
            'liked': liked,
            'likedByOwner': liked_by_owner,
            'pinned': self.pinned,
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

    # Transcode state for videos. Everything else is born READY.
    #
    # A video upload used to be re-encoded inside the POST request, which held
    # one of gunicorn's request slots for the whole encode -- 18s for a minute
    # of 1080p, ~55s for three. Enough simultaneous uploads and every slot is
    # busy and the site answers nobody. The encode now happens in the
    # `transcode_worker` process and this column is the queue.
    #
    # PENDING is not "broken": `url` already points at the original upload,
    # which plays. Clients that understand the state show a processing tile,
    # and older builds simply play the original. FAILED means the encode did
    # not survive its retries and the original is what everyone keeps -- a
    # video that could not be re-encoded is worth more than no video at all.
    READY = 'ready'
    PENDING = 'pending'
    PROCESSING = 'processing'
    FAILED = 'failed'
    STATUSES = [
        (READY, 'Ready'),
        (PENDING, 'Pending transcode'),
        (PROCESSING, 'Transcoding'),
        (FAILED, 'Transcode failed'),
    ]

    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name='media_items')
    media_type = models.CharField(max_length=10, choices=TYPES, default='image')
    url = models.TextField()
    duration = models.FloatField(null=True, blank=True)
    order = models.IntegerField(default=0)
    # Poster frame for a video, written by the transcode worker. Empty for
    # images, and for videos uploaded before posters existed. Anywhere a video
    # is shown before it plays -- a DM's shared-post card, a notification row --
    # used to draw a black square with a play glyph, because decoding a video
    # client-side just to get one frame is not worth it. The worker already has
    # the file open, so it costs nothing to take the frame there instead.
    thumb_url = models.TextField(blank=True, default='')
    status = models.CharField(max_length=12, choices=STATUSES, default=READY, db_index=True)
    attempts = models.IntegerField(default=0)
    # How far through the encode this video is, 0-100. Only meaningful while
    # status is PROCESSING; the app shows it so a twenty-second wait reads as
    # work happening rather than as the app having hung.
    progress = models.IntegerField(default=0)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['order']

    @property
    def is_processing(self):
        return self.status in (self.PENDING, self.PROCESSING)


class PostReport(models.Model):
    REASONS = [
        ('spam', 'Spam'),
        ('nudity', 'Nudity or sexual activity'),
        ('hate_speech', 'Hate speech or symbols'),
        ('violence', 'Violence or dangerous organizations'),
        ('illegal_goods', 'Sale of illegal or regulated goods'),
        ('bullying', 'Bullying or harassment'),
        ('intellectual_property', 'Intellectual property violation'),
        ('self_injury', 'Suicide or self-injury'),
        ('eating_disorders', 'Eating disorders'),
        ('scam', 'Scam or fraud'),
        ('false_information', 'False information'),
        ('dislike', "I just don't like it"),
        ('other', 'Something else'),
    ]
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name='reports')
    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='post_reports',
    )
    reason = models.CharField(max_length=50, choices=REASONS)
    sub_reason = models.CharField(max_length=200, blank=True, default='')
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['post', 'reporter'], name='unique_post_report'),
        ]

    def __str__(self):
        return f"{self.reporter.username} reported post {self.post_id}: {self.reason}"


class CommentReport(models.Model):
    REASONS = [
        ('spam', 'Spam'),
        ('nudity', 'Nudity or sexual activity'),
        ('hate_speech', 'Hate speech or symbols'),
        ('violence', 'Violence or dangerous organizations'),
        ('illegal_goods', 'Sale of illegal or regulated goods'),
        ('bullying', 'Bullying or harassment'),
        ('intellectual_property', 'Intellectual property violation'),
        ('self_injury', 'Suicide or self-injury'),
        ('eating_disorders', 'Eating disorders'),
        ('scam', 'Scam or fraud'),
        ('false_information', 'False information'),
        ('dislike', "I just don't like it"),
        ('other', 'Something else'),
    ]
    comment = models.ForeignKey(PostComment, on_delete=models.CASCADE, related_name='reports')
    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='comment_reports',
    )
    reason = models.CharField(max_length=50, choices=REASONS, default='other')
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['comment', 'reporter'], name='unique_comment_report'),
        ]

    def __str__(self):
        return f"{self.reporter.username} reported comment {self.comment_id}: {self.reason}"


class Poll(models.Model):
    post = models.OneToOneField(Post, on_delete=models.CASCADE, related_name='poll')
    created = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Poll for post {self.post_id}"


class PollOption(models.Model):
    poll = models.ForeignKey(Poll, on_delete=models.CASCADE, related_name='options')
    text = models.CharField(max_length=200)
    order = models.IntegerField(default=0)

    class Meta:
        ordering = ['order']

    def __str__(self):
        return self.text


class PollVote(models.Model):
    poll = models.ForeignKey(Poll, on_delete=models.CASCADE, related_name='votes')
    option = models.ForeignKey(PollOption, on_delete=models.CASCADE, related_name='votes_rows')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='poll_votes')
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['poll', 'user'], name='unique_poll_vote'),
        ]


class NeatPointsAward(models.Model):
    """One row per (user, post, period) the post spent in a Virals top-10.

    Neat Points are cumulative, but a post's score is not monotonic — likes can
    be withdrawn and a post can drop out of the top-10 entirely. Storing the
    high-water mark per period means a balance only ever grows, so a user never
    watches points they were already shown disappear.

    `period_key` is the calendar day the award belongs to (the charts period
    the app reads for the Neat Pass). Past days are frozen; only today's rows
    are ever recomputed.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='neat_points_awards',
    )
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name='neat_points_awards')
    period_key = models.CharField(max_length=20)
    city = models.CharField(max_length=120, blank=True, default='')
    points = models.FloatField(default=0.0)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'post', 'period_key'],
                name='unique_neat_points_award',
            ),
        ]
        indexes = [models.Index(fields=['user', 'period_key'])]

    def __str__(self):
        return f'{self.user_id} +{self.points:.0f} ({self.period_key})'


class StagedUpload(models.Model):
    """A file uploaded before the post that will carry it exists.

    Composing a post takes time — picking the video, writing the caption,
    adding a poll — and during all of it the network sits idle, because the
    upload only began when Post was pressed. On a phone connection an 11 MB
    video then costs the poster a minute of staring at a progress ring for
    bytes that could have gone up while they were typing.

    So the file is uploaded as soon as it is picked, and the post that follows
    refers to it by id rather than carrying it. By the time somebody finishes a
    caption, the upload is usually already done and posting is instant.

    Rows are claimed (and deleted) when a post is created from them. Anything
    left is an abandoned compose — the user picked a video and changed their
    mind — and is swept up by `purge_staged_uploads`.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='staged_uploads'
    )
    url = models.TextField()
    media_type = models.CharField(max_length=10, default='image')
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['created'])]

    def __str__(self):
        return f'{self.user_id}: {self.media_type} {self.url[:40]}'
