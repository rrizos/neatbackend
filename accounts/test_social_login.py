"""What the social endpoint does with a token it has already trusted.

Verification is covered in test_social_auth; here the verifier is replaced so
these can concentrate on the decision that follows it — which account, or a new
one — because that decision is where a mistake hands somebody the wrong
account rather than merely refusing a good one.
"""

import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from accounts.models import SocialAccount

User = get_user_model()

URL = '/api/auth/social/'


def _claims(**overrides):
    base = {
        'provider': 'google',
        'subject': 'google-sub-1',
        'email': 'someone@gmail.com',
        'email_verified': True,
        'name': 'Some One',
    }
    base.update(overrides)
    return base


class SocialLoginTests(TestCase):
    def _post(self, claims, **body):
        payload = {'provider': claims['provider'], 'idToken': 'irrelevant'}
        payload.update(body)
        with patch.dict(
            'accounts.social_auth.VERIFIERS',
            {claims['provider']: lambda *a, **k: claims},
        ):
            return self.client.post(
                URL, data=json.dumps(payload), content_type='application/json'
            )

    def test_a_new_identity_creates_an_account_with_no_city(self):
        res = self._post(_claims())
        self.assertEqual(res.status_code, 201)
        body = res.json()
        self.assertTrue(body['token'])
        # No city is the signal the client uses to finish sign-up on the map.
        self.assertEqual(body['user']['city'], '')
        self.assertEqual(User.objects.count(), 1)

    def test_signing_in_again_reuses_the_same_account(self):
        first = self._post(_claims()).json()
        second = self._post(_claims()).json()
        self.assertEqual(first['user']['username'], second['user']['username'])
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(SocialAccount.objects.count(), 1)
        # A fresh token each time, so signing in on a second device does not
        # invalidate the first.
        self.assertNotEqual(first['token'], second['token'])

    def test_a_verified_email_joins_the_account_that_already_uses_it(self):
        existing = User.objects.create_user(
            username='already_here', email='someone@gmail.com', password='x' * 12
        )
        res = self._post(_claims(email_verified=True))
        # 200, not 201: the account already existed, this only reached it by a
        # new route.
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['user']['username'], 'already_here')
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(existing.social_accounts.count(), 1)

    def test_an_unverified_email_never_joins_an_existing_account(self):
        """The account-takeover case.

        If a provider will hand out a token saying "this is someone@gmail.com"
        without having checked that the person owns it, then linking on the
        address alone would give whoever asked the account that already uses
        it. So an unverified address gets a new account of its own instead.
        """
        User.objects.create_user(
            username='already_here', email='someone@gmail.com', password='x' * 12
        )
        res = self._post(_claims(email_verified=False))
        self.assertEqual(res.status_code, 201)
        self.assertNotEqual(res.json()['user']['username'], 'already_here')
        self.assertEqual(User.objects.count(), 2)

    def test_two_providers_for_one_person_stay_separate_identities(self):
        self._post(_claims(provider='google', subject='g-1'))
        self._post(_claims(provider='apple', subject='a-1'))
        # Same verified address, so both land on one account, reachable either way.
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(SocialAccount.objects.count(), 2)

    def test_the_username_comes_from_the_email(self):
        self._post(_claims(email='maria.papadopoulou@gmail.com'))
        self.assertTrue(User.objects.filter(username='maria.papadopoulou').exists())

    def test_a_taken_username_is_given_a_suffix_rather_than_failing(self):
        User.objects.create_user(username='someone', email='other@x.com',
                                 password='x' * 12)
        res = self._post(_claims(email='someone@gmail.com'))
        self.assertEqual(res.status_code, 201)
        username = res.json()['user']['username']
        self.assertNotEqual(username, 'someone')
        self.assertTrue(username.startswith('someone'))

    def test_an_account_created_this_way_has_no_usable_password(self):
        self._post(_claims())
        user = User.objects.get()
        self.assertFalse(user.has_usable_password())

    def test_an_unknown_provider_is_refused(self):
        res = self.client.post(
            URL,
            data=json.dumps({'provider': 'facebook', 'idToken': 'x'}),
            content_type='application/json',
        )
        self.assertEqual(res.status_code, 400)


