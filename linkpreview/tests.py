import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from accounts.models import AuthToken

from . import oembed, service
from .fetcher import (
    UnsafeUrl,
    _validate_url,
    fetch_preview,
    normalise_url,
    parse_metadata,
)
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
        with patch('linkpreview.service.fetch_preview', return_value=SAMPLE) as m:
            res = self.get('https://in.gr/news/', **self.auth)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['preview']['title'], 'Τίτλος άρθρου')
        self.assertEqual(m.call_count, 1)
        self.assertTrue(LinkPreview.objects.filter(ok=True).exists())

    def test_second_request_is_served_from_cache(self):
        with patch('linkpreview.service.fetch_preview', return_value=SAMPLE) as m:
            self.get('https://in.gr/news/', **self.auth)
            self.get('https://in.gr/news/', **self.auth)
        self.assertEqual(m.call_count, 1, 'the second hit must not refetch')

    def test_unsafe_url_yields_null_and_is_negative_cached(self):
        with patch('linkpreview.service.fetch_preview',
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
        with patch('linkpreview.service.fetch_preview',
                   side_effect=TimeoutError('slow')):
            res = self.get('https://slow.example.com/', **self.auth)
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.json()['preview'])

    def test_page_with_no_metadata_gets_no_card(self):
        bare = {**SAMPLE, 'title': '', 'image_url': ''}
        with patch('linkpreview.service.fetch_preview', return_value=bare):
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
        with patch('linkpreview.service.fetch_preview', return_value=SAMPLE) as m:
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


class SchemelessUrlTests(TestCase):
    """People type "www.in.gr", not "https://www.in.gr", and the client lifts
    the link out of message text verbatim."""

    def test_bare_host_gains_https(self):
        self.assertEqual(normalise_url('www.in.gr'), 'https://www.in.gr')
        self.assertEqual(normalise_url('in.gr/news'), 'https://in.gr/news')

    def test_an_explicit_scheme_is_left_alone(self):
        self.assertEqual(normalise_url('http://in.gr'), 'http://in.gr')
        self.assertEqual(normalise_url('https://in.gr'), 'https://in.gr')

    def test_protocol_relative_is_not_mangled(self):
        self.assertEqual(normalise_url('//in.gr/x'), 'https://in.gr/x')

    def test_a_colon_in_the_path_is_not_read_as_a_scheme(self):
        self.assertEqual(normalise_url('in.gr/a:b'), 'https://in.gr/a:b')

    def test_scheme_less_urls_pass_validation(self):
        # The bug: these raised UnsafeUrl, so a typed website link showed no
        # card at all while pasted social links (always https://) worked.
        for raw in ('www.in.gr', 'in.gr/news', 'neatapp.gr/post/12'):
            parts, port = _validate_url(raw)
            self.assertEqual(parts.scheme, 'https')
            self.assertEqual(port, 443)

    def test_private_addresses_are_still_blocked_without_a_scheme(self):
        for raw in ('127.0.0.1', '10.0.0.5', '169.254.169.254'):
            with self.assertRaises(UnsafeUrl):
                _validate_url(raw)


class ServiceTests(TestCase):
    """Resolving the link inside post text, for the edge functions that build
    the Open Graph tags and social image for /post/<id>."""

    def test_first_url_matches_the_client_extractor(self):
        self.assertEqual(
            service.first_url('δες https://www.tiktok.com/@a/video/1 τώρα'),
            'https://www.tiktok.com/@a/video/1')
        self.assertEqual(service.first_url('www.in.gr είναι καλό'), 'www.in.gr')
        self.assertEqual(service.first_url('δες in.gr/news.'), 'in.gr/news')

    def test_an_email_is_not_a_link(self):
        self.assertIsNone(service.first_url('γράψε στο someone@example.com'))

    def test_text_without_a_link_needs_no_lookup(self):
        with patch('linkpreview.service.fetch_preview') as m:
            self.assertIsNone(service.preview_for_text('καλημέρα σε όλους'))
        m.assert_not_called()

    def test_resolves_and_caches_the_first_link_in_a_post(self):
        with patch('linkpreview.service.fetch_preview', return_value=SAMPLE) as m:
            first = service.preview_for_text('δες https://in.gr/news/')
            second = service.preview_for_text('δες https://in.gr/news/')
        self.assertEqual(first['title'], 'Τίτλος άρθρου')
        self.assertEqual(second['title'], 'Τίτλος άρθρου')
        self.assertEqual(m.call_count, 1, 'the second read must hit the cache')

    def test_resolve_false_never_fetches(self):
        with patch('linkpreview.service.fetch_preview') as m:
            service.preview_for_text('https://in.gr/news/', resolve=False)
        m.assert_not_called()

    def test_a_failing_link_yields_none_and_is_not_retried(self):
        with patch('linkpreview.service.fetch_preview',
                   side_effect=TimeoutError('slow')) as m:
            self.assertIsNone(service.preview_for_text('https://slow.example.com/'))
            self.assertIsNone(service.preview_for_text('https://slow.example.com/'))
        self.assertEqual(m.call_count, 1)

    def test_scheme_less_link_in_post_text_still_resolves(self):
        with patch('linkpreview.service.fetch_preview', return_value=SAMPLE) as m:
            got = service.preview_for_text('δες www.in.gr σήμερα')
        self.assertIsNotNone(got)
        # The fetcher must be handed an absolute URL, not the bare host.
        self.assertEqual(m.call_args[0][0], 'https://www.in.gr')


