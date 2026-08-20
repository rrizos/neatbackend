import json

from django.contrib.auth import get_user_model
from django.test import TestCase

from accounts.models import AuthToken
from push.models import DeviceToken

User = get_user_model()


class DeviceRegistrationTests(TestCase):
    """One phone, one token.

    FCM issues a new token on reinstall and nothing tied it to the old one, so
    every reinstall left another live row behind. One account had four, and
    every notification was delivered to all four — which is what "I get
    notifications twice" was.
    """

    def setUp(self):
        self.user = User.objects.create_user('pusher', password='x')
        self.token = AuthToken.create_for_user(self.user).key

    def _register(self, fcm_token, device_id=None):
        body = {'token': fcm_token, 'platform': 'ios'}
        if device_id:
            body['deviceId'] = device_id
        return self.client.post(
            '/api/push/devices/register/',
            data=json.dumps(body),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Token {self.token}',
        )

    def test_a_new_token_from_the_same_phone_replaces_the_old_one(self):
        self._register('token-one', device_id='phone-A')
        self._register('token-two', device_id='phone-A')
        tokens = list(DeviceToken.objects.values_list('token', flat=True))
        self.assertEqual(tokens, ['token-two'],
                         'the reinstall left the old token behind')

    def test_two_real_devices_both_keep_their_token(self):
        self._register('token-phone', device_id='phone-A')
        self._register('token-tablet', device_id='phone-B')
        self.assertEqual(DeviceToken.objects.count(), 2,
                         'a second device must not evict the first')

    def test_a_client_without_a_device_id_still_registers(self):
        # Builds released before device ids existed keep working; they simply
        # do not get the de-duplication.
        res = self._register('legacy-token')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(DeviceToken.objects.count(), 1)
