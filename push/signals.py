from django.db.models.signals import post_save
from django.dispatch import receiver

from accounts.models import Notification
from dm_messages.models import Message

from .senders import send_message_alert, send_soft


@receiver(post_save, sender=Notification)
def notify_soft_push(sender, instance, created, **kwargs):
    if not created:
        return
    # instance.verb is already a full human-readable phrase, e.g.
    # "liked your post" / "followed you" — see the _notify() helpers in
    # accounts/posts/events views.py.
    send_soft(
        instance.recipient,
        title=instance.actor.username,
        body=instance.verb,
        data={
            'type': 'notification',
            'notificationId': instance.id,
            'targetType': instance.target_type,
            'targetId': instance.target_id,
        },
    )


@receiver(post_save, sender=Message)
def notify_dm_push(sender, instance, created, **kwargs):
    if not created:
        return
    recipients = instance.conversation.members.exclude(user=instance.sender).select_related(
        'user__profile'
    )
    for member in recipients:
        send_message_alert(
            member.user,
            sender_profile=getattr(instance.sender, 'profile', None),
            sender_username=instance.sender.username,
            text=instance.text,
            conversation_id=instance.conversation_id,
        )