class BatchPreviewTests(TestCase):
    """What feeds and inboxes use: known cards ship with the list, and nothing
    in a list is ever allowed to trigger an outbound fetch."""

    def setUp(self):
        LinkPreview.objects.create(
            url_hash=url_fingerprint('https://in.gr/news/'),
            url='https://in.gr/news/', title='Τίτλος', site_name='in.gr',
            image_url='https://in.gr/og.jpg', ok=True,
        )

    def test_returns_the_card_for_a_known_link(self):
        got = service.previews_for_texts(['δες https://in.gr/news/ τώρα'])
        self.assertEqual(got['https://in.gr/news/']['title'], 'Τίτλος')

    def test_matches_a_link_written_without_a_scheme(self):
        got = service.previews_for_texts(['δες in.gr/news/ τώρα'])
        self.assertIn('https://in.gr/news/', got)

    def test_never_fetches(self):
        with patch('linkpreview.service.fetch_preview') as m:
            service.previews_for_texts(['https://unknown.example.com/'])
        m.assert_not_called()

    def test_unknown_links_are_simply_absent(self):
        got = service.previews_for_texts(['https://unknown.example.com/'])
        self.assertEqual(got, {})

    def test_texts_without_links_cost_no_query(self):
        with self.assertNumQueries(0):
            self.assertEqual(service.previews_for_texts(['καλημέρα', '']), {})

    def test_a_whole_page_costs_one_query(self):
        texts = [f'post {i} https://in.gr/news/' for i in range(20)]
        with self.assertNumQueries(1):
            service.previews_for_texts(texts)

    def test_failed_rows_are_not_served_but_stale_ones_are(self):
        """Two different meanings that used to be treated the same.

        `ok=False` means we asked and there is no card — serving it would put an
        empty box under every dead link. Stale means we *have* a card and have
        not re-confirmed it lately, which is no reason to show nothing: titles
        and thumbnails do not change, and hosts that block crawlers can never be
        re-confirmed at all. Excluding both is what made a link show its card
        for a week and then go blank permanently.
        """
        LinkPreview.objects.create(
            url_hash=url_fingerprint('https://dead.example.com/'),
            url='https://dead.example.com/', ok=False,
        )
        LinkPreview.objects.create(
            url_hash=url_fingerprint('https://old.example.com/'),
            url='https://old.example.com/', title='old', ok=True,
            fetched_at=timezone.now() - timezone.timedelta(days=30),
        )
        got = service.previews_for_texts(
            ['https://dead.example.com/', 'https://old.example.com/'])

        self.assertNotIn('https://dead.example.com/', got)
        self.assertIn('https://old.example.com/', got)
        self.assertEqual(got['https://old.example.com/']['title'], 'old')


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

    def test_a_captionless_video_gets_no_title_rather_than_the_app_shell(self):
        # TikTok's page <title> is "TikTok - Make Your Day" whatever the video
        # is. oEmbed is authoritative about the caption, so when it says there
        # isn't one, captioning the video with TikTok's slogan is worse than
        # showing none — the creator and thumbnail carry it instead.
        card = {**self.CARD, 'title': ''}
        page = {**self.PAGE, 'title': 'TikTok - Make Your Day'}
        with patch('linkpreview.fetcher.fetch_oembed', return_value=card), \
             patch('linkpreview.fetcher.fetch_head_html', return_value=('u', '')), \
             patch('linkpreview.fetcher.parse_metadata', return_value=page):
            d = fetch_preview('https://www.tiktok.com/@zachking/video/1')
        self.assertEqual(d['title'], '')
        self.assertEqual(d['author_name'], 'Zach King')
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


