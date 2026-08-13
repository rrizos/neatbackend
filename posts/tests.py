import json

from django.contrib.auth import get_user_model
from django.test import TestCase

from accounts.models import AuthToken, Profile

from .models import Post, PostComment

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
        self.make_posts(20, comments_each=2)
        with self.assertNumQueries(self.baseline_queries()):
            self.feed()
        # Twice the posts, same number of queries.
        self.make_posts(20, comments_each=2)
        with self.assertNumQueries(self.baseline_queries()):
            self.feed()

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
