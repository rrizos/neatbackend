"""
WSGI config for neatbackend project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.0/howto/deployment/wsgi/
"""

import os
import logging

import django
from django.core.management import call_command
from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'neatbackend.settings')

logger = logging.getLogger(__name__)


def _run_startup_migrations():
    if os.environ.get('RUN_MIGRATIONS_ON_STARTUP', '1') != '1':
        return

    try:
        django.setup()
        call_command('migrate', interactive=False, run_syncdb=True, verbosity=0)
    except Exception:
        logger.exception('Automatic startup migrations failed')
        raise


_run_startup_migrations()
application = get_wsgi_application()
