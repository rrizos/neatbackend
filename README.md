# neatbackend

## Deploying

The app talks to this over plain JSON views (no DRF). After pulling code that
touches a model, **run migrations before restarting gunicorn** — the DM views
select every `Message` column, so serving new code against an un-migrated
database takes messaging down entirely rather than degrading:

```sh
python manage.py migrate
```

`_ensure_messages_tables()` in `dm_messages/views.py` creates *missing tables*
on the fly, which is why the DM app survived a fresh database without a
migrate. It does not add *columns*, so it is no safety net here.

## What a conversation costs to open

Photos and voice notes are base64 inside `Message.text`, so a thread's weight
follows its media, not its message count. Three things keep that off the
critical path, and all three are covered by `dm_messages/tests.py`:

- **Threads are paged.** `GET /api/messages/<id>/` returns the newest 40 with
  `has_more`; older pages come from `?before=<message id>`.
- **Media is fetched on demand.** Clients that send `X-Neat-Client: 2` get the
  message without its payload (`text: "__neat_image__:"`, plus `media: true`)
  and pull the bytes from
  `GET /api/messages/<conv>/messages/<id>/media/` when a bubble is drawn or a
  voice note played. That response is the one here that may be cached, since a
  message's media never changes. Anything without the header is an older build
  and still gets the base64 inline.
- **The database never ships bytes it doesn't need to.** `_load_thread_window`
  defers `text` and reads back only a prefix-sized head and tail, and the
  inbox's preview is a `SUBSTR` — so listing a thread or the inbox no longer
  drags every photo across the connection to MySQL.

Measured on a synthetic thread of 400 messages and 25 photos: opening it went
from 7.34 MB to 0.01 MB.

## Temporary photos ("view once" / "allow replay")

`Message.photo_mode` is `''` for everything ordinary, or `once`/`replay` for a
photo the sender chose to make temporary (`dm_messages/models.py`). The rule
the feature stands on is that the picture lives in exactly one place:

- The thread and the inbox never carry the bytes — `_message_to_dict` sends
  `text: ''` for a message with a mode, and the inbox sends a bare
  `__neat_image__:` so the client can still say "sent a photo".
- `POST /api/messages/<conversation>/messages/<message>/open/` is the only
  route to them. It spends one viewing, and on the last allowed one it clears
  `Message.text`, so the bytes stop existing rather than merely being hidden.
- **Each participant has their own viewings**, the sender included: `once` is
  one each, `replay` two each, tracked in `MessageOpen` rather than a single
  counter on the message. One side spending theirs leaves the other's
  untouched — a shared counter greyed the photo out for the sender the moment
  the recipient opened it. The bytes are cleared only once *nobody* has a
  viewing left.
- Because "how many viewings are left" is now a different answer per person,
  updates to a temporary photo go out with `_push_message`, one payload per
  member, instead of a single broadcast. Each payload also carries
  `opened_by_other`, which is what the sender's "Opened" line reports.

`dm_messages/tests.py` covers all of it:

```sh
python manage.py test dm_messages
```

Clients older than this feature ignore `photo_mode` and simply show an empty
bubble where a temporary photo is — they never see the picture, and nothing
they send can create one.
