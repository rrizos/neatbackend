import json

from django.contrib.auth import get_user_model
from django.test import TestCase

from accounts.models import AuthToken, Profile

from .models import Conversation, ConversationMember, Message

User = get_user_model()

PHOTO = '__neat_image__:aGVsbG8='  # "hello", enough to be a distinct payload


class TemporaryPhotoTests(TestCase):
    """The "view once" / "allow replay" path.

    The point of these is the one property the feature stands on: the bytes
    live in exactly one place — the open endpoint's response — and every other
    route to them (the thread, the inbox, a second open) comes back empty.
    """

    def setUp(self):
        self.sender = User.objects.create_user('sender', password='x')
        self.recipient = User.objects.create_user('recipient', password='x')
        for user in (self.sender, self.recipient):
            Profile.objects.update_or_create(user=user, defaults={'city': 'Αθήνα'})
        self.sender_token = AuthToken.create_for_user(self.sender).key
        self.recipient_token = AuthToken.create_for_user(self.recipient).key

        self.conversation = Conversation.objects.create()
        for user in (self.sender, self.recipient):
            ConversationMember.objects.create(conversation=self.conversation, user=user)

    def auth(self, token):
        return {'HTTP_AUTHORIZATION': f'Token {token}'}

    def send(self, token, text=PHOTO, photo_mode=None):
        body = {'text': text}
        if photo_mode is not None:
            body['photo_mode'] = photo_mode
        response = self.client.post(
            f'/api/messages/{self.conversation.id}/',
            data=json.dumps(body),
            content_type='application/json',
            **self.auth(token),
        )
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def thread(self, token):
        response = self.client.get(
            f'/api/messages/{self.conversation.id}/', **self.auth(token)
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()['messages']

    def open(self, token, message_id):
        return self.client.post(
            f'/api/messages/{self.conversation.id}/messages/{message_id}/open/',
            content_type='application/json',
            **self.auth(token),
        )

    # ── sending ──────────────────────────────────────────────────────────────

    def test_ordinary_photo_is_unaffected(self):
        sent = self.send(self.sender_token)
        self.assertEqual(sent['text'], PHOTO)
        self.assertNotIn('photo_mode', sent)
        self.assertEqual(self.thread(self.recipient_token)[0]['text'], PHOTO)

    def test_mode_is_ignored_on_a_text_message(self):
        sent = self.send(self.sender_token, text='γεια', photo_mode='once')
        self.assertNotIn('photo_mode', sent)
        self.assertEqual(sent['text'], 'γεια')

    def test_unknown_mode_is_ignored(self):
        sent = self.send(self.sender_token, photo_mode='forever')
        self.assertNotIn('photo_mode', sent)

    # ── the thread never carries the picture ─────────────────────────────────

    def test_temporary_photo_is_withheld_from_the_thread(self):
        self.send(self.sender_token, photo_mode='once')
        for token in (self.sender_token, self.recipient_token):
            message = self.thread(token)[0]
            self.assertEqual(message['text'], '')
            self.assertEqual(message['photo_mode'], 'once')
            # Both sides start with their own viewing.
            self.assertEqual(message['opens_left'], 1)
            self.assertFalse(message['opened_by_other'])

    def test_inbox_shows_a_photo_but_not_its_bytes(self):
        self.send(self.sender_token, photo_mode='once')
        response = self.client.get('/api/messages/inbox/', **self.auth(self.recipient_token))
        self.assertEqual(response.status_code, 200, response.content)
        summary = response.json()['conversations'][0]
        self.assertEqual(summary['lastMessage'], '__neat_image__:')

    # ── opening ──────────────────────────────────────────────────────────────

    def test_view_once_can_be_opened_exactly_once_per_person(self):
        sent = self.send(self.sender_token, photo_mode='once')

        first = self.open(self.recipient_token, sent['id'])
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(first.json()['photo'], PHOTO)
        self.assertEqual(first.json()['message']['opens_left'], 0)

        second = self.open(self.recipient_token, sent['id'])
        self.assertEqual(second.status_code, 410)

        # The sender still has their own viewing, and the picture is still
        # there for it.
        self.assertEqual(Message.objects.get(pk=sent['id']).text, PHOTO)
        senders = self.open(self.sender_token, sent['id'])
        self.assertEqual(senders.status_code, 200, senders.content)
        self.assertEqual(senders.json()['photo'], PHOTO)

        # Now nobody has one left, so the bytes are gone from the row.
        self.assertEqual(Message.objects.get(pk=sent['id']).text, '')
        self.assertEqual(self.open(self.sender_token, sent['id']).status_code, 410)

    def test_one_persons_viewings_do_not_touch_the_others(self):
        """The bug this replaced: the recipient opening greyed it for the sender."""
        sent = self.send(self.sender_token, photo_mode='replay')
        self.open(self.recipient_token, sent['id'])

        sender_view = self.thread(self.sender_token)[0]
        recipient_view = self.thread(self.recipient_token)[0]
        self.assertEqual(sender_view['opens_left'], 2, 'sender lost a viewing they never spent')
        self.assertEqual(recipient_view['opens_left'], 1)
        # And each side is told what the *other* has done, which is what
        # "Opened" reports on a sent photo.
        self.assertTrue(sender_view['opened_by_other'])
        self.assertFalse(recipient_view['opened_by_other'])

    def test_allow_replay_can_be_opened_twice(self):
        sent = self.send(self.sender_token, photo_mode='replay')

        first = self.open(self.recipient_token, sent['id'])
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(first.json()['photo'], PHOTO)
        self.assertEqual(first.json()['message']['opens_left'], 1)
        # Still there for the replay.
        self.assertEqual(Message.objects.get(pk=sent['id']).text, PHOTO)

        second = self.open(self.recipient_token, sent['id'])
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(second.json()['photo'], PHOTO)

        third = self.open(self.recipient_token, sent['id'])
        self.assertEqual(third.status_code, 410)
        # The sender's two are untouched by the recipient spending theirs.
        self.assertEqual(Message.objects.get(pk=sent['id']).text, PHOTO)
        self.assertEqual(self.open(self.sender_token, sent['id']).status_code, 200)
        self.assertEqual(self.open(self.sender_token, sent['id']).status_code, 200)
        self.assertEqual(self.open(self.sender_token, sent['id']).status_code, 410)
        self.assertEqual(Message.objects.get(pk=sent['id']).text, '')

    def test_sender_opening_does_not_spend_the_recipients_viewing(self):
        sent = self.send(self.sender_token, photo_mode='once')

        self.assertEqual(self.open(self.sender_token, sent['id']).status_code, 200)
        self.assertEqual(self.open(self.sender_token, sent['id']).status_code, 410)

        # The recipient's viewing is still theirs to spend.
        recipients = self.open(self.recipient_token, sent['id'])
        self.assertEqual(recipients.status_code, 200, recipients.content)
        self.assertEqual(recipients.json()['photo'], PHOTO)

    def test_outsider_cannot_open(self):
        sent = self.send(self.sender_token, photo_mode='once')
        outsider = User.objects.create_user('outsider', password='x')
        Profile.objects.update_or_create(user=outsider, defaults={'city': 'Αθήνα'})
        token = AuthToken.create_for_user(outsider).key

        self.assertEqual(self.open(token, sent['id']).status_code, 404)
        self.assertEqual(Message.objects.get(pk=sent['id']).opens, 0)

    def test_opening_an_ordinary_photo_is_refused(self):
        sent = self.send(self.sender_token)
        self.assertEqual(self.open(self.recipient_token, sent['id']).status_code, 400)


class ThreadPayloadTests(TestCase):
    """What a conversation actually costs to open.

    A thread's photos and voice notes live as base64 inside `Message.text`, so
    the whole history used to be downloaded — several megabytes of it — before
    the first bubble could be drawn. These pin the two things that fixed it:
    the response is a page, and it carries no media.
    """

    def setUp(self):
        self.a = User.objects.create_user('a', password='x')
        self.b = User.objects.create_user('b', password='x')
        for user in (self.a, self.b):
            Profile.objects.update_or_create(user=user, defaults={'city': 'Αθήνα'})
        self.token = AuthToken.create_for_user(self.b).key
        self.conversation = Conversation.objects.create()
        for user in (self.a, self.b):
            ConversationMember.objects.create(conversation=self.conversation, user=user)

    def auth(self, lean=True):
        headers = {'HTTP_AUTHORIZATION': f'Token {self.token}'}
        if lean:
            headers['HTTP_X_NEAT_CLIENT'] = '2'
        return headers

    def get(self, query='', lean=True):
        response = self.client.get(
            f'/api/messages/{self.conversation.id}/{query}', **self.auth(lean)
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def bulk(self, count, text=lambda i: f'μήνυμα {i}'):
        Message.objects.bulk_create(
            [Message(conversation=self.conversation, sender=self.a, text=text(i))
             for i in range(count)]
        )

    def test_thread_returns_a_page_not_the_history(self):
        self.bulk(120)
        body = self.get()
        self.assertEqual(len(body['messages']), 40)
        self.assertTrue(body['has_more'])
        # The newest ones, in reading order.
        self.assertEqual(body['messages'][-1]['text'], 'μήνυμα 119')
        self.assertEqual(body['messages'][0]['text'], 'μήνυμα 80')

    def test_older_pages_are_reachable(self):
        self.bulk(120)
        first = self.get()
        older = self.get(f'?before={first["messages"][0]["id"]}')
        self.assertEqual(len(older['messages']), 40)
        self.assertEqual(older['messages'][-1]['text'], 'μήνυμα 79')
        oldest = self.get(f'?before={older["messages"][0]["id"]}')
        self.assertEqual(oldest['messages'][0]['text'], 'μήνυμα 0')
        self.assertFalse(oldest['has_more'])

    def test_media_is_withheld_and_fetched_on_demand(self):
        Message.objects.create(conversation=self.conversation, sender=self.a, text=PHOTO)
        message = self.get()['messages'][0]
        self.assertEqual(message['text'], '__neat_image__:')
        self.assertTrue(message['media'])

        response = self.client.get(
            f'/api/messages/{self.conversation.id}/messages/{message["id"]}/media/',
            **self.auth(),
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['text'], PHOTO)

    def test_voice_keeps_its_duration_without_its_bytes(self):
        Message.objects.create(
            conversation=self.conversation,
            sender=self.a,
            text='__neat_voice__:aGVsbG8=|17',
        )
        message = self.get()['messages'][0]
        self.assertEqual(message['text'], '__neat_voice__:|17')

    def test_older_clients_still_get_their_media_inline(self):
        Message.objects.create(conversation=self.conversation, sender=self.a, text=PHOTO)
        message = self.get(lean=False)['messages'][0]
        self.assertEqual(message['text'], PHOTO)
        self.assertNotIn('media', message)

    def test_inbox_carries_no_base64(self):
        Message.objects.create(conversation=self.conversation, sender=self.a, text=PHOTO)
        response = self.client.get('/api/messages/inbox/', **self.auth())
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['conversations'][0]['lastMessage'], '__neat_image__:')

    def test_temporary_photo_is_not_served_by_the_media_route(self):
        sent = Message.objects.create(
            conversation=self.conversation, sender=self.a, text=PHOTO, photo_mode='once'
        )
        response = self.client.get(
            f'/api/messages/{self.conversation.id}/messages/{sent.id}/media/', **self.auth()
        )
        self.assertEqual(response.status_code, 403)

    def test_opening_a_thread_does_not_scale_with_its_media(self):
        # 30 photos, of which only the newest page could possibly be needed —
        # and none of their bytes should cross the wire or leave the database.
        self.bulk(30, text=lambda i: PHOTO)
        with self.assertNumQueries(self.thread_query_count()):
            response = self.client.get(f'/api/messages/{self.conversation.id}/', **self.auth())
        self.assertNotIn(b'aGVsbG8', response.content)

    def thread_query_count(self):
        # Pinned loosely: the point is that it is a constant, not a function of
        # how many messages or photos the conversation holds.
        response = self.client.get(f'/api/messages/{self.conversation.id}/', **self.auth())
        self.assertEqual(response.status_code, 200)
        from django.test.utils import CaptureQueriesContext
        from django.db import connection
        with CaptureQueriesContext(connection) as ctx:
            self.client.get(f'/api/messages/{self.conversation.id}/', **self.auth())
        return len(ctx)
