"""Turning an uploaded video into something worth serving.

This used to live in `posts/views.py` and run inside the POST request. Encoding
is the most expensive thing this box does -- 18 seconds for a minute of 1080p on
two vCPUs, ~55 for three minutes -- and doing it inline held one of gunicorn's
request slots for the whole time. Twelve simultaneous uploads occupied every
slot and the site answered nobody.

So the encode moved here, and `manage.py transcode_worker` is the only thing
that calls it. The request now saves the file, marks the row PENDING and
returns. What the reader sees in between is `PostMedia.status`.
"""

import json
import logging
import os
import subprocess
import time
import uuid

from django.conf import settings
from django.core.files.storage import default_storage

logger = logging.getLogger(__name__)

# Give up on a single encode well before anything else does. Nothing kills the
# worker at a deadline any more, but a runaway ffmpeg would otherwise hold the
# queue forever, and one stuck video must not stop every video behind it.
TRANSCODE_TIMEOUT_SECONDS = 900

# What a file has to already be for the encode to be skipped entirely. These
# mirror _ffmpeg_command exactly -- if you change one, change the other.
_TARGET_MAX_EDGE = 1920
# Above the 5000k encode ceiling with room for VBR overshoot, so a file the
# phone produced at the target is passed through rather than re-encoded. Too
# high and oversized files slip through; too low and nothing ever takes the
# fast path, which is what happened when this and the app disagreed.
_TARGET_MAX_BITRATE = 6_000_000


def probe(src):
    """What ffprobe says about a file, or None if it cannot be read."""
    try:
        out = subprocess.run(
            [
                'ffprobe', '-v', 'error',
                '-show_entries',
                'stream=codec_name,codec_type,width,height,pix_fmt:format=duration,bit_rate',
                '-of', 'json', src,
            ],
            check=True, capture_output=True, timeout=30,
        ).stdout
        data = json.loads(out)
    except Exception as exc:
        logger.info('ffprobe failed for %s: %s', src, exc)
        return None

    video = next(
        (s for s in data.get('streams', []) if s.get('codec_type') == 'video'), None
    )
    if video is None:
        return None
    fmt = data.get('format', {})

    def num(value, default=0):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    return {
        'codec': video.get('codec_name', ''),
        'pix_fmt': video.get('pix_fmt', ''),
        'width': int(num(video.get('width'))),
        'height': int(num(video.get('height'))),
        'duration': num(fmt.get('duration')),
        'bitrate': num(fmt.get('bit_rate')),
    }


def needs_reencode(info):
    """Whether this file actually has to be re-encoded, or merely repackaged.

    Since the app started compressing on the phone, most uploads arrive already
    H.264, already small, already within the size cap -- and the server was
    re-encoding them anyway, which is 20 seconds of "processing" spent to
    produce a near-identical file. Anything that already matches the target gets
    a remux instead: same bytes, new container, about a second.
    """
    if info is None:
        return True  # unreadable: let the full encode deal with it
    if info['codec'] != 'h264':
        return True
    if info['pix_fmt'] not in ('yuv420p', 'yuvj420p'):
        return True
    if max(info['width'], info['height']) > _TARGET_MAX_EDGE:
        return True
    # An unknown bitrate is not a pass; it usually means a container we cannot
    # reason about, and guessing wrong here ships an oversized file forever.
    if not info['bitrate'] or info['bitrate'] > _TARGET_MAX_BITRATE:
        return True
    return False


def _remux_command(src, dst):
    """Repackage without touching the pixels. Seconds, not tens of seconds."""
    return [
        'nice', '-n', '15',
        'ffmpeg', '-y', '-i', src,
        '-c', 'copy',
        # The one thing worth doing even to an already-fine file: move the index
        # to the front so playback can start before the whole file arrives.
        '-movflags', '+faststart',
        dst,
    ]


