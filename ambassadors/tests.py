"""The ambassador link.

/a/<code> shows the ordinary landing page. What has to survive that is the
counting: the visit is recorded, and the click token still reaches the two
places the app reads it back from.
"""

import json
import os
import re
import tempfile
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase

from lockedcities.cities import APP_CITIES

from . import views
from .models import (
    Ambassador, AmbassadorClick, AmbassadorContent, AmbassadorSignup,
)

User = get_user_model()

PLAY = 'https://play.google.com/store/apps/details?id=gr.app.neat'
LANDING = (
    '<!doctype html><html><head><title>Neat</title></head><body>'
    '<p class="lede">Κατέβασε την Neat</p>'
    '<a class="store-badge" href="https://apps.apple.com/gr/app/neat/id6748038152?l=el">App Store</a>'
    f'<a class="store-badge" href="{PLAY}">Google Play</a>'
    '</body></html>'
)


class PublishedLanding:
    """Puts a landing page where the view looks for one.

    Without it /a/<code> has nothing to serve and redirects instead, which is
    a different path from the one these tests are about.
    """

    def publish_landing(self):
        self.root = tempfile.mkdtemp()
        with open(os.path.join(self.root, 'index.html'), 'w', encoding='utf-8') as f:
            f.write(LANDING)
        patcher = mock.patch('web.views.WEB_ROOT', self.root)
        patcher.start()
        self.addCleanup(patcher.stop)


class AmbassadorLinkTests(PublishedLanding, TestCase):
    def setUp(self):
        cache.clear()
        self.ambassador = Ambassador.objects.create(name='Μαρία Π.', code='maria')
        self.publish_landing()

    def test_shows_the_landing_page_not_the_ambassador(self):
        response = self.client.get('/a/maria')

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('Κατέβασε την Neat', body)
        self.assertNotIn('Μαρία', body)
        self.assertNotIn('σε προσκαλεί', body)

    def test_still_counts_the_click_and_hands_out_its_token(self):
        body = self.client.get('/a/maria').content.decode()

        click = AmbassadorClick.objects.get(ambassador=self.ambassador)
        self.assertEqual(click.source, AmbassadorClick.WEB)
        # Android: the install referrer on the Play badge.
        self.assertIn(f'{PLAY}&amp;referrer=neat_ct%3D{click.token}', body)
        self.assertNotIn(f'href="{PLAY}"', body)
        # iOS: the pasteboard, written when a badge is pressed.
        self.assertIn(f'"neat_ct={click.token}"', body)
        self.assertLess(body.index('navigator.clipboard'), body.index('</body>'))

    def test_unknown_code_gets_the_same_page_with_nothing_to_claim(self):
        response = self.client.get('/a/nobody')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode(), LANDING)
        self.assertFalse(AmbassadorClick.objects.exists())

    def test_head_does_not_count(self):
        self.client.head('/a/maria')

        self.assertFalse(AmbassadorClick.objects.exists())

    def test_without_a_published_landing_page_it_still_counts(self):
        os.remove(os.path.join(self.root, 'index.html'))

        response = self.client.get('/a/maria')

        self.assertRedirects(response, '/', fetch_redirect_response=False)
        self.assertEqual(AmbassadorClick.objects.count(), 1)

    def test_a_republished_landing_page_is_picked_up(self):
        self.client.get('/a/nobody')
        path = os.path.join(self.root, 'index.html')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(LANDING.replace('Κατέβασε την Neat', 'Νέο κείμενο'))
        stat = os.stat(path)
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

        self.assertIn('Νέο κείμενο', self.client.get('/a/nobody').content.decode())


