from django.conf import settings
from django.db import models


class Event(models.Model):
    OFFICIAL = 'official'
    COMMUNITY = 'community'

    EVENT_TYPES = [
        (OFFICIAL, 'Official'),
        (COMMUNITY, 'Community'),
    ]

    city = models.CharField(max_length=120)
    event_type = models.CharField(max_length=16, choices=EVENT_TYPES)
    title = models.CharField(max_length=180)
    description = models.TextField(blank=True, default='')
    location = models.CharField(max_length=200, blank=True, default='')
    image_url = models.TextField(blank=True, default='')
    category = models.CharField(max_length=50, blank=True, default='')
    date = models.DateTimeField(null=True, blank=True)
    creator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='created_events',
    )
    organizer = models.CharField(max_length=150, blank=True, default='')
    has_tickets = models.BooleanField(default=False)
    tickets_url = models.URLField(blank=True, default='')
    attendees = models.IntegerField(default=0)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-attendees', '-created']

    def to_dict(self, attending_event_ids=None):
        """attending_event_ids, when given, is the set of event ids the
        current viewer is attending (see attending_ids_for) — batched so
        listing events never runs one attendance query per event.
        """
        return {
            'id': self.id,
            'city': self.city,
            'eventType': self.event_type,
            'title': self.title,
            'description': self.description,
            'location': self.location,
            'imageUrl': self.image_url,
            'category': self.category,
            'date': self.date.isoformat() if self.date else '',
            'creator': self.creator.username if self.creator_id else '',
            'organizer': self.organizer,
            'hasTickets': self.has_tickets,
            'ticketsUrl': self.tickets_url,
            'attendees': self.attendees,
            'isAttending': attending_event_ids is not None and self.id in attending_event_ids,
            'created': self.created.isoformat(),
        }

    @staticmethod
    def attending_ids_for(viewer, events):
        """Single-query batch lookup of which of [events] the viewer attends."""
        if viewer is None or not getattr(viewer, 'is_authenticated', False):
            return set()
        return set(
            EventAttendance.objects.filter(user=viewer, event__in=events).values_list('event_id', flat=True)
        )


class EventAttendance(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='attendance_rows')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='event_attendance')
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['event', 'user'], name='unique_event_attendance'),
        ]


class EventReport(models.Model):
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
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='reports')
    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='event_reports',
    )
    reason = models.CharField(max_length=50, choices=REASONS, default='other')
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['event', 'reporter'], name='unique_event_report'),
        ]

    def __str__(self):
        return f"{self.reporter.username} reported event {self.event_id}: {self.reason}"


class EventComment(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='comment_rows')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='event_comments')
    text = models.TextField()
    pinned = models.BooleanField(default=False)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created']
        constraints = [
            # Only one comment per event may be pinned at a time.
            models.UniqueConstraint(
                fields=['event'],
                condition=models.Q(pinned=True),
                name='unique_pinned_comment_per_event',
            ),
        ]

    def to_dict(self, viewer=None, owner_id=None):
        if owner_id is None:
            owner_id = self.event.creator_id
        liked = False
        if viewer is not None and viewer.is_authenticated:
            liked = self.like_rows.filter(user=viewer).exists()
        liked_by_owner = bool(owner_id) and self.like_rows.filter(user_id=owner_id).exists()
        return {
            'id': self.id,
            'author': self.user.username,
            'text': self.text,
            'created': self.created.isoformat(),
            'pinned': self.pinned,
            'likes': self.like_rows.count(),
            'liked': liked,
            'likedByOwner': liked_by_owner,
        }


class EventCommentLike(models.Model):
    comment = models.ForeignKey(EventComment, on_delete=models.CASCADE, related_name='like_rows')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='event_comment_likes')
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['comment', 'user'], name='unique_event_comment_like'),
        ]


class EventCommentReport(models.Model):
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
    comment = models.ForeignKey(EventComment, on_delete=models.CASCADE, related_name='reports')
    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='event_comment_reports',
    )
    reason = models.CharField(max_length=50, choices=REASONS, default='other')
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['comment', 'reporter'], name='unique_event_comment_report'),
        ]

    def __str__(self):
        return f"{self.reporter.username} reported event comment {self.comment_id}: {self.reason}"
