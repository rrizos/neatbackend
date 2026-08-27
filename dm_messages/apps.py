from django.apps import AppConfig


class DmMessagesConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'dm_messages'

    def ready(self):
        # Registers the on-delete cleanup for stored DM media.
        from . import signals  # noqa: F401
