import io
import json

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.test.client import BOUNDARY, MULTIPART_CONTENT, encode_multipart
from PIL import Image

from accounts.models import AuthToken, Notification, Profile

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


class NotificationReadTests(TestCase):
    """Opening the tab reads the tab.

    Marking one notification per tap left the badge lit over a list the user
    had already been through, so the client now says "all of them" when the
    sheet opens. It has to mean *all* of them, not the newest page: the list
    endpoint hands back fifty, and anything older would otherwise keep the
    badge on forever.
    """

    def setUp(self):
        self.user = User.objects.create_user('mina', password='x')
        self.other = User.objects.create_user('nikos', password='x')
        self.token = AuthToken.create_for_user(self.user).key

    def _notify(self, recipient, verb='like'):
        return Notification.objects.create(
            recipient=recipient, actor=self.other, verb=verb
        )

    def _post(self, body):
        return self.client.post(
            '/api/auth/notifications/',
            data=json.dumps(body),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Token {self.token}',
        )

    def test_all_marks_every_unread_one(self):
        rows = [self._notify(self.user) for _ in range(60)]
        res = self._post({'all': True})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(
            Notification.objects.filter(recipient=self.user, is_read=False).count(), 0,
            'a notification past the fiftieth still keeps the badge lit',
        )
        self.assertEqual(len(rows), 60)

    def test_all_does_not_touch_anyone_elses(self):
        theirs = self._notify(self.other)
        self._notify(self.user)
        self._post({'all': True})
        theirs.refresh_from_db()
        self.assertFalse(theirs.is_read)

    def test_ids_still_work_for_older_builds(self):
        one = self._notify(self.user)
        two = self._notify(self.user)
        res = self._post({'ids': [one.id]})
        self.assertEqual(res.status_code, 200, res.content)
        one.refresh_from_db()
        two.refresh_from_db()
        self.assertTrue(one.is_read)
        self.assertFalse(two.is_read)


class UsernameFormatTests(TestCase):
    """A username has to survive being put in a URL.

    The profile is fetched as /api/auth/profiles/<username>/, so a name
    holding a slash, a space or an emoji produced an account whose own profile
    would not open — and nothing refused it, because signup never checked the
    format at all. Both doors onto the column are checked now; the accounts
    that already hold an odd name keep it, and are only asked to pick a new
    one if they change it themselves.
    """

    def _signup(self, username):
        return self.client.post(
            '/api/auth/signup/',
            data=json.dumps({
                'username': username,
                'password': 'a-long-enough-passphrase-42',
                'email': f'{abs(hash(username))}@example.com',
            }),
            content_type='application/json',
        )

    def test_a_username_that_would_break_its_own_url_is_refused(self):
        for name in ('ri/zos', 'ri zos', 'Μαρία', 'exa🎀', 'a', 'giannhs.',
                     'this-name-is-far-too-long-to-fit'):
            with self.subTest(name=name):
                res = self._signup(name)
                self.assertEqual(res.status_code, 400, name)
                self.assertFalse(User.objects.filter(username=name).exists())

    def test_an_ordinary_username_still_signs_up(self):
        res = self._signup('rizos.99_a')

        self.assertEqual(res.status_code, 201)
        self.assertTrue(User.objects.filter(username='rizos.99_a').exists())

    def test_an_existing_odd_username_is_left_alone(self):
        """Created before the rule, and nothing here evicts them from it."""
        user = User.objects.create_user('Nikos Pritsios', password='x')
        token = AuthToken.create_for_user(user).key

        res = self.client.patch(
            '/api/auth/me/',
            data=json.dumps({'bio': 'γεια'}),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Token {token}',
        )

        self.assertEqual(res.status_code, 200)
        user.refresh_from_db()
        self.assertEqual(user.username, 'Nikos Pritsios')

    def test_an_existing_account_can_change_to_another_odd_name(self):
        """Only what a URL cannot carry is refused on an edit — a name that
        merely predates the rule is still theirs to keep."""
        user = User.objects.create_user('old-style-name', password='x')
        token = AuthToken.create_for_user(user).key

        res = self.client.patch(
            '/api/auth/me/',
            data=json.dumps({'username': 'another-dashed-name'}),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Token {token}',
        )

        self.assertEqual(res.status_code, 200)
        user.refresh_from_db()
        self.assertEqual(user.username, 'another-dashed-name')

    def test_changing_to_a_broken_username_is_refused(self):
        user = User.objects.create_user('Nikos Pritsios', password='x')
        token = AuthToken.create_for_user(user).key

        res = self.client.patch(
            '/api/auth/me/',
            data=json.dumps({'username': 'nikos/p'}),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Token {token}',
        )

        self.assertEqual(res.status_code, 400)
        user.refresh_from_db()
        self.assertEqual(user.username, 'Nikos Pritsios')
