"""Signing in with an address instead of a username."""

import json

from django.contrib.auth import get_user_model
from django.test import TestCase

User = get_user_model()
PASSWORD = 'a-str0ng-passphrase!'


class EmailLoginTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='rafael', email='Rafael@Example.com', password=PASSWORD
        )

    def _login(self, who, password=PASSWORD):
        return self.client.post(
            '/api/auth/login/',
            data=json.dumps({'username': who, 'password': password}),
            content_type='application/json',
        )

    def test_the_username_still_works(self):
        self.assertEqual(self._login('rafael').status_code, 200)

    def test_the_email_works_too(self):
        self.assertEqual(self._login('Rafael@Example.com').status_code, 200)

    def test_the_email_is_matched_regardless_of_case(self):
        self.assertEqual(self._login('rafael@example.com').status_code, 200)

    def test_the_wrong_password_is_still_refused(self):
        self.assertEqual(self._login('rafael@example.com', 'wrong').status_code, 400)

    def test_an_unknown_address_is_refused(self):
        self.assertEqual(self._login('nobody@example.com').status_code, 400)

    def test_a_shared_address_is_refused_rather_than_guessed(self):
        """Several accounts here share one address.

        Signing in with it cannot mean one account in particular, and choosing
        one would put somebody in an account they did not ask for. They use
        their username instead.
        """
        User.objects.create_user(
            username='someone_else', email='Rafael@Example.com', password=PASSWORD
        )
        self.assertEqual(self._login('rafael@example.com').status_code, 400)
        # The username still gets each of them into their own account.
        self.assertEqual(self._login('rafael').status_code, 200)
        self.assertEqual(self._login('someone_else').status_code, 200)
