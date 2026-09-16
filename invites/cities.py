"""Greek city names in a form that survives a URL.

The app builds invite links with the same transformation, in
lib/src/core/locked_cities.dart (citySlug). The two only have to agree with
each other — no one reads a slug back as Greek and no one types one — so the
scheme being loosely ELOT rather than exactly ELOT costs nothing. Change one
and you must change the other, or the page stops recognising the city and
falls back to the copy that names none.
"""

import re

ACCENTS = {
    'ά': 'α', 'έ': 'ε', 'ή': 'η', 'ί': 'ι', 'ό': 'ο', 'ύ': 'υ', 'ώ': 'ω',
    'ϊ': 'ι', 'ϋ': 'υ', 'ΐ': 'ι', 'ΰ': 'υ',
}
DIGRAPHS = {'ου': 'ou', 'αυ': 'av', 'ευ': 'ev'}
LETTERS = {
    'α': 'a', 'β': 'v', 'γ': 'g', 'δ': 'd', 'ε': 'e', 'ζ': 'z', 'η': 'i',
    'θ': 'th', 'ι': 'i', 'κ': 'k', 'λ': 'l', 'μ': 'm', 'ν': 'n', 'ξ': 'x',
    'ο': 'o', 'π': 'p', 'ρ': 'r', 'σ': 's', 'ς': 's', 'τ': 't', 'υ': 'y',
    'φ': 'f', 'χ': 'ch', 'ψ': 'ps', 'ω': 'o',
}


def city_slug(city):
    # Accents come off first so the digraph pass still sees the "ευ" in
    # Λευκάδα when it arrives written "εύ".
    source = ''.join(ACCENTS.get(ch, ch) for ch in (city or '').lower())

    out = []
    i = 0
    while i < len(source):
        digraph = DIGRAPHS.get(source[i:i + 2])
        if digraph:
            out.append(digraph)
            i += 2
            continue
        ch = source[i]
        if ch in LETTERS:
            out.append(LETTERS[ch])
        elif re.match(r'[a-z0-9]', ch):
            out.append(ch)
        else:
            out.append('-')
        i += 1

    return re.sub(r'-+', '-', ''.join(out)).strip('-')
