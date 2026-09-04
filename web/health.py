"""Operational health for the box, in one page, with the verdict spelled out.

The audience is whoever is holding the phone when something feels wrong, which
may be someone who has never opened a shell. So every probe ends in a finding
with a severity and, when it is not OK, the specific thing to *do* — not a
number to interpret. `collect()` never raises: a probe that fails becomes a
finding saying so, because a health page that 500s is worse than none at all.

Numbers that look arbitrary are measured, not guessed. On 2026-09-03 a
graduated read-only load test against this box put feed throughput at a flat
~19 req/s from 4 concurrent requests all the way to 32 — latency rose linearly
while throughput did not move, which is a fully saturated server. Memory
(1191 MB free at peak) and database connections (23 of 61) were nowhere near
their limits. CPU is the only wall, which is why the resize advice below says
vCPU and not RAM.
"""

import os
import re
import shutil
import socket
import ssl
import subprocess
import time
from datetime import datetime, timedelta, timezone

from django.core.cache import cache
from django.db import connection

# ── Tunables ────────────────────────────────────────────────────────────────

VCPUS = os.cpu_count() or 2
MEASURED_CEILING_RPS = 19.0        # see module docstring
FEED_BASELINE_MS = 84.0            # single-request feed latency when idle
DB_PLAN_GB = 40                    # storage on the Lightsail micro database plan

UNITS = ('gunicorn', 'gunicorn-asgi', 'neat-transcode', 'nginx', 'redis-server')
ACCESS_LOG = '/var/log/nginx/access.log'
LOG_TAIL_BYTES = 2 * 1024 * 1024   # enough for well over the 15 min we look at
TRAFFIC_WINDOW_MIN = 15

CACHE_KEY = 'health:snapshot'
CACHE_SECONDS = 10                 # a refresh-happy phone must not add load
DISK_HISTORY_KEY = 'health:disk_history'

OK, NOTE, WARN, CRIT = 'ok', 'note', 'warn', 'crit'

# NOTE ranks with OK on purpose. It is for standing facts worth showing but not
# worth alarming about — an unlimited Redis, media that has no backup. Ranking
# them as warnings would leave this page permanently yellow and /health/ready
# permanently failing, which trains everyone to ignore both.
_RANK = {OK: 0, NOTE: 0, WARN: 1, CRIT: 2}


def _finding(sev, title, detail='', action=''):
    return {'severity': sev, 'title': title, 'detail': detail, 'action': action}


def _safe(fn, label):
    """Run a probe; turn any failure into a finding instead of an exception."""
    try:
        return fn()
    except Exception as exc:
        return {
            'findings': [_finding(
                WARN, f'{label} could not be read', f'{type(exc).__name__}: {exc}',
                'Not necessarily a problem with the app — the probe itself failed.',
            )],
        }


# ── System ──────────────────────────────────────────────────────────────────