def _ffmpeg_command(src, dst):
    return [
        # `nice`, so the encoder yields to anything serving a request. It will
        # still use both vCPUs when they are idle; the moment a web request
        # wants CPU, the scheduler prefers it. An upload should cost the
        # uploader time, not everybody else.
        'nice', '-n', '15',
        # -progress writes machine-readable key=value blocks to stdout, which is
        # what turns "processing" into a percentage. -nostats keeps the usual
        # human progress line off stderr so it is not competing with it.
        'ffmpeg', '-y', '-nostats', '-progress', 'pipe:1', '-i', src,
        # veryfast trades a little compression efficiency for a large cut in
        # encode CPU time, which is the scarce resource here.
        # CRF 23 rather than 25: at 1080p the extra detail is the whole point,
        # and 25 leaves visible blocking on high-motion phone footage.
        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23',
        # Fit within 1920x1920, keep aspect, round to even dimensions. Phone
        # screens are 1170px wide and up, so 960 was visibly soft on the device
        # it was being watched on. 1080p in either orientation (1920x1080 or
        # 1080x1920) is 8160 macroblocks, still inside H.264 Level 4.0's 8192 —
        # which is what keeps Android 7+ hardware decoders reliable (Level 5.0
        # causes runtime crashes even when reported as supported).
        '-vf', 'scale=1920:1920:force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2',
        # Phones shoot 4:2:0, but a screen recording or a desktop upload can
        # arrive in another subsampling, which -profile:v high rejects outright
        # ("high profile doesn't support 4:4:4"). Convert rather than fail.
        '-pix_fmt', 'yuv420p',
        '-profile:v', 'high', '-level', '4.0',
        # Hard bitrate ceiling so a high-motion clip can't blow past what a feed
        # video needs -- bounds worst-case file size, and therefore the
        # bandwidth of serving it, regardless of content. 5000k is roughly what
        # 1080p needs to not fall apart in motion; below ~4000k it does.
        '-maxrate', '5000k', '-bufsize', '10000k',
        '-c:a', 'aac', '-b:a', '128k',
        # Moves the index to the front so playback can start before the whole
        # file has arrived.
        '-movflags', '+faststart',
        dst,
    ]


def _poster_command(src, dst, seek_seconds):
    """A single frame, for anywhere a video is shown before it is played.

    [seek_seconds] of 0 means "the very first frame"; anything higher is passed
    as -ss *before* -i, which seeks without decoding everything up to it.
    """
    seek = ['-ss', str(seek_seconds)] if seek_seconds else []
    return [
        'nice', '-n', '15',
        'ffmpeg', '-y',
        *seek,
        '-i', src,
        # -update tells the image muxer this is one file, not a numbered
        # sequence; without it ffmpeg warns on every single extraction.
        '-frames:v', '1', '-update', '1',
        '-vf', 'scale=720:720:force_original_aspect_ratio=decrease',
        '-q:v', '4',
        dst,
    ]


def generate_poster(src_abs):
    """Write a poster frame next to [src_abs]. Returns its media path or ''.

    Never raises. A missing poster costs a nicer thumbnail, not the video.
    """
    dst_rel = f'posts/{uuid.uuid4()}.jpg'
    dst_abs = os.path.join(settings.MEDIA_ROOT, dst_rel)

    # A second in first, then the very first frame. The fallback matters more
    # than it looks: a clip shorter than the seek point yields no frame at all,
    # and so does one whose container declares no duration to seek within.
    for seek in (1, 0):
        try:
            subprocess.run(
                _poster_command(src_abs, dst_abs, seek),
                check=True, capture_output=True, timeout=60,
            )
            # ffmpeg exits 0 having written nothing when the seek lands past
            # the end, so success has to be judged on the file, not the code.
            if os.path.exists(dst_abs) and os.path.getsize(dst_abs) > 0:
                return default_storage.url(dst_rel)
            _discard(dst_abs)
        except Exception as exc:
            logger.info('poster attempt at %ss failed for %s: %s', seek, src_abs, exc)
            _discard(dst_abs)
    return ''



