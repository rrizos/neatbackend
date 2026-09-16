"""What an invitation did after it left the app.

One table, three kinds of row, because the three are the same event seen at
different distances and the dashboard reads them together:

* ``sent``   — somebody copied their link. The only one of the three the
               inviter performs, and the only one that can be counted exactly.
* ``opened`` — somebody landed on the invite page. Deduplicated per visitor
               per day (see ``visitor``), so a friend who taps the link three
               times in a chat is one person, not three.
* ``joined`` — an account was created off the back of one. Attribution is
               explicit, not inferred: the app reports it, and ``joined_user``
               is unique so a second report cannot credit the same signup
               twice or credit it to a second inviter.

There is deliberately no unique constraint on the ``opened`` shape. A partial
index is what that would want, MySQL has none, and an unconditional one would
also collapse every ``sent`` row for a day into one — which is the number the
inviter most wants to see move. Deduplication happens in
``record_open``; a race that slips two rows through costs a slightly generous
open count, not a wrong one.
"""

import hashlib

from django.conf import settings
from django.db import models
from django.utils import timezone


class InviteEvent(models.Model):
    SENT = 'sent'
    OPENED = 'opened'
    JOINED = 'joined'
    KINDS = [(SENT, 'sent'), (OPENED, 'opened'), (JOINED, 'joined')]

    kind = models.CharField(max_length=8, choices=KINDS, db_index=True)
    inviter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='invite_events',
    )
    #: The city the invitation was about — the one on the card it was sent
    #: from, which is not always the city the inviter lives in.
    city = models.CharField(max_length=120, blank=True)
    #: Opaque per-visitor value for `opened` rows; see `visitor_hash`. Empty
    #: for the other two kinds.
    visitor = models.CharField(max_length=64, blank=True, db_index=True)
    #: Single-use handle for an `opened` row, and the only thing a claim may
    #: present. It leaves on the Play URL and on the clipboard, exactly as the
    #: ambassador one does — the mechanism is the same because the problem is:
    #: no app store tells an app which link installed it, and nobody taps a
    #: link twice. Null rather than blank so MySQL's unique index tolerates
    #: the rows that have none (`sent`, `joined`).
    token = models.CharField(max_length=64, null=True, blank=True,
                             unique=True, default=None)
    #: Salted hash of the address an `opened` row came from, so a signup with
    #: no token at all can still be paired with a click from the same network.
    ip_hash = models.CharField(max_length=64, blank=True, default='', db_index=True)
    used_at = models.DateTimeField(null=True, blank=True)
    #: Which day's deduplication bucket an `opened` row belongs to.
    day = models.DateField(default=timezone.localdate, db_index=True)
    #: The account created by a `joined` row. Unique, so one signup is one
    #: person's credit, forever.
    joined_user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name='invite_join',
    )
    created = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [models.Index(fields=['inviter', 'kind'])]

    def __str__(self):
        return f'{self.kind} · {self.inviter_id} · {self.city or "—"}'


def visitor_hash(ip, user_agent):
    """A stable, non-reversible handle for one visitor-ish.

    IP plus user agent, hashed with the site's secret so the table cannot be
    walked back to an address. It is not an identity — a household behind one
    NAT on the same phone model counts once — and it is not meant to be: it
    exists only to stop one person re-reading a page from looking like a
    crowd.
    """
    material = f'{ip}|{user_agent}|{settings.SECRET_KEY}'.encode('utf-8', 'ignore')
    return hashlib.sha256(material).hexdigest()[:64]