class UsernameChoiceTests(TestCase):
    """Replacing the username the server invented.

    A social sign-up never sees a username field, so the server puts one there
    to be able to create the account at all. These cover the handover: the
    account is flagged as carrying an invented name, the owner replaces it, and
    the flag stops the app asking again.
    """

    def _social(self, **overrides):
        claims = _claims(**overrides)
        with patch.dict(
            'accounts.social_auth.VERIFIERS',
            {claims['provider']: lambda *a, **k: claims},
        ):
            return self.client.post(
                URL,
                data=json.dumps({'provider': claims['provider'], 'idToken': 'x'}),
                content_type='application/json',
            ).json()

    def _patch_me(self, token, **fields):
        return self.client.patch(
            '/api/auth/me/',
            data=json.dumps(fields),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Token {token}',
        )

    def test_a_social_signup_is_asked_to_pick_a_username(self):
        body = self._social()
        self.assertTrue(body['user']['usernamePending'])

    def test_an_email_signup_is_not_asked(self):
        res = self.client.post(
            '/api/auth/signup/',
            data=json.dumps({'username': 'chosen_myself', 'password': 'a-str0ng-pass!'}),
            content_type='application/json',
        )
        self.assertFalse(res.json()['user']['usernamePending'])

    def test_choosing_a_username_clears_the_prompt(self):
        body = self._social()
        res = self._patch_me(body['token'], username='rafael')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['user']['username'], 'rafael')
        self.assertFalse(res.json()['user']['usernamePending'])

    def test_a_taken_username_is_refused(self):
        User.objects.create_user(username='taken', email='t@x.com', password='x' * 12)
        body = self._social()
        res = self._patch_me(body['token'], username='taken')
        self.assertEqual(res.status_code, 400)

    def test_the_format_rules_are_enforced_while_pending(self):
        for bad in ('ab', 'a' * 21, 'has spaces', 'has-a-dash', 'e@mail', '.leading'):
            body = self._social(subject=f'sub-{bad}', email=f'{abs(hash(bad))}@x.com')
            res = self._patch_me(body['token'], username=bad)
            self.assertEqual(res.status_code, 400, f'{bad!r} should have been refused')

    def test_a_reasonable_username_is_accepted(self):
        for good in ('rafael', 'rafael_r', 'rafael.rizos', 'neat123'):
            body = self._social(subject=f'sub-{good}', email=f'{good}@x.com')
            res = self._patch_me(body['token'], username=good)
            self.assertEqual(res.status_code, 200, f'{good!r} should have been allowed')

    def test_an_existing_account_can_still_use_a_name_the_new_rules_would_refuse(self):
        """The rules apply to the handover, not to everybody's edits.

        Accounts created before these rules existed hold names with characters
        the format check rejects. Enforcing it on every edit would start
        refusing changes for people whose names were always fine.
        """
        user = User.objects.create_user(username='old-style-name', email='o@x.com',
                                        password='x' * 12)
        from accounts.models import AuthToken
        token = AuthToken.create_for_user(user)
        res = self._patch_me(token.key, username='another-dashed-name')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['user']['username'], 'another-dashed-name')


class SetPasswordTests(TestCase):
    """Giving a provider-only account a way back in that is not the provider."""

    def _social(self, **overrides):
        claims = _claims(**overrides)
        with patch.dict(
            'accounts.social_auth.VERIFIERS',
            {claims['provider']: lambda *a, **k: claims},
        ):
            return self.client.post(
                URL,
                data=json.dumps({'provider': claims['provider'], 'idToken': 'x'}),
                content_type='application/json',
            ).json()

    def _set(self, token, **fields):
        return self.client.post(
            '/api/auth/password/set/',
            data=json.dumps(fields),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Token {token}',
        )

    def test_a_social_account_starts_with_no_password(self):
        self.assertFalse(self._social()['user']['hasPassword'])

    def test_it_can_set_a_first_password_without_naming_an_old_one(self):
        body = self._social()
        res = self._set(body['token'], password='a-str0ng-passphrase!')
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()['user']['hasPassword'])

    def test_that_password_then_works_for_signing_in(self):
        """The whole point: a second way into the account."""
        body = self._social()
        username = body['user']['username']
        self._set(body['token'], password='a-str0ng-passphrase!')
        res = self.client.post(
            '/api/auth/login/',
            data=json.dumps({'username': username, 'password': 'a-str0ng-passphrase!'}),
            content_type='application/json',
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()['token'])

    def test_a_weak_password_is_refused(self):
        body = self._social()
        res = self._set(body['token'], password='12345')
        self.assertEqual(res.status_code, 400)
        self.assertFalse(User.objects.get().has_usable_password())

    def test_replacing_an_existing_password_needs_the_current_one(self):
        """A borrowed unlocked phone must not be able to lock the owner out."""
        body = self._social()
        self._set(body['token'], password='the-first-passphrase!')
        res = self._set(body['token'], password='an-attackers-passphrase!')
        self.assertEqual(res.status_code, 400)
        # And the original still works.
        user = User.objects.get()
        self.assertTrue(user.check_password('the-first-passphrase!'))

    def test_replacing_it_works_when_the_current_one_is_given(self):
        body = self._social()
        self._set(body['token'], password='the-first-passphrase!')
        res = self._set(
            body['token'],
            currentPassword='the-first-passphrase!',
            password='the-second-passphrase!',
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(User.objects.get().check_password('the-second-passphrase!'))

    def test_it_needs_a_signed_in_account(self):
        res = self.client.post(
            '/api/auth/password/set/',
            data=json.dumps({'password': 'a-str0ng-passphrase!'}),
            content_type='application/json',
        )
        self.assertEqual(res.status_code, 401)
