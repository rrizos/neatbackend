# Operations

Running Neat in production: configuration, the services on the server,
deploying, scheduled jobs, and what to do when something is wrong.

- [Configuration](#configuration)
- [The server](#the-server)
- [Deploying](#deploying)
- [Scheduled jobs](#scheduled-jobs)
- [Management commands](#management-commands)
- [Health and incidents](#health-and-incidents)
- [Branches and production](#branches-and-production)

## Configuration

Settings come from the environment ([`settings.py`](../neatbackend/settings.py)).
On the server they live in `.env.prod`, which is not in git. That file cannot
be `source`d by a shell (some values contain `)` and `>`); scripts that need it
parse it line by line, splitting on the first `=`.

| Variable | Purpose |
|---|---|
| `DJANGO_SECRET_KEY` | **Must be set in production.** `settings.py` has a development fallback that is in git |
| `DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS` | Standard Django |
| `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT` | MySQL. With none set, SQLite is used |
| `REDIS_URL` | Channels layer and cache. Without it: in-memory layer, database cache |
| `PUBLIC_BASE_URL` | Absolute base for media URLs |
| `MEDIA_CDN_URL` | Optional CDN in front of `/media/` |
| `APPLE_CLIENT_IDS`, `GOOGLE_CLIENT_IDS` | Accepted audiences for social sign-in tokens (public identifiers) |
| `FIREBASE_CREDENTIALS_PATH` | Service-account JSON for push; push is off without it |
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_USE_TLS`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `DEFAULT_FROM_EMAIL`, `EMAIL_FROM_NAME` | Outbound e-mail (password reset, deletion requests) |
| `NEAT_LAUNCH_DATE` † | Start of the `/analytics` window |
| `TICKETMASTER_API_KEY`, `GEMINI_API_KEY` † | Event ingestion sources |
| `NEAT_WEB_ROOT` | Where the static site is published (the shared-post page reads the default social image from it) |
| `RUN_MIGRATIONS_ON_STARTUP` | Unless `0`, `wsgi.py` runs `migrate` as each gunicorn starts. Run migrations by hand before restarting regardless (below) |

## The server

One AWS Lightsail instance (Debian, 2 vCPU, 2 GB RAM, 2 GB swap) with a managed
Lightsail MySQL database on a separate host. The code lives in
`/home/bitnami/neatbackend` with its virtualenv in `venv/`.

| systemd unit | What it runs |
|---|---|
| `nginx` | TLS, the static site (`/home/bitnami/neat-web`), `/media/`, and proxying to the two gunicorns |
| `gunicorn` | WSGI: the API and server-rendered pages |
| `gunicorn-asgi` | ASGI: the DM WebSocket |
| `neat-transcode` | `manage.py transcode_worker` — video encoding and link previews, at low CPU and I/O priority |
| `redis-server` | Channels layer and cache |

**nginx routes are explicit.** `neatapp.gr` is a static site, and only the
paths listed in the nginx site config reach Django (`/api/`, `/ws/`, `/post/`,
the admin pages, the invite and ambassador routes…). A new public URL needs a
`location` there as well as a route in `urls.py`, or nginx answers `404` and the
request never reaches Django. After editing: `sudo nginx -t && sudo systemctl
reload nginx`.

**Certificates.** `neatapp.gr` (+ `www`) and `api.neatapp.gr` use Let's Encrypt
and renew automatically. App builds released before the move to the domain
connect to the server's **IP address** and pin its self-signed certificate.
Two rules keep them working:

- The IP's server block must stay nginx's `default_server` on port 443 — a
  client connecting by IP sends no SNI and gets the default. If a Let's Encrypt
  certificate ever becomes the default, every old build loses the API.
- Never run `certbot --nginx` (it rewrites server blocks). Use
  `certbot certonly --webroot`.

Resizing the instance means restoring a snapshot onto a new machine; the
self-signed key must be carried across by hand.

## Deploying

The server does not pull from GitHub (it has no credentials), so changed files
are copied to it. Because the server has, at times, held changes that git did
not (see [Branches](#branches-and-production)), **never copy a whole file
over the server's copy without comparing them first**.

1. **Compare.** For each file you will replace, compare the server's copy with
   the version you think it has (`md5sum` on both sides). If they differ, apply
   your change to the server's copy instead of overwriting it.
2. **Back up** the files: `cp <files> ~/deploy-backups/$(date +%Y%m%d%H%M%S)/`.
3. **Copy** the changed files up.
4. **Check before restarting** — the views read every column of the models they
   touch, so new code against an unmigrated database fails outright:
   ```sh
   venv/bin/python manage.py check
   venv/bin/python manage.py migrate --plan
   venv/bin/python manage.py migrate
   ```
5. **Restart:** `sudo systemctl restart gunicorn gunicorn-asgi`. Add
   `neat-transcode` only when the worker's code changed — a restart waits for
   the encode in flight. Template changes need a restart too.
6. **Smoke-test** through nginx (`curl https://neatapp.gr/api/auth/health/`, a
   `/post/<id>` page) and watch `journalctl -u gunicorn -f`.

The static site deploys separately, from the app repository
(`tools/deploy_web.sh`).

Getting the server to deploy with `git pull` again needs a read-only deploy key
for this repository installed on it.

## Scheduled jobs

These are meant to run on timers on the server; the timer definitions are not
in this repository.

| Command | Schedule | Why |
|---|---|---|
| `award_neat_points` | hourly | Banks each city's daily top-10 Neat Points |
| `purge_staged_uploads` | daily | Deletes uploads never attached to a post (older than 24 h) |
| `purge_audit_logs` | daily | Audit-log retention (365 days) |
| `ingest_events` † | not yet scheduled | Gathers upcoming events for review; run by hand for now |

## Management commands

Run as `venv/bin/python manage.py <command>`. Most one-off commands take
`--dry-run`.

| Command | App | Kind | What it does |
|---|---|---|---|
| `transcode_worker` | posts | service | The video queue; link previews when idle |
| `award_neat_points` | posts | scheduled | See above |
| `purge_staged_uploads` | posts | scheduled | See above |
| `purge_audit_logs` | security | scheduled | See above |
| `ingest_events` † | events | ad hoc | See [Apps → events](apps.md#events) |
| `warm_previews` | linkpreview | ad hoc | Resolve link previews for recent posts and messages — run before a launch |
| `send_code_push` † | push | ad hoc | Silent push asking installs to fetch the new Shorebird patch |
| `broadcast_greece_launch` ‡ | push | once | Announce the national feed |
| `backfill_posters` | posts | once | Poster frames for old videos |
| `localise_avatars`, `localise_post_images`, `localise_dm_media`, `localise_event_images`, `localise_remaining_base64` | various | once | Move base64 media out of the database into files |
| `localise_thumbnails` | linkpreview | once | Copy preview thumbnails to local media |
| `recompress_post_images` | posts | once | Re-encode oversized post images |
| `normalise_voice_notes` † | dm_messages | once | Convert stored voice notes to ADTS AAC |
| `import_wp_community` | posts | once | Import the earlier WordPress community |

## Health and incidents

| Where | What |
|---|---|
| `https://neatapp.gr/health` | Admin login. The server's state — CPU, memory, disk, services, traffic, database, Redis, the transcode queue, certificates — with a verdict and the specific action for anything not OK. `?format=json` for machines |
| `/health/live`, `/health/ready` | Public, one word, `503` when unhealthy — for an uptime monitor |
| `https://neatapp.gr/runbook` | Admin login. What to do by symptom: slow, down, disk full, memory, database, uploads stuck — and what never to do |
| `/analytics` | Admin login. Product metrics |
| logs | `journalctl -u gunicorn` (and `gunicorn-asgi`, `neat-transcode`), `/var/log/nginx/` |

Capacity, measured: about 19 feed requests a second, limited by CPU; memory
and database connections have plenty of headroom. The `/health` page and the
runbook explain what to do when the ceiling is reached.

## Branches and production

As of October 2026, three copies of the backend differ:

| | Has |
|---|---|
| `main` | The Greece national feed (`Post.scope`, `GET /api/posts/greece/`), profile `postCount` and per-user posts, locked-city thresholds and unlocking, city-scoped profiles |
| `referrals-ambassadors-killswitch` | `invites`, `ambassadors`, `lockedcities`, event ingestion, ADTS voice notes, analytics launch scope, `send_code_push`, and 17 commits in all |
| **the server** | As far as git can tell, both of the above — plus at least a `CityConfig` model that is in neither |

What that means in practice:

- **`main` does not start.** `posts/views.py` imports a `CityConfig` model that
  is not defined in `posts/models.py` on any branch (`manage.py check` fails
  with an `ImportError`); the branch imports it lazily, so it starts, but its
  invite pages and kill switch need the model too. `main`'s `/analytics` view also calls `collect()` with an
  argument the function no longer takes.
- **The app calls two routes no branch defines**: `GET /api/posts/city-locks/`
  (lock state on the map) and `GET /api/posts/liked/` (the Liked tab). Their
  views are on `main`, but no committed `urls.py` routes them — check the
  server's copy.
- **`Post.scope` has no migration**; the column is added at runtime by the
  view's table check.
- **Commit `dbf481d` on `main` also removed settings** the branch still has —
  the Redis `socket_timeout` that keeps the DM socket alive, the Redis cache,
  `NEAT_LAUNCH_DATE`, `MEDIA_CDN_URL`, the default Google client ids — and
  replaced Athens wall-clock times with UTC in DM payloads. Merging `main` into
  the branch applies those removals *without* a conflict, so they need to be
  put back by hand.

**To reconcile:** copy the server's `posts/models.py`, `posts/urls.py` and any
migrations it has that git lacks into a branch; merge `main` and
`referrals-ambassadors-killswitch` into it, restoring the settings above;
confirm `manage.py check`, `makemigrations --check` and the test suite pass;
then compare the result file by file against the server before the next deploy.
After that, `main` is the deployable truth again.

The `Procfile` and the `render` remote some checkouts have are left from June
2026, when the backend briefly ran on Render; production does not use them.
