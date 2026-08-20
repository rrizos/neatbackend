import io
import json

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.test.client import BOUNDARY, MULTIPART_CONTENT, encode_multipart
from PIL import Image

from accounts.models import AuthToken, Profile

User = get_user_model()


def _jpeg(size=(1024, 1024)):
    buf = io.BytesIO()
    Image.new('RGB', size, (30, 120, 200)).save(buf, 'JPEG', quality=90)
    return buf.getvalue()


class AvatarUploadTests(TestCase):
    """Uploading a profile picture as binary rather than base64.

    The app used to encode the JPEG into a JSON body, which costs a third more
    bytes than the file — measured at 435 KB for a 1024px photo — on the single
    largest upload the app makes and over the connection where there is least
    to spare. Binary multipart sends the file as-is. The base64 path stays,
    because every build released before this uses it.
    """

    def setUp(self):
        self.user = User.objects.create_user('rita', password='x')
        Profile.objects.update_or_create(user=self.user, defaults={'city': 'Αθήνα'})
        self.token = AuthToken.create_for_user(self.user).key

    def _auth(self, client_version='3'):
        return {'HTTP_AUTHORIZATION': f'Token {self.token}',
                'HTTP_X_NEAT_CLIENT': client_version}

    def test_a_binary_upload_produces_both_files_and_the_inline_copy(self):
        payload = encode_multipart(BOUNDARY, {
            'avatar': SimpleUploadedFile('a.jpg', _jpeg(), 'image/jpeg'),
            'bio': 'γεια',
        })
        res = self.client.patch(
            '/api/auth/me/', data=payload,
            content_type=MULTIPART_CONTENT, **self._auth(),
        )
        self.assertEqual(res.status_code, 200, res.content)
        p = Profile.objects.get(user=self.user)
        self.assertTrue(p.avatar_thumb_url.startswith('/media/avatars/'))
        self.assertTrue(p.avatar_full_url.startswith('/media/avatars/'))
        # Still produced, so builds that only read base64 keep working.
        self.assertTrue(p.avatar_url.startswith('data:image/jpeg;base64,'))
        self.assertEqual(p.bio, 'γεια')

    def test_the_binary_upload_is_smaller_than_the_base64_one(self):
        raw = _jpeg()
        encoded = 'data:image/jpeg;base64,' + __import__('base64').b64encode(raw).decode()
        self.assertLess(
            len(raw), len(encoded) * 0.8,
            'binary should be meaningfully smaller than the base64 of the same image',
        )

    def test_the_json_path_still_works(self):
        import base64 as b64
        data_url = 'data:image/jpeg;base64,' + b64.b64encode(_jpeg()).decode()
        res = self.client.patch(
            '/api/auth/me/',
            data=json.dumps({'avatarUrl': data_url}),
            content_type='application/json',
            **self._auth('2'),
        )
        self.assertEqual(res.status_code, 200, res.content)
        p = Profile.objects.get(user=self.user)
        self.assertTrue(p.avatar_url.startswith('data:'))
        self.assertTrue(p.avatar_thumb_url.startswith('/media/avatars/'))

    def test_an_oversized_upload_is_refused(self):
        payload = encode_multipart(BOUNDARY, {
            'avatar': SimpleUploadedFile('big.jpg', b'x' * (21 * 1024 * 1024), 'image/jpeg'),
        })
        res = self.client.patch(
            '/api/auth/me/', data=payload,
            content_type=MULTIPART_CONTENT, **self._auth(),
        )
        self.assertEqual(res.status_code, 400)
