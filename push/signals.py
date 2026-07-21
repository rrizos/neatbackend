from django.db.models.signals import post_save
from django.dispatch import receiver

from accounts.models import Notification
from dm_messages.models import Message, MessageReaction

from .senders import send_message_alert, send_reaction_alert, send_soft


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


@receiver(post_save, sender=MessageReaction)
def notify_dm_reaction_push(sender, instance, created, **kwargs):
    # No `created` guard here (unlike the Message/Notification receivers
    # above): message_react() in dm_messages/views.py is the only place a
    # MessageReaction is ever saved, and it only reaches update_or_create()
    # (i.e. only ever calls .save()) when the reaction is brand new OR the
    # emoji actually changed — a re-tap of the SAME emoji takes the delete()
    # branch instead and never hits this signal at all. So every save here
    # is already guaranteed to be a genuine new-or-changed reaction.
    message = instance.message
    if instance.user_id == message.sender_id:
        return  # don't notify yourself for reacting to your own message
    send_reaction_alert(
        message.sender,
        reactor_profile=getattr(instance.user, 'profile', None),
        reactor_username=instance.user.username,
        emoji=instance.emoji,
        conversation_id=message.conversation_id,
    )
