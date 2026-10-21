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
from datetime import timedelta
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


def titles_match(left, right):
    a, b = fingerprint(left), fingerprint(right)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    return SequenceMatcher(None, a, b).ratio() >= TITLE_RATIO


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
        if abs(existing.date - candidate.starts_at) > SAME_TIME_WINDOW:
            continue
        if titles_match(existing.title, candidate.title):
            return existing
    return None
