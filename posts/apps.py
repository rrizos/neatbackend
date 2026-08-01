from django.apps import AppConfig


class PostsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'posts'

    def ready(self):
        # Registers the post_delete receivers that clear uploaded files off
        # disk when the rows referencing them go.
        from . import signals  # noqa: F401
