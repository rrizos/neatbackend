"""What a media URL looks like to the app, with and without a CDN.

The rewrite happens when serialising, never when storing: the stored path is
how the transcoder, the image tools and every cleanup command find the file on
disk. A test here is cheap insurance against somebody deciding it would be
tidier to rewrite at the point of upload.
"""

from django.test import TestCase, override_settings

from neatbackend.cdn import cdn


class CdnUrlTests(TestCase):
    def test_without_a_cdn_nothing_changes(self):
        with override_settings(MEDIA_CDN_URL=''):
            self.assertEqual(cdn('/media/posts/a.jpg'), '/media/posts/a.jpg')

    def test_with_a_cdn_media_points_at_the_edge(self):
        with override_settings(MEDIA_CDN_URL='https://d123.cloudfront.net'):
            self.assertEqual(cdn('/media/posts/a.jpg'),
                             'https://d123.cloudfront.net/media/posts/a.jpg')

    def test_a_trailing_slash_does_not_double_up(self):
        with override_settings(MEDIA_CDN_URL='https://d123.cloudfront.net/'):
            self.assertEqual(cdn('/media/posts/a.jpg'),
                             'https://d123.cloudfront.net/media/posts/a.jpg')

    def test_somebody_elses_url_is_left_alone(self):
        """Giphy and friends are not ours to serve."""
        with override_settings(MEDIA_CDN_URL='https://d123.cloudfront.net'):
            remote = 'https://media0.giphy.com/media/x/200w.gif'
            self.assertEqual(cdn(remote), remote)

    def test_an_already_absolute_media_url_is_left_alone(self):
        with override_settings(MEDIA_CDN_URL='https://d123.cloudfront.net'):
            absolute = 'https://neatapp.gr/media/posts/a.jpg'
            self.assertEqual(cdn(absolute), absolute)

    def test_empty_stays_empty(self):
        with override_settings(MEDIA_CDN_URL='https://d123.cloudfront.net'):
            self.assertEqual(cdn(''), '')
            self.assertIsNone(cdn(None))
