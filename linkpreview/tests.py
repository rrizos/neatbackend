import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from accounts.models import AuthToken

from . import oembed
from .fetcher import UnsafeUrl, fetch_preview, parse_metadata
from .models import LinkPreview, url_fingerprint

SAMPLE = {
    'url': 'https://in.gr/news/',
    'title': 'Τίτλος άρθρου',
    'description': 'Μια περιγραφή.',
    'image_url': 'https://in.gr/og.jpg',
    'site_name': 'in.gr',
    'author_name': '',
    'author_handle': '',
    'author_url': '',
    'kind': 'article',
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


class RichContentParsingTests(TestCase):
    """The cases that make a shared TikTok/Instagram look like it does in
    Instagram: the creator's name, the caption, and a video badge."""

    IG_HTML = '''<html><head>
        <meta property="og:type" content="article">
        <meta property="og:site_name" content="Instagram">
        <meta property="og:url" content="https://www.instagram.com/nasa/reel/DbY_N/">
        <meta property="og:title" content="NASA on Instagram: &quot;The Sun and Moon&quot;">
        <meta property="og:image" content="https://scontent.cdninstagram.com/x.jpg">
    </head></html>'''

    def test_instagram_author_comes_from_the_permalink(self):
        d = parse_metadata('https://www.instagram.com/reel/DbY_N/', self.IG_HTML)
        self.assertEqual(d['author_handle'], 'nasa')
        self.assertEqual(d['author_name'], 'NASA')
        self.assertEqual(d['author_url'], 'https://www.instagram.com/nasa/')

    def test_instagram_title_drops_the_author_preamble(self):
        d = parse_metadata('https://www.instagram.com/reel/DbY_N/', self.IG_HTML)
        self.assertEqual(d['title'], 'The Sun and Moon')

    def test_instagram_preamble_is_stripped_in_greek_too(self):
        # We send Accept-Language: el, so Instagram localises the connector.
        html = self.IG_HTML.replace('NASA on Instagram:', 'NASA στο Instagram:')
        d = parse_metadata('https://www.instagram.com/reel/DbY_N/', html)
        self.assertEqual(d['title'], 'The Sun and Moon')
        self.assertEqual(d['author_name'], 'NASA')

    def test_multi_word_author_is_not_truncated(self):
        html = self.IG_HTML.replace('NASA on Instagram:', 'Zach King on Instagram:')
        d = parse_metadata('https://www.instagram.com/zachking/reel/X/', html)
        self.assertEqual(d['author_name'], 'Zach King')

    def test_instagram_reel_is_typed_as_video_despite_og_type_article(self):
        d = parse_metadata('https://www.instagram.com/reel/DbY_N/', self.IG_HTML)
        self.assertEqual(d['kind'], 'video')

    def test_a_plain_site_gets_no_author(self):
        html = ('<html><head><meta property="og:title" content="in.gr">'
                '<meta property="og:type" content="website"></head></html>')
        d = parse_metadata('https://www.in.gr/', html)
        self.assertEqual(d['author_name'], '')
        self.assertEqual(d['kind'], 'website')

    def test_tiktok_author_comes_from_the_path(self):
        html = '<html><head><title>TikTok</title></head></html>'
        d = parse_metadata('https://www.tiktok.com/@zachking/video/123', html)
        self.assertEqual(d['author_handle'], 'zachking')
        self.assertEqual(d['kind'], 'video')


class OembedTests(TestCase):
    def test_endpoint_matches_provider_hosts(self):
        self.assertIn('tiktok.com/oembed',
                      oembed.endpoint_for('https://www.tiktok.com/@a/video/1'))
        self.assertIn('youtube.com/oembed',
                      oembed.endpoint_for('https://youtu.be/abc'))
        self.assertIsNone(oembed.endpoint_for('https://www.in.gr/news/'))

    def test_lookalike_domain_is_not_matched(self):
        # notyoutube.com must not be treated as youtube.com.
        self.assertIsNone(oembed.endpoint_for('https://notyoutube.com/watch?v=1'))

    def test_parse_pulls_creator_and_thumbnail(self):
        payload = json.dumps({
            'type': 'video', 'title': 'caption here',
            'author_name': 'Zach King', 'author_unique_id': 'zachking',
            'author_url': 'https://www.tiktok.com/@zachking',
            'thumbnail_url': 'https://cdn/thumb.jpg', 'provider_name': 'TikTok',
        })
        d = oembed.parse(payload)
        self.assertEqual(d['author_name'], 'Zach King')
        self.assertEqual(d['author_handle'], 'zachking')
        self.assertEqual(d['kind'], 'video')
        self.assertEqual(d['thumbnail_url'], 'https://cdn/thumb.jpg')

    def test_empty_payload_yields_nothing_to_render(self):
        self.assertIsNone(oembed.parse('{"type":"video"}'))
        self.assertIsNone(oembed.parse('not json'))


class MergeTests(TestCase):
    """oEmbed and the page each win the fields they actually know better."""

    PAGE = {
        'url': 'https://www.tiktok.com/@zachking/video/1',
        'title': 'TikTok', 'description': 'desc from page',
        'image_url': '', 'image_width': 0, 'image_height': 0,
        'site_name': 'tiktok.com',
        'author_name': 'zachking', 'author_handle': 'zachking',
        'author_url': '', 'kind': 'video',
    }
    CARD = {
        'title': 'the real caption', 'thumbnail_url': 'https://cdn/t.jpg',
        'image_width': 576, 'image_height': 1090,
        'author_name': 'Zach King', 'author_handle': 'zachking',
        'author_url': 'https://www.tiktok.com/@zachking',
        'site_name': 'TikTok', 'kind': 'video',
    }

    def test_oembed_caption_and_creator_win(self):
        with patch('linkpreview.fetcher.fetch_oembed', return_value=self.CARD), \
             patch('linkpreview.fetcher.fetch_head_html', return_value=('u', '')), \
             patch('linkpreview.fetcher.parse_metadata', return_value=self.PAGE):
            d = fetch_preview('https://www.tiktok.com/@zachking/video/1')
        self.assertEqual(d['title'], 'the real caption')
        self.assertEqual(d['author_name'], 'Zach King')
        self.assertEqual(d['site_name'], 'TikTok')
        # The page still supplies what oEmbed lacks.
        self.assertEqual(d['description'], 'desc from page')
        self.assertEqual(d['image_url'], 'https://cdn/t.jpg')
        # Dimensions must follow the image that won, or a portrait thumbnail
        # gets drawn at the page image's landscape ratio.
        self.assertEqual((d['image_width'], d['image_height']), (576, 1090))

    def test_oembed_alone_still_makes_a_card_when_the_page_fails(self):
        with patch('linkpreview.fetcher.fetch_oembed', return_value=self.CARD), \
             patch('linkpreview.fetcher.fetch_head_html',
                   side_effect=UnsafeUrl('blocked')):
            d = fetch_preview('https://www.tiktok.com/@zachking/video/1')
        self.assertEqual(d['title'], 'the real caption')
        self.assertEqual(d['image_url'], 'https://cdn/t.jpg')

    def test_page_alone_is_used_when_there_is_no_oembed_provider(self):
        with patch('linkpreview.fetcher.fetch_oembed', return_value=None), \
             patch('linkpreview.fetcher.fetch_head_html', return_value=('u', '')), \
             patch('linkpreview.fetcher.parse_metadata', return_value=self.PAGE):
            d = fetch_preview('https://www.in.gr/')
        self.assertEqual(d['title'], 'TikTok')


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