class CreatorDashboardTests(TestCase):
    """The creator's own page.

    It was redesigned, so these pin the parts a redesign can quietly take
    away: the two things a creator can do here, the tab they come back to,
    and the URLs the poster and the printout are fetched from.
    """

    def setUp(self):
        cache.clear()
        self.ambassador = Ambassador.objects.create(
            name='Μαρία Π.', code='maria', payout_per_signup=Decimal('1.50'))
        self.url = f'/creator/{self.ambassador.dashboard_key}/'

    def test_it_renders_with_the_link_the_poster_and_the_print_page(self):
        body = self.client.get(self.url).content.decode()

        self.assertIn('neatapp.gr/a/maria', body)
        self.assertIn(f'/creator/{self.ambassador.dashboard_key}/qr.svg?style=basic', body)
        self.assertIn(f'/creator/{self.ambassador.dashboard_key}/qr.png?download=1', body)
        self.assertIn('/a/maria/print?style=basic', body)
        # Every style is rendered, which is what makes switching tabs free.
        for style in ('basic', 'aggressive', 'custom'):
            self.assertIn(f'id="img-{style}"', body)
            self.assertIn(f'data-pane="{style}"', body)

    def test_the_hidden_key_never_reaches_a_stranger(self):
        self.assertEqual(self.client.get('/creator/' + 'x' * 24 + '/').status_code, 404)

    def test_saving_a_slogan_comes_back_to_that_tab(self):
        res = self.client.post(self.url, {'slogan': 'Έλα στη Neat'})

        self.assertEqual(res.status_code, 302)
        self.assertTrue(res['Location'].endswith('?tab=custom'))
        self.ambassador.refresh_from_db()
        self.assertEqual(self.ambassador.custom_slogan, 'Έλα στη Neat')

    def test_the_requested_tab_is_the_one_left_open(self):
        """Saving a slogan comes back with ?tab=custom, and landing on the
        first tab instead is how you lose sight of what you just did."""
        body = self.client.get(self.url, {'tab': 'custom'}).content.decode()

        self.assertIn('aria-selected="true">Δικό σου', body)
        self.assertIn('data-pane="basic" hidden', body)
        self.assertNotIn('data-pane="custom" hidden', body)

    def test_logging_a_post_keeps_what_was_typed_when_it_is_refused(self):
        res = self.client.post(self.url, {
            'action': 'content',
            'platform': AmbassadorContent.PLATFORMS[0][0],
            'city': APP_CITIES[0],
            'url': 'not-a-url',
            'uploaded_at': '',
        })

        self.assertEqual(res.status_code, 200)
        self.assertFalse(AmbassadorContent.objects.exists())
        self.assertIn('class="err"', res.content.decode())

    def test_money_reads_the_same_as_the_payout_page(self):
        user = User.objects.create_user('kostas', password='x')
        AmbassadorSignup.objects.create(
            ambassador=self.ambassador, user=user,
            status=AmbassadorSignup.PAID, amount=Decimal('1.50'))

        body = self.client.get(self.url).content.decode()

        self.assertIn('€1.50', body)
        self.assertIn('@kostas', body)


class CountingOpensTests(PublishedLanding, TestCase):
    """What "14 openings" should have said.

    One link shared once recorded fourteen opens in eleven minutes: six
    `facebookexternalhit` fetches, five more from Meta's own addresses wearing
    an iPhone user agent, one datacenter scanner, and two people. A preview
    crawler makes a real request, so nothing about the request itself settles
    it — only running the page's script does, which is what these pin.
    """

    def setUp(self):
        cache.clear()
        self.ambassador = Ambassador.objects.create(name='Βίκυ', code='viki')
        self.url = f'/creator/{self.ambassador.dashboard_key}/'
        self.publish_landing()

    def _opens_shown(self):
        body = self.client.get(self.url).content.decode()
        # The tile reads "<b>N</b><span>Ανοίγματα link</span>".
        return int(re.search(r'<b>(\d+)</b><span>Ανοίγματα link', body).group(1))

    def _open_link(self):
        self.client.get('/a/viki')
        return AmbassadorClick.objects.latest('id')

    def test_a_fetch_alone_is_not_an_opening(self):
        self._open_link()

        self.assertEqual(AmbassadorClick.objects.count(), 1, 'the row is still kept')
        self.assertEqual(self._opens_shown(), 0)

    def test_the_page_saying_a_browser_ran_it_is(self):
        click = self._open_link()

        res = self.client.post('/api/ambassadors/seen/',
                               data=json.dumps({'token': click.token}),
                               content_type='application/json')

        self.assertEqual(res.status_code, 200)
        click.refresh_from_db()
        self.assertTrue(click.confirmed)
        self.assertEqual(self._opens_shown(), 1)

    def test_a_reload_does_not_count_twice(self):
        click = self._open_link()
        for _ in range(3):
            self.client.post('/api/ambassadors/seen/',
                             data=json.dumps({'token': click.token}),
                             content_type='application/json')

        self.assertEqual(self._opens_shown(), 1)

    def test_a_made_up_token_counts_nothing_and_says_nothing(self):
        self._open_link()

        res = self.client.post('/api/ambassadors/seen/',
                               data=json.dumps({'token': 'not-a-real-token'}),
                               content_type='application/json')

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {})
        self.assertEqual(self._opens_shown(), 0)

    def test_the_page_carries_the_beacon(self):
        body = self.client.get('/a/viki').content.decode()
        token = AmbassadorClick.objects.latest('id').token

        self.assertIn('/api/ambassadors/seen/', body)
        self.assertIn(f'JSON.stringify({{token: "{token}"}})', body)

    def test_an_app_asking_for_a_token_is_a_device_already(self):
        res = self.client.post('/api/ambassadors/click/',
                               data=json.dumps({'code': 'viki'}),
                               content_type='application/json')

        self.assertEqual(res.status_code, 200)
        self.assertTrue(AmbassadorClick.objects.latest('id').confirmed)


