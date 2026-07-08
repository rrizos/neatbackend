import logging

from django.conf import settings

logger = logging.getLogger(__name__)

_app = None
_init_failed = False


def get_app():
    """Lazily initializes the firebase_admin app from the service-account
    credentials file. Returns None (and logs once) if credentials aren't
    configured yet, so push sends can no-op gracefully before Firebase is
    wired up."""
    global _app, _init_failed

    if _app is not None:
        return _app
    if _init_failed:
        return None

    credentials_path = getattr(settings, 'FIREBASE_CREDENTIALS_PATH', '')
    if not credentials_path:
        _init_failed = True
        logger.warning('FIREBASE_CREDENTIALS_PATH is not set; push notifications are disabled')
        return None

    try:
        import firebase_admin
        from firebase_admin import credentials

        cred = credentials.Certificate(credentials_path)
        _app = firebase_admin.initialize_app(cred)
    except Exception:
        _init_failed = True
        logger.exception('Failed to initialize firebase_admin')
        return None

    return _app