def _run_with_progress(command, duration, on_progress):
    """Run ffmpeg, reporting how far through the clip it is.

    ffmpeg will emit machine-readable progress on stdout given `-progress
    pipe:1`; each block carries `out_time_ms`, which against the known duration
    is a percentage. Without this the app can only say "processing" and hope the
    user waits — and a silent twenty seconds reads as broken.

    [on_progress] is called with 0-100. Falls back to no reporting (but still a
    correct encode) when the duration is unknown.
    """
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    deadline = time.monotonic() + TRANSCODE_TIMEOUT_SECONDS
    last_sent = -1
    try:
        for line in proc.stdout:
            if time.monotonic() > deadline:
                proc.kill()
                raise subprocess.TimeoutExpired(command, TRANSCODE_TIMEOUT_SECONDS)
            if not line.startswith('out_time_ms=') or duration <= 0:
                continue
            try:
                done = int(line.split('=', 1)[1].strip()) / 1_000_000.0
            except ValueError:
                continue
            percent = int(max(0, min(99, done / duration * 100)))
            # Whole steps of 5 only: this writes to the database, and the app
            # is polling every few seconds, so finer resolution is invisible
            # and just costs writes.
            if percent >= last_sent + 5:
                last_sent = percent
                on_progress(percent)
        proc.wait(timeout=max(1, int(deadline - time.monotonic())))
    finally:
        if proc.poll() is None:
            proc.kill()
        stderr = proc.stderr.read() if proc.stderr else ''
        try:
            proc.stdout.close()
            proc.stderr.close()
        except Exception:
            pass

    if proc.returncode != 0:
        tail = ' | '.join((stderr or '').strip().splitlines()[-4:])
        raise subprocess.CalledProcessError(proc.returncode, command, stderr=tail.encode())

def transcode_media(media):
    """Re-encode one PostMedia's video to a *new* file and repoint it.

    Returns True when `media.url` now names a freshly encoded file.

    Deliberately not in-place. The original is already public at its own URL
    and nginx serves /media/ as `immutable`, so a client that fetched it during
    the queue wait is entitled to cache it for a year -- rewriting those bytes
    underneath them would leave some people on the un-encoded copy forever.
    A new name means the old URL keeps its old content and simply stops being
    referenced.
    """
    src_rel = media.url[len(settings.MEDIA_URL):].lstrip('/')
    src_abs = os.path.join(settings.MEDIA_ROOT, src_rel)
    if not os.path.exists(src_abs):
        raise FileNotFoundError(src_abs)

    dst_rel = f'posts/{uuid.uuid4()}.mp4'
    dst_abs = os.path.join(settings.MEDIA_ROOT, dst_rel)
    os.makedirs(os.path.dirname(dst_abs), exist_ok=True)

    info = probe(src_abs)
    reencode = needs_reencode(info)
    duration = (info or {}).get('duration', 0)

    def report(percent):
        # Written straight to the row the client is polling. update() rather
        # than save() so this cannot race the status field alongside it.
        type(media).objects.filter(pk=media.pk).update(progress=percent)

    try:
        if reencode:
            _run_with_progress(
                _ffmpeg_command(src_abs, dst_abs), duration, report,
            )
        else:
            # Already what we would have produced: repackage and move on.
            logger.info('remuxing %s (already conformant)', media.url)
            subprocess.run(
                _remux_command(src_abs, dst_abs),
                check=True, capture_output=True, timeout=120,
            )
    except subprocess.CalledProcessError as exc:
        # ffmpeg says why on stderr; without this the log just says "exit 1".
        detail = exc.stderr
        if isinstance(detail, bytes):
            detail = detail.decode('utf-8', 'replace')
        logger.error('ffmpeg failed for %s: %s', media.url, (detail or '').strip()[-400:])
        _discard(dst_abs)
        raise
    except Exception:
        _discard(dst_abs)
        raise

    old_url = media.url
    media.url = default_storage.url(dst_rel)
    # From the re-encoded file, so the poster matches what people actually see.
    media.thumb_url = generate_poster(dst_abs)
    media.progress = 100
    media.save(update_fields=[
        'url', 'thumb_url', 'status', 'attempts', 'progress', 'updated',
    ])

    # Only once the row points at the new file, so a crash mid-way leaves the
    # original in place rather than a row referencing something deleted.
    _discard(os.path.join(settings.MEDIA_ROOT, old_url[len(settings.MEDIA_URL):].lstrip('/')))
    logger.info('transcoded %s -> %s', old_url, media.url)
    return True


def _discard(path):
    try:
        if path and os.path.exists(path):
            os.unlink(path)
    except Exception:
        logger.warning('could not remove %s', path, exc_info=True)
