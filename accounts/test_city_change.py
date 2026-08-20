"""The one-month hold on changing your home city.

Everything in the app is scoped to a city — the feed, who may post, which
events show, who can find you — so the value is not an ordinary profile field
and cannot be edited like one.
"""

import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from accounts.models import AuthToken, Profile
from accounts.serializers import ensure_profile

User = get_user_model()


class CityChangeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='rafael', email='r@example.com', password='a-str0ng-pass!'
        )
        self.profile = ensure_profile(self.user)
        self.profile.city = 'Ρόδος'
        self.profile.save(update_fields=['city'])
        self.token = AuthToken.create_for_user(self.user).key

    def _patch(self, **fields):
        return self.client.patch(
            '/api/auth/me/',
            data=json.dumps(fields),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Token {self.token}',
        )

    def _age(self, days):
        """Backdate the account so it behaves as if [days] have passed."""
        old = timezone.now() - timezone.timedelta(days=days)
        Profile.objects.filter(pk=self.profile.pk).update(created=old)
        self.profile.refresh_from_db()

    def test_a_new_account_cannot_change_city_immediately(self):
        res = self._patch(city='Αθήνα')
        self.assertEqual(res.status_code, 400)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.city, 'Ρόδος')

    def test_after_a_month_it_can(self):
        self._age(31)
        res = self._patch(city='Αθήνα')
        self.assertEqual(res.status_code, 200)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.city, 'Αθήνα')

    def test_changing_it_starts_the_month_again(self):
        self._age(31)
        self.assertEqual(self._patch(city='Αθήνα').status_code, 200)
        # Straight back for a second hop — refused.
        res = self._patch(city='Θεσσαλονίκη')
        self.assertEqual(res.status_code, 400)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.city, 'Αθήνα')

    def test_the_clock_runs_from_the_last_change_not_from_signup(self):
        self._age(400)
        self.assertEqual(self._patch(city='Αθήνα').status_code, 200)
        self.profile.refresh_from_db()
        # Signed up long ago, but changed just now.
        self.assertFalse(self.profile.can_change_city())

    def test_editing_other_fields_is_never_blocked_by_the_hold(self):
        """A held city must not make the rest of the profile read-only."""
        res = self._patch(fullName='Ραφαήλ', bio='γεια')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['user']['fullName'], 'Ραφαήλ')

    def test_resending_the_same_city_is_not_treated_as_a_change(self):
        res = self._patch(city='Ρόδος', bio='unchanged city')
        self.assertEqual(res.status_code, 200)

    def test_the_response_says_when_the_next_change_is_allowed(self):
        body = self._patch(bio='x').json()['user']
        self.assertFalse(body['canChangeCity'])
        self.assertTrue(body['cityChangeAllowedAt'])
        self._age(31)
        body = self._patch(bio='y').json()['user']
        self.assertTrue(body['canChangeCity'])
