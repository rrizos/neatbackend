"""Health endpoint tests.

The point of a health page is to be reliable when nothing else is, so the
things worth testing are that it never raises, never leaks, and never cries
wolf — not that any particular number is right.
"""

from unittest import mock

from django.test import TestCase

from web import health


class HealthCollectTests(TestCase):
    def test_collect_never_raises_when_every_probe_fails(self):
        """A probe blowing up must become a finding, not a 500. This is the
        whole contract: a health page that fails when the box is unhealthy is
        worse than not having one."""
        def boom():
            raise RuntimeError('probe exploded')

        with mock.patch.object(health, 'PROBES', tuple((n, boom) for n, _ in health.PROBES)):
            snap = health.collect(use_cache=False)

        self.assertIn(snap['status'], (health.WARN, health.CRIT))
        self.assertTrue(snap['findings'])
        for f in snap['findings']:
            self.assertNotEqual(f['severity'], health.CRIT, 'a failed probe is not a crisis')

    def test_notes_do_not_raise_the_verdict(self):
        """NOTE is for standing facts. If it escalated, the page would sit
        permanently yellow and everyone would learn to ignore it."""
        def only_notes():
            return {'findings': [health._finding(health.NOTE, 'standing fact', 'detail', 'do')]}

        with mock.patch.object(health, 'PROBES', (('x', only_notes),)):
            snap = health.collect(use_cache=False)

        self.assertEqual(snap['status'], health.OK)
        self.assertEqual(snap['actions'], [], 'a note is not an action')

    def test_crit_finding_drives_status_and_actions(self):
        def crit():
            return {'findings': [health._finding(
                health.CRIT, 'disk full', '96%', 'clear space')]}

        with mock.patch.object(health, 'PROBES', (('x', crit),)):
            snap = health.collect(use_cache=False)

        self.assertEqual(snap['status'], health.CRIT)
        self.assertEqual(len(snap['actions']), 1)
        self.assertEqual(snap['actions'][0]['action'], 'clear space')


class HealthViewTests(TestCase):
    def test_dashboard_requires_admin_and_leaks_nothing(self):
        resp = self.client.get('/health')
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        # The login form, not the dashboard.
        self.assertNotIn('Do this', body)
        self.assertNotIn('vCPU', body)

    def test_json_also_requires_admin(self):
        resp = self.client.get('/health?format=json')
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('application/json', resp['Content-Type'])

    def test_liveness_is_public_and_says_one_word(self):
        for url in ('/health/live', '/health/ready'):
            resp = self.client.get(url)
            self.assertIn(resp.status_code, (200, 503))
            body = resp.content.decode().strip()
            self.assertEqual(len(body.split()), 1, f'{url} leaked detail: {body!r}')
            self.assertIn(body, ('ok', 'note', 'warn', 'crit', 'error'))
            self.assertEqual(resp['Cache-Control'], 'no-store')

    def test_liveness_reports_503_when_critical(self):
        with mock.patch.object(health, 'collect', return_value={
                'status': health.CRIT, 'findings': [
                    {'severity': health.CRIT, 'title': 'secret internal detail'}]}):
            resp = self.client.get('/health/live')
        self.assertEqual(resp.status_code, 503)
        self.assertNotIn('secret', resp.content.decode())


class AnalyticsLaunchScopeTests(TestCase):
    """The launch cutoff is the whole point of the page being trustworthy: the
    imported and test accounts never opened the app, so counting them sinks
    retention and the activation funnel while adding nothing true."""

    LAUNCH = '2026-09-07'

    def _user(self, name, joined):
        from django.contrib.auth import get_user_model
        u = get_user_model().objects.create_user(username=name, password='x')
        get_user_model().objects.filter(pk=u.pk).update(date_joined=joined)
        return u

    def setUp(self):
        from django.utils import timezone
        from datetime import datetime
        cut = timezone.make_aware(datetime(2026, 9, 7), timezone.get_current_timezone())
        self.before = self._user('legacy_import', cut - timezone.timedelta(days=30))
        self.after = self._user('real_signup', cut + timezone.timedelta(hours=2))

    def test_scoped_collect_excludes_prelaunch_accounts(self):
        from web import analytics
        with self.settings(NEAT_LAUNCH_DATE=self.LAUNCH):
            data = analytics.collect(launch_scoped=True)
        self.assertEqual(data['head']['total_users'], 1, 'pre-launch account leaked in')
        self.assertTrue(data['scope']['scoped'])
        self.assertEqual(data['scope']['excluded_users'], 1)

    def test_unscoped_collect_includes_everything(self):
        from web import analytics
        with self.settings(NEAT_LAUNCH_DATE=self.LAUNCH):
            data = analytics.collect(launch_scoped=False)
        self.assertEqual(data['head']['total_users'], 2)
        self.assertFalse(data['scope']['scoped'])

    def test_unparseable_launch_date_disables_scoping_rather_than_raising(self):
        """A bad date in the environment should cost a filter, not the page."""
        from web import analytics
        with self.settings(NEAT_LAUNCH_DATE='not-a-date'):
            self.assertIsNone(analytics.launch_date())
            data = analytics.collect(launch_scoped=True)
        self.assertEqual(data['head']['total_users'], 2)

    def test_scope_does_not_leak_between_calls(self):
        """The scope is a contextvar; an unscoped render must not leave the
        next request unscoped."""
        from web import analytics
        with self.settings(NEAT_LAUNCH_DATE=self.LAUNCH):
            analytics.collect(launch_scoped=False)
            data = analytics.collect(launch_scoped=True)
        self.assertEqual(data['head']['total_users'], 1)