def _system():
    with open('/proc/loadavg') as fh:
        parts = fh.read().split()
    load1, load5, load15 = (float(p) for p in parts[:3])

    mem = {}
    with open('/proc/meminfo') as fh:
        for line in fh:
            k, _, v = line.partition(':')
            mem[k] = int(v.split()[0]) // 1024          # MB

    total_mb = mem.get('MemTotal', 0)
    avail_mb = mem.get('MemAvailable', 0)
    swap_used = mem.get('SwapTotal', 0) - mem.get('SwapFree', 0)

    du = shutil.disk_usage('/')
    disk_pct = du.used / du.total * 100

    with open('/proc/uptime') as fh:
        uptime_days = float(fh.read().split()[0]) / 86400

    # Load average is per-core, so utilisation is load / vCPUs. Above ~0.7 the
    # queue is forming; above ~1.3 requests are waiting far longer than they
    # are being served, which is what the load test looked like at 32 clients.
    util = load1 / VCPUS
    f = []
    if util >= 1.3:
        f.append(_finding(
            CRIT, 'CPU saturated',
            f'load {load1:.2f} on {VCPUS} vCPU ({util * 100:.0f}%)',
            'Requests are queueing. If this is real traffic rather than one '
            'abusive IP (check Traffic below), the fix is a bigger instance — '
            'add vCPU, not RAM. Do NOT restart gunicorn: it drops every '
            'in-flight request and the load returns immediately.',
        ))
    elif util >= 0.7:
        f.append(_finding(
            WARN, 'CPU busy',
            f'load {load1:.2f} on {VCPUS} vCPU ({util * 100:.0f}%)',
            'Approaching the measured ~19 req/s ceiling. Watch Traffic below '
            'to see whether it is real users or the unauthenticated feed.',
        ))
    else:
        f.append(_finding(OK, 'CPU healthy', f'load {load1:.2f} on {VCPUS} vCPU'))

    if avail_mb < 200:
        f.append(_finding(
            CRIT, 'Memory nearly exhausted', f'{avail_mb} MB available',
            'Restart gunicorn — this is the case where restarting is the right move.'))
    elif avail_mb < 400:
        f.append(_finding(WARN, 'Memory low', f'{avail_mb} MB available'))
    else:
        f.append(_finding(OK, 'Memory healthy', f'{avail_mb} MB of {total_mb} MB available'))

    # Swap is the honest early warning: this box normally never touches it.
    if swap_used > 300:
        f.append(_finding(
            CRIT, 'Swapping heavily', f'{swap_used} MB of swap in use',
            'Everything will feel slow. Restart gunicorn, then look for a leak.'))
    elif swap_used > 100:
        f.append(_finding(WARN, 'Swap in use', f'{swap_used} MB',
                          'Normally this box uses almost none.'))
    else:
        f.append(_finding(OK, 'Swap barely touched', f'{swap_used} MB'))

    if disk_pct >= 90:
        f.append(_finding(
            CRIT, 'Disk almost full', f'{disk_pct:.0f}% used',
            'This is the one failure that does NOT self-heal: MySQL writes and '
            'nginx both stop. Clear space now — old files in ~/deploy-backups '
            'and journal logs are the usual candidates.'))
    elif disk_pct >= 80:
        f.append(_finding(WARN, 'Disk filling', f'{disk_pct:.0f}% used',
                          'Plan to clear space.'))
    else:
        f.append(_finding(OK, 'Disk healthy',
                          f'{disk_pct:.0f}% used, {du.free // 2**30} GB free'))

    return {
        'load': [load1, load5, load15], 'vcpus': VCPUS, 'util_pct': util * 100,
        'mem_available_mb': avail_mb, 'mem_total_mb': total_mb,
        'swap_used_mb': swap_used, 'disk_pct': disk_pct,
        'disk_free_gb': du.free / 2**30, 'uptime_days': uptime_days,
        'days_to_full': _disk_projection(du.used, du.total),
        'findings': f,
    }


def _disk_projection(used, total):
    """Rough days-until-full from samples we keep ourselves. Returns None until
    there are two samples at least an hour apart, so it cannot mislead early."""
    try:
        now = time.time()
        history = cache.get(DISK_HISTORY_KEY) or []
        history = [h for h in history if now - h[0] < 14 * 86400]
        if not history or now - history[-1][0] > 3600:
            history.append((now, used))
        cache.set(DISK_HISTORY_KEY, history[-64:], timeout=30 * 86400)
        if len(history) < 2:
            return None
        (t0, u0), (t1, u1) = history[0], history[-1]
        if t1 - t0 < 3600 or u1 <= u0:
            return None
        rate = (u1 - u0) / (t1 - t0)                    # bytes/sec
        return (total - u1) / rate / 86400
    except Exception:
        return None


# ── Services ────────────────────────────────────────────────────────────────

