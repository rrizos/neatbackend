"""Template helpers for the public post page.

The share page is the one part of Neat rendered outside Flutter, so the
formatting the app does in Dart has to be reproduced here. Greek only — the
app ships el_GR and the page follows it.
"""

import re
from datetime import datetime

from django import template
from django.utils import timezone
from django.utils.html import escape
from django.utils.safestring import mark_safe

register = template.Library()

_GREEK_MONTHS = [
    'Ιαν', 'Φεβ', 'Μαρ', 'Απρ', 'Μαΐ', 'Ιουν',
    'Ιουλ', 'Αυγ', 'Σεπ', 'Οκτ', 'Νοε', 'Δεκ',
]


@register.filter
def greek_ago(iso_string):
    """"πριν 5 λ.", "πριν 3 ώ.", "5 Αυγ 2026" — matching the app's feed."""
    if not iso_string:
        return ''
    try:
        moment = datetime.fromisoformat(str(iso_string))
    except (TypeError, ValueError):
        return ''
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment, timezone.get_default_timezone())

    seconds = (timezone.now() - moment).total_seconds()
    if seconds < 60:
        return 'μόλις τώρα'
    minutes = int(seconds // 60)
    if minutes < 60:
        return f'πριν {minutes} λ.'
    hours = minutes // 60
    if hours < 24:
        return f'πριν {hours} ώ.'
    days = hours // 24
    if days < 7:
        return f'πριν {days} ημ.'
    return f'{moment.day} {_GREEK_MONTHS[moment.month - 1]} {moment.year}'


@register.filter
def greek_count(value, forms):
    """Pluralise a count: {{ n|greek_count:"σχόλιο,σχόλια" }}."""
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        number = 0
    singular, _, plural = forms.partition(',')
    return f'{number} {singular if number == 1 else plural}'


@register.filter
def poll_percent(option, poll):
    """An option's share of the vote, for the results bar."""
    try:
        total = sum(o.get('votes', 0) for o in poll.get('options', []))
        if not total:
            return 0
        return round(option.get('votes', 0) * 100 / total)
    except (AttributeError, TypeError, ZeroDivisionError):
        return 0


def _grouped(number):
    """1.204 — Greek groups thousands with a full stop."""
    return f'{number:,}'.replace(',', '.')


@register.filter
def neat_count(value):
    """999 · 12,5 χιλ. · 1,2 εκ. — the app's own way of shrinking a number."""
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return '0'
    if number < 1000:
        return str(number)
    if number < 1_000_000:
        scaled = number / 1000
        unit = 'χιλ.'
    else:
        scaled = number / 1_000_000
        unit = 'εκ.'
    # A decimal while it still says something ("12,5 χιλ."), none once the
    # number is big enough that it does not ("125 χιλ."). The comma is the
    # Greek decimal mark.
    text = f'{scaled:.1f}'.replace('.', ',') if scaled < 100 else str(int(scaled))
    return f'{text.removesuffix(",0")} {unit}'


# What the app makes tappable inside a caption. Matched in one pass so a
# fragment (#) inside a URL can never be mistaken for a hashtag.
_TOKENS = re.compile(
    r'(?P<url>https?://[^\s<>"\']+)'
    r'|(?P<mention>(?<![\w@])@\w[\w.]{1,29})'
    r'|(?P<tag>(?<![\w#])\#\w{1,40})'
)


def _short_url(url):
    """"neatapp.gr/post/12" — a link the eye can read, like the app shows it."""
    trimmed = re.sub(r'^https?://(www\.)?', '', url).rstrip('/')
    return trimmed if len(trimmed) <= 42 else trimmed[:41] + '…'


@register.filter
def neat_rich(text):
    """Escape a caption, then light up its links, @mentions and #hashtags.

    Mentions and tags are spans, not anchors: there is no public web profile to
    send anyone to, so they open the app instead (see the tap handler).
    """
    if not text:
        return ''
    text = str(text)
    out = []
    cursor = 0
    for match in _TOKENS.finditer(text):
        out.append(escape(text[cursor:match.start()]))
        raw = match.group(0)
        if match.lastgroup == 'url':
            out.append(
                f'<a class="ln" href="{escape(raw)}" rel="nofollow noopener ugc" '
                f'target="_blank">{escape(_short_url(raw))}</a>'
            )
        else:
            out.append(f'<span class="tok" data-gate="1">{escape(raw)}</span>')
        cursor = match.end()
    out.append(escape(text[cursor:]))
    return mark_safe(''.join(out))


@register.filter
def poll_total(poll):
    """"1.204 ψήφοι" under the bars — the same line the app puts there."""
    try:
        total = sum(option.get('votes', 0) for option in poll.get('options', []))
    except (AttributeError, TypeError):
        return ''
    return '1 ψήφος' if total == 1 else f'{_grouped(total)} ψήφοι'
