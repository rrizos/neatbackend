"""Remove a profile's avatar files when the profile goes.

Replacing an avatar already deletes the one it replaces (see avatars.py), but
nothing removed them when the account itself was deleted — so a deleted user's
photograph stayed on disk, still fetchable by anyone holding the URL. That is
the same problem posts/signals.py exists to solve, and the Privacy Policy has
to be able to say the picture is gone.
"""

from django.db.models.signals import post_delete
from django.dispatch import receiver

from .avatars import _delete_media_file
from .models import Profile


@receiver(post_delete, sender=Profile)
def _delete_avatar_files(sender, instance, **kwargs):
    _delete_media_file(instance.avatar_thumb_url)
    _delete_media_file(instance.avatar_full_url)
