"""Paid referrals.

This is the invites table's stricter sibling, and the difference is money.
An invite is a friend telling a friend: if the count is a little generous,
nobody is harmed. An ambassador is paid per person they bring, which makes
every number here a claim on the bank account and every shortcut a way to
drain it. So:

* Nothing is credited on the app's say-so. The app can only present a token
  the server itself minted and recorded, and each token is single-use.
* Nothing becomes payable automatically. A signup has to clear a quality bar
  (city chosen, something posted, still using the app on day three) *and* be
  approved by hand, and the two are separate gates on purpose.
* Anything that looks off is flagged rather than silently dropped or silently
  counted, because both of those are ways to be wrong without anyone noticing.

The honest limit, stated here because it decides how much the rest is worth:
neither platform tells an app which link led to its installation, so
attribution rests on the new phone asking the server for a token and handing
it back after signing up. A determined ambassador with a pile of SIM cards can
still manufacture signups. The quality bar and the approval step are what stand
between that and a payout — not the token.
"""

import hashlib
import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone


def new_code(name=''):
    """A link code that reads like a person and cannot be guessed.

    The readable half is courtesy — an ambassador sees their own name in the
    link they post. The random half is the security: six url-safe characters
    from `secrets`, so codes cannot be enumerated by trying neighbours of one
    that is public.
    """
    stem = ''.join(ch for ch in (name or '').lower() if ch.isalnum())[:12]
    suffix = secrets.token_urlsafe(6).replace('-', '').replace('_', '')[:6]
    return f'{stem}-{suffix}' if stem else suffix


def visitor_hash(ip, user_agent):
    """A stable, non-reversible handle for one device-ish.

    Salted with the site secret so the table cannot be walked back to an IP.
    Used to notice the same phone being credited twice, not to identify anyone.
    """
    material = f'{ip}|{user_agent}|{settings.SECRET_KEY}'.encode('utf-8', 'ignore')
    return hashlib.sha256(material).hexdigest()[:64]


def ip_hash(ip):
    material = f'ip|{ip}|{settings.SECRET_KEY}'.encode('utf-8', 'ignore')
    return hashlib.sha256(material).hexdigest()[:64]


class Ambassador(models.Model):
    """Someone paid to bring people in."""

    name = models.CharField(max_length=120)
    #: The public half of their link: neatapp.gr/a/<code>.
    code = models.CharField(max_length=64, unique=True, db_index=True)
    #: However you reach them to pay them — handle, email, phone. Free text
    #: because it is for a human, not for the system.
    contact = models.CharField(max_length=200, blank=True, default='')
    note = models.TextField(blank=True, default='')

    #: Their own Neat account, when they have one. Recorded so the server can
    #: refuse to credit them for signing themselves up.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True, on_delete=models.SET_NULL,
        related_name='ambassador_profiles',
    )

    #: What one approved signup is worth. Decimal, never float: this is money.
    payout_per_signup = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    #: Signups past this many in a rolling day are flagged rather than counted.
    #: Not a punishment — a rate this high is either a hit or a script, and
    #: both are worth a human look before they are worth a payment.
    daily_cap = models.IntegerField(default=25)

    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True, on_delete=models.SET_NULL,
        related_name='ambassadors_created',
    )
    created = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.name} ({self.code})'

    @property
    def link(self):
        return f'https://neatapp.gr/a/{self.code}'


class AmbassadorClick(models.Model):
    """One recorded opening of an ambassador's link.

    `WEB` rows are someone reading the landing page. `APP` rows are an
    installed app asking for a token to claim with, and are the only kind a
    signup can be credited against — a claim must reference something the
    server minted, so that the app can never simply assert a code.
    """

    WEB = 'web'
    APP = 'app'
    SOURCES = [(WEB, 'web'), (APP, 'app')]

    ambassador = models.ForeignKey(
        Ambassador, on_delete=models.CASCADE, related_name='clicks',
    )
    source = models.CharField(max_length=4, choices=SOURCES, default=WEB, db_index=True)
    #: Opaque, single-use, and the only thing a claim may present.
    token = models.CharField(max_length=64, unique=True, db_index=True)
    visitor = models.CharField(max_length=64, blank=True, default='', db_index=True)
    ip = models.CharField(max_length=64, blank=True, default='')
    user_agent = models.CharField(max_length=300, blank=True, default='')
    created = models.DateTimeField(auto_now_add=True, db_index=True)
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=['ambassador', 'source', '-created'])]

    #: A token older than this is not evidence of anything. Long enough for
    #: someone to install the app the next day and finish signing up at the
    #: weekend; short enough that a stockpile of tokens goes stale.
    MAX_AGE = timezone.timedelta(days=14)

    @property
    def expired(self):
        return timezone.now() - self.created > self.MAX_AGE


