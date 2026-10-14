# API reference

Every endpoint the app and the website use. For the reasoning behind the shapes,
see [Architecture](architecture.md).

**Markers:** ‡ only on `main` · † only on `referrals-ambassadors-killswitch`.
Both are live in production — see [Operations → Branches](operations.md#branches-and-production).

## Conventions

| | |
|---|---|
| Base URL | `https://neatapp.gr` (also `https://api.neatapp.gr`) |
| Auth | `Authorization: Token <key>` — the key comes back from sign-up, login or social login |
| Client generation | `X-Neat-Client: 3` — see [API generations](architecture.md#api-generations-x-neat-client) |
| Bodies | JSON, or `multipart/form-data` for file uploads |
| Errors | A status code and `{"error": "<message>"}`; the message is safe to show to the user |
| CORS | Every API view answers `OPTIONS` and sends `Access-Control-Allow-Origin: *` |
| Caching | API responses are `Cache-Control: no-store`, except DM media (immutable, private) |

**Pagination** is by cursor: pass the id of the last item you have as
`before=<id>`, and keep going while `has_more` is true. Feeds page by
`(created, id)` underneath, so the cursor is stable even where ids and dates
disagree.

**City rules.** Liking, commenting, voting, attending and messaging are allowed
only in the caller's own city (Greece-scope posts excepted) and fail with
`400`/`403` otherwise.

---

## Accounts — `/api/auth/`

### Sign-up, sign-in, session

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `signup/` | — | `{username, email, password, fullName, city, bio}` → `201 {token, user}`. Username 3–20 of `[a-zA-Z0-9._]`, no leading/trailing dot |
| POST | `login/` | — | `{username \| email, password}` → `{token, user}` |
| POST | `social/` | — | `{provider: "apple"\|"google", idToken, nonce?, fullName?}` → `{token, user}` (`201` if the account is new). New accounts come back with `usernamePending: true` |
| POST | `logout/` | token | Deletes this token |
| POST | `forgot-password/` | — | `{email}` (an e-mail or a username) → `{ok, maskedEmail, username}`; e-mails a 6-digit code valid for 15 minutes |
| POST | `reset-password/` | — | `{email, code, newPassword}` → `{token, user}`; signs out every other session |
| POST | `password/set/` | token | `{password, currentPassword?}` — set a first password (social accounts), or change it with the current one |
| POST | `session/ping/` | token | Session heartbeat for analytics; header `X-Neat-Platform: ios\|android` |
| GET | `health/` | — | `{"ok": true}` |

### Profile and social graph

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `me/` | token | The signed-in user, including `cityChangeAllowedAt`, `hasPassword`, `usernamePending`; ‡ `cityLocked`, `cityThreshold`, `cityMemberCount` |
| PATCH | `me/` | token | JSON or multipart (`avatar` file): `username`, `email`, `fullName`, `bio`, `city` (once per 30 days), `avatar` |
| DELETE | `me/` | token | Deletes the account, its posts and events |
| GET | `profiles/<username>/` | token | A profile with `mutuals` (first 3) and `mutualsCount`; `404` across a block |
| GET | `profiles/<username>/followers/` | token | Followers (‡ scoped to the profile's current city) |
| GET | `profiles/<username>/following/` | token | Following (‡ same) |
| POST | `profiles/<username>/follow/` | token | Toggle, or `{"follow": false}`; same city only |
| POST | `profiles/<username>/block/` | token | Toggle a block |
| GET | `blocked/` | token | The caller's block list |
| GET | `suggestions/` | token | Up to 10 people in the caller's city |
| GET | `search/?q=` | token | People in the caller's city, mutuals first, up to 25 |
| GET, POST, DELETE | `search-history/`, `search-history/<query>/` | token | The last 8 searches |
| GET | `notifications/` | token | Newest 50 and an `unread` count |
| POST | `notifications/` | token | `{"ids": [...]}` marks those read; † `{"all": true}` marks everything read |
| GET | `neat-pass/` | token | Neat Points balance |

### Admin (`Profile.is_admin`)

| Method | Path | Description |
|---|---|---|
| GET | `admin/analytics/` | Totals, growth, DAU/WAU/MAU, engagement, top cities, 30-day series |
| GET | `admin/reports/` | Reported posts, messages, events and comments, with report counts |
| DELETE | `admin/reports/<id>/?type=post\|message\|event\|comment` | Dismiss a report |
| DELETE | `admin/posts/<id>/`, `admin/messages/<id>/`, `admin/comments/<id>/` | Remove content |
| GET | `admin/comments/?q=`, `admin/users/?q=` | Search |
| POST | `admin/users/<username>/verify/` | `{verified}` |
| POST | `admin/users/<username>/official-eligibility/` | `{eligible}` — may create official events |
| DELETE | `admin/users/<username>/delete/` | Delete an account |

### Security (admin) — `/api/auth/admin/security/`

| Method | Path | Description |
|---|---|---|
| GET | `logs/` | Audit log. Filters: `severity` (`alerts`, `all`, or a level), `event_type` (prefix), `actor`, `q`, `hours`, `limit` (≤ 300), `before`; returns `nextBefore` |
| GET | `summary/` | Counts by severity (24 h / 7 d), top events and IPs, locked accounts, hash-chain integrity |
| POST | `actions/` | `{action: "revoke_sessions"\|"lock_account"\|"unlock_account"\|"acknowledge", username, logId}` |

## Posts — `/api/posts/`

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `` (the root) | optional | **City feed.** `?city=` (default: the caller's), `before`, `limit` (default 20, max 60) → `{posts, avatars, has_more}`. Without `X-Neat-Client ≥ 2`: the newest 50 as a bare list |
| POST | `` (the root) | token | **Create a post** in the caller's city. Multipart: `text`, `media` (JSON list of `{upload_id}`, `{url}` or `{type, file_index}` with `media_<n>` file parts), `poll` (JSON); ‡ `scope: "city"\|"greece"`. Up to 4 media → `201` post |
| POST | `upload/` | token | **Stage a file** as soon as it is picked: multipart `file`, `type: image\|video` (video ≤ 150 MB, image ≤ 20 MB) → `201 {id, type, url}`; refer to it as `upload_id` |
| GET | `greece/` ‡ | token | The national feed; same paging and shape as the city feed |
| GET | `user/<username>/?scope=city\|greece` ‡ | token | A user's posts |
| GET | `<id>/` | optional | One post with its comments |
| POST | `<id>/like/` | token | `{liked}` |
| POST | `<id>/save/` | token | `{saved}` |
| GET | `saved/` | token | `{posts}` — the caller's saves |
| POST | `<id>/share/` | token | Count a share |
| POST | `<id>/poll/vote/` | token | `{option_id}` — vote, switch, or tap again to remove |
| GET | `<id>/likers/` | token | `{users}` — mutuals first |
| POST | `<id>/comments/` | token | `{text, imageUrl?, parentId?, replyToUsername?}` → the post |
| DELETE | `<id>/comments/` | token | `{commentId}` — own comment, or admin |
| DELETE | `<id>/delete/` | token | Own post, or admin |
| POST | `<id>/report/` | token | `{reason, sub_reason}` |
| POST | `comments/<id>/like/`, `comments/<id>/report/`, `comments/<id>/pin/` | token | Comment actions; pinning is for the post's author |
| GET | `viral/` | optional | **Virals:** top 10, `?city=` or `?exclude_city=`, `period=daily\|weekly\|monthly`, `light=1` |
| GET | `city-heat/` | — | `{city: ratio}` for unusually busy cities |
| GET | `cities/` | optional | Cities that have posts |
| GET | `exist/?ids=1,2,3` | token | `{missing: [...]}` — for post cards shared into DMs |

Each media item in a post: `{type, url, thumbUrl, duration, status:
"processing"|"ready", progress}`. Clients play `url` whatever the status.

The app also calls `city-locks/` (lock state for the map) and `liked/` (the
Liked tab). Their views exist on `main` but neither committed branch routes
them yet — see [Operations → Branches](operations.md#branches-and-production).

## Direct messages — `/api/messages/`

| Method | Path | Description |
|---|---|---|
| GET | `inbox/` | Conversations, newest first, with a text-only preview |
| POST | `start/` | `{username}` — get or create a conversation (same city, not blocked) |
| GET | `<conv>/` | A page of messages: newest 40, `before=<message id>`, `limit` (≤ 200), `has_more`. Marks the thread read |
| POST | `<conv>/` | Send: JSON `{text}`, or multipart `media` + `media_kind: image\|voice` (+ `media_suffix`, `photo_mode: once\|replay`) → `201` |
| DELETE | `<conv>/delete/` | Hide the conversation for the caller ("delete for me") |
| GET, POST | `<conv>/typing/` | Typing state (REST fallback for the socket) |
| PATCH | `<conv>/messages/<id>/edit/` | Own text message, within 15 minutes |
| DELETE | `<conv>/messages/<id>/delete/` | Own message |
| GET | `<conv>/messages/<id>/media/` | The media of one message: `{url}` (or base64 for old builds); cached immutable |
| POST | `<conv>/messages/<id>/open/` | Spend one viewing of a view-once / replay photo → `{message, photo}`; `410` once spent |
| POST | `<conv>/messages/<id>/react/` | `{emoji}` — set, replace, or tap again to remove |
| POST | `<conv>/messages/<id>/report/` | `{reason}` |
| POST | `presence/` | Presence heartbeat |

### WebSocket — `wss://neatapp.gr/ws/messages/`

Send `{"action": "auth", "token": "<key>"}` within 5 s of connecting (else the
socket closes with `4001`). Messages are `{"event": …, "payload": …}`.

| Direction | Events |
|---|---|
| server → app | `authenticated`, `auth_error`, `message.new`, `message.edited`, `message.deleted`, `message.reaction`, `conversation.new`, `conversation.deleted`, `typing`, `read_receipt`, `presence`, `block`, `unblock` |
| app → server | `typing {conversation_id, typing}`, `mark_read {conversation_id}`, `ping` |

## Events — `/api/events/`

| Method | Path | Description |
|---|---|---|
| GET | `` (the root) | `?city=` (default: the caller's), `?type=official\|community`; up to 200 |
| POST | `` (the root) | Create: JSON or multipart (`image` file): `title, description, location, date, eventType, category, organizer, hasTickets, ticketsUrl`. Official events need organiser rights |
| POST | `<id>/attend/` | Toggle attendance (same city) |
| GET | `<id>/attendees/` | Who is going |
| POST, PATCH | `<id>/update/` | Creator or admin |
| DELETE | `<id>/delete/` | Creator or admin |
| GET, POST, DELETE | `<id>/comments/` | Event comments |
| POST | `<id>/report/`, `comments/<id>/report/`, `comments/<id>/pin/`, `comments/<id>/like/` | Moderation and comment actions |

† Events gathered from the web arrive as `pending` and are listed only once a
reviewer publishes them — see [Apps → events](apps.md#events).

## Push — `/api/push/`

| Method | Path | Description |
|---|---|---|
| POST | `devices/register/` | `{token, platform: "ios"\|"android", deviceId}` |
| POST | `devices/unregister/` | `{token}` |
| GET | `badge/` | `{badge}` — unread DMs + unread notifications |

## Link previews — `/api/link-preview/`

| Method | Path | Description |
|---|---|---|
| GET | `?url=` | `{preview: {url, resolved_url, title, description, image_url, image_width, image_height, site_name, author_name, author_handle, author_url, kind} \| null}` |

## Invites † — `/api/invites/`

| Method | Path | Description |
|---|---|---|
| POST | `sent/` | The user copied their invite link |
| POST | `joined/` | `{inviter, city}` — the app opened `/<inviter>/invite` before this account signed up |
| POST | `claim/` | `{token}` — an invite token recovered from the install referrer or the clipboard |

## Ambassadors † — `/api/ambassadors/`

| Method | Path | Description |
|---|---|---|
| POST | `click/` | `{code}` — the app opened `/a/<code>`; mints a single-use token |
| POST | `seen/` | `{token}` — beacon from the landing page: a person, not a crawler, opened the link |
| POST | `claim/` | `{token, method}` — credit this new account to an ambassador |

## Web pages

Server-rendered by the `web` app (and † `invites`, `ambassadors`, `lockedcities`).

| Path | Who | What |
|---|---|---|
| `/post/<id>` | public | A shared post: rendered post, Open Graph / Twitter tags, JSON-LD, store badges, opens the app if installed |
| `/post/<id>/og-image` | public | The image used for the link preview |
| `/safetyportal` | public | Support and data page (Greek / English) |
| `/deleteaccount` | public | Account-deletion request form |
| `/<username>/invite`, `/invite` † | public | A personal invite page |
| `/a/<code>` † | public | An ambassador's link; `/a/<code>/poster.png|pdf`, `/a/<code>/print` for the QR poster |
| `/creator/<key>`, `/c/<code>` † | key / code + PIN | An ambassador's own dashboard |
| `/analytics`, `/health`, `/runbook` | admin login | Product analytics, server health, incident runbook |
| `/invites`, `/ambassadors`, `/ambassadorsstats`, `/stoplocked` † | admin login | Invite funnel, ambassador management and payouts, the locked-cities switch |
| `/health/live`, `/health/ready` | public | One word (`ok`, `warn`, `crit`…) for uptime monitors; `503` when unhealthy |
| `/admin/` | Django staff | Django admin (used to review gathered events †) |
