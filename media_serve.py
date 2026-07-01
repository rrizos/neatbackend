import mimetypes
import os
from django.conf import settings
from django.http import HttpResponse, HttpResponseNotModified, Http404, StreamingHttpResponse
from django.utils.http import http_date

# Chunk size for streamed reads. Small enough to keep per-request memory low,
# large enough to avoid excessive syscall overhead for big video files.
_CHUNK_SIZE = 512 * 1024


def _file_iterator(full_path, start, length):
    with open(full_path, 'rb') as f:
        f.seek(start)
        remaining = length
        while remaining > 0:
            chunk = f.read(min(_CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def serve_media(request, path):
    """
    Range-aware media file server.

    Django's built-in static() / FileResponse have no support for the Range
    header, which Android's ExoPlayer and iOS's AVPlayer both require to
    buffer/seek video (they expect HTTP 206 Partial Content). This view adds
    that support itself, streaming from disk in chunks rather than buffering
    a whole range in memory, and marks responses as long-lived + immutable so
    that clients and any CDN/proxy in front of this server stop re-fetching
    media they already have — post/comment/message media files are written
    once under a content-addressed (uuid4) filename and never modified
    afterwards, so this is always safe.
    """
    safe_path = os.path.normpath(path).lstrip('/')
    if safe_path.startswith('..'):
        raise Http404

    full_path = os.path.join(settings.MEDIA_ROOT, safe_path)
    if not os.path.isfile(full_path):
        raise Http404

    content_type, _ = mimetypes.guess_type(full_path)
    content_type = content_type or 'application/octet-stream'
    stat = os.stat(full_path)
    file_size = stat.st_size
    last_modified = http_date(stat.st_mtime)
    etag = f'"{int(stat.st_mtime_ns):x}-{file_size:x}"'

    def _base_headers(response):
        response['Accept-Ranges'] = 'bytes'
        response['Access-Control-Allow-Origin'] = '*'
        response['Cache-Control'] = 'public, max-age=31536000, immutable'
        response['Last-Modified'] = last_modified
        response['ETag'] = etag
        return response

    if_none_match = request.META.get('HTTP_IF_NONE_MATCH')
    if_modified_since = request.META.get('HTTP_IF_MODIFIED_SINCE')
    if (if_none_match and if_none_match == etag) or (
        if_modified_since and if_modified_since == last_modified
    ):
        return _base_headers(HttpResponseNotModified())

    range_header = request.META.get('HTTP_RANGE')

    if range_header:
        try:
            range_spec = range_header.strip().replace('bytes=', '')
            start_str, end_str = range_spec.split('-')
            start = int(start_str) if start_str else 0
            # Clamp an out-of-range/omitted end to the last byte instead of
            # rejecting — some players send an end far past EOF expecting it
            # to be clamped, per RFC 7233.
            end = min(int(end_str), file_size - 1) if end_str else file_size - 1
        except (ValueError, AttributeError):
            return HttpResponse(status=416)

        if file_size == 0 or start >= file_size or start > end:
            response = HttpResponse(status=416)
            response['Content-Range'] = f'bytes */{file_size}'
            return response

        length = end - start + 1
        response = StreamingHttpResponse(
            _file_iterator(full_path, start, length),
            status=206,
            content_type=content_type,
        )
        response['Content-Range'] = f'bytes {start}-{end}/{file_size}'
        response['Content-Length'] = str(length)
        return _base_headers(response)

    # No Range header — stream the whole file (still chunked, not buffered).
    response = StreamingHttpResponse(
        _file_iterator(full_path, 0, file_size),
        content_type=content_type,
    )
    response['Content-Length'] = str(file_size)
    return _base_headers(response)