def _services():
    out = subprocess.run(
        ['systemctl', 'show', '--property=Id,ActiveState,NRestarts,ActiveEnterTimestamp',
         *UNITS],
        capture_output=True, text=True, timeout=10,
    ).stdout

    blocks, cur = [], {}
    for line in out.splitlines():
        if not line.strip():
            if cur:
                blocks.append(cur); cur = {}
            continue
        k, _, v = line.partition('=')
        cur[k] = v
    if cur:
        blocks.append(cur)

    rows, f = [], []
    prev = cache.get('health:restarts') or {}
    now_restarts = {}
    for b in blocks:
        unit = b.get('Id', '?')
        state = b.get('ActiveState', 'unknown')
        restarts = int(b.get('NRestarts') or 0)
        now_restarts[unit] = restarts
        rows.append({'unit': unit, 'state': state, 'restarts': restarts,
                     'since': b.get('ActiveEnterTimestamp', '')})
        if state != 'active':
            f.append(_finding(
                CRIT, f'{unit} is {state}', '',
                f'Restart it: sudo systemctl restart {unit}'))
        # A climbing restart count is systemd papering over repeated crashes.
        if unit in prev and restarts > prev[unit]:
            f.append(_finding(
                WARN, f'{unit} restarted since last check',
                f'{prev[unit]} to {restarts}',
                'Something is crashing and being restarted automatically. '
                f'Check: sudo journalctl -u {unit} -n 100'))
    cache.set('health:restarts', now_restarts, timeout=7 * 86400)

    if not f:
        f.append(_finding(OK, 'All services active', ', '.join(r['unit'] for r in rows)))
    return {'rows': rows, 'findings': f}


# ── Database ────────────────────────────────────────────────────────────────

