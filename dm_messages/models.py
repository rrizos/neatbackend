from django.conf import settings
from django.db import models


class Conversation(models.Model):
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Conversation {self.pk}"


class ConversationMember(models.Model):
    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name='members',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='conversation_memberships',
    )
    last_read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['conversation', 'user'],
                name='unique_conversation_member',
            )
        ]


class Message(models.Model):
    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name='messages',
    )
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='sent_messages',
    )
    text = models.TextField()
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created']


class MessageReport(models.Model):
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
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name='reports')
    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='message_reports',
    )
    reason = models.CharField(max_length=50, choices=REASONS, default='other')
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['message', 'reporter'],
                name='unique_message_report',
            )
        ]

    def __str__(self):
        return f"{self.reporter.username} reported message {self.message_id}: {self.reason}"

