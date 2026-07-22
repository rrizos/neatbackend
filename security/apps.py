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

    user = getattr(settings, 'EMAIL_HOST_USER', '')
    password = getattr(settings, 'EMAIL_HOST_PASSWORD', '')

    if not user or not password:
        issues.append(
            CheckWarning(
                'Outbound email is not configured — password reset will fail.',
                hint=(
                    'Set EMAIL_HOST_USER (the sending Gmail address) and '
                    'EMAIL_HOST_PASSWORD (a Google App Password, not the account '
                    'password) in the environment, then restart gunicorn.'
                ),
                id='neat_security.E001',
            )
        )
        return issues

    host = getattr(settings, 'EMAIL_HOST', '')
    sender = getattr(settings, 'DEFAULT_FROM_EMAIL', '')
    # DEFAULT_FROM_EMAIL is usually "Neat <account@gmail.com>" — compare the
    # address inside the angle brackets, not the whole display string.
    import re

    match = re.search(r'<([^>]+)>', sender or '')
    sender_address = (match.group(1) if match else (sender or '')).strip()
    if 'gmail' in host and sender_address and sender_address.lower() != user.lower():
        issues.append(
            CheckWarning(
                f'DEFAULT_FROM_EMAIL ({sender}) differs from the authenticated Gmail '
                f'account ({user}); Gmail refuses to send as an unrelated address.',
                hint=(
                    'Leave DEFAULT_FROM_EMAIL unset so it follows EMAIL_HOST_USER, or '
                    'set it to an alias configured on that Gmail account.'
                ),
                id='neat_security.E002',
            )
        )

    # An App Password is 16 characters; Google shows it in 4 groups of 4.
    stripped = password.replace(' ', '')
    if len(stripped) != 16:
        issues.append(
            CheckWarning(
                'EMAIL_HOST_PASSWORD does not look like a Google App Password '
                f'(expected 16 characters, got {len(stripped)}).',
                hint=(
                    'Regular account passwords are rejected by Gmail SMTP. Generate '
                    'one at Google Account > Security > 2-Step Verification > App passwords.'
                ),
                id='neat_security.E003',
            )
        )
    return issues


class SecurityConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'security'
    label = 'neat_security'
    verbose_name = 'Security & Audit'
