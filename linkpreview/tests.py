from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from accounts.models import AuthToken

from .fetcher import UnsafeUrl, parse_metadata
from .models import LinkPreview, url_fingerprint

SAMPLE = {
    'url': 'https://in.gr/news/',
    'title': 'Τίτλος άρθρου',
    'description': 'Μια περιγραφή.',
    'image_url': 'https://in.gr/og.jpg',
    'site_name': 'in.gr',
}


class LinkPreviewViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='rafa', password='pw12345!'
        )
        self.token = AuthToken.create_for_user(self.user)
        self.auth = {'HTTP_AUTHORIZATION': f'Token {self.token.key}'}

    def get(self, url, **extra):
        return self.client.get('/api/link-preview/', {'url': url}, **extra)

    def test_requires_authentication(self):
        res = self.get('https://in.gr/news/')
        self.assertEqual(res.status_code, 401)

    def test_returns_card_and_caches_it(self):
        with patch('linkpreview.views.fetch_preview', return_value=SAMPLE) as m:
            res = self.get('https://in.gr/news/', **self.auth)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['preview']['title'], 'Τίτλος άρθρου')
        self.assertEqual(m.call_count, 1)
        self.assertTrue(LinkPreview.objects.filter(ok=True).exists())

    def test_second_request_is_served_from_cache(self):
        with patch('linkpreview.views.fetch_preview', return_value=SAMPLE) as m:
            self.get('https://in.gr/news/', **self.auth)
            self.get('https://in.gr/news/', **self.auth)
        self.assertEqual(m.call_count, 1, 'the second hit must not refetch')

    def test_unsafe_url_yields_null_and_is_negative_cached(self):
        with patch('linkpreview.views.fetch_preview',
                   side_effect=UnsafeUrl('nope')) as m:
            first = self.get('http://169.254.169.254/', **self.auth)
            self.get('http://169.254.169.254/', **self.auth)
        self.assertEqual(first.status_code, 200)
        self.assertIsNone(first.json()['preview'])
        self.assertEqual(m.call_count, 1, 'failures must be cached too')
        row = LinkPreview.objects.get(
            url_hash=url_fingerprint('http://169.254.169.254/'))
        self.assertFalse(row.ok)

    def test_timeout_is_swallowed_into_a_null_preview(self):
        with patch('linkpreview.views.fetch_preview',
                   side_effect=TimeoutError('slow')):
            res = self.get('https://slow.example.com/', **self.auth)
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.json()['preview'])

    def test_page_with_no_metadata_gets_no_card(self):
        bare = {**SAMPLE, 'title': '', 'image_url': ''}
        with patch('linkpreview.views.fetch_preview', return_value=bare):
            res = self.get('https://boring.example.com/', **self.auth)
        self.assertIsNone(res.json()['preview'])

    def test_missing_url_is_rejected(self):
        res = self.client.get('/api/link-preview/', **self.auth)
        self.assertEqual(res.status_code, 400)

    def test_stale_row_is_refetched(self):
        LinkPreview.objects.create(
            url_hash=url_fingerprint('https://in.gr/news/'),
            url='https://in.gr/news/', title='old', ok=True,
            fetched_at=timezone.now() - timezone.timedelta(days=30),
        )
        with patch('linkpreview.views.fetch_preview', return_value=SAMPLE) as m:
            res = self.get('https://in.gr/news/', **self.auth)
        self.assertEqual(m.call_count, 1)
        self.assertEqual(res.json()['preview']['title'], 'Τίτλος άρθρου')


class MetadataParsingTests(TestCase):
    def test_prefers_og_over_bare_title(self):
        html = '''<html><head>
            <title>bare</title>
            <meta property="og:title" content="OG wins">
            <meta property="og:description" content="desc">
            <meta property="og:image" content="/img.png">
            <meta property="og:site_name" content="Site">
        </head></html>'''
        data = parse_metadata('https://example.com/a/b', html)
        self.assertEqual(data['title'], 'OG wins')
        self.assertEqual(data['site_name'], 'Site')
        # Relative og:image must be resolved against the final URL.
        self.assertEqual(data['image_url'], 'https://example.com/img.png')

    def test_falls_back_to_title_tag_and_host(self):
        html = '<html><head><title>Just a title</title></head></html>'
        data = parse_metadata('https://www.in.gr/news/', html)
        self.assertEqual(data['title'], 'Just a title')
        self.assertEqual(data['site_name'], 'in.gr')

    def test_entities_and_whitespace_are_cleaned(self):
        html = ('<html><head><meta property="og:title" '
                'content="A &amp;  B\n  C"></head></html>')
        data = parse_metadata('https://example.com/', html)
        self.assertEqual(data['title'], 'A & B C')

    def test_non_http_image_is_dropped(self):
        html = ('<html><head><meta property="og:title" content="t">'
                '<meta property="og:image" content="data:image/png;base64,AAA">'
                '</head></html>')
        data = parse_metadata('https://example.com/', html)
        self.assertEqual(data['image_url'], '')