class CreatorEntryTests(PublishedLanding, TestCase):
    """neatapp.gr/c/<code> — the way in a creator can remember.

    The memorable half is the code already on their poster, so everything
    rests on the six digits: these pin that a wrong one gets nowhere, that a
    right one is not asked for again on the same phone, and that the page
    says the same thing either way about which creators exist.
    """

    def setUp(self):
        cache.clear()
        self.ambassador = Ambassador.objects.create(name='Βίκυ', code='viki')
        self.pin = self.ambassador.dashboard_pin

    def test_the_pin_hands_over_the_dashboard(self):
        res = self.client.post('/c/viki', {'pin': self.pin})

        self.assertEqual(res.status_code, 302)
        self.assertEqual(res['Location'].rstrip('/'),
                         f'/creator/{self.ambassador.dashboard_key}')

    def test_the_wrong_pin_does_not(self):
        wrong = '000000' if self.pin != '000000' else '111111'

        res = self.client.post('/c/viki', {'pin': wrong})

        self.assertEqual(res.status_code, 200)
        self.assertIn('Λάθος κωδικός', res.content.decode())

    def test_the_same_phone_is_not_asked_twice(self):
        self.client.post('/c/viki', {'pin': self.pin})

        res = self.client.get('/c/viki')

        self.assertEqual(res.status_code, 302)
        self.assertEqual(res['Location'].rstrip('/'),
                         f'/creator/{self.ambassador.dashboard_key}')

    def test_a_phone_that_knows_one_code_does_not_know_another(self):
        other = Ambassador.objects.create(name='Άλλος', code='allos')
        self.client.post('/c/viki', {'pin': self.pin})

        res = self.client.get('/c/allos')

        self.assertEqual(res.status_code, 200, 'asked for the other PIN')
        self.assertNotIn(other.dashboard_key, res.content.decode())

    def test_an_unknown_code_looks_exactly_like_a_wrong_pin(self):
        real = self.client.post('/c/viki', {'pin': '999999' if self.pin != '999999' else '888888'})
        fake = self.client.post('/c/nobody-at-all', {'pin': '999999'})

        self.assertEqual(real.status_code, fake.status_code)
        self.assertIn('Λάθος κωδικός', fake.content.decode())

    def test_guessing_is_capped(self):
        wrong = '000000' if self.pin != '000000' else '111111'
        for _ in range(12):
            self.client.post('/c/viki', {'pin': wrong})

        res = self.client.post('/c/viki', {'pin': self.pin})

        self.assertEqual(res.status_code, 200, 'the right PIN is refused too')
        self.assertIn('Πολλές προσπάθειες', res.content.decode())

    def test_an_inactive_ambassador_cannot_be_let_in(self):
        self.ambassador.is_active = False
        self.ambassador.save(update_fields=['is_active'])

        res = self.client.post('/c/viki', {'pin': self.pin})

        self.assertEqual(res.status_code, 200)
        self.assertIn('Λάθος κωδικός', res.content.decode())

    def test_the_dashboard_says_how_to_come_back(self):
        body = self.client.get(f'/creator/{self.ambassador.dashboard_key}/').content.decode()

        self.assertIn('neatapp.gr/c/viki', body)
        self.assertIn(self.pin, body)

    def test_every_ambassador_gets_their_own_pin(self):
        pins = {Ambassador.objects.create(name=f'A{i}', code=f'code{i}').dashboard_pin
                for i in range(12)}

        self.assertGreater(len(pins), 1, 'one shared PIN is the same as none')
        for pin in pins:
            self.assertRegex(pin, r'^\d{6}$')
