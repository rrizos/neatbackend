import mimetypes
import os
from django.conf import settings
from django.http import FileResponse, HttpResponse, Http404


def serve_media(request, path):
    """
    Range-aware media file server.

    Django's built-in static() serve view returns plain 200 responses with no
    Accept-Ranges header, which causes Android's ExoPlayer to fail on video
    files (it requires HTTP 206 Partial Content support to buffer/seek).

    This view adds full range-request support so ExoPlayer can play videos.
    """
    safe_path = os.path.normpath(path).lstrip('/')
    if safe_path.startswith('..'):
        raise Http404

    full_path = os.path.join(settings.MEDIA_ROOT, safe_path)
    if not os.path.isfile(full_path):
        raise Http404

    content_type, _ = mimetypes.guess_type(full_path)
    content_type = content_type or 'application/octet-stream'
    file_size = os.path.getsize(full_path)

    range_header = request.META.get('HTTP_RANGE')

    if range_header:
        # Parse "bytes=start-end"
        try:
            range_spec = range_header.strip().replace('bytes=', '')
            start_str, end_str = range_spec.split('-')
            start = int(start_str) if start_str else 0
            end = int(end_str) if end_str else file_size - 1
        except (ValueError, AttributeError):
            return HttpResponse(status=416)  # Range Not Satisfiable

        if start >= file_size or end >= file_size or start > end:
            response = HttpResponse(status=416)
            response['Content-Range'] = f'bytes */{file_size}'
            return response

        length = end - start + 1
        f = open(full_path, 'rb')
        f.seek(start)
        data = f.read(length)
        f.close()

        response = HttpResponse(data, status=206, content_type=content_type)
        response['Content-Range'] = f'bytes {start}-{end}/{file_size}'
        response['Content-Length'] = str(length)
        response['Accept-Ranges'] = 'bytes'
        response['Access-Control-Allow-Origin'] = '*'
        return response

    # Full file — still advertise range support so ExoPlayer knows it can seek
    response = FileResponse(open(full_path, 'rb'), content_type=content_type)
    response['Content-Length'] = str(file_size)
    response['Accept-Ranges'] = 'bytes'
    response['Access-Control-Allow-Origin'] = '*'
    return response
