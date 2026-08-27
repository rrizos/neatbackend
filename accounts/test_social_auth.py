"""What must never be accepted as proof of identity.

These sign their own tokens with a throwaway key and hand the verifier that
key, so nothing here talks to Apple or Google. The point is not that a good
token works — it is that each individual check actually refuses when it should,
because every one of them is the only thing standing between a stranger and
somebody else's account.
"""

import hashlib
import time

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import TestCase

from accounts import social_auth

def _pem(key):
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PRIVATE_PEM = _pem(_KEY)


class _FakeJWKClient:
    """Stands in for the provider's key server, handing back our own key."""

    def __init__(self, key):
        self._key = key

    def get_signing_key_from_jwt(self, _token):
        class _Signing:
            key = self._key
        return _Signing()


def _token(**overrides):
    claims = {
        'iss': social_auth.APPLE_ISSUER,
        'aud': 'NeatApp.Neat',
        'sub': 'apple-subject-123',
        'email': 'someone@example.com',
        'email_verified': 'true',
        'exp': int(time.time()) + 600,
        'iat': int(time.time()),
    }
    claims.update(overrides)
    return jwt.encode(claims, _PRIVATE_PEM, algorithm='RS256')


class AppleTokenTests(TestCase):
    def setUp(self):
        social_auth._clients[social_auth.APPLE_JWKS_URL] = _FakeJWKClient(
            _KEY.public_key()
        )
        self.addCleanup(social_auth._clients.clear)

    def test_a_well_formed_token_is_accepted(self):
        claims = social_auth.verify_apple(_token(), ('NeatApp.Neat',))
        self.assertEqual(claims['subject'], 'apple-subject-123')
        self.assertEqual(claims['email'], 'someone@example.com')
        self.assertTrue(claims['email_verified'])

    def test_a_token_for_another_app_is_refused(self):
        """The whole point of checking the audience.

        This token is genuinely signed by the provider and completely valid —
        it was simply issued to somebody else's app. Accepting it would let
        that app's operator sign in here as any of its users.
        """
        with self.assertRaises(social_auth.SocialAuthError):
            social_auth.verify_apple(_token(aud='com.someone.else'),
                                     ('NeatApp.Neat',))

    def test_an_expired_token_is_refused(self):
        with self.assertRaises(social_auth.SocialAuthError):
            social_auth.verify_apple(_token(exp=int(time.time()) - 60),
                                     ('NeatApp.Neat',))

    def test_a_token_from_the_wrong_issuer_is_refused(self):
        with self.assertRaises(social_auth.SocialAuthError):
            social_auth.verify_apple(_token(iss='https://evil.example'),
                                     ('NeatApp.Neat',))

    def test_a_token_signed_by_someone_else_is_refused(self):
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        forged = jwt.encode(
            {
                'iss': social_auth.APPLE_ISSUER, 'aud': 'NeatApp.Neat',
                'sub': 'apple-subject-123', 'exp': int(time.time()) + 600,
            },
            _pem(other), algorithm='RS256',
        )
        with self.assertRaises(social_auth.SocialAuthError):
            social_auth.verify_apple(forged, ('NeatApp.Neat',))

    def test_no_configured_audience_refuses_rather_than_skipping_the_check(self):
        """An unset client id must fail closed.

        Treating "nothing configured" as "no audience to check" would accept
        tokens minted for any app at all — the exact thing the check exists to
        prevent — so it has to be an error, not a shrug.
        """
        with self.assertRaises(social_auth.SocialAuthError):
            social_auth.verify_apple(_token(), ())

    # ── Nonce ───────────────────────────────────────────────────────────────

    def test_the_matching_nonce_is_accepted(self):
        raw = 'the-nonce-this-attempt-chose'
        digest = hashlib.sha256(raw.encode()).hexdigest()
        claims = social_auth.verify_apple(
            _token(nonce=digest), ('NeatApp.Neat',), raw
        )
        self.assertEqual(claims['subject'], 'apple-subject-123')

    def test_a_token_bound_to_a_different_nonce_is_refused(self):
        """Replay protection: a token captured from another sign-in attempt
        carries that attempt's nonce, not this one's."""
        digest = hashlib.sha256(b'a-different-attempt').hexdigest()
        with self.assertRaises(social_auth.SocialAuthError):
            social_auth.verify_apple(
                _token(nonce=digest), ('NeatApp.Neat',), 'the-nonce-this-attempt-chose'
            )

    def test_a_nonce_bound_token_is_refused_when_the_client_sends_none(self):
        digest = hashlib.sha256(b'something').hexdigest()
        with self.assertRaises(social_auth.SocialAuthError):
            social_auth.verify_apple(_token(nonce=digest), ('NeatApp.Neat',))


class GoogleTokenTests(TestCase):
    def setUp(self):
        social_auth._clients[social_auth.GOOGLE_JWKS_URL] = _FakeJWKClient(
            _KEY.public_key()
        )
        self.addCleanup(social_auth._clients.clear)

    def _google(self, **overrides):
        claims = {
            'iss': 'https://accounts.google.com',
            'aud': 'web-client-id.apps.googleusercontent.com',
            'sub': 'google-subject-456',
            'email': 'someone@gmail.com',
            'email_verified': True,
            'name': 'Some One',
            'exp': int(time.time()) + 600,
        }
        claims.update(overrides)
        return jwt.encode(claims, _PRIVATE_PEM, algorithm='RS256')

    def test_both_issuer_spellings_are_accepted(self):
        for iss in social_auth.GOOGLE_ISSUERS:
            claims = social_auth.verify_google(
                self._google(iss=iss),
                ('web-client-id.apps.googleusercontent.com',),
            )
            self.assertEqual(claims['subject'], 'google-subject-456')

    def test_a_token_for_another_project_is_refused(self):
        with self.assertRaises(social_auth.SocialAuthError):
            social_auth.verify_google(
                self._google(aud='someone-else.apps.googleusercontent.com'),
                ('web-client-id.apps.googleusercontent.com',),
            )

    def test_the_boolean_email_verified_google_sends_is_understood(self):
        claims = social_auth.verify_google(
            self._google(email_verified=True),
            ('web-client-id.apps.googleusercontent.com',),
        )
        self.assertTrue(claims['email_verified'])