class ThumbnailCopyTests(TestCase):
    """A card must not outlive the picture it points at.

    TikTok (and Instagram, and Facebook) sign their CDN image URLs with an
    `x-expires` roughly two days out, while a resolved card is kept for seven.
    Storing their URL therefore produced a card that rendered correctly for two
    days and then, for the remaining five, pointed at a URL returning 403 —
    which the client's errorBuilder collapsed to nothing. That is the
    "thumbnails disappear when I reopen the app" report.
    """

    TIKTOK = 'https://vm.tiktok.com/ZN8dSmPsR/'
    SIGNED = (
        'https://p16-common-sign.tiktokcdn-eu.com/tos/x~tplv-tiktokx-origin.image'
        '?dr=10395&x-expires=1786712400&x-signature=abc123'
    )

    def _resolved(self, image_url):
        return dict(SAMPLE, url=self.TIKTOK, image_url=image_url)

    def test_expiring_url_is_replaced_by_our_own_copy(self):
        with patch.object(service, 'fetch_preview', return_value=self._resolved(self.SIGNED)), \
             patch.object(service, 'store_thumbnail', return_value='/media/linkpreview/abc.jpg') as copy:
            row = service.resolve_and_store(self.TIKTOK)

        copy.assert_called_once_with(self.SIGNED)
        self.assertEqual(row.image_url, '/media/linkpreview/abc.jpg')
        self.assertTrue(row.is_local_image)
        # Nothing signed, and nothing that can expire, survives into the card.
        self.assertNotIn('x-expires', row.to_dict()['image_url'])

    def test_the_client_is_given_an_absolute_url(self):
        # The app loads this with Image.network, which cannot resolve a bare
        # /media/ path. Stored relative, served absolute.
        with patch.object(service, 'fetch_preview', return_value=self._resolved(self.SIGNED)), \
             patch.object(service, 'store_thumbnail', return_value='/media/linkpreview/abc.jpg'):
            row = service.resolve_and_store(self.TIKTOK)

        self.assertTrue(row.to_dict()['image_url'].startswith('https://'))
        self.assertTrue(row.to_dict()['image_url'].endswith('/media/linkpreview/abc.jpg'))

    def test_a_failed_copy_falls_back_to_the_original_url(self):
        """Hotlinking is worse than copying, but far better than no picture."""
        with patch.object(service, 'fetch_preview', return_value=self._resolved(self.SIGNED)), \
             patch.object(service, 'store_thumbnail', return_value=''):
            row = service.resolve_and_store(self.TIKTOK)

        self.assertEqual(row.image_url, self.SIGNED)
        self.assertFalse(row.is_local_image)
        # And it is still handed over unchanged, not prefixed with our host.
        self.assertEqual(row.to_dict()['image_url'], self.SIGNED)

    def test_refreshing_a_card_removes_the_copy_it_replaces(self):
        with patch.object(service, 'fetch_preview', return_value=self._resolved(self.SIGNED)), \
             patch.object(service, 'store_thumbnail', return_value='/media/linkpreview/first.jpg'):
            service.resolve_and_store(self.TIKTOK)

        with patch.object(service, 'fetch_preview', return_value=self._resolved(self.SIGNED)), \
             patch.object(service, 'store_thumbnail', return_value='/media/linkpreview/second.jpg'), \
             patch.object(service, 'discard_thumbnail') as discard:
            row = service.resolve_and_store(self.TIKTOK)

        discard.assert_called_once_with('/media/linkpreview/first.jpg')
        self.assertEqual(row.image_url, '/media/linkpreview/second.jpg')

    def test_a_third_party_url_is_never_deleted_as_if_it_were_ours(self):
        from .thumbnails import discard_thumbnail
        # A no-op that must not raise, and must not try to touch the filesystem.
        discard_thumbnail(self.SIGNED)
        discard_thumbnail('/media/posts/not-a-thumbnail.jpg')
        discard_thumbnail('/media/linkpreview/../../etc/passwd')


