"""Setting a home city, and the month before it can be changed again.

The distinction everything here turns on: **setting a first city is not a
change.** Every account reaches that step seconds after being created, so a
rule measured from sign-up refuses it and nobody can finish signing up — which
is what happened the first time this was written. The hold applies only once a
city is actually held.
"""

import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from accounts.models import AuthToken, Profile
from accounts.serializers import ensure_profile

User = get_user_model()


class CityRuleTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='rafael', email='r@example.com', password='a-str0ng-pass!'
        )
        self.profile = ensure_profile(self.user)
        self.token = AuthToken.create_for_user(self.user).key

    def _patch(self, **fields):
        return self.client.patch(
            '/api/auth/me/',
            data=json.dumps(fields),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Token {self.token}',
        )

    def _backdate_city_choice(self, days):
        """Behave as if the city was chosen [days] ago."""
        Profile.objects.filter(pk=self.profile.pk).update(
            city_changed_at=timezone.now() - timezone.timedelta(days=days)
        )
        self.profile.refresh_from_db()

    # ── Signing up ──────────────────────────────────────────────────────────

    def test_a_seconds_old_account_can_set_its_first_city(self):
        """The regression that made sign-up impossible."""
        res = self._patch(city='Ρόδος')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['user']['city'], 'Ρόδος')

    def test_a_new_account_is_told_it_may_choose(self):
        body = self._patch(bio='x').json()['user']
        self.assertTrue(body['canChangeCity'])
        self.assertIsNone(body['cityChangeAllowedAt'])

    def test_the_month_runs_from_the_choice_not_from_signup(self):
        self._patch(city='Ρόδος')
        self.profile.refresh_from_db()
        self.assertIsNotNone(self.profile.city_changed_at)

    # ── Changing it ─────────────────────────────────────────────────────────

    def test_it_cannot_be_changed_again_straight_away(self):
        self.assertEqual(self._patch(city='Ρόδος').status_code, 200)
        res = self._patch(city='Αθήνα')
        self.assertEqual(res.status_code, 400)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.city, 'Ρόδος')

    def test_it_can_be_changed_after_a_month(self):
        self._patch(city='Ρόδος')
        self._backdate_city_choice(31)
        res = self._patch(city='Αθήνα')
        self.assertEqual(res.status_code, 200)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.city, 'Αθήνα')

    def test_changing_it_restarts_the_month(self):
        self._patch(city='Ρόδος')
        self._backdate_city_choice(31)
        self.assertEqual(self._patch(city='Αθήνα').status_code, 200)
        self.assertEqual(self._patch(city='Θεσσαλονίκη').status_code, 400)

    def test_an_account_from_before_this_field_is_measured_from_signup(self):
        self.profile.city = 'Ρόδος'
        self.profile.city_changed_at = None
        self.profile.save(update_fields=['city', 'city_changed_at'])
        self.assertFalse(self.profile.can_change_city())
        Profile.objects.filter(pk=self.profile.pk).update(
            created=timezone.now() - timezone.timedelta(days=400)
        )
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.can_change_city())

    # ── Everything else keeps working ───────────────────────────────────────

    def test_resending_the_same_city_is_not_a_change(self):
        self._patch(city='Ρόδος')
        res = self._patch(city='Ρόδος', bio='still here')
        self.assertEqual(res.status_code, 200)

    def test_a_held_city_never_blocks_the_rest_of_the_profile(self):
        self._patch(city='Ρόδος')
        res = self._patch(fullName='Ραφαήλ', bio='γεια')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['user']['fullName'], 'Ραφαήλ')

    def test_making_a_second_account_is_never_affected_by_the_first(self):
        """Two accounts, both brand new, both able to pick a city."""
        self._patch(city='Ρόδος')
        other = User.objects.create_user(username='second', password='a-str0ng-pass!')
        ensure_profile(other)
        token = AuthToken.create_for_user(other).key
        res = self.client.patch(
            '/api/auth/me/',
            data=json.dumps({'city': 'Αθήνα'}),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Token {token}',
        )
        self.assertEqual(res.status_code, 200)


class SocialSignupCityTests(TestCase):
    """The full provider path, which is where the breakage actually showed."""

    def test_a_google_signup_can_finish_choosing_its_city(self):
        from unittest.mock import patch as mock_patch
        claims = {
            'provider': 'google', 'subject': 'g-city-1',
            'email': 'city@example.com', 'email_verified': True, 'name': 'City',
        }
        with mock_patch.dict('accounts.social_auth.VERIFIERS',
                             {'google': lambda *a, **k: claims}):
            body = self.client.post(
                '/api/auth/social/',
                data=json.dumps({'provider': 'google', 'idToken': 'x'}),
                content_type='application/json',
            ).json()
        self.assertEqual(body['user']['city'], '')
        res = self.client.patch(
            '/api/auth/me/',
            data=json.dumps({'city': 'Ρόδος'}),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Token {body["token"]}',
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['user']['city'], 'Ρόδος')
