"""The rooms that tell you how big a night is.

A ticket seller lists a stadium show and a sixty-seat bar gig the same way, and
neither says how many people will turn up. The venue does: nobody books the
Odeon of Herodes Atticus for something nobody has heard of. So the size and
standing of the room is one of the signals ranking.py weighs, and this is the
list it weighs against.

    3  arenas, stadiums, the big open-air theatres, the national institutions
    2  the established mid-size rooms and cultural centres people know by name
    1  small but well-known rooms, where being on the bill still means something

Matching is on the folded text of an event's `location` (see dedupe.fold), so
capitals and accents do not matter. Aliases are whole words: "ΣΕΦ" does not
match inside some other word. A venue missing here is not penalised, it is only
not helped — the list is a boost for the obvious cases, not a gate.

The second table is for programme pages read off a venue's own site (the `llm`
sources in sources.py): there the page itself says which room it is, even when
the extracted `location` is a hall name like "Αίθουσα Αιμίλιος Ριάδης".
"""

import re
from functools import lru_cache
from urllib.parse import urlsplit

from .dedupe import fold

#: (tier, aliases). Aliases in any case, with or without accents.
VENUES = [
    # ── Αθήνα / Πειραιάς
    (3, ['ΟΑΚΑ', 'Ολυμπιακό Στάδιο', 'Olympic Stadium', 'OAKA']),
    (3, ['Telekom Center', 'Κλειστό ΟΑΚΑ', 'OAKA Indoor']),
    (3, ['ΣΕΦ', 'Στάδιο Ειρήνης και Φιλίας', 'Peace and Friendship']),
    (3, ['Καραϊσκάκη', 'Karaiskakis']),
    (3, ['OPAP Arena', 'Allwyn Arena', 'Αγιά Σοφιά', 'Agia Sophia']),
    (3, ['Ηρώδειο', 'Ωδείο Ηρώδου Αττικού', 'Herodes Atticus', 'Herodion']),
    (3, ['Καλλιμάρμαρο', 'Παναθηναϊκό Στάδιο', 'Panathenaic Stadium']),
    (3, ['Θέατρο Λυκαβηττού', 'Lycabettus Theatre', 'Lycabettus Hill Theater']),
    (3, ['Πλατεία Νερού', 'Plateia Nerou']),
    (3, ['Μέγαρο Μουσικής Αθηνών', 'Megaron', 'Μέγαρο Μουσικής']),
    (3, ['Κέντρο Πολιτισμού Ίδρυμα Σταύρος Νιάρχος', 'ΚΠΙΣΝ', 'SNFCC',
         'Stavros Niarchos', 'Εθνική Λυρική Σκηνή', 'Greek National Opera']),
    (3, ['Release Athens', 'Ejekt Festival', 'Rockwave Festival']),
    (2, ['Στέγη Ιδρύματος Ωνάση', 'Onassis Stegi', 'Onassis Cultural Centre']),
    (2, ['Θέατρο Βράχων', 'Theatro Vrachon']),
    (2, ['Τεχνόπολη', 'Technopolis', 'Γκάζι Τεχνόπολη']),
    (2, ['Gazarte']),
    (2, ['Fuzz Club', 'Fuzz Live Music Club']),
    (2, ['Piraeus 117 Academy', 'Piraeus Academy']),
    (2, ['Gagarin 205', 'Gagarin']),
    (2, ['Christmas Theater', 'Christmas Theatre']),
    (2, ['Θέατρο Παλλάς', 'Pallas Theatre']),
    (2, ['Badminton Theater', 'Θέατρο Badminton']),
    (2, ['Θέατρο Ολύμπια', 'Δημοτικό Μουσικό Θέατρο Μαρία Κάλλας']),
    (2, ['Εθνικό Θέατρο', 'National Theatre of Greece', 'Θέατρο Ρεξ']),
    (2, ['Θέατρο Ακροπόλ', 'Θέατρο Παλλάδιον', 'Θέατρο Κάππα']),
    (2, ['Δημοτικό Θέατρο Πειραιά']),
    (2, ['Floyd Live Music Venue', 'Floyd']),
    (2, ['Μουσείο Μπενάκη', 'Benaki Museum', 'ΕΜΣΤ', 'Εθνική Πινακοθήκη']),
    (1, ['Κύτταρο', 'Kyttaro', 'Six D.O.G.S', 'Six Dogs', 'Half Note', 'Temple Athens',
         'Death Disco', 'An Club', 'Σταυρός του Νότου', 'Stavros tou Notou',
         'Μετρόπολις', 'Theater of the Night', 'Romeo Plus']),

    # ── Θεσσαλονίκη
    (3, ['Τούμπα', 'Toumba', 'Γήπεδο ΠΑΟΚ']),
    (3, ['Κλεάνθης Βικελίδης', 'Vikelidis']),
    (3, ['Καυτατζόγλειο', 'Kaftanzoglio']),
    (3, ['PAOK Sports Arena', 'Παλέ Πυλαίας', 'Παλαί Πυλαίας']),
    (3, ['Αλεξάνδρειο Μέλαθρο', 'Αλεξάνδρειο', 'Nick Galis Hall', 'Νίκος Γκάλης']),
    (3, ['Θέατρο Γης', 'Theatro Gis']),
    (3, ['Θέατρο Δάσους', 'Theatro Dasous', 'Forest Theatre']),
    (3, ['Μέγαρο Μουσικής Θεσσαλονίκης', 'Thessaloniki Concert Hall', 'ΜΜΘ']),
    (3, ['Ολύμπιον', 'Olympion', 'Φεστιβάλ Κινηματογράφου Θεσσαλονίκης']),
    (3, ['Helexpo', 'ΔΕΘ', 'Διεθνής Έκθεση Θεσσαλονίκης']),
    (2, ['Βασιλικό Θέατρο', 'Royal Theatre']),
    (2, ['Κρατικό Θέατρο Βορείου Ελλάδος', 'ΚΘΒΕ', 'Θέατρο Εταιρείας Μακεδονικών Σπουδών',
         'Θέατρο ΕΜΣ']),
    (2, ['Fix Factory of Sound', 'Fix Factory']),
    (2, ['Principal Club Theater', 'Principal Club']),
    (2, ['Μύλος', 'Mylos']),
    (2, ['Block 33']),
    (2, ['Τελλόγλειο', 'Teloglion']),
    (2, ['Αποθήκη Γ', 'Αποθήκη Δ', 'Μουσείο Κινηματογράφου']),
    (1, ['Eightball', 'Υδρόγειος']),

    # ── Other cities
    (3, ['Παμπελοποννησιακό', 'Pampeloponnisiako']),
    (2, ['Δημοτικό Θέατρο Απόλλων', 'Θέατρο Απόλλων']),
    (3, ['Παγκρήτιο Στάδιο', 'Pancretan Stadium']),
    (2, ['Πολιτιστικό Συνεδριακό Κέντρο Ηρακλείου', 'Πολιτιστικό Συνεδριακό Κέντρο']),
    (3, ['AEL FC Arena']),
    (3, ['Πανθεσσαλικό', 'Panthessaliko']),
    (3, ['Θέατρο Επιδαύρου', 'Αρχαίο Θέατρο Επιδαύρου', 'Epidaurus']),
    (2, ['Αρχαίο Θέατρο', 'Ancient Theatre']),
    (2, ['Θέατρο Κήπου', 'Μέγαρο Χορού Καλαμάτας']),
]

