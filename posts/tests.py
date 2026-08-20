import json

from django.contrib.auth import get_user_model
from django.test import TestCase

from accounts.models import AuthToken, Profile

from .management.commands.transcode_worker import MAX_ATTEMPTS
from .management.commands.transcode_worker import Command as TranscodeCommand
from .models import Post, PostComment, PostMedia, StagedUpload
from .views import _post_to_dict

User = get_user_model()

AVATAR = 'data:image/jpeg;base64,' + 'A' * 2000  # a small avatar, roughly
CITY = 'Αθήνα'


class FeedPayloadTests(TestCase):
    """What the city feed costs to load.

    Every post used to arrive with its whole comment thread and a copy of the
    author's base64 avatar — the same picture repeated once per post and once
    per comment. These pin the three things that fixed it, and the fact that
    older builds still get exactly what they got before.
    """

    def setUp(self):
        self.author = User.objects.create_user('author', password='x')
        Profile.objects.update_or_create(
            user=self.author, defaults={'city': CITY, 'avatar_url': AVATAR}
        )
        self.viewer = User.objects.create_user('viewer', password='x')
        Profile.objects.update_or_create(user=self.viewer, defaults={'city': CITY})
        self.token = AuthToken.create_for_user(self.viewer).key

    def auth(self, lean=True):
        headers = {'HTTP_AUTHORIZATION': f'Token {self.token}'}
        if lean:
            headers['HTTP_X_NEAT_CLIENT'] = '2'
        return headers

    def feed(self, query='', lean=True):
        response = self.client.get(f'/api/posts/{query}', **self.auth(lean))
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def make_posts(self, count, comments_each=0):
        made = []
        for i in range(count):
            post = Post.objects.create(user=self.author, city=CITY, text=f'κείμενο {i}')
            for n in range(comments_each):
                PostComment.objects.create(post=post, user=self.author, text=f'σχόλιο {n}')
            made.append(post)
        return made

    def test_feed_is_paged(self):
        self.make_posts(25)
        body = self.feed()
        self.assertEqual(len(body['posts']), 20)
        self.assertTrue(body['has_more'])

        older = self.feed(f'?before={body["posts"][-1]["id"]}')
        self.assertEqual(len(older['posts']), 5)
        self.assertFalse(older['has_more'])

    def test_paging_holds_when_ids_and_dates_disagree(self):
        """The shape the imported WordPress posts actually have.

        Those rows were created later — so they carry high ids — while their
        `created` timestamps are old. The feed is ordered by date, so paging on
        the id returned a second page that overlapped the first and skipped
        whatever fell between. Every post must appear exactly once.
        """
        from datetime import timedelta

        from django.utils import timezone

        now = timezone.now()
        made = self.make_posts(30)
        # Newest ids, oldest dates: the import, in miniature.
        for offset, post in enumerate(made):
            stamp = now - timedelta(days=offset if post.id % 2 else 100 + offset)
            Post.objects.filter(pk=post.id).update(created=stamp)

        seen = []
        body = self.feed()
        while True:
            seen.extend(p['id'] for p in body['posts'])
            if not body['has_more']:
                break
            body = self.feed(f'?before={body["posts"][-1]["id"]}')

        self.assertEqual(len(seen), len(set(seen)), 'a post was served twice')
        self.assertEqual(set(seen), {p.id for p in made}, 'a post was skipped')

    def test_each_avatar_is_sent_once(self):
        self.make_posts(5)
        body = self.feed()
        self.assertEqual(body['avatars'], {'author': AVATAR})
        for post in body['posts']:
            self.assertEqual(post['avatarUrl'], '')
        # The avatar appears exactly once in the whole response.
        self.assertEqual(json.dumps(body).count(AVATAR), 1)

    def test_comments_are_left_to_the_comment_sheet(self):
        self.make_posts(1, comments_each=3)
        post = self.feed()['posts'][0]
        self.assertEqual(post['comments'], [])
        self.assertEqual(post['comment_count'], 3)

    def test_feed_does_not_query_per_post(self):
        """Query count must not grow with how many posts are on the page.

        The previous version of this compared a 20-post page against another
        20-post page — the feed pages at 20, so both sides were the same size
        and it passed no matter how many queries each row cost. Varying the
        page size is what actually exercises it.
        """
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self.make_posts(20, comments_each=2)

        def queries_for(limit):
            self.feed(f'?limit={limit}')  # warm any lazy setup
            with CaptureQueriesContext(connection) as ctx:
                body = self.feed(f'?limit={limit}')
            self.assertEqual(len(body['posts']), limit)
            return len(ctx)

        small = queries_for(4)
        large = queries_for(20)
        self.assertEqual(
            small, large,
            f'{large - small} extra queries for 16 extra posts — the feed is '
            f'querying per row ({small} for 4 posts, {large} for 20)',
        )

    def baseline_queries(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as ctx:
            self.feed()
        return len(ctx)

    def test_older_clients_get_the_old_shape(self):
        self.make_posts(25, comments_each=2)
        body = self.feed(lean=False)
        # A bare list, everything in it, comments and avatars inline.
        self.assertIsInstance(body, list)
        self.assertEqual(len(body), 25)
        self.assertEqual(body[0]['avatarUrl'], AVATAR)
        self.assertEqual(len(body[0]['comments']), 2)


class TranscodeQueueTests(TestCase):
    """The video queue, which exists so an encode never holds a request slot.

    Encoding used to run inside the POST, so an upload occupied one of
    gunicorn's request threads for the whole encode and enough simultaneous
    uploads could occupy all of them. These pin the properties that make
    deferring it safe: the row is queued rather than encoded, the original
    stays playable in the meantime, a claim cannot be taken twice, and a video
    that ffmpeg will never accept ends up served rather than lost.
    """

    def setUp(self):
        self.author = User.objects.create_user('poster', password='x')
        Profile.objects.update_or_create(user=self.author, defaults={'city': CITY})
        self.post = Post.objects.create(user=self.author, city=CITY, text='ένα βίντεο')

    def video(self, **kwargs):
        defaults = {
            'post': self.post,
            'media_type': 'video',
            'url': '/media/posts/original.mp4',
            'status': PostMedia.PENDING,
        }
        defaults.update(kwargs)
        return PostMedia.objects.create(**defaults)

    def test_queued_video_is_reported_as_processing(self):
        self.video()
        data = _post_to_dict(self.post)
        self.assertEqual(data['media'][0]['status'], 'processing')
        # And still carries a URL, so a client that ignores `status` plays the
        # original rather than showing a hole.
        self.assertEqual(data['media'][0]['url'], '/media/posts/original.mp4')

    def test_finished_and_failed_video_both_read_as_ready(self):
        """A failed transcode must not look different to a reader.

        The original is still there and still plays; only the queue cares that
        ffmpeg refused it.
        """
        for status in (PostMedia.READY, PostMedia.FAILED):
            PostMedia.objects.all().delete()
            self.video(status=status)
            data = _post_to_dict(self.post)
            self.assertEqual(data['media'][0]['status'], 'ready', status)

    def test_images_are_never_queued(self):
        PostMedia.objects.create(
            post=self.post, media_type='image', url='/media/posts/a.jpg',
        )
        self.assertEqual(PostMedia.objects.filter(status=PostMedia.PENDING).count(), 0)

    def test_a_job_can_only_be_claimed_once(self):
        media = self.video()
        command = TranscodeCommand()
        first = command._claim()
        second = command._claim()
        self.assertEqual(first.pk, media.pk)
        self.assertIsNone(second, 'the same video was handed out twice')
        self.assertEqual(first.attempts, 1)

    def test_failure_retries_then_retires_still_serving_the_original(self):
        media = self.video()
        command = TranscodeCommand()
        command._running = False  # no back-off sleeping inside the test

        for attempt in range(1, MAX_ATTEMPTS):
            claimed = command._claim()
            self.assertIsNotNone(claimed, f'not retried on attempt {attempt}')
            command._fail(claimed)
            self.assertEqual(
                PostMedia.objects.get(pk=media.pk).status, PostMedia.PENDING
            )

        # The last allowed attempt retires it.
        claimed = command._claim()
        command._fail(claimed)
        media.refresh_from_db()
        self.assertEqual(media.status, PostMedia.FAILED)
        self.assertEqual(media.url, '/media/posts/original.mp4')
        self.assertIsNone(command._claim(), 'a retired video was queued again')


class SharedPostExistenceTests(TestCase):
    """A post shared into a chat is a snapshot, so the chat cannot tell on its
    own that the original has since been deleted — it kept drawing a perfect
    card for something that was gone, and tapping it was the only way to find
    out. This is the batched lookup that lets a thread label them.
    """

    def setUp(self):
        self.user = User.objects.create_user('sharer', password='x')
        Profile.objects.update_or_create(user=self.user, defaults={'city': CITY})
        self.token = AuthToken.create_for_user(self.user).key
        self.alive = Post.objects.create(user=self.user, city=CITY, text='ζωντανή')
        self.dead = Post.objects.create(user=self.user, city=CITY, text='διαγραμμένη')
        self.dead_id = self.dead.id
        self.dead.delete()

    def get(self, ids, auth=True):
        headers = {'HTTP_AUTHORIZATION': f'Token {self.token}'} if auth else {}
        return self.client.get(f'/api/posts/exist/?ids={ids}', **headers)

    def test_reports_only_the_deleted_one(self):
        res = self.get(f'{self.alive.id},{self.dead_id}')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['missing'], [self.dead_id])

    def test_cost_does_not_grow_with_the_number_of_posts_asked_about(self):
        """One lookup for the whole thread, not one per card.

        Asserted as "40 ids cost the same as 4" rather than an absolute count,
        so unrelated middleware queries don't make this brittle.
        """
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        def queries_for(ids):
            with CaptureQueriesContext(connection) as ctx:
                self.get(','.join(str(i) for i in ids))
            return len(ctx)

        few = queries_for([self.alive.id, self.dead_id, 999001, 999002])
        many = queries_for([self.alive.id, self.dead_id] + list(range(999000, 999038)))
        self.assertEqual(few, many)

        res = self.get(f'{self.alive.id},{self.dead_id},999001,999002')
        self.assertEqual(res.json()['missing'], sorted([self.dead_id, 999001, 999002]))

    def test_rubbish_ids_are_ignored_rather_than_fatal(self):
        res = self.get(f'abc,,{self.alive.id},-5,99 9')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['missing'], [])

    def test_requires_authentication(self):
        self.assertEqual(self.get(str(self.alive.id), auth=False).status_code, 401)

    def test_empty_query_is_not_an_error(self):
        res = self.client.get('/api/posts/exist/',
                              HTTP_AUTHORIZATION=f'Token {self.token}')
        self.assertEqual(res.json()['missing'], [])