class AmbassadorSignup(models.Model):
    """One account credited to one ambassador. At most once, ever.

    The status ladder is deliberately not automatic all the way to the end:

        pending    a real claim, not yet good enough to pay for
        qualified  cleared the quality bar, waiting on a human
        approved   a human said yes; this is what is owed
        paid       settled
        flagged    something looked wrong; read `flags` before touching it
        rejected   a human said no
    """

    PENDING = 'pending'
    QUALIFIED = 'qualified'
    APPROVED = 'approved'
    PAID = 'paid'
    FLAGGED = 'flagged'
    REJECTED = 'rejected'
    STATUSES = [
        (PENDING, 'pending'), (QUALIFIED, 'qualified'), (APPROVED, 'approved'),
        (PAID, 'paid'), (FLAGGED, 'flagged'), (REJECTED, 'rejected'),
    ]
    #: The statuses a human has settled. Re-qualification leaves these alone.
    FINAL = {APPROVED, PAID, REJECTED}

    ambassador = models.ForeignKey(
        Ambassador, on_delete=models.CASCADE, related_name='signups',
    )
    click = models.OneToOneField(
        AmbassadorClick, on_delete=models.PROTECT, related_name='signup',
    )
    #: One credit per account for all time, enforced by the database rather
    #: than by whichever view happens to run.
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='ambassador_signup',
    )
    status = models.CharField(max_length=10, choices=STATUSES, default=PENDING, db_index=True)

    #: How this credit reached us, because the four are not equally believable
    #: and a reviewer should not have to guess which one they are looking at:
    #:
    #:   token     the app followed the link after installing and exchanged
    #:             the code for a token. The strongest, and the rarest,
    #:             because it needs a second tap nobody makes.
    #:   referrer  Google Play handed the app the referrer from the store URL
    #:             on first launch. Exact, and needs nothing from the user.
    #:   clipboard the download button left a token on the pasteboard and the
    #:             app read it back on first launch. iOS only, because iOS has
    #:             no install referrer at all.
    #:   network   nobody handed us anything: the server paired a new account
    #:             with a recent click from the same address. A lead, not
    #:             proof, which is why these arrive flagged.
    TOKEN = 'token'
    REFERRER = 'referrer'
    CLIPBOARD = 'clipboard'
    NETWORK = 'network'
    METHODS = [(TOKEN, 'token'), (REFERRER, 'referrer'),
               (CLIPBOARD, 'clipboard'), (NETWORK, 'network')]
    claim_method = models.CharField(max_length=10, choices=METHODS, default=TOKEN)
    #: Why this was flagged, one short reason per line. Empty when clean.
    flags = models.TextField(blank=True, default='')

    #: The quality bar, stored as it was measured so the dashboard does not
    #: have to re-derive it for every row on every load.
    has_city = models.BooleanField(default=False)
    post_count = models.IntegerField(default=0)
    #: Likes, comments and follows aimed at other people's accounts.
    interaction_count = models.IntegerField(default=0)
    retained_day3 = models.BooleanField(default=False)
    qualified_at = models.DateTimeField(null=True, blank=True)

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='ambassador_reviews',
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    #: The rate at the moment of approval. Copied rather than read live, so
    #: changing an ambassador's rate cannot rewrite what was already owed.
    amount = models.DecimalField(max_digits=8, decimal_places=2, default=0)

    created = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [models.Index(fields=['ambassador', 'status'])]

    def __str__(self):
        return f'{self.user_id} → {self.ambassador_id} ({self.status})'