#: A venue's own site, for programme pages read off it.
DOMAINS = {
    'megaron.gr': 3,
    'tch.gr': 3,
    'snfcc.org': 3,
    'nationalopera.gr': 3,
    'aefestival.gr': 3,
    'filmfestival.gr': 3,
    'releaseathens.gr': 3,
    'onassis.org': 2,
    'n-t.gr': 2,
    'ntng.gr': 2,
    'technopolis-athens.com': 2,
    'gazarte.gr': 2,
    'fuzzclub.gr': 2,
    'piraeus117academy.gr': 2,
}


@lru_cache(maxsize=1)
def _patterns():
    """One compiled pattern per tier, longest aliases first."""
    by_tier = {}
    for tier, aliases in VENUES:
        by_tier.setdefault(tier, []).extend(fold(a) for a in aliases)
    return {
        tier: re.compile(
            r'(?<!\w)(' + '|'.join(re.escape(a) for a in sorted(set(al), key=len, reverse=True))
            + r')(?!\w)')
        for tier, al in by_tier.items()
    }


def _domain_tier(url):
    host = (urlsplit(url or '').hostname or '').lower()
    for domain, tier in DOMAINS.items():
        if host == domain or host.endswith('.' + domain):
            return tier
    return 0


def tier_for(location='', source_url=''):
    """0–3: how big a room [location] names, or the venue site [source_url] is."""
    text = fold(location)
    best = 0
    if text:
        for tier, pattern in _patterns().items():
            if tier > best and pattern.search(text):
                best = tier
    return max(best, _domain_tier(source_url))