class StagedUploadTests(TestCase):
    """Uploading while the caption is still being written.

    Composing takes time and the network used to sit idle through all of it,
    because the upload only started when Post was pressed. Staging the file on
    pick means an 11 MB video is usually already up by the time somebody
    finishes typing — which is the difference between posting feeling instant
    and feeling like a minute of waiting.
    """

    def setUp(self):
        self.user = User.objects.create_user('stager', password='x')
        Profile.objects.update_or_create(user=self.user, defaults={'city': CITY})
        self.token = AuthToken.create_for_user(self.user).key

    def _auth(self):
        return {'HTTP_AUTHORIZATION': f'Token {self.token}', 'HTTP_X_NEAT_CLIENT': '3'}

    def _jpeg(self):
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new('RGB', (200, 200), (90, 30, 140)).save(buf, 'JPEG')
        return buf.getvalue()

    def _stage(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        return self.client.post(
            '/api/posts/upload/',
            data={'file': SimpleUploadedFile('p.jpg', self._jpeg(), 'image/jpeg'),
                  'type': 'image'},
            **self._auth(),
        )

    def test_a_file_can_be_uploaded_before_the_post_exists(self):
        res = self._stage()
        self.assertEqual(res.status_code, 201, res.content)
        self.assertIn('id', res.json())
        self.assertTrue(res.json()['url'].startswith('/media/posts/'))

    def test_the_post_then_carries_no_bytes_at_all(self):
        upload_id = self._stage().json()['id']
        res = self.client.post(
            '/api/posts/',
            data={'text': 'δες', 'media': json.dumps([
                {'type': 'image', 'upload_id': upload_id, 'order': 0}])},
            **self._auth(),
        )
        self.assertEqual(res.status_code, 201, res.content)
        media = res.json()['media']
        self.assertEqual(len(media), 1)
        self.assertTrue(media[0]['url'].startswith('/media/posts/'))
        # Claimed, so it cannot be attached to a second post.
        self.assertEqual(StagedUpload.objects.count(), 0)

    def test_another_user_cannot_claim_your_upload(self):
        upload_id = self._stage().json()['id']
        other = User.objects.create_user('thief', password='x')
        Profile.objects.update_or_create(user=other, defaults={'city': CITY})
        token = AuthToken.create_for_user(other).key
        res = self.client.post(
            '/api/posts/',
            data={'text': 'mine now', 'media': json.dumps([
                {'type': 'image', 'upload_id': upload_id, 'order': 0}])},
            HTTP_AUTHORIZATION=f'Token {token}', HTTP_X_NEAT_CLIENT='3',
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(StagedUpload.objects.count(), 1, 'it must still be there')

    def test_an_abandoned_upload_is_swept_up(self):
        from django.core.management import call_command
        from django.utils import timezone
        self._stage()
        StagedUpload.objects.update(created=timezone.now() - timezone.timedelta(days=2))
        call_command('purge_staged_uploads', hours=24)
        self.assertEqual(StagedUpload.objects.count(), 0)
