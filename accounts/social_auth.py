"""Verifying the identity tokens Apple and Google hand the app.

The app signs in with the provider itself and sends us the resulting ID token.
That token is the *only* evidence we have, so everything here exists to make it
trustworthy before it is allowed to name a user:

  * **the signature**, checked against the provider's published keys — without
    this anyone could hand us a token they wrote themselves,
  * **the audience**, which must be one of *our* client IDs — a valid Google
    token issued to some other app is still a real Google token, and accepting
    it would let that app's operator sign in as any of its users here,
  * **the issuer and expiry**, so a token from elsewhere or from last month is
    refused.

Keys are fetched from the providers over the network and cached by PyJWT's
`PyJWKClient`; both rotate them, so they are never pinned or vendored.
"""

import hashlib

import jwt
from jwt import PyJWKClient

APPLE_ISSUER = 'https://appleid.apple.com'
APPLE_JWKS_URL = 'https://appleid.apple.com/auth/keys'

# Google has used both spellings for years and still issues both.
GOOGLE_ISSUERS = ('accounts.google.com', 'https://accounts.google.com')
GOOGLE_JWKS_URL = 'https://www.googleapis.com/oauth2/v3/certs'

#: How long a fetched key set is reused. Short enough to pick up a rotation
#: without a deploy, long enough that sign-in is not one network round trip
#: slower than it needs to be.
_JWKS_CACHE_KEYS = 16

_clients = {}


class SocialAuthError(Exception):
    """The token could not be trusted. The message is safe to show a user."""


def _jwk_client(url):
    client = _clients.get(url)
    if client is None:
        client = PyJWKClient(url, cache_keys=True, max_cached_keys=_JWKS_CACHE_KEYS)
        _clients[url] = client
    return client


def _decode(id_token, *, jwks_url, issuer, audiences):
    if not id_token:
        raise SocialAuthError('Missing identity token.')
    if not audiences:
        # Refusing here rather than skipping the check: an unset client ID is a
        # deployment mistake, and the failure mode of carrying on would be
        # accepting tokens minted for anybody's app.
        raise SocialAuthError('This sign-in method is not configured yet.')
    try:
        key = _jwk_client(jwks_url).get_signing_key_from_jwt(id_token).key
        return jwt.decode(
            id_token,
            key,
            algorithms=['RS256', 'ES256'],
            audience=list(audiences),
            issuer=issuer,
            options={'require': ['exp', 'iss', 'sub']},
        )
    except jwt.ExpiredSignatureError:
        raise SocialAuthError('That sign-in has expired. Please try again.')
    except jwt.InvalidAudienceError:
        raise SocialAuthError('That sign-in was not issued for this app.')
    except jwt.PyJWTError as exc:
        raise SocialAuthError('That sign-in could not be verified.') from exc
    except Exception as exc:  # network failure reaching the provider's keys
        raise SocialAuthError('Could not reach the sign-in provider.') from exc


def verify_apple(id_token, audiences, raw_nonce=''):
    """Claims from a verified Apple identity token.

    Apple sends the user's name only on the very first authorisation and never
    inside the token, so `name` is always absent here — the client passes it
    separately, and it is treated as a hint rather than as fact.

    [raw_nonce] is the value the client chose for this one attempt; Apple only
    ever saw its SHA-256. Checking it here is what stops a token captured
    somewhere else from being replayed against this endpoint, since that token
    is bound to a nonce belonging to a different attempt.
    """
    claims = _decode(
        id_token,
        jwks_url=APPLE_JWKS_URL,
        issuer=APPLE_ISSUER,
        audiences=audiences,
    )
    token_nonce = claims.get('nonce') or ''
    if raw_nonce:
        expected = hashlib.sha256(raw_nonce.encode()).hexdigest()
        if token_nonce != expected:
            raise SocialAuthError('That sign-in could not be verified.')
    elif token_nonce:
        # The token was bound to a nonce the client did not send us, so we
        # cannot prove this attempt is the one it was minted for.
        raise SocialAuthError('That sign-in could not be verified.')
    return {
        'provider': 'apple',
        'subject': claims['sub'],
        'email': (claims.get('email') or '').strip().lower(),
        # Apple sends this as the string "true"/"false" as often as a bool.
        'email_verified': str(claims.get('email_verified', '')).lower() == 'true',
        'name': '',
    }


def verify_google(id_token, audiences, raw_nonce=''):  # noqa: ARG001
    claims = _decode(
        id_token,
        jwks_url=GOOGLE_JWKS_URL,
        issuer=list(GOOGLE_ISSUERS),
        audiences=audiences,
    )
    return {
        'provider': 'google',
        'subject': claims['sub'],
        'email': (claims.get('email') or '').strip().lower(),
        'email_verified': str(claims.get('email_verified', '')).lower() == 'true',
        'name': (claims.get('name') or '').strip(),
    }


VERIFIERS = {'apple': verify_apple, 'google': verify_google}
