"""Remove uploaded files from disk when the row that referenced them goes.

Deleting a post (or an account, which cascades to its posts) only ever removed
database rows. The image and video files stayed in MEDIA_ROOT and stayed
publicly fetchable at their original /media/posts/<uuid>.<ext> URL, so content
a user had deleted was still retrievable by anyone holding the link. That is
both a privacy problem and something the Privacy Policy has to be able to deny.

post_delete fires for cascaded deletes too, which is what makes this cover
account deletion without touching delete_user_and_content().
"""

import logging
import os

from django.conf import settings
from django.core.files.storage import default_storage
from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import Post, PostComment, PostMedia

logger = logging.getLogger(__name__)


def delete_media_file(url):
    """Delete the file a stored media URL points at, if it is one of ours.

    Media URLs come in three shapes: a MEDIA_URL path for uploaded files, a
    base64 data: URL (legacy inline media, no file to remove), and absolute
    third-party URLs such as Giphy. Only the first has a file on our disk.
    """
    if not url or not url.startswith(settings.MEDIA_URL):
        return

    relative = url[len(settings.MEDIA_URL):].lstrip('/')
    # Refuse anything that climbs out of MEDIA_ROOT, however it got stored.
    root = os.path.realpath(settings.MEDIA_ROOT)
    target = os.path.realpath(os.path.join(root, relative))
    if not target.startswith(root + os.sep):
        logger.warning('refusing to delete media outside MEDIA_ROOT: %s', url)
        return

    try:
        if default_storage.exists(relative):
            default_storage.delete(relative)
    except Exception:
        # A failed cleanup must never break the delete the user asked for.
        logger.exception('could not delete media file for %s', url)


@receiver(post_delete, sender=PostMedia)
def _delete_post_media_file(sender, instance, **kwargs):
    delete_media_file(instance.url)
    # The poster frame is a second file for the same row.
    delete_media_file(instance.thumb_url)


@receiver(post_delete, sender=Post)
def _delete_legacy_post_image(sender, instance, **kwargs):
    # Posts made before PostMedia existed keep their single image on the post.
    delete_media_file(instance.image_url)


@receiver(post_delete, sender=PostComment)
def _delete_comment_image(sender, instance, **kwargs):
    delete_media_file(instance.image_url)
