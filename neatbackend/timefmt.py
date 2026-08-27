"""How timestamps are written for the app.

Everything is stored in UTC and that does not change. What changes is the
shape on the wire: an instant is sent as **Greek wall-clock with no offset**
rather than as `...+00:00`.

That looks wrong, and the reason is specific. Dart's `DateTime.parse` folds any
offset it is given back into UTC:

    2026-08-23T19:00:00+00:00  ->  hour 19, isUtc true
    2026-08-23T22:00:00+03:00  ->  hour 19, isUtc true   (same instant)
    2026-08-23T22:00:00        ->  hour 22, isUtc false

So a client that reads `.hour` without calling `.toLocal()` shows UTC no matter
which offset is sent — sending `+03:00` fixes nothing. The app now converts at
the parse site, but the version already on people's phones does not, and they
would keep seeing times three hours behind until they updated. The third form is
the only one that is right for both, and `.toLocal()` on an already-local value
is a no-op, so the fixed client stays correct too.

The cost is that these timestamps no longer state their zone, which is a trap
for any future consumer that is not this app — a web client would have to know
the convention. Worth it while the released app cannot be changed; revisit once
nothing old is left in the wild.

Deliberately not used for event dates. Those already carry a wall-clock time
somebody typed rather than an instant, and are stamped UTC on the way in for
exactly that reason — see events/views.py `_parse_event_datetime`.
"""

from django.utils import timezone


def local_iso(dt, empty=''):
    """[dt] as Greek wall-clock, without an offset."""
    if dt is None:
        return empty
    return timezone.localtime(dt).replace(tzinfo=None).isoformat()