def _database():
    """The managed MySQL, which is the piece with no redundancy: a single-AZ
    micro instance. Most of what matters here is not the size of anything but
    whether it is still accepting writes, whether the connection ceiling has
    ever actually been hit, and whether one stuck query is holding up the rest."""
    f = []
    t0 = time.monotonic()
    with connection.cursor() as c:
        c.execute('SELECT 1')
        c.fetchone()
        latency_ms = (time.monotonic() - t0) * 1000

        # Two round trips, not twenty. Each SHOW ... LIKE is a separate call to
        # a managed instance on another host, so asking for eighteen values one
        # at a time cost more than every other probe on the page combined — and
        # cost it again on each liveness check, which is exactly the load you do
        # not want to add while something is already wrong.
        c.execute('SHOW GLOBAL STATUS')
        status_all = {k.lower(): v for k, v in c.fetchall()}
        c.execute('SHOW VARIABLES')
        vars_all = {k.lower(): v for k, v in c.fetchall()}

        def var(name):
            return vars_all.get(name.lower())

        def stat(name, default=0):
            try:
                return int(status_all.get(name.lower(), default))
            except (TypeError, ValueError):
                return default

        max_conn = int(var('max_connections') or 0)
        read_only = (var('read_only') or 'OFF').upper()
        long_query_time = var('long_query_time') or '?'
        threads, running = stat('Threads_connected'), stat('Threads_running')
        peak, uptime = stat('Max_used_connections'), stat('Uptime')
        questions, slow = stat('Questions'), stat('Slow_queries')
        aborted, refused = stat('Aborted_connects'), stat('Connection_errors_max_connections')
        bp_req, bp_disk = stat('Innodb_buffer_pool_read_requests'), stat('Innodb_buffer_pool_reads')
        lock_waits = stat('Innodb_row_lock_waits')

        c.execute('SELECT ROUND(SUM(data_length + index_length) / 1048576, 1) '
                  'FROM information_schema.tables WHERE table_schema = DATABASE()')
        size_mb = float(c.fetchone()[0] or 0)

        c.execute('SELECT table_name, ROUND((data_length + index_length) / 1048576, 2) '
                  'FROM information_schema.tables WHERE table_schema = DATABASE() '
                  'ORDER BY (data_length + index_length) DESC LIMIT 5')
        biggest = [(t, float(v or 0)) for t, v in c.fetchall()]

        # A query running for a long time is the classic silent outage: it holds
        # locks, everything behind it queues, and nothing reports an error until
        # the connections run out.
        c.execute("SELECT ID, USER, TIME, STATE, LEFT(INFO, 120) "
                  "FROM information_schema.PROCESSLIST "
                  "WHERE COMMAND <> 'Sleep' AND INFO IS NOT NULL AND TIME > 10 "
                  "ORDER BY TIME DESC LIMIT 5")
        long_running = [
            {'id': r[0], 'user': r[1], 'seconds': r[2], 'state': r[3], 'sql': r[4]}
            for r in c.fetchall() if 'PROCESSLIST' not in (r[4] or '')
        ]

    # Rates need two samples; the first call after a restart simply has none.
    qps = slow_rate = None
    try:
        prev, now = cache.get('health:db_counters'), time.time()
        if prev and now > prev['t'] and questions >= prev['questions']:
            span = now - prev['t']
            qps = (questions - prev['questions']) / span
            slow_rate = (slow - prev['slow']) / span * 3600
        cache.set('health:db_counters',
                  {'t': now, 'questions': questions, 'slow': slow}, timeout=86400)
    except Exception:
        pass

    if read_only != 'OFF':
        f.append(_finding(
            CRIT, 'Database is read-only', f'read_only={read_only}',
            'Every write is failing — no posts, no signups, no messages. This is '
            'what a half-finished failover looks like. Check the database in the '
            'Lightsail console.'))

    if refused:
        f.append(_finding(
            CRIT, 'Connections have been refused',
            f'{refused} times at the {max_conn} limit',
            'The app has hit the connection ceiling. Restart gunicorn to drop '
            'stale connections; if it recurs, the plan is too small.'))

    pct = threads / max_conn * 100 if max_conn else 0
    if pct >= 85:
        f.append(_finding(CRIT, 'Database connections nearly exhausted',
                          f'{threads} of {max_conn}',
                          'Requests will start failing with "Too many '
                          'connections". Restart gunicorn.'))
    elif pct >= 65:
        f.append(_finding(WARN, 'Database connections high', f'{threads} of {max_conn}'))
    else:
        f.append(_finding(OK, 'Database connections healthy',
                          f'{threads} of {max_conn} (peak {peak}, refused {refused})'))

    # Threads_running is the honest concurrency number — connected threads are
    # mostly idle, running ones are actually executing.
    if running >= 20:
        f.append(_finding(CRIT, 'Database is contended',
                          f'{running} queries executing at once',
                          'Queries are piling up rather than completing. See the '
                          'long-running list.'))
    elif running >= 8:
        f.append(_finding(WARN, 'Database busy', f'{running} queries executing'))
    else:
        f.append(_finding(OK, 'Database not contended', f'{running} executing'))

    if long_running:
        worst = long_running[0]
        f.append(_finding(
            CRIT if worst['seconds'] > 60 else WARN,
            f'{len(long_running)} long-running quer{"y" if len(long_running) == 1 else "ies"}',
            f'oldest {worst["seconds"]}s: {(worst["sql"] or "")[:80]}',
            'A stuck query holds locks and queues everything behind it. From a '
            f'mysql shell: KILL {worst["id"]};'))

    if latency_ms > 200:
        f.append(_finding(CRIT, 'Database slow to answer',
                          f'{latency_ms:.0f} ms for SELECT 1',
                          'The instance or the network to it is struggling.'))
    elif latency_ms > 50:
        f.append(_finding(WARN, 'Database latency raised', f'{latency_ms:.0f} ms'))
    else:
        f.append(_finding(OK, 'Database responsive', f'{latency_ms:.1f} ms'))

    if uptime < 3600:
        f.append(_finding(
            WARN, 'Database restarted recently', f'up {uptime // 60} min',
            'A managed-instance restart or failover happened. Writes were '
            'failing while it was down.'))

    hit_rate = (1 - bp_disk / bp_req) * 100 if bp_req else 100.0
    if hit_rate < 99:
        f.append(_finding(
            NOTE, 'Working set does not fit in memory',
            f'buffer pool hit rate {hit_rate:.2f}%',
            'Queries are reaching disk. This is the one case where a bigger '
            'database plan actually buys something.'))

    if size_mb / (DB_PLAN_GB * 1024) * 100 > 80:
        f.append(_finding(WARN, 'Database storage filling',
                          f'{size_mb:.0f} MB of ~{DB_PLAN_GB} GB'))

    if slow_rate and slow_rate > 60:
        f.append(_finding(WARN, 'Slow queries appearing', f'~{slow_rate:.0f}/hour',
                          f'Queries taking over {long_query_time}s.'))

    # Standing fact, not an alarm: there is no standby to fail over to.
    f.append(_finding(
        NOTE, 'Database has no standby', 'single-AZ managed instance',
        'If its host fails the app is down regardless of the web server. The '
        'Lightsail high-availability plan is the only thing that changes that.'))

    return {'threads': threads, 'running': running, 'max_connections': max_conn,
            'peak': peak, 'refused': refused, 'latency_ms': latency_ms,
            'size_mb': size_mb, 'read_only': read_only, 'uptime_days': uptime / 86400,
            'hit_rate': hit_rate, 'qps': qps, 'slow': slow, 'aborted': aborted,
            'lock_waits': lock_waits, 'biggest': biggest,
            'long_running': long_running, 'findings': f}


