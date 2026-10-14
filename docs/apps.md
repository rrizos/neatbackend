# Apps

The server is one Django project, [`neatbackend/`](../neatbackend), and one app
per area of the product. Each section below says what the app is for, what it
stores, how it behaves where that is not obvious, and how it is tested.
Endpoints are listed in the [API reference](api.md).

† marks code on the `referrals-ambassadors-killswitch` branch and ‡ code only on
`main`; both are live in production — see
[Operations → Branches](operations.md#branches-and-production).

- [accounts](#accounts) · [posts](#posts) · [dm_messages](#dm_messages) ·
  [events](#events) · [push](#push) · [linkpreview](#linkpreview) ·
  [security](#security) · [web](#web) · [invites †](#invites-) ·
  [ambassadors †](#ambassadors-) · [lockedcities †](#lockedcities-) ·
  [neatbackend](#neatbackend)

---

## accounts

**Identity, profiles and the social graph.** Email/password, Apple and Google
sign-in; profiles; follows and blocks; people search; activity notifications;
session analytics; and the admin API behind the app's admin panel.

| Model | Holds |
|---|---|
| `Profile` | city, name, bio, avatar (file URLs + a small inline copy for old builds), `username_pending` for social sign-ups, `city_changed_at`, `is_verified`, `is_admin`, `can_create_official_events` |
| `AuthToken` | the API token (random 64 chars), `last_used` |
| `SocialAccount` | Apple / Google subject linked to a user |
| `Follow`, `Block` | the graph; database constraints stop self-follow and self-block |
| `Notification` | actor, verb, target, read state |
| `AppSession` | foreground sessions from the app's heartbeat (a gap of 30 minutes starts a new one) — retention and DAU come from here |
| `PasswordResetCode`, `SearchHistory` | 6-digit reset codes (15 min); the last 8 searches |

Worth knowing:
- **Social sign-in** verifies the provider's token on the server
  ([`social_auth.py`](../accounts/social_auth.py)) and links to an existing
  account by e-mail only when the provider marks it verified. A new social
  account gets a placeholder username and `username_pending`, which the app
  resolves before anything else.
- **Home city** changes are limited to once every 30 days (the first choice is
  free). ‡ A profile's post count, followers and following count only the
  current city, so moving is a clean slate — and moving back restores it.
- **E-mail login** works only when exactly one account has that address,
  because some addresses are shared by several accounts.
- **Rate limits** ([`ratelimit.py`](../accounts/ratelimit.py)) cover sign-up,
  login, social login, password reset and change, and search.

Command: `localise_avatars` (one-off: base64 avatars → files).
Tests: `tests.py`, `test_city_change.py`, `test_email_login.py`,
`test_social_auth.py` (token signature, audience, issuer, expiry, nonce),
`test_social_login.py` (account creation, linking, usernames).

## posts

**Feeds and everything on them.** Posts with up to four photos/videos/GIFs or a
poll; likes, saves, shares; threaded comments with images, likes, one pin;
@mentions (notifying people in the same city); reports; Virals; city heat;
Neat Points; staged uploads and the video transcode queue.

| Model | Holds |
|---|---|
| `Post` | author, city, text, counters; ‡ `scope` (`city` / `greece`) |
| `PostMedia` | type, `url`, `thumb_url`, `duration`, and the encode state: `status` (`ready` / `pending` / `processing` / `failed`), `attempts`, `progress` |
| `StagedUpload` | a file uploaded before its post exists |
| `PostComment`, `CommentLike` | comments with `parent` for replies; at most one pinned per post (a conditional unique constraint) |
| `PostLike`, `PostSave`, `PostReport`, `CommentReport` | one row per user per post/comment |
| `Poll`, `PollOption`, `PollVote` | one vote per user per poll |
| `NeatPointsAward` | points per user, post and day, kept as a high-water mark |

Worth knowing:
- The feed's design (cursor, lean shape, constant query count) is described in
  [Architecture → The feed](architecture.md#the-feed); the video queue in
  [Architecture → The video pipeline](architecture.md#the-video-pipeline).
- [`transcode.py`](../posts/transcode.py) holds the encode settings. The app's
  on-device compression must produce what `_TARGET_MAX_EDGE` allows, or every
  upload is re-encoded instead of remuxed.
- Posts imported from the earlier WordPress community (`import_wp_community`)
  have old `created` dates under new ids — the reason feeds page on
  `(created, id)`.

| Command | Purpose | When |
|---|---|---|
| `transcode_worker` | the video queue + background link previews | always running (`neat-transcode`) |
| `award_neat_points` | bank each city's daily top-10 points | hourly |
| `purge_staged_uploads` | delete uploads never attached to a post (24 h) | daily |
| `backfill_posters`, `localise_post_images`, `localise_remaining_base64`, `recompress_post_images` | one-off backfills from earlier storage formats | once |
| `import_wp_community` | import the WordPress community (users, posts, media, polls, comments) | once, idempotent |

Tests: `tests.py` — the feed's paging and payload, the transcode queue, staged
uploads, shared-post existence, † one-city feeds.

## dm_messages

**Direct messages.** One-to-one conversations between people in the same city:
text, photos (animated GIFs kept as they are), voice notes, shared posts,
replies, emoji reactions, edits within 15 minutes, deletes, reports, typing,
read receipts and presence — over REST, with live updates through the
WebSocket consumer in [`consumers.py`](../dm_messages/consumers.py).

| Model | Holds |
|---|---|
| `Conversation`, `ConversationMember` | membership with `last_read_at`, `typing_at`, `hidden_at` ("delete for me") |
| `Message` | `text` (with a media prefix for photos and voice notes), `media_url`, `edited`, `photo_mode` |
| `MessageOpen` | viewings spent per person on a temporary photo |
| `MessageReaction`, `MessageReport` | one per user per message |

**Temporary photos.** `photo_mode` is `''` for an ordinary message, or `once` /
`replay` for a photo the sender made temporary. The rule the feature rests on
is that the picture lives in exactly one place:

- The thread and the inbox never carry it; `POST …/open/` is the only route.
- **Each participant has their own viewings**, the sender included — `once`
  is one each, `replay` two each, counted in `MessageOpen`. One side spending
  theirs leaves the other's untouched.
- The bytes are cleared from the row when *nobody* has a viewing left, so they
  stop existing rather than being hidden.
- Because "viewings left" differs per person, updates go out as one payload per
  member, each with `opened_by_other` (the sender's "Opened").
- Builds older than the feature show an empty bubble; they never see the
  picture and cannot create one.

**Voice notes** are stored as ADTS AAC (see
[Architecture → Media](architecture.md#media)).

Commands: `localise_dm_media` (one-off: base64 → files), †
`normalise_voice_notes` (one-off: convert stored voice notes to ADTS).
Tests: `tests.py` — temporary photos, thread paging and media withholding,
stored media, multipart uploads, † voice-note containers.

## events

**City events.** *Official* events (venues, organisers — creating one needs
organiser rights) and *community* events (anyone). Attendance, attendees,
comments with likes and a pin, reports. Images are stored as files.

| Model | Holds |
|---|---|
| `Event` | city, type, title, description, location, date, category, organiser, tickets; † `status` (`pending` / `published` / `rejected`), `source`, `source_url`, `external_id`, `content_hash` |
| `EventAttendance`, `EventComment`, `EventCommentLike`, `EventReport`, `EventCommentReport` | |

† **Events gathered from the web.** `manage.py ingest_events` collects
upcoming events so a city's calendar is not empty on day one:

- **Sources** (`events/ingest/sources.py`): the
  Ticketmaster Discovery API for Athens and Thessaloniki, and venue sites that
  publish no machine-readable data — read by a language model (Gemini) from the
  page text. schema.org JSON-LD and iCal feeds are supported for venues that
  publish them.
- **Pipeline:** respects `robots.txt`, fetches through the link-preview
  fetcher's SSRF protections, normalises times to Athens, keeps only events
  from 6 hours ago to 120 days ahead, and de-duplicates against what exists
  (same source id, or same city within 90 minutes with a matching title).
- **Review:** everything gathered arrives as `pending` and is invisible in the
  app until someone publishes it in the Django admin (`/admin/events/event/`),
  where times are shown in Athens time and a missing clock time is flagged.
  Rejected events are never re-added. Events people create are never
  overwritten.

Commands: `localise_event_images` (one-off), † `ingest_events`.
Tests: `test_event_time.py`; † `test_ingest.py` (parsers, dedupe, pipeline,
review gate, Ticketmaster, extraction).

## push

**Push notifications** through Firebase Cloud Messaging: messages and reactions
(with sound), activity (silent), and the badge count. See
[Architecture → Push](architecture.md#push-notifications).

Model: `DeviceToken` (token, platform, `device_id`). Registering a device
removes its older tokens.

Commands: † `send_code_push` (a silent push telling installs to fetch a new
Shorebird patch — run after `shorebird patch`), ‡ `broadcast_greece_launch`
(the one-off announcement of the national feed).
Tests: `tests.py` — device de-duplication.

## linkpreview

**Preview cards for links** in posts, comments and messages: Open Graph plus
oEmbed (TikTok, YouTube, Vimeo, SoundCloud, Spotify), with creator handles for
Instagram and TikTok. The fetcher is built to be safe against server-side
request forgery — see [Architecture → Link previews](architecture.md#link-previews).

Model: `LinkPreview` — one row per normalised URL, good for 7 days, failures
for an hour.

Commands: `warm_previews` (resolve recent links ahead of a launch),
`localise_thumbnails` (copy thumbnails to local media).
Tests: `tests.py` — 59 cases covering parsing, merging, oEmbed, the cache,
stale cards and thumbnails.

## security

**Audit trail and threat detection** (app label `neat_security`).

- `AuditLog` is append-only and **hash-chained**: each entry stores the hash of
  the one before, so an edited or deleted row breaks the chain, and the
  dashboard reports chain integrity. Entries are sanitised (control characters
  stripped, anything resembling a secret redacted) and written in batches by a
  background thread, never on the request path. Session ids are hashes of the
  token, never the token.
- [`middleware.py`](../security/middleware.py) records access denials, injection
  and traversal patterns in URLs, and accounts making unusual request volumes;
  login code records brute-force attempts and sign-ins from a new IP.
- The admin API lets an admin filter the log, and lock, unlock or sign out an
  account — each of those actions is itself audited.
- On start-up, Django checks warn if outbound e-mail is not configured, since
  password reset depends on it.

Command: `purge_audit_logs` (retention, 365 days by default; daily).

## web

**Server-rendered pages on neatapp.gr.**

- **`/post/<id>`** — a shared post as a real page: the post, media, poll and
  comments, with Open Graph and Twitter tags (so it previews in Messenger,
  WhatsApp, iMessage), JSON-LD, store badges and a deep link into the app. The
  templates' formatting mirrors the app's (Greek relative times, counts).
- **`/safetyportal`, `/deleteaccount`** — the support page and the
  account-deletion request form required by the stores (rate-limited, with a
  honeypot; requests are e-mailed to the team).
- **`/analytics`** — the product dashboard: activation milestones, retention by
  cohort, push reach, sign-up methods, creator vs. consumer split, cities,
  hourly activity. Scoped to accounts since the launch date; test accounts
  (`@fake.com`) are excluded †.
- **`/health`** — the server's state in one page, written for whoever is holding
  the phone when something feels wrong: CPU, memory, disk, services, traffic
  (requests/s, top IPs, client versions, 5xx, p50/p95), MySQL, Redis, the
  transcode queue, certificate expiry — each finding with a severity and the
  specific thing to do. `/health/live` and `/health/ready` give uptime monitors
  a one-word answer.
- **`/runbook`** — what to do when the app is slow or down, by symptom.

`/analytics`, `/health` and `/runbook` share one admin login (app credentials,
`is_admin`, rate-limited, 8-hour session).

Tests: `tests.py` — health never raises, admin gating, public liveness, the
runbook leaks nothing to anonymous visitors.

## invites †

**Personal invite links:** `neatapp.gr/<username>/invite?city=<city>`.

- The page names the inviter and the city, shows how many more people the city
  needs once that number is small (≤ 25), and points out that the national feed
  is open meanwhile.
- **Attribution** survives the store install: the token rides in the Play
  install referrer on Android, or a one-time clipboard hand-off on iOS, and the
  app claims it after sign-up (within 3 days of the account's creation, 14 of
  the token's). The app also reports the inviter it saw in the link; a
  same-network match within 6 hours is the last resort.
- **`/invites`** (admin) shows copies → opens → joins, and how many of those
  joins became real users (a city, a post, two interactions, still active on
  day 3).
- Nothing is paid for invites.

Model: `InviteEvent` (`sent` / `opened` / `joined`).

## ambassadors †

**The street-poster programme.** Ambassadors — local promoters, usually without
a Neat account — get a short link, `neatapp.gr/a/<code>`, and printable QR
posters (PNG, PDF, A4 print page; `segno` + Pillow, with a choice of slogans).

- **Crediting a sign-up:** the same install-surviving token as invites; or,
  failing that, a same-network match within 6 hours, or the window after an
  ambassador's logged social post in a city. Network and content matches are
  always flagged for review.
- **Fraud controls:** one credited account per IP, a daily cap per ambassador,
  device clustering, self-referral blocked, and crawlers kept out of "opens"
  (an open counts only when the page itself reports a real visit).
- **Payout** happens only for sign-ups that pass the quality bar (city, post,
  interactions, active on day 3) **and** are approved by a person on
  `/ambassadorsstats`.
- **Creators' dashboard:** `/creator/<key>`, or the memorable route `/c/<code>`
  plus a 6-digit PIN — earnings, confirmed opens, posters, and a form to log
  their own social posts.

Models: `Ambassador`, `AmbassadorClick`, `AmbassadorSignup`,
`AmbassadorContent`, `SignupAddress` (kept 90 days).
Tests: `tests.py` — links, creator dashboard, counting opens, PIN entry.

## lockedcities †

**The locked-cities kill switch**, `/stoplocked` (admin, type `STOP` to
confirm). Turning it off snapshots every city's lock state and opens every
city; turning it back on restores the snapshot. It exists because flipping the
`LOCKED_CITIES_ENABLED` setting instead would leave every installed app
showing cities as locked.

The locking itself — thresholds, counting real members, unlocking, refusing
city-feed posts while locked — is ‡ in `posts/views.py` and
`neatbackend/settings.py` (`LOCKED_CITIES_*`): Athens and Thessaloniki are
always open, other cities need 100–300 local members.

## neatbackend

Project wiring: [`settings.py`](../neatbackend/settings.py) (configuration from
the environment — see [Operations → Configuration](operations.md#configuration)),
[`urls.py`](../neatbackend/urls.py), [`asgi.py`](../neatbackend/asgi.py) (HTTP +
the DM WebSocket), [`cdn.py`](../neatbackend/cdn.py) (optional CDN rewriting of
media URLs) and [`timefmt.py`](../neatbackend/timefmt.py) (times sent as Athens
wall-clock without an offset, which is what released app builds parse
correctly).
