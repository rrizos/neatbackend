"""Every city the shipped app draws a pin for.

Copied from lib/src/map/greece_cities.dart, and it has to stay in step with
it, because of how the app decides what is locked:

    final info = locks[cityName];
    if (info != null) return info.isLocked;
    return true;   // no row from the server -> locked

A city the server says nothing about is locked on the phone. That inverts the
obvious kill switch: emptying the API response (which is what
LOCKED_CITIES_ENABLED=False does) would lock *every* city on every installed
copy of the app. Switching the feature off therefore means saying
`locked: false` about all of these, out loud — which is why the list lives
here.
"""

APP_CITIES = [
    "Αθήνα",
    "Θεσσαλονίκη",
    "Πάτρα",
    "Ηράκλειο",
    "Λάρισα",
    "Βόλος",
    "Ιωάννινα",
    "Χανιά",
    "Ρόδος",
    "Καβάλα",
    "Καλαμάτα",
    "Τρίκαλα",
    "Σέρρες",
    "Αγρίνιο",
    "Βέροια",
    "Κατερίνη",
    "Λαμία",
    "Ξάνθη",
    "Δράμα",
    "Κέρκυρα",
    "Χαλκίδα",
    "Πύργος",
    "Τρίπολη",
    "Αλεξανδρούπολη",
    "Κοζάνη",
    "Καστοριά",
    "Φλώρινα",
    "Έδεσσα",
    "Γιαννιτσά",
    "Πτολεμαΐδα",
    "Γρεβενά",
    "Αίγινα",
    "Ναύπλιο",
    "Σπάρτη",
    "Άργος",
    "Κόρινθος",
    "Μέγαρα",
    "Θήβα",
    "Λιβαδειά",
    "Μεσολόγγι",
    "Ναύπακτος",
    "Αμαλιάδα",
    "Αίγιο",
    "Καρδίτσα",
    "Αλμυρός",
    "Καλαμπάκα",
    "Ορεστιάδα",
    "Διδυμότειχο",
    "Κομοτηνή",
    "Σάμος",
    "Χίος",
    "Λέσβος",
    "Κως",
    "Κάλυμνος",
    "Λέρος",
    "Σύρος",
    "Μύκονος",
    "Πάρος",
    "Νάξος",
    "Σαντορίνη",
    "Μήλος",
    "Κάρπαθος",
    "Ζάκυνθος",
    "Κεφαλονιά",
    "Λευκάδα",
    "Πρέβεζα",
    "Άρτα",
    "Πάργα",
    "Νάουσα",
    "Κιλκίς",
    "Πολύκαστρο",
    "Τύρναβος",
    "Ελασσόνα",
    "Φάρσαλα",
    "Πέλλα",
    "Ρέθυμνο",
]