# ── Redis ───────────────────────────────────────────────────────────────────

def _redis():
    import redis as redis_lib
    from django.conf import settings
    from urllib.parse import urlsplit

    url = os.environ.get('REDIS_URL', '').strip()
    if not url:
        return {'findings': [_finding(
            CRIT, 'REDIS_URL is not set', '',
            'The channel layer falls back to in-memory, so DMs break across '
            'workers, and the cache falls back to the database.')]}

    r = redis_lib.Redis.from_url(url, socket_timeout=2)
    info = r.info()
    used = info.get('used_memory', 0)
    maxmem = info.get('maxmemory', 0)
    policy = (r.config_get('maxmemory-policy') or {}).get('maxmemory-policy', '?')

    cache_db = urlsplit(settings.CACHES['default'].get('LOCATION', '')).path.lstrip('/') or '0'
    keys = {}
    for db in ('0', cache_db):
        try:
            keys[db] = redis_lib.Redis.from_url(url, db=int(db), socket_timeout=2).dbsize()
        except Exception:
            keys[db] = None

    f = [_finding(OK, 'Redis responding', f'{used / 2**20:.1f} MB in use')]
    if maxmem == 0 and policy == 'noeviction':
        f.append(_finding(
            NOTE, 'Redis has no memory limit',
            f'maxmemory 0, policy {policy}',
            'It will grow until the box runs out of RAM rather than evicting. '
            'Not urgent at this size, but worth setting a maxmemory.'))
    return {'used_mb': used / 2**20, 'maxmemory': maxmem, 'policy': policy,
            'keys': keys, 'cache_db': cache_db, 'findings': f}


# ── Transcode queue ─────────────────────────────────────────────────────────

def _queue():
    from django.utils import timezone as djtz
    from posts.models import PostMedia

    rows = {}
    for m in PostMedia.objects.values_list('status', flat=True):
        rows[m] = rows.get(m, 0) + 1

    pending = rows.get('pending', 0)
    processing = rows.get('processing', 0)
    failed = rows.get('failed', 0)

    oldest_age = None
    stuck = PostMedia.objects.filter(status__in=('pending', 'processing')).order_by('updated').first()
    if stuck is not None and getattr(stuck, 'updated', None):
        oldest_age = (djtz.now() - stuck.updated).total_seconds()

    f = []
    if oldest_age and oldest_age > 900:
        f.append(_finding(
            CRIT, 'Transcode queue is stuck',
            f'oldest item waiting {oldest_age / 60:.0f} min',
            'Uploads are not becoming playable. Restart the worker: '
            'sudo systemctl restart neat-transcode'))
    elif oldest_age and oldest_age > 300:
        f.append(_finding(WARN, 'Transcode queue backing up',
                          f'oldest item waiting {oldest_age / 60:.0f} min'))
    elif pending or processing:
        f.append(_finding(OK, 'Transcode queue moving',
                          f'{pending} pending, {processing} processing'))
    else:
        f.append(_finding(OK, 'Transcode queue empty', f'{rows.get("ready", 0)} ready'))

    if failed:
        f.append(_finding(NOTE, f'{failed} media failed to encode', '',
                          'These still serve the original upload, so nothing is '
                          'broken for users — but ffmpeg is rejecting something.'))

    # This is the load multiplier nobody expects: while ANY post in a loaded
    # feed is processing, every viewer looking at that feed re-fetches the whole
    # feed every 5 seconds for up to two minutes (home_page.dart).
    if processing:
        f.append(_finding(
            WARN, 'Feed polling is amplifying load',
            f'{processing} media processing',
            'While this is non-zero, every active viewer re-requests the full '
            'feed every 5s — roughly tripling per-user request rate. Expect '
            'higher load than the user count suggests.'))

    return {'counts': rows, 'pending': pending, 'processing': processing,
            'failed': failed, 'oldest_age_s': oldest_age, 'findings': f}


# ── Traffic, from the nginx access log ──────────────────────────────────────