class StaleCardTests(TestCase):
    """A card that resolved once should not disappear a week later.

    Cards expire after GOOD_TTL. Past that the feed stopped attaching them and
    the client had to re-resolve live — and Instagram and TikTok refuse a
    crawler, so that re-resolve fails every time. Worse, the failure was
    recorded as `ok=False`, which turned a working card into a dead one
    permanently. Together that is "the preview shows once, then never again
    after I reopen the app".
    """

    URL = 'https://www.instagram.com/reel/DPOYCjWimPS/'

    def _aged_card(self, days, ok=True):
        row = LinkPreview.objects.create(
            url_hash=url_fingerprint(self.URL),
            url=self.URL,
            title='Ένα reel',
            image_url='/media/linkpreview/a.jpg',
            site_name='Instagram',
            ok=ok,
        )
        LinkPreview.objects.filter(pk=row.pk).update(
            fetched_at=timezone.now() - timezone.timedelta(days=days)
        )
        return LinkPreview.objects.get(pk=row.pk)

    def test_a_fortnight_old_card_still_reaches_the_feed(self):
        self._aged_card(days=14)
        found = service.previews_for_texts([f'δες αυτό {self.URL}'])
        self.assertIn(self.URL, found, 'a stale card must still be attached')
        self.assertEqual(found[self.URL]['title'], 'Ένα reel')

    def test_a_failed_refresh_does_not_kill_a_working_card(self):
        row = self._aged_card(days=14)
        self.assertTrue(row.is_stale)

        # The host refuses the crawler, as Instagram does.
        with patch.object(service, 'fetch_preview', side_effect=UnsafeUrl('403')):
            self.assertIsNone(service.resolve_and_store(self.URL))

        row.refresh_from_db()
        self.assertTrue(row.ok, 'a failed refresh marked a good card dead')
        self.assertEqual(row.title, 'Ένα reel')
        # And it is still served.
        self.assertIn(self.URL, service.previews_for_texts([self.URL]))

    def test_a_link_that_never_resolved_is_still_remembered_as_a_failure(self):
        # The negative cache still has to work, or a dead link costs every
        # viewer a timeout.
        with patch.object(service, 'fetch_preview', side_effect=UnsafeUrl('nope')):
            self.assertIsNone(service.resolve_and_store('https://nowhere.invalid/x'))
        row = LinkPreview.objects.get(url='https://nowhere.invalid/x')
        self.assertFalse(row.ok)

    def test_the_endpoint_falls_back_to_the_good_card_when_the_refresh_fails(self):
        """It still tries to refresh — it just never answers "nothing"."""
        self._aged_card(days=14)
        user = get_user_model().objects.create_user('reader', password='x')
        token = AuthToken.create_for_user(user).key
        with patch.object(service, 'fetch_preview',
                          side_effect=UnsafeUrl('403')) as fetch:
            res = self.client.get(
                f'/api/link-preview/?url={self.URL}',
                HTTP_AUTHORIZATION=f'Token {token}',
            )
        fetch.assert_called_once()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['preview']['title'], 'Ένα reel')


class EmptyCardTests(TestCase):
    """A card has to carry something the URL doesn't.

    TikTok answers a request for any video it won't describe — deleted,
    private, or a short link that resolved to an empty username — with its
    boilerplate page title and nothing else. Stored, that rendered as a card
    reading "TikTok - Make Your Day" over a blank space, which reads as broken
    rather than as an ordinary link.
    """

    def _resolved(self, **over):
        base = dict(SAMPLE, title='TikTok - Make Your Day',
                    description='', image_url='')
        base.update(over)
        return base

    def test_a_title_with_nothing_else_is_not_a_card(self):
        with patch.object(service, 'fetch_preview', return_value=self._resolved()):
            self.assertIsNone(service.resolve_and_store('https://vm.tiktok.com/ZNdHHUCt8/'))

    def test_a_title_with_a_picture_is_a_card(self):
        with patch.object(service, 'fetch_preview',
                          return_value=self._resolved(image_url='https://x/y.jpg')), \
             patch.object(service, 'store_thumbnail', return_value='/media/linkpreview/a.jpg'):
            row = service.resolve_and_store('https://vm.tiktok.com/ZN88eejyf/')
        self.assertIsNotNone(row)
        self.assertEqual(row.image_url, '/media/linkpreview/a.jpg')

    def test_a_title_with_a_description_is_a_card(self):
        # An article with no lead image is still worth a card.
        with patch.object(service, 'fetch_preview',
                          return_value=self._resolved(title='Τίτλος',
                                                      description='Μια περίληψη.')):
            row = service.resolve_and_store('https://www.in.gr/news/x/')
        self.assertIsNotNone(row)
        self.assertEqual(row.description, 'Μια περίληψη.')
