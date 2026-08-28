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


def _age_minutes(iso_string):
    """Whole minutes since `iso_string`, or None if it cannot be read."""
    if not iso_string:
        return None
    try:
        moment = datetime.fromisoformat(str(iso_string))
    except (TypeError, ValueError):
        return None
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment, timezone.get_default_timezone())
    return max(0, int((timezone.now() - moment).total_seconds() // 60))


@register.filter
def greek_ago(iso_string):
    """"τώρα", "5λ", "3ω", "2εβδ" — postAge() in lib/src/core/post_card.dart.

    Compact, unprefixed and using the same units the feed does, because this
    page sits next to screenshots of the feed.
    """
    minutes = _age_minutes(iso_string)
    if minutes is None:
        return ''
    if minutes < 1:
        return 'τώρα'
    if minutes < 60:
        return f'{minutes}λ'
    if minutes < 1440:
        return f'{minutes // 60}ω'
    if minutes < 10080:
        return f'{minutes // 1440}η'
    if minutes < 43200:
        return f'{minutes // 10080}εβδ'
    if minutes < 525600:
        return f'{minutes // 43200}μήν'
    return f'{minutes // 525600}χρ'


@register.filter
def greek_ago_long(iso_string):
    """"τώρα", "5λ πριν", "3ω πριν", "2η πριν" — _timeAgo() in home_page.dart.

    The comment sheet says it this way rather than the header's bare "5λ".
    """
    minutes = _age_minutes(iso_string)
    if minutes is None:
        return ''
    if minutes < 1:
        return 'τώρα'
    if minutes < 60:
        return f'{minutes}λ πριν'
    if minutes < 1440:
        return f'{minutes // 60}ω πριν'
    return f'{minutes // 1440}η πριν'


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
    """999 · 12.5K · 1.2M — _formatCount() in lib/src/core/post_card.dart.

    The app shows Latin K/M even in Greek, and drops the decimal when it is
    zero ("1K", not "1.0K"), so the page does too.
    """
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return '0'
    for divisor, suffix in ((1_000_000, 'M'), (1000, 'K')):
        if number >= divisor:
            scaled = number / divisor
            whole = f'{scaled:.1f}'.removesuffix('.0')
            return f'{whole}{suffix}'
    return str(number)


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
