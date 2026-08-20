"""Delete a message's file when the message goes.

DM media used to live in the row, so deleting the row was the whole cleanup.
Now that ordinary photos and voice notes are files, the row going away has to
take the file with it — otherwise deleting a conversation leaves its pictures
on disk, still fetchable by anyone holding the URL.
"""

from django.db.models.signals import post_delete
from django.dispatch import receiver

from .media import discard_message_media
from .models import Message


@receiver(post_delete, sender=Message)
def _delete_message_media(sender, instance, **kwargs):
    discard_message_media(instance.media_url)
