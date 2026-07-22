from django.apps import AppConfig
from django.core.checks import Warning as CheckWarning
from django.core.checks import register


@register()
def check_email_configured(app_configs, **kwargs):
    """Surface unconfigured outbound mail at deploy time.

    Password reset silently depended on SMTP credentials that were never set,
    so the only signal was a user hitting "we couldn't send the email". This
    makes `manage.py check` say so instead.
    """
    from django.conf import settings

    issues = []
    backend = getattr(settings, 'EMAIL_BACKEND', '')
    if 'smtp' not in backend:
        return issues  # console/locmem backends need no credentials

    if not getattr(settings, 'EMAIL_HOST_USER', '') or not getattr(
        settings, 'EMAIL_HOST_PASSWORD', ''
    ):
        issues.append(
            CheckWarning(
                'Outbound email is not configured — password reset will fail.',
                hint=(
                    'Set EMAIL_HOST_USER (Brevo SMTP login) and EMAIL_HOST_PASSWORD '
                    '(Brevo SMTP key) in the environment, then restart gunicorn.'
                ),
                id='neat_security.E001',
            )
        )

    sender = getattr(settings, 'DEFAULT_FROM_EMAIL', '')
    if sender.endswith('@smtp-brevo.com'):
        issues.append(
            CheckWarning(
                'DEFAULT_FROM_EMAIL is set to the Brevo SMTP login, which is not a '
                'sendable mailbox — Brevo will reject it as an unverified sender.',
                hint='Use an address verified in Brevo, e.g. noreply@neatapp.gr.',
                id='neat_security.E002',
            )
        )
    return issues


class SecurityConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'security'
    label = 'neat_security'
    verbose_name = 'Security & Audit'
