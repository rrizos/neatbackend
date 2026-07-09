import logging

from .firebase import get_app
from .models import DeviceToken

logger = logging.getLogger(__name__)

# Mirrors the prefixes checked client-side in messages_page.dart (_kPostPrefix,
# _kImagePrefix, _kVoicePrefix, _kReplyPrefix) so the push preview text matches
# what the inbox row already shows instead of leaking raw encoded payloads.
_RICH_MESSAGE_LABELS = [
    ('__neat_post__:', 'Sent a post'),
    ('__neat_image__:', 'Sent a photo'),
    ('__neat_voice__:', 'Sent a voice message'),
    ('__neat_reply__:', 'Replied'),
]


def message_preview_text(text):
    for prefix, label in _RICH_MESSAGE_LABELS:
        if text.startswith(prefix):
            return label
    return text[:200]


def _stringify_data(data):
    return {str(k): str(v) for k, v in (data or {}).items() if v is not None}


def _usable_image_url(url):
    return url if url and (url.startswith('http://') or url.startswith('https://')) else None


def _send_to_user(user, *, title, body, data, silent, image=None):
    app = get_app()
    if app is None:
        return

    tokens = list(DeviceToken.objects.filter(user=user).values_list('id', 'token'))
    if not tokens:
        return

    from firebase_admin import messaging

    channel_id = 'messages_channel' if not silent else 'soft_channel'
    # Brand blue for the small-icon badge — without this Android shows it in
    # a plain gray/white circle instead of a colored one.
    android_notification_kwargs = {'channel_id': channel_id, 'color': '#1479FF'}
    apns_aps_kwargs = {}
    if silent:
        # Omitting the sound key on both platforms is what keeps these
        # "soft" — they show in the tray but never ring or vibrate.
        pass
    else:
        android_notification_kwargs['sound'] = 'default'
        apns_aps_kwargs['sound'] = 'default'
    if image:
        android_notification_kwargs['image'] = image

    messages = []
    ordered_tokens = []
    id_by_token = {}
    for token_id, token in tokens:
        apns_payload = {'aps': messaging.Aps(**apns_aps_kwargs)}
        apns_fcm_options = messaging.APNSFCMOptions(image=image) if image else None
        messages.append(
            messaging.Message(
                token=token,
                notification=messaging.Notification(title=title, body=body),
                data=_stringify_data(data),
                android=messaging.AndroidConfig(
                    priority='high' if not silent else 'normal',
                    notification=messaging.AndroidNotification(**android_notification_kwargs),
                ),
                apns=messaging.APNSConfig(
                    payload=messaging.APNSPayload(**apns_payload),
                    fcm_options=apns_fcm_options,
                    headers={'apns-priority': '10' if not silent else '5'},
                ),
            )
        )
        ordered_tokens.append(token)
        id_by_token[token] = token_id

    try:
        response = messaging.send_each(messages)
    except Exception:
        logger.exception('Failed to send push notification batch to user %s', user.username)
        return

    stale_token_ids = []
    for token, result in zip(ordered_tokens, response.responses):
        if result.success:
            continue
        exc = result.exception
        code = getattr(exc, 'code', '') or ''
        if code in ('NOT_FOUND', 'UNREGISTERED') or type(exc).__name__ == 'UnregisteredError':
            stale_token_ids.append(id_by_token[token])
        else:
            logger.warning('Push send failed for a token of user %s: %s', user.username, exc)

    if stale_token_ids:
        DeviceToken.objects.filter(id__in=stale_token_ids).delete()


def send_soft(user, *, title, body, data=None):
    """A notification-center push: shows in the tray, never rings/vibrates."""
    try:
        _send_to_user(user, title=title, body=body, data=data, silent=True)
    except Exception:
        logger.exception('send_soft failed for user %s', user.username)


def send_message_alert(user, *, sender_profile, sender_username, text, conversation_id):
    """A DM push: full alert with default sound + the sender's avatar image,
    matching Instagram's message notification."""
    try:
        title = f'@{sender_username}'
        body = message_preview_text(text)
        image = _usable_image_url(getattr(sender_profile, 'avatar_url', ''))
        _send_to_user(
            user,
            title=title,
            body=body,
            data={'type': 'dm', 'conversationId': conversation_id},
            silent=False,
            image=image,
        )
    except Exception:
        logger.exception('send_message_alert failed for user %s', user.username)