_LOG_RE = re.compile(
    r'^(?P<ip>\S+) \S+ \S+ \[(?P<ts>[^\]]+)\] "(?P<req>[^"]*)" (?P<status>\d{3}) '
    r'(?P<bytes>\S+) "[^"]*" "[^"]*"(?: rt=(?P<rt>\S+))?(?: nc="(?P<nc>[^"]*)")?'
)


def _traffic():
    if not os.path.exists(ACCESS_LOG):
        return {'findings': [_finding(WARN, 'nginx access log not found', ACCESS_LOG)]}
    if not os.access(ACCESS_LOG, os.R_OK):
        return {'findings': [_finding(
            WARN, 'nginx access log is not readable', ACCESS_LOG,
            'Traffic and abuse detection are unavailable. Fix with: '
            'sudo usermod -aG adm bitnami && sudo systemctl restart gunicorn')]}

    size = os.path.getsize(ACCESS_LOG)
    with open(ACCESS_LOG, 'rb') as fh:
        if size > LOG_TAIL_BYTES:
            fh.seek(size - LOG_TAIL_BYTES)
            fh.readline()
        raw = fh.read().decode('utf-8', 'replace')

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=TRAFFIC_WINDOW_MIN)
    total = legacy = 0
    per_ip, per_min, rts, statuses = {}, {}, [], {}
    api_legacy_ips = {}
    # The lean/legacy split needs $http_x_neat_client in the log format. Until
    # nginx is emitting it, every feed request would parse as header-less and
    # raise a false alarm about a scraper, so the whole analysis is withheld
    # rather than guessed at.
    have_client_field = False

    for line in raw.splitlines():
        m = _LOG_RE.match(line)
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group('ts'), '%d/%b/%Y:%H:%M:%S %z')
        except ValueError:
            continue
        if ts < cutoff:
            continue
        total += 1
        ip = m.group('ip')
        per_ip[ip] = per_ip.get(ip, 0) + 1
        minute = ts.strftime('%H:%M')
        per_min[minute] = per_min.get(minute, 0) + 1
        code = m.group('status')
        statuses[code] = statuses.get(code, 0) + 1
        if m.group('rt'):
            try:
                rts.append(float(m.group('rt')))
            except ValueError:
                pass
        # The expensive path. Measured on this box 2026-09-04, same endpoint,
        # same moment: a lean request is 13 KB and 0.031s, the legacy one is
        # 980 KB and 0.335s — 74x the bytes and 11x the CPU. At that cost only
        # ~6 legacy req/s saturates both cores, so one scraper is enough.
        req = m.group('req') or ''
        nc = m.group('nc')
        if nc is not None:
            have_client_field = True
        if nc is not None and '/api/posts/' in req and req.startswith('GET'):
            try:
                modern = int(nc) >= 2 if nc else False
            except (TypeError, ValueError):
                modern = False
            if not modern:
                legacy += 1
                api_legacy_ips[ip] = api_legacy_ips.get(ip, 0) + 1

    window_s = TRAFFIC_WINDOW_MIN * 60
    rps = total / window_s if total else 0.0
    rts.sort()
    p50 = rts[len(rts) // 2] if rts else None
    p95 = rts[int(len(rts) * 0.95)] if rts else None
    top = sorted(per_ip.items(), key=lambda kv: -kv[1])[:8]
    top_legacy = sorted(api_legacy_ips.items(), key=lambda kv: -kv[1])[:5]

    f = []
    pct_ceiling = rps / MEASURED_CEILING_RPS * 100
    if pct_ceiling >= 80:
        f.append(_finding(
            CRIT, 'At the measured throughput ceiling',
            f'{rps:.1f} req/s of ~{MEASURED_CEILING_RPS:.0f} req/s',
            'Resize the instance — add vCPU, not RAM (CPU is the only wall, '
            'measured 2026-09-03).'))
    elif pct_ceiling >= 50:
        f.append(_finding(WARN, 'Traffic above half the ceiling',
                          f'{rps:.1f} req/s of ~{MEASURED_CEILING_RPS:.0f} req/s'))
    else:
        f.append(_finding(OK, 'Traffic comfortable',
                          f'{rps:.2f} req/s of ~{MEASURED_CEILING_RPS:.0f} req/s'))

    # One IP taking a large share is the cheapest problem to fix and the most
    # likely cause of a sudden slowdown, because /api/posts/ has no rate limit.
    if top and total > 60 and top[0][1] / total > 0.35:
        ip, n = top[0]
        f.append(_finding(
            CRIT, 'One IP dominates traffic', f'{ip} sent {n} of {total} requests',
            f'Almost certainly a scraper. Block it immediately:\n'
            f'  sudo iptables -I INPUT -s {ip} -j DROP\n'
            'That is reversible (-D instead of -I) and takes effect instantly.'))

    if not have_client_field and total:
        f.append(_finding(
            NOTE, 'Legacy-feed detection is off',
            'nginx is not logging X-Neat-Client',
            'Add the neat log_format to /etc/nginx/nginx.conf and reload to see '
            'which share of traffic is hitting the unauthenticated feed path.'))
    elif legacy and total and legacy / max(total, 1) > 0.15:
        who = ', '.join(f'{ip} ({n})' for ip, n in top_legacy) or 'unknown'
        f.append(_finding(
            CRIT, 'Unauthenticated legacy feed is being hit hard',
            f'{legacy} of {total} requests, from {who}',
            'Each costs ~11x a normal feed request (0.335s vs 0.031s, measured) '
            'and needs no login, so roughly 6 per second saturates the box. '
            'Block the IP above, and install the nginx rate limit.'))
    elif legacy:
        # A trickle is normal — old installs still exist. Only the ratio matters.
        f.append(_finding(NOTE, 'Some legacy feed requests',
                          f'{legacy} in {TRAFFIC_WINDOW_MIN} min'))

    errors = sum(n for code, n in statuses.items() if code.startswith('5'))
    if errors:
        f.append(_finding(
            CRIT if errors > 20 else WARN, f'{errors} server errors (5xx)',
            f'in the last {TRAFFIC_WINDOW_MIN} min',
            'Check: sudo journalctl -u gunicorn -n 100'))

    if p95 is not None and rts and p95 > 2.0:
        f.append(_finding(CRIT, 'Responses are slow', f'p95 {p95:.2f}s',
                          'Users are feeling this. See CPU and Traffic above.'))
    elif p95 is not None and p95 > 0.7:
        f.append(_finding(WARN, 'Responses slower than usual', f'p95 {p95:.2f}s'))

    return {'total': total, 'rps': rps, 'pct_ceiling': pct_ceiling, 'legacy': legacy,
            'have_client_field': have_client_field,
            'p50': p50, 'p95': p95, 'top_ips': top, 'statuses': statuses,
            'window_min': TRAFFIC_WINDOW_MIN, 'findings': f}


# ── TLS ─────────────────────────────────────────────────────────────────────

def _tls():
    """Expiry of the certificate nginx actually serves for the app's hostname.
    Read over a real handshake rather than off disk, so it reflects what a
    phone would get — and needs no privilege to do it."""
    from cryptography import x509

    # No verification: we want the certificate that is served, not a judgement
    # on it, and the pinned-IP vhost deliberately serves a self-signed one.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection(('127.0.0.1', 443), timeout=5) as sock:
        with ctx.wrap_socket(sock, server_hostname='neatapp.gr') as tls:
            der = tls.getpeercert(binary_form=True)

    expires = x509.load_der_x509_certificate(der).not_valid_after_utc
    days = (expires - datetime.now(timezone.utc)).days

    if days < 7:
        f = [_finding(CRIT, 'TLS certificate expiring', f'{days} days left',
                      'Renew now: sudo certbot renew. Never run certbot --nginx '
                      'here — it rewrites the server blocks and breaks the '
                      'pinned-IP vhost for older app builds.')]
    elif days < 21:
        f = [_finding(WARN, 'TLS certificate renewing soon', f'{days} days left',
                      'Auto-renewal should handle it; confirm it ran.')]
    else:
        f = [_finding(OK, 'TLS certificate healthy', f'{days} days left')]
    return {'days': days, 'expires': expires.isoformat(), 'findings': f}


# ── Growth and hygiene ──────────────────────────────────────────────────────

def _growth():
    from django.contrib.auth import get_user_model
    from django.utils import timezone as djtz
    from posts.models import Post

    since = djtz.now() - timedelta(hours=24)
    User = get_user_model()
    users = User.objects.count()
    new_users = User.objects.filter(date_joined__gte=since).count()
    posts = Post.objects.count()
    new_posts = Post.objects.filter(created__gte=since).count()

    f = [_finding(OK, 'Growth',
                  f'{users} users (+{new_users} in 24h), {posts} posts (+{new_posts})')]
    # During a campaign this is the number that says whether spend is working,
    # and it is also the leading indicator of load.
    if new_users > 300:
        f.append(_finding(
            WARN, 'Signups spiking', f'+{new_users} in 24h',
            'Load follows signups. Watch CPU above.'))
    return {'users': users, 'new_users': new_users, 'posts': posts,
            'new_posts': new_posts, 'findings': f}


def _hygiene():
    from django.conf import settings
    from django.utils import timezone as djtz

    f = []
    media_bytes = 0
    try:
        for root, _dirs, files in os.walk(settings.MEDIA_ROOT):
            for name in files:
                try:
                    media_bytes += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
    except Exception:
        pass

    staged = None
    try:
        from posts.models import StagedUpload
        staged = StagedUpload.objects.filter(
            created__lt=djtz.now() - timedelta(hours=24)).count()
        if staged > 50:
            f.append(_finding(
                WARN, f'{staged} abandoned staged uploads',
                'older than 24h',
                'Run: manage.py purge_staged_uploads (it should run daily).'))
    except Exception:
        pass

    f.append(_finding(OK, 'Media on disk', f'{media_bytes / 2**20:.0f} MB'))
    f.append(_finding(
        NOTE, 'MEDIA_ROOT is not backed up', 'no cron, no sync, no object storage',
        'Uploaded photos and videos exist only on this disk. An instance '
        'snapshot is the only copy. Worth knowing before you need it.'))
    return {'media_mb': media_bytes / 2**20, 'staged': staged, 'findings': f}


# ── Orchestration ───────────────────────────────────────────────────────────

PROBES = (
    ('system', _system), ('services', _services), ('traffic', _traffic),
    ('database', _database), ('redis', _redis), ('queue', _queue),
    ('tls', _tls), ('growth', _growth), ('hygiene', _hygiene),
)


def collect(use_cache=True):
    """Every section, plus an overall verdict and the actions worth taking.
    Cached briefly so refreshing the page cannot become the extra load that
    tips a struggling box over."""
    if use_cache:
        try:
            cached = cache.get(CACHE_KEY)
        except Exception:
            cached = None          # a cache that is down must not stop the page
        if cached:
            cached['cached'] = True
            return cached

    started = time.monotonic()
    sections = {name: _safe(fn, name) for name, fn in PROBES}

    findings = []
    for name, data in sections.items():
        for item in data.get('findings', []):
            findings.append({**item, 'section': name})
    findings.sort(key=lambda x: -_RANK.get(x['severity'], 0))

    worst = max((_RANK.get(f['severity'], 0) for f in findings), default=0)
    status = {0: OK, 1: WARN, 2: CRIT}[worst]
    actions = [f for f in findings
               if f['severity'] in (WARN, CRIT) and f['action']]

    snapshot = {
        'status': status,
        'headline': {
            OK: 'Everything is healthy.',
            WARN: 'Working, but something needs attention.',
            CRIT: 'Action needed now.',
        }[status],
        'sections': sections,
        'findings': findings,
        'actions': actions,
        'generated': datetime.now(timezone.utc).isoformat(),
        'took_ms': (time.monotonic() - started) * 1000,
        'cached': False,
    }
    try:
        cache.set(CACHE_KEY, snapshot, timeout=CACHE_SECONDS)
    except Exception:
        pass
    return snapshot


def liveness(strict=False):
    """(http_status, text) for an uptime monitor. Any monitor that alerts on a
    non-200 then alerts on real degradation, not merely on the box being off.
    strict=False escalates only on genuine failures; strict=True also escalates
    on warnings, for a second, more sensitive alarm."""
    try:
        snap = collect()
        status = snap['status']
    except Exception:
        return 503, 'error'
    if status == CRIT or (strict and status == WARN):
        titles = '; '.join(f['title'] for f in snap['findings']
                           if f['severity'] in (WARN, CRIT))[:300]
        return 503, f'{status}: {titles}'
    return 200, status
