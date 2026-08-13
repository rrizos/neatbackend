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
    typing_at = models.DateTimeField(null=True, blank=True)
    hidden_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['conversation', 'user'],
                name='unique_conversation_member',
            )
        ]


class Message(models.Model):
    #: A photo the sender chose to make temporary, and how temporary.
    #: Blank — the default, and what every text message and ordinary photo
    #: carries — means the message simply stays in the conversation.
    PHOTO_ONCE = 'once'
    PHOTO_REPLAY = 'replay'
    PHOTO_MODES = [
        (PHOTO_ONCE, 'View once'),
        (PHOTO_REPLAY, 'Allow replay'),
    ]

    #: How many times *each* participant may open one before it is gone for
    #: them. "Allow replay" is one viewing plus one replay, which is what the
    #: name promises.
    #:
    #: The budget is per person, sender included: spending your own viewings
    #: is your business, and it should not grey the photo out for the other
    #: side — which is what a single shared counter did.
    OPEN_BUDGET = {PHOTO_ONCE: 1, PHOTO_REPLAY: 2}

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
    edited = models.BooleanField(default=False)
    photo_mode = models.CharField(
        max_length=6, choices=PHOTO_MODES, blank=True, default=''
    )
    #: Total viewings across everyone — kept for the record; who has spent
    #: what is in MessageOpen.
    opens = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['created']

    @property
    def open_budget(self):
        return self.OPEN_BUDGET.get(self.photo_mode, 0)

    def opens_by(self, user_id):
        row = next((o for o in self.opens_rows.all() if o.user_id == user_id), None)
        return row.count if row else 0

    def opens_left_for(self, user_id):
        if not self.photo_mode:
            return 0
        return max(0, self.open_budget - self.opens_by(user_id))

    def is_spent_for(self, user_id):
        return bool(self.photo_mode) and self.opens_left_for(user_id) <= 0

    def is_spent_for_everyone(self, member_ids):
        """True once nobody has a viewing left — when the bytes can go.

        The picture has to outlive the first person to open it now that both
        sides have their own viewings; it goes when the last of them is used.
        """
        if not self.photo_mode:
            return False
        return all(self.is_spent_for(user_id) for user_id in member_ids)


class MessageOpen(models.Model):
    """How many times one person has opened one temporary photo.

    A row per viewer rather than a counter on the message: each participant
    gets their own viewings, so the recipient using theirs must not grey the
    photo out for the sender, or the other way round.
    """

    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name='opens_rows')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='message_opens',
    )
    count = models.PositiveSmallIntegerField(default=0)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['message', 'user'],
                name='unique_message_open_per_user',
            )
        ]


class MessageReaction(models.Model):
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name='reactions')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='message_reactions',
    )
    emoji = models.CharField(max_length=16)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['message', 'user'],
                name='unique_message_reaction_per_user',
            )
        ]


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

