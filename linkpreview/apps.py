from django.apps import AppConfig


class LinkPreviewConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'linkpreview'

    def ready(self):
        # Registers the on-delete cleanup for copied thumbnails.
        from . import signals  # noqa: F401
