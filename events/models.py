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

    def to_dict(self):
        return {
            'id': self.id,
            'city': self.city,
            'eventType': self.event_type,
            'title': self.title,
            'description': self.description,
            'location': self.location,
            'imageUrl': self.image_url,
            'creator': self.creator.username if self.creator_id else '',
            'organizer': self.organizer,
            'hasTickets': self.has_tickets,
            'ticketsUrl': self.tickets_url,
            'attendees': self.attendees,
            'created': self.created.isoformat(),
        }


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
        ('harassment', 'Harassment or bullying'),
        ('hate', 'Hate speech'),
        ('inappropriate', 'Inappropriate content'),
        ('other', 'Other'),
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
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created']

    def to_dict(self):
        return {
            'id': self.id,
            'author': self.user.username,
            'text': self.text,
            'created': self.created.isoformat(),
        }
