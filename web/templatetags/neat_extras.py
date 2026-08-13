"""Template helpers for the public post page.

The share page is the one part of Neat rendered outside Flutter, so the
formatting the app does in Dart has to be reproduced here. Greek only — the
app ships el_GR and the page follows it.
"""

from datetime import datetime

from django import template
from django.utils import timezone

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
