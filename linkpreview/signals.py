"""Remove a card's copied thumbnail when the card itself goes.

Thumbnails are files on disk (see thumbnails.py), so deleting only the row
would leave them behind with nothing referencing them and no other cleanup
path — the same trap posts/signals.py exists to avoid.
"""

from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import LinkPreview
from .thumbnails import discard_thumbnail


@receiver(post_delete, sender=LinkPreview)
def _delete_thumbnail(sender, instance, **kwargs):
    if instance.is_local_image:
        discard_thumbnail(instance.image_url)
