"""Have we got this one already?

The same concert reaches us from the venue's own page, from a ticket seller
and, sooner or later, from a person who posts it in the app. Three copies of
one evening is worse than none: it reads as a broken app rather than a busy
city.

Two questions, in order. Did *this source* give us this event before — an
exact answer, from the id the source itself uses. If not, is this the same
evening as something we already hold — a judgement, made narrow on purpose:
same city, starting within the hour, and a title that matches once the noise
is taken out of it.
"""

import re
import unicodedata
from datetime import time, timedelta
from difflib import SequenceMatcher

#: Two events in one city starting this far apart are not the same event.
#: Sources disagree about door time versus start time, which is the gap this
#: has to absorb; a second show the same evening is usually further off.
SAME_TIME_WINDOW = timedelta(minutes=90)

#: How alike two titles must read. Tuned against the way the same event is
#: written by a venue and by a ticket seller — "Sold out" suffixes, a support
#: act appended, the same name with and without the venue in it.
TITLE_RATIO = 0.86

#: Words that say nothing about which event this is.
_NOISE = re.compile(
    r'\b(live|show|concert|συναυλία|παράσταση|εκδήλωση|sold\s*out|'
    r'εξαντλήθηκε|tickets?|εισιτήρια|official|presents|feat\.?|ft\.?)\b',
    re.IGNORECASE,
)


def fingerprint(title):
    """A title with the noise taken out, for comparing.

    Accents are left alone: in Greek they are part of the word, and dropping
    them collides names that are genuinely different.
    """
    text = (title or '').casefold()
    text = _NOISE.sub(' ', text)
    text = re.sub(r'[^\w\s]', ' ', text, flags=re.UNICODE)
    return re.sub(r'\s+', ' ', text).strip()


def fold(text):
    """[text] without case or accents, for comparing and nothing else.

    Ticket sellers write "ΛΕΥΤΕΡΗΣ ΠΑΝΤΑΖΗΣ", the press writes "Λευτέρης
    Πανταζής", and Greek capitals carry no accents. Only ever used next to a
    narrower test (same city, same evening), where a collision between two
    genuinely different names is far less likely than a missed duplicate.
    """
    decomposed = unicodedata.normalize('NFD', (text or '').casefold())
    return ''.join(ch for ch in decomposed if unicodedata.category(ch) != 'Mn')


_FOLDED_NOISE = re.compile(
    r'\b(live|show|concert|συναυλια|παρασταση|εκδηλωση|sold\s*out|'
    r'εξαντληθηκε|tickets?|εισιτηρια|official|presents|feat\.?|ft\.?)\b',
)


def match_key(title):
    """[title] folded and stripped of noise: equal for the same show however
    a source capitalises or accents it."""
    text = _FOLDED_NOISE.sub(' ', fold(title))
    text = re.sub(r'[^\w\s]', ' ', text, flags=re.UNICODE)
    return re.sub(r'\s+', ' ', text).strip()


def _alike(a, b):
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    return SequenceMatcher(None, a, b).ratio() >= TITLE_RATIO


def titles_match(left, right):
    return (_alike(fingerprint(left), fingerprint(right))
            or _alike(match_key(left), match_key(right)))


def has_no_time(when):
    """Whether a stored date is a day with no hour.

    Midnight exactly is what a source that gave only a day turns into (see
    normalise.from_jsonld and the admin's "no time?" warning); a real start
    time has never been seen to land on 00:00:00.000.
    """
    return when is not None and when.time() == time(0, 0)


def same_evening(left, right):
    """Two stored dates that describe one event.

    Within the hour and a half sources disagree by, or the same day when one
    of them only knew the day — a research page often says "Saturday 14th"
    where the ticket seller says 21:00.
    """
    if left is None or right is None:
        return False
    if has_no_time(left) or has_no_time(right):
        return left.date() == right.date()
    return abs(left - right) <= SAME_TIME_WINDOW


def find_existing(candidate, queryset):
    """The event [candidate] is a new copy of, or None.

    [queryset] is every event that could plausibly match — the caller narrows
    it to the city and a date window, so this never walks the table.
    """
    # The exact answer first: this source has given us this id before.
    if candidate.external_id:
        same_id = next(
            (e for e in queryset
             if e.source == candidate.source and e.external_id == candidate.external_id),
            None,
        )
        if same_id is not None:
            return same_id

    for existing in queryset:
        if existing.date is None or existing.city != candidate.city:
            continue
        if not same_evening(existing.date, candidate.starts_at):
            continue
        if titles_match(existing.title, candidate.title):
            return existing
    return None
