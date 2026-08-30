import datetime
import json
import logging
import os
import re
import uuid
from PIL import Image, UnidentifiedImageError
from django.conf import settings
from django.core.files.storage import default_storage
from django.db import connection
from django.http import JsonResponse, HttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from accounts.auth import get_authenticated_user, require_authenticated_user
from linkpreview import service as linkpreview_service
from accounts.models import Follow, Notification, blocked_user_ids, is_blocked
from accounts.serializers import user_to_dict
from django.db.models import Count, ExpressionWrapper, F, FloatField, Q
from .images import store_comment_image, store_comment_image_data
from .models import (
    Post, PostComment, PostLike, PostSave, CommentLike, PostMedia, PostReport, CommentReport,
    Poll, PollOption, PollVote, StagedUpload,
)

logger = logging.getLogger(__name__)

# Reject oversized uploads before spending disk/CPU on them. The client's
# declared "type" is what selects between these — it used to only apply the
# cap when type=="video", leaving images completely unbounded.
_MAX_VIDEO_UPLOAD_BYTES = 150 * 1024 * 1024
_MAX_IMAGE_UPLOAD_BYTES = 20 * 1024 * 1024


def _cors_json(response):
    response["Access-Control-Allow-Origin"] = "*"
    response["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response["Access-Control-Allow-Methods"] = "GET,POST,DELETE,OPTIONS"
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    return response


def _get_post_or_404(post_id):
    try:
        return Post.objects.get(pk=post_id)
    except Post.DoesNotExist:
        return None


def _unauthorized():
    return _cors_json(JsonResponse({"error": "Authentication required"}, status=401))


def _preview_map_for(posts):
    """Already-resolved link cards for a page of posts, in one query.

    Never fetches — a feed must not block on outbound requests. A link nobody
    has resolved yet is simply missing here and the client asks for that one
    itself; everything already known ships with the feed instead of costing a
    round trip per row.
    """
    try:
        return linkpreview_service.previews_for_texts(
            [(p.text or "") for p in posts]
        )
    except Exception:
        # Previews are decoration; a feed must render without them.
        return {}


# ── Feed payload ──────────────────────────────────────────────────────────────
#
# A city's feed used to come back whole: every post ever made there, each with
# its full comment threads, and a base64 avatar repeated for the author of
# every post and every comment. The same person's picture could be in the
# response fifty times.
#
# Clients that identify themselves (see dm_messages/views.py for the same
# header) get a paged feed instead, with comments left to the comment sheet —
# which already fetches them on open — and each avatar sent once, by username.
# Anything without the header is an older build and still gets the old shape,
# down to the bare JSON list.

_FEED_PAGE_SIZE = 20
_FEED_PAGE_MAX = 60
# Hard ceiling for the unpaginated legacy feed. Not a page size -- there is no
# second page for those clients -- but a bound on the worst a single request
# can cost.
_LEGACY_FEED_CAP = 50


def _wants_lean_feed(request):
    try:
        return int(request.headers.get('X-Neat-Client', '1')) >= 2
    except (TypeError, ValueError):
        return False


def _feed_page_limit(request):
    try:
        limit = int(request.GET.get('limit', _FEED_PAGE_SIZE))
    except (TypeError, ValueError):
        return _FEED_PAGE_SIZE
    return max(1, min(limit, _FEED_PAGE_MAX))


def _lean_feed_payload(posts, viewer, viewer_following_ids, preview_map):
    """Rows with each author's avatar lifted out into a lookup table."""
    avatars = {}
    rows = []
    page_ctx = _viewer_page_context(posts, viewer, viewer_following_ids)
    for post in posts:
        data = _post_to_dict(
            post,
            viewer=viewer,
            viewer_following_ids=viewer_following_ids,
            light=True,
            preview_map=preview_map,
            page_ctx=page_ctx,
        )
        author = data.get('author') or ''
        avatar = data.get('avatarUrl') or ''
        if author and avatar:
            avatars[author] = avatar
        data['avatarUrl'] = ''
        rows.append(data)
    return rows, avatars



def _viewer_page_context(posts, viewer, viewer_following_ids):
    """Everything per-viewer for a whole page, in a fixed number of queries.

    `_post_to_dict` used to ask the database three separate questions about
    *each* post — have I liked it, have I saved it, which of the people I
    follow liked it — plus a COUNT for its likes and, for a poll, a vote lookup
    per option. Measured: 27 queries for a 4-post page and 75 for a 20-post
    page, so three per row on top of a fixed base. That is the whole reason a
    feed request cost ~130ms of CPU, and CPU is what caps the box at ~17
    requests a second.

    Returns None for an anonymous viewer, who has none of these.
    """
    ids = [p.id for p in posts]
    if not ids or not (viewer and viewer.is_authenticated):
        return None

    liked = set(
        PostLike.objects.filter(post_id__in=ids, user=viewer)
        .values_list('post_id', flat=True)
    )
    saved = set(
        PostSave.objects.filter(post_id__in=ids, user=viewer)
        .values_list('post_id', flat=True)
    )

    # Who, among the people this viewer follows, liked each post. One query for
    # the page; the per-post cap of three names is applied in Python.
    by_following = {}
    if viewer_following_ids:
        rows = (
            PostLike.objects
            .filter(post_id__in=ids, user_id__in=viewer_following_ids)
            .select_related('user')
            .order_by('post_id', 'created')
            .values_list('post_id', 'user__username')
        )
        for post_id, username in rows:
            names = by_following.setdefault(post_id, [])
            if len(names) < 3:
                names.append(username)

    # Poll votes for the page, keyed by poll.
    poll_ids = [p.poll.id for p in posts if getattr(p, 'poll', None) is not None]
    votes = {}
    if poll_ids:
        votes = dict(
            PollVote.objects.filter(poll_id__in=poll_ids, user=viewer)
            .values_list('poll_id', 'option_id')
        )

    return {
        'liked': liked,
        'saved': saved,
        'liked_by_following': by_following,
        'poll_votes': votes,
    }

def _post_to_dict(post, viewer=None, viewer_following_ids=None, light=False,
                  with_link_preview=False, preview_map=None, page_ctx=None):
    data = post.to_dict()
    # Two ways in. `preview_map` is the list path: already-known cards, looked
    # up in one query for the whole page, never fetched. `with_link_preview` is
    # the single-post path, where resolving on the spot is worth it because a
    # shared permalink has to preview correctly the first time anyone opens it.
    if preview_map is not None:
        url = linkpreview_service.first_url(data.get("text") or "")
        if url:
            preview = preview_map.get(linkpreview_service.normalise_url(url))
            if preview:
                data["link_preview"] = preview
    elif with_link_preview:
        try:
            preview = linkpreview_service.preview_for_text(data.get("text") or "")
        except Exception:
            preview = None
        if preview:
            data["link_preview"] = preview
    # `light` mode (used by the viral/charts list) skips serializing the full
    # comment threads -- the charts cards only render the comment *count*, and
    # viral posts are the most-commented ones, so shipping every comment + reply
    # is a huge wasted payload. The count is sent instead; the comment sheet
    # lazy-loads the real thread from the post-detail endpoint when opened.
    if light:
        count = getattr(post, "comment_count", None)
        if count is None:
            count = post.comment_rows.count()
        data["comment_count"] = count
        data["comments"] = []
    else:
        row_comments = list(
            post.comment_rows
            .filter(parent__isnull=True)
            .select_related("user")
            .prefetch_related("comment_likes", "replies__user", "replies__comment_likes")
            .order_by("-pinned", "created")
        )
        if row_comments:
            data["comments"] = [comment.to_dict(viewer=viewer, owner_id=post.user_id) for comment in row_comments]
        data["comment_count"] = len(row_comments)
    # len() over the prefetched rows, not .count(): a related manager's
    # .count() ignores the prefetch cache and issues its own COUNT per post.
    data["likes"] = len(post.like_rows.all()) or post.likes
    data["shares"] = post.shares
    poll = getattr(post, "poll", None)
    if poll is not None:
        voted_option_id = None
        if viewer and viewer.is_authenticated:
            if page_ctx is not None:
                voted_option_id = page_ctx['poll_votes'].get(poll.id)
            else:
                vote = PollVote.objects.filter(poll=poll, user=viewer).first()
                voted_option_id = vote.option_id if vote else None
        data["poll"] = {
            "id": poll.id,
            "options": [
                {"id": o.id, "text": o.text, "votes": len(o.votes_rows.all())}
                for o in poll.options.all()
            ],
            "voted_option_id": voted_option_id,
        }
    data["liked"] = False
    data["saved"] = False
    data["likedByFollowing"] = []
    if viewer and viewer.is_authenticated:
        data["following"] = post.user_id == viewer.id or post.user_id is not None
        if page_ctx is not None:
            # Answered once for the whole page — see _viewer_page_context.
            data["liked"] = post.id in page_ctx['liked']
            data["saved"] = post.id in page_ctx['saved']
            data["likedByFollowing"] = page_ctx['liked_by_following'].get(post.id, [])
        else:
            # Single-post path (post detail), where one row's worth of queries
            # is the whole request rather than one of twenty.
            data["liked"] = PostLike.objects.filter(post=post, user=viewer).exists()
            data["saved"] = PostSave.objects.filter(post=post, user=viewer).exists()
            if viewer_following_ids is None:
                viewer_following_ids = set(
                    Follow.objects.filter(follower=viewer).values_list('following_id', flat=True)
                )
            liked_by_following = list(
                PostLike.objects.filter(post=post, user_id__in=viewer_following_ids)
                .select_related('user')
                .order_by('created')[:3]
            )
            data["likedByFollowing"] = [pl.user.username for pl in liked_by_following]
    else:
        data["following"] = post.user_id is not None

    # Verified badge for the post author
    try:
        data["authorVerified"] = bool(post.user_id and post.user and getattr(getattr(post.user, 'profile', None), 'is_verified', False))
    except Exception:
        data["authorVerified"] = False

    # Media items (prefetched when called from feed queries)
    media_qs = list(post.media_items.all())
    if media_qs:
        data["media"] = [
            {
                "type": m.media_type,
                "url": m.url,
                "duration": m.duration,
                # '' until the worker has produced one, and for every video
                # that predates posters — clients fall back to their old
                # behaviour when it is empty.
                "thumbUrl": m.thumb_url,
                # Clients that know this key draw a processing tile while a
                # video is queued; ones that don't play `url`, which is the
                # original upload and works. Only ever "processing" or absent,
                # so a failed transcode reads as an ordinary playable video.
                "status": "processing" if m.is_processing else "ready",
                "progress": m.progress if m.is_processing else 100,
            }
            for m in media_qs
        ]
    elif data.get("imageUrl"):
        # Backward-compat: old single-image posts surface as a one-item media array
        data["media"] = [
            {"type": "image", "url": data["imageUrl"], "duration": None, "status": "ready"}
        ]
    else:
        data["media"] = []

    # In light mode (charts), the client renders from `media`, so a populated
    # `imageUrl` is a redundant copy of the same (potentially large base64)
    # image -- drop it to avoid shipping the picture twice.
    if light and data["media"] and data.get("imageUrl"):
        data["imageUrl"] = ""

    return data


def _notify(recipient, actor, verb, post, comment=None):
    if recipient == actor or recipient is None:
        return
    Notification.objects.create(
        recipient=recipient,
        actor=actor,
        verb=verb,
        target_type='post',
        target_id=str(post.id),
        target_comment_id=str(comment.id) if comment is not None else '',
        target_text=post.text[:255],
    )


_MENTION_RE = re.compile(r'@([\w.]+)')


def _notify_mentions(text, actor, city, post, verb='mentioned you in a post', comment=None):
    """Notify @mentioned users, restricted to people in the same city as the
    post they're being tagged into — mentioning is a hyperlocal-only action.
    """
    usernames = set(_MENTION_RE.findall(text or ''))
    if not usernames:
        return
    from django.contrib.auth import get_user_model
    User = get_user_model()
    mentioned = User.objects.filter(username__in=usernames).select_related('profile').exclude(pk=actor.pk)
    for user in mentioned:
        if getattr(getattr(user, 'profile', None), 'city', '') != city:
            continue
        Notification.objects.create(
            recipient=user,
            actor=actor,
            verb=verb,
            target_type='post',
            target_id=str(post.id),
            target_comment_id=str(comment.id) if comment is not None else '',
            target_text=text[:255],
        )


def _ensure_posts_table():
    table_name = Post._meta.db_table
    with connection.cursor() as cursor:
        existing_tables = connection.introspection.table_names(cursor)
    if table_name in existing_tables:
        return

    with connection.schema_editor() as schema_editor:
        schema_editor.create_model(Post)


@csrf_exempt
@require_http_methods(["GET", "OPTIONS"])
def post_detail(request, post_id):
    if request.method == "OPTIONS":
        return _cors_json(HttpResponse())

    _ensure_posts_table()
    viewer = get_authenticated_user(request)
    post = _get_post_or_404(post_id)
    if post is None:
        return _cors_json(JsonResponse({"error": "Not found"}, status=404))
    if viewer and is_blocked(viewer, post.user):
        return _cors_json(JsonResponse({"error": "Not found"}, status=404))

    data = _post_to_dict(post, viewer=viewer, with_link_preview=True)
    return _cors_json(JsonResponse(data))


@csrf_exempt
@require_http_methods(["GET", "OPTIONS"])
def cities_list(request):
    if request.method == "OPTIONS":
        return _cors_json(HttpResponse())

    _ensure_posts_table()
    viewer = get_authenticated_user(request)
    viewer_city = getattr(getattr(viewer, "profile", None), "city", "") if viewer else ""
    cities = list(
        Post.objects.exclude(city='')
        .values_list('city', flat=True)
        .distinct()
        .order_by('city')
    )
    if viewer_city and viewer_city not in cities:
        cities.insert(0, viewer_city)
    return _cors_json(JsonResponse({"cities": cities}))


def _viral_period_start(period):
    """Calendar-aligned period start (today/this week's Monday/this month),
    computed in the server's UTC clock. This can be a few hours off from a
    viewer's local midnight, but that only fuzzes which borderline posts are
    included right at the edge of the window -- it doesn't affect the
    ranking of posts safely inside it, and this app has no per-user
    timezone stored anywhere to do better than that."""
    now = timezone.now()
    if period == "daily":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "monthly":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    monday = now - datetime.timedelta(days=now.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


@csrf_exempt
@require_http_methods(["GET", "OPTIONS"])
def viral_posts(request):
    """Top-10 "charts" ranking for the search page. Ranking (score = likes*0.45
    + comment_count*0.55, scaled) and the period/city filtering all happen in
    SQL here instead of the client downloading the whole city feed and
    sorting it locally on every load -- that used to transfer and re-rank the
    entire city's post history just to show the top 10."""
    if request.method == "OPTIONS":
        return _cors_json(HttpResponse())

    _ensure_posts_table()
    viewer = get_authenticated_user(request)
    city = (request.GET.get("city") or "").strip()
    # The charts scope is a choice of two: the viewer's own city, or everywhere
    # else. "Everywhere else" is an exclusion rather than an empty city filter,
    # which would otherwise fold the viewer's own city back into the results.
    exclude_city = (request.GET.get("exclude_city") or "").strip()
    period = (request.GET.get("period") or "weekly").strip().lower()
    if period not in ("daily", "weekly", "monthly"):
        period = "weekly"
    # Opt-in lightweight payload: only clients that know how to read
    # `comment_count` and lazy-load threads on demand send `light=1`. Older
    # installed apps omit it and keep getting the full payload, so deploying
    # this can't break their charts comments before they update.
    light = (request.GET.get("light") or "").strip() in ("1", "true")

    # In light mode the comment threads aren't serialized, so don't prefetch
    # every comment + author just to discard them; the count comes from the
    # SQL annotation below.
    prefetch = ["like_rows", "media_items"]
    if not light:
        prefetch.insert(0, "comment_rows__user")
    posts = Post.objects.select_related("user", "user__profile").prefetch_related(*prefetch)
    if city:
        posts = posts.filter(city=city)
    if exclude_city:
        posts = posts.exclude(city=exclude_city)
    if viewer and viewer.is_authenticated:
        posts = posts.exclude(user_id__in=blocked_user_ids(viewer))

    posts = (
        posts.filter(created__gte=_viral_period_start(period))
        .annotate(comment_count=Count("comment_rows", distinct=True))
        .annotate(
            score=ExpressionWrapper(
                (F("likes") * 0.45 + F("comment_count") * 0.55) * 100,
                output_field=FloatField(),
            )
        )
        .order_by("-score", "-created")[:10]
    )

    viewer_following_ids = None
    if viewer and viewer.is_authenticated:
        viewer_following_ids = set(
            Follow.objects.filter(follower=viewer).values_list('following_id', flat=True)
        )
    preview_map = _preview_map_for(posts)
    _legacy_ctx = _viewer_page_context(posts, viewer, viewer_following_ids)
    data = [_post_to_dict(p, viewer=viewer, viewer_following_ids=viewer_following_ids,
                          page_ctx=_legacy_ctx,
                          light=light, preview_map=preview_map) for p in posts]
    return _cors_json(JsonResponse(data, safe=False))




@csrf_exempt
@require_http_methods(['POST', 'OPTIONS'])
def stage_upload(request):
    """Accept one file now, so the post that uses it can be instant later.

    `POST /api/posts/upload/` with a `file` part -> `{"id": ..., "type": ...}`.
    The client calls this the moment a photo or video is picked, while the user
    is still writing the caption; `posts_list` then takes the id instead of the
    bytes.

    A video is queued for transcoding exactly as it would be if it had arrived
    with the post, so the encode is usually finished too by the time the post
    exists.
    """
    if request.method == 'OPTIONS':
        return _cors_json(HttpResponse())
    user = require_authenticated_user(request)
    if user is None:
        return _unauthorized()

    uploaded = request.FILES.get('file')
    if uploaded is None:
        return _cors_json(JsonResponse({'error': 'No file'}, status=400))

    media_type = (request.POST.get('type') or 'image').strip()
    is_video = media_type == 'video'
    cap = _MAX_VIDEO_UPLOAD_BYTES if is_video else _MAX_IMAGE_UPLOAD_BYTES
    if uploaded.size > cap:
        return _cors_json(JsonResponse(
            {'error': f'File is too large (max {cap // (1024 * 1024)}MB).'}, status=400))

    if not is_video:
        try:
            Image.open(uploaded).verify()
        except (UnidentifiedImageError, OSError):
            return _cors_json(JsonResponse(
                {'error': "That doesn't look like a valid image."}, status=400))
        uploaded.seek(0)

    ext = 'mp4' if is_video else 'jpg'
    path = default_storage.save(f'posts/{uuid.uuid4()}.{ext}', uploaded)
    staged = StagedUpload.objects.create(
        user=user, url=default_storage.url(path), media_type=media_type,
    )
    return _cors_json(JsonResponse({
        'id': str(staged.id),
        'type': media_type,
        'url': staged.url,
    }, status=201))

@csrf_exempt
@require_http_methods(['GET', 'OPTIONS'])
def posts_exist(request):
    """Which of the given post ids still exist.

    `GET /api/posts/exist/?ids=1,2,3` -> `{"missing": [2]}`.

    Sharing a post into a chat stores a *snapshot* of it in the message, so the
    card kept rendering perfectly after the post itself was deleted — tapping it
    was the only way to find out, and what you got was an error. The chat needs
    to know, and it needs to know for a whole thread at once rather than one
    request per card, which is what this is for.

    Only ids are exposed, never content: the caller is telling us which posts it
    already holds a copy of.
    """
    if request.method == 'OPTIONS':
        return _cors_json(HttpResponse())
    if require_authenticated_user(request) is None:
        return _unauthorized()

    raw = (request.GET.get('ids') or '').strip()
    ids = []
    for part in raw.split(',')[:200]:  # bounded: this is a URL, not a body
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    if not ids:
        return _cors_json(JsonResponse({'missing': []}))

    present = set(
        Post.objects.filter(id__in=ids).values_list('id', flat=True)
    )
    return _cors_json(JsonResponse({'missing': sorted(set(ids) - present)}))

@csrf_exempt
@require_http_methods(["GET", "POST", "OPTIONS"])
def posts_list(request):
    # Simple CORS support for development
    if request.method == "OPTIONS":
        return _cors_json(HttpResponse())

    _ensure_posts_table()

    if request.method == "GET":
        viewer = get_authenticated_user(request)
        viewer_city = ""
        if viewer and viewer.is_authenticated and hasattr(viewer, "profile"):
            viewer_city = viewer.profile.city
        lean = _wants_lean_feed(request)
        posts = Post.objects.select_related("user", "user__profile")
        if lean:
            # No comment prefetch: `light` mode only needs the count, and
            # pulling every comment row would drag each commenter's base64
            # avatar into memory to serialise nothing.
            posts = posts.select_related("poll").prefetch_related(
                "like_rows", "media_items", "poll__options__votes_rows",
            ).annotate(
                comment_count=Count("comment_rows", distinct=True)
            )
        else:
            posts = posts.select_related("poll").prefetch_related(
                "comment_rows__user", "like_rows", "media_items",
                "poll__options__votes_rows",
            )
        posts = posts.all().order_by("-created", "-id")
        requested_city = (request.GET.get("city") or "").strip()
        is_admin_viewer = viewer and viewer.is_authenticated and getattr(getattr(viewer, 'profile', None), 'is_admin', False)
        if requested_city:
            posts = posts.filter(city=requested_city)
            if viewer_city and requested_city != viewer_city:
                viewer = None
        elif viewer_city and not is_admin_viewer:
            posts = posts.filter(city=viewer_city)
        viewer_following_ids = None
        if viewer and viewer.is_authenticated:
            posts = posts.exclude(user_id__in=blocked_user_ids(viewer))
            viewer_following_ids = set(
                Follow.objects.filter(follower=viewer).values_list('following_id', flat=True)
            )
        if lean:
            before = (request.GET.get("before") or "").strip()
            if before:
                # Cursor on the sort key, not on the id. The imported
                # WordPress posts carry old timestamps under new ids, so the
                # two orders disagree and paging by id returned a window that
                # overlapped the previous page and skipped what was between.
                try:
                    anchor = Post.objects.filter(pk=int(before)).values("created", "id").first()
                except (TypeError, ValueError):
                    return _cors_json(JsonResponse({"error": "Invalid before"}, status=400))
                if anchor:
                    posts = posts.filter(
                        Q(created__lt=anchor["created"])
                        | Q(created=anchor["created"], id__lt=anchor["id"])
                    )
            limit = _feed_page_limit(request)
            page = list(posts[: limit + 1])
            has_more = len(page) > limit
            page = page[:limit]
            preview_map = _preview_map_for(page)
            rows, avatars = _lean_feed_payload(
                page, viewer, viewer_following_ids, preview_map
            )
            return _cors_json(
                JsonResponse({"posts": rows, "avatars": avatars, "has_more": has_more})
            )

        # Legacy clients (no X-Neat-Client header) get no pagination of their
        # own, so this branch used to serialise *every* post in the city:
        # 2.9 MB and ~1.8s of one worker at 248 posts, growing without limit as
        # the table does, reachable without authenticating. The newest
        # _LEGACY_FEED_CAP is all any build this old could show before the user
        # ran out of scroll anyway, and it stops one request from being able to
        # cost more the more successful the app gets.
        posts = posts[:_LEGACY_FEED_CAP]
        preview_map = _preview_map_for(posts)
        _viral_ctx = _viewer_page_context(posts, viewer, viewer_following_ids)
        data = [_post_to_dict(p, viewer=viewer, viewer_following_ids=viewer_following_ids,
                              page_ctx=_viral_ctx,
                              preview_map=preview_map) for p in posts]
        return _cors_json(JsonResponse(data, safe=False))

    # POST
    user = require_authenticated_user(request)
    if user is None:
        return _unauthorized()
    user_city = getattr(getattr(user, "profile", None), "city", "")
    if not user_city:
        return _cors_json(JsonResponse({"error": "Choose a city first"}, status=400))

    content_type = request.content_type or ""
    if "multipart" in content_type:
        # New path: multipart/form-data upload
        text = (request.POST.get("text") or "").strip()
        if not text:
            return _cors_json(JsonResponse({"error": "Missing text"}, status=400))
        try:
            media_info = json.loads(request.POST.get("media", "[]"))
        except Exception:
            media_info = []
        try:
            poll_info = json.loads(request.POST.get("poll", "{}"))
        except Exception:
            poll_info = {}

        media_list = []
        for item in media_info[:4]:
            if item.get("url"):
                # External URL (e.g. Giphy) — store as-is
                media_list.append({"type": item.get("type", "image"), "url": item["url"]})
            elif item.get("upload_id"):
                # Already uploaded while the caption was being written.
                staged = StagedUpload.objects.filter(
                    pk=item["upload_id"], user=user
                ).first()
                if staged is None:
                    return _cors_json(JsonResponse(
                        {"error": "That upload has expired. Please try again."},
                        status=400))
                media_list.append({
                    "type": staged.media_type,
                    "url": staged.url,
                    "status": (PostMedia.PENDING if staged.media_type == "video"
                               else PostMedia.READY),
                })
                staged.delete()
            else:
                file_key = f"media_{item.get('file_index', len(media_list))}"
                uploaded = request.FILES.get(file_key)
                if uploaded:
                    is_video = item.get("type") == "video"
                    if is_video:
                        if uploaded.size > _MAX_VIDEO_UPLOAD_BYTES:
                            return _cors_json(JsonResponse(
                                {"error": "Video is too large (max 150MB)."},
                                status=400,
                            ))
                    else:
                        if uploaded.size > _MAX_IMAGE_UPLOAD_BYTES:
                            return _cors_json(JsonResponse(
                                {"error": "Image is too large (max 20MB)."},
                                status=400,
                            ))
                        # Verify this is actually a decodable image rather than
                        # trusting the client-supplied "type" — otherwise an
                        # arbitrary file can be uploaded with a fake type and
                        # get served back under a .jpg URL.
                        try:
                            Image.open(uploaded).verify()
                        except (UnidentifiedImageError, OSError):
                            return _cors_json(JsonResponse(
                                {"error": "That doesn't look like a valid image."},
                                status=400,
                            ))
                        uploaded.seek(0)
                    ext = "mp4" if is_video else "jpg"
                    filename = f"posts/{uuid.uuid4()}.{ext}"
                    # Save the UploadedFile directly (it streams via .chunks()
                    # internally) instead of buffering the whole file into a
                    # Python bytes object first via ContentFile(uploaded.read()).
                    path = default_storage.save(filename, uploaded)
                    url = default_storage.url(path)
                    # Videos are queued, not encoded here. Running ffmpeg
                    # inline held a gunicorn request slot for the whole encode,
                    # so a dozen simultaneous uploads could occupy every slot
                    # and the site stopped answering anybody. `transcode_worker`
                    # picks this up within seconds; until it does, `url` is the
                    # original upload and plays fine.
                    media_list.append({
                        "type": item.get("type", "image"),
                        "url": url,
                        "status": PostMedia.PENDING if is_video else PostMedia.READY,
                    })
    else:
        # Legacy path: JSON body with base64 data URLs
        try:
            body = json.loads(request.body.decode("utf-8") or "{}")
        except Exception:
            return _cors_json(JsonResponse({"error": "Invalid JSON"}, status=400))

        text = body.get("text") or body.get("content")
        if not text:
            return _cors_json(JsonResponse({"error": "Missing text"}, status=400))

        media_list = body.get("media") or []
        image_url = (body.get("imageUrl") or body.get("image_url") or "").strip()
        if not media_list and image_url:
            media_list = [{"type": "image", "url": image_url}]
        poll_info = body.get("poll") or {}

    poll_options = [
        opt.strip() for opt in (poll_info.get("options") or []) if opt and opt.strip()
    ][:4]
    if poll_options and len(poll_options) < 2:
        return _cors_json(JsonResponse({"error": "A poll needs at least 2 options"}, status=400))

    # Legacy field: keep first image URL for old clients reading imageUrl directly
    legacy_image_url = ""
    for item in media_list:
        if item.get("type") == "image" and item.get("url"):
            legacy_image_url = item["url"]
            break

    post = Post.objects.create(
        user=user,
        author=user.username,
        text=text,
        city=user_city,
        image_url=legacy_image_url,
    )

    for i, item in enumerate(media_list[:4]):
        url = (item.get("url") or "").strip()
        media_type = item.get("type", "image")
        if url and media_type in ("image", "video"):
            PostMedia.objects.create(
                post=post,
                media_type=media_type,
                url=url,
                duration=item.get("duration"),
                order=i,
                # Only the multipart upload path queues work; a Giphy link or a
                # legacy base64 body has nothing to transcode.
                status=item.get("status") or PostMedia.READY,
            )

    if len(poll_options) >= 2:
        poll = Poll.objects.create(post=post)
        for i, option_text in enumerate(poll_options):
            PollOption.objects.create(poll=poll, text=option_text, order=i)

    _notify_mentions(post.text, user, post.city, post)
    return _cors_json(JsonResponse(_post_to_dict(post, viewer=user), status=201))


@csrf_exempt
@require_http_methods(["POST", "OPTIONS"])
def post_like(request, post_id):
    if request.method == "OPTIONS":
        return _cors_json(HttpResponse())

    _ensure_posts_table()
    user = require_authenticated_user(request)
    if user is None:
        return _unauthorized()

    post = _get_post_or_404(post_id)
    if post is None:
        return _cors_json(JsonResponse({"error": "Post not found"}, status=404))
    if is_blocked(user, post.user):
        return _cors_json(JsonResponse({"error": "Post not found"}, status=404))
    if getattr(getattr(user, "profile", None), "city", "") != post.city:
        return _cors_json(JsonResponse({"error": "You can only interact in your city"}, status=400))

    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return _cors_json(JsonResponse({"error": "Invalid JSON"}, status=400))

    liked = body.get("liked")
    if liked is None:
        return _cors_json(JsonResponse({"error": "Missing liked value"}, status=400))

    if bool(liked):
        PostLike.objects.get_or_create(post=post, user=user)
        _notify(post.user, user, 'liked your post', post)
    else:
        PostLike.objects.filter(post=post, user=user).delete()
    post.likes = post.like_rows.count()
    post.save(update_fields=["likes"])
    return _cors_json(JsonResponse(_post_to_dict(post, viewer=user)))


@csrf_exempt
@require_http_methods(["POST", "OPTIONS"])
def post_share(request, post_id):
    if request.method == "OPTIONS":
        return _cors_json(HttpResponse())

    _ensure_posts_table()
    user = require_authenticated_user(request)
    if user is None:
        return _unauthorized()

    post = _get_post_or_404(post_id)
    if post is None:
        return _cors_json(JsonResponse({"error": "Post not found"}, status=404))
    if is_blocked(user, post.user):
        return _cors_json(JsonResponse({"error": "Post not found"}, status=404))

    Post.objects.filter(pk=post.pk).update(shares=F("shares") + 1)
    post.refresh_from_db(fields=["shares"])
    return _cors_json(JsonResponse(_post_to_dict(post, viewer=user)))


@csrf_exempt
@require_http_methods(["POST", "OPTIONS"])
def post_poll_vote(request, post_id):
    if request.method == "OPTIONS":
        return _cors_json(HttpResponse())

    _ensure_posts_table()
    user = require_authenticated_user(request)
    if user is None:
        return _unauthorized()

    post = _get_post_or_404(post_id)
    if post is None:
        return _cors_json(JsonResponse({"error": "Post not found"}, status=404))
    if is_blocked(user, post.user):
        return _cors_json(JsonResponse({"error": "Post not found"}, status=404))
    if getattr(getattr(user, "profile", None), "city", "") != post.city:
        return _cors_json(JsonResponse({"error": "You can only interact in your city"}, status=400))

    poll = getattr(post, "poll", None)
    if poll is None:
        return _cors_json(JsonResponse({"error": "This post has no poll"}, status=404))

    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return _cors_json(JsonResponse({"error": "Invalid JSON"}, status=400))

    option_id = body.get("option_id") or body.get("optionId")
    try:
        option = poll.options.get(pk=int(option_id))
    except (PollOption.DoesNotExist, TypeError, ValueError):
        return _cors_json(JsonResponse({"error": "Invalid option"}, status=400))

    # Tap-to-toggle voting (matches the client, which calls this same endpoint
    # for every tap regardless of prior vote state): tapping your current
    # choice retracts it, tapping a different option switches to it, and
    # having no prior vote just casts one.
    existing = PollVote.objects.filter(poll=poll, user=user).first()
    if existing and existing.option_id == option.id:
        existing.delete()
    elif existing:
        existing.option = option
        existing.save(update_fields=["option"])
    else:
        PollVote.objects.create(poll=poll, option=option, user=user)
    return _cors_json(JsonResponse(_post_to_dict(post, viewer=user), status=200))


@csrf_exempt
@require_http_methods(["POST", "DELETE", "OPTIONS"])
def post_comment(request, post_id):
    if request.method == "OPTIONS":
        return _cors_json(HttpResponse())

    _ensure_posts_table()
    user = require_authenticated_user(request)
    if user is None:
        return _unauthorized()

    post = _get_post_or_404(post_id)
    if post is None:
        return _cors_json(JsonResponse({"error": "Post not found"}, status=404))
    if is_blocked(user, post.user):
        return _cors_json(JsonResponse({"error": "Post not found"}, status=404))
    if getattr(getattr(user, "profile", None), "city", "") != post.city:
        return _cors_json(JsonResponse({"error": "You can only interact in your city"}, status=400))

    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return _cors_json(JsonResponse({"error": "Invalid JSON"}, status=400))

    if request.method == "DELETE":
        comment_id = body.get("commentId") or body.get("id")
        if not comment_id:
            return _cors_json(JsonResponse({"error": "commentId required"}, status=400))
        try:
            comment = PostComment.objects.get(pk=int(comment_id), post=post)
        except (PostComment.DoesNotExist, ValueError):
            return _cors_json(JsonResponse({"error": "Comment not found"}, status=404))
        is_admin = getattr(getattr(user, 'profile', None), 'is_admin', False)
        if comment.user_id != user.id and not is_admin:
            return _cors_json(JsonResponse({"error": "Cannot delete other user's comment"}, status=403))
        comment.delete()
        return _cors_json(JsonResponse(_post_to_dict(post, viewer=user)))

    # POST
    if "multipart" in (request.content_type or ""):
        body = {k: v for k, v in request.POST.items()}
    text = (body.get("text") or body.get("comment") or "").strip()
    image_url = (body.get("imageUrl") or body.get("image_url") or "").strip()
    # A binary upload wins over the JSON field: same picture, a third fewer
    # bytes. Stored as a file either way -- a comment image used to be base64
    # in the row, exactly as post images were.
    uploaded = (request.FILES.get("image")
                if "multipart" in (request.content_type or "") else None)
    if uploaded is not None:
        stored = store_comment_image(uploaded)
        if stored:
            image_url = stored
    elif image_url.startswith("data:"):
        image_url = store_comment_image_data(image_url) or image_url
    parent_id = body.get("parentId")
    reply_to_username = (body.get("replyToUsername") or "").strip()[:150]

    if not text and not image_url:
        return _cors_json(JsonResponse({"error": "Missing text or image"}, status=400))

    parent = None
    if parent_id is not None:
        try:
            parent = PostComment.objects.get(pk=int(parent_id), post=post)
        except (PostComment.DoesNotExist, ValueError):
            return _cors_json(JsonResponse({"error": "Parent comment not found"}, status=404))

    comment = PostComment.objects.create(
        post=post, user=user, text=text, image_url=image_url,
        parent=parent,
        reply_to_username=reply_to_username if parent_id else '',
    )
    if parent is None:
        _notify(post.user, user, 'commented on your post', post, comment=comment)
    else:
        _notify(parent.user, user, 'replied to your comment', post, comment=comment)
    _notify_mentions(text, user, post.city, post, verb='mentioned you in a comment', comment=comment)
    return _cors_json(JsonResponse(_post_to_dict(post, viewer=user)))


@csrf_exempt
@require_http_methods(["POST", "OPTIONS"])
def post_save(request, post_id):
    if request.method == "OPTIONS":
        return _cors_json(HttpResponse())

    _ensure_posts_table()
    user = require_authenticated_user(request)
    if user is None:
        return _unauthorized()

    post = _get_post_or_404(post_id)
    if post is None:
        return _cors_json(JsonResponse({"error": "Post not found"}, status=404))
    if is_blocked(user, post.user):
        return _cors_json(JsonResponse({"error": "Post not found"}, status=404))

    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return _cors_json(JsonResponse({"error": "Invalid JSON"}, status=400))

    if body.get("saved"):
        PostSave.objects.get_or_create(post=post, user=user)
    else:
        PostSave.objects.filter(post=post, user=user).delete()

    return _cors_json(JsonResponse(_post_to_dict(post, viewer=user)))


@csrf_exempt
@require_http_methods(["GET", "OPTIONS"])
def saved_posts(request):
    if request.method == "OPTIONS":
        return _cors_json(HttpResponse())

    _ensure_posts_table()
    user = require_authenticated_user(request)
    if user is None:
        return _unauthorized()

    save_rows = (
        PostSave.objects
        .filter(user=user)
        .select_related('post__user')
        .prefetch_related('post__comment_rows__user', 'post__like_rows', 'post__media_items')
        .order_by('-created')
    )
    viewer_following_ids = set(
        Follow.objects.filter(follower=user).values_list('following_id', flat=True)
    )
    _saved_ctx = _viewer_page_context(
        [s.post for s in save_rows], user, viewer_following_ids
    )
    posts = [
        _post_to_dict(s.post, viewer=user,
                      viewer_following_ids=viewer_following_ids,
                      page_ctx=_saved_ctx)
        for s in save_rows
    ]
    return _cors_json(JsonResponse({"posts": posts}))


@csrf_exempt
@require_http_methods(["DELETE", "OPTIONS"])
def post_delete(request, post_id):
    if request.method == "OPTIONS":
        return _cors_json(HttpResponse())

    _ensure_posts_table()
    user = require_authenticated_user(request)
    if user is None:
        return _unauthorized()

    post = _get_post_or_404(post_id)
    if post is None:
        return _cors_json(JsonResponse({"error": "Post not found"}, status=404))
    is_admin = getattr(getattr(user, 'profile', None), 'is_admin', False)
    if post.user_id != user.id and not is_admin:
        return _cors_json(JsonResponse({"error": "You can only delete your own post"}, status=403))

    post.delete()
    return _cors_json(JsonResponse({"ok": True}))


@csrf_exempt
@require_http_methods(["GET", "OPTIONS"])
def post_likers(request, post_id):
    if request.method == "OPTIONS":
        return _cors_json(HttpResponse())

    _ensure_posts_table()
    user = require_authenticated_user(request)
    if user is None:
        return _unauthorized()

    post = _get_post_or_404(post_id)
    if post is None:
        return _cors_json(JsonResponse({"error": "Post not found"}, status=404))

    if is_blocked(user, post.user):
        return _cors_json(JsonResponse({"error": "Post not found"}, status=404))

    from django.contrib.auth import get_user_model
    User = get_user_model()

    liker_ids = list(PostLike.objects.filter(post=post).values_list('user_id', flat=True))
    likers = list(User.objects.filter(id__in=liker_ids).select_related('profile'))

    following_ids = set(Follow.objects.filter(follower=user).values_list('following_id', flat=True))
    follower_ids = set(Follow.objects.filter(following=user).values_list('follower_id', flat=True))

    def _sort_key(liker):
        lid = liker.id
        if lid in following_ids and lid in follower_ids:
            return 0  # mutual — know each other
        if lid in following_ids:
            return 1  # viewer follows them
        if lid in follower_ids:
            return 2  # they follow viewer
        return 3       # no connection

    likers.sort(key=_sort_key)

    return _cors_json(JsonResponse({
        'users': [user_to_dict(liker, viewer=user) for liker in likers]
    }))


@csrf_exempt
@require_http_methods(["POST", "OPTIONS"])
def comment_like(request, comment_id):
    if request.method == "OPTIONS":
        return _cors_json(HttpResponse())

    user = require_authenticated_user(request)
    if user is None:
        return _unauthorized()

    try:
        comment = PostComment.objects.select_related('post').get(pk=comment_id)
    except PostComment.DoesNotExist:
        return _cors_json(JsonResponse({"error": "Not found"}, status=404))

    if is_blocked(user, comment.post.user):
        return _cors_json(JsonResponse({"error": "Not found"}, status=404))

    # City restriction: viewer must be in the same city as the post
    post_city = (comment.post.city or '').strip().lower()
    viewer_city = getattr(getattr(user, 'profile', None), 'city', '').strip().lower()
    if post_city and viewer_city and post_city != viewer_city:
        return _cors_json(JsonResponse(
            {"error": "You can only like comments on posts from your city"},
            status=403,
        ))

    try:
        body = json.loads(request.body or b'{}')
    except Exception:
        body = {}

    if body.get("liked", True):
        _, created = CommentLike.objects.get_or_create(comment=comment, user=user)
        if created:
            _notify(comment.user, user, 'liked your comment', comment.post, comment=comment)
    else:
        CommentLike.objects.filter(comment=comment, user=user).delete()

    return _cors_json(JsonResponse({
        "likes": comment.comment_likes.count(),
        "liked": CommentLike.objects.filter(comment=comment, user=user).exists(),
    }))


@csrf_exempt
@require_http_methods(["POST", "OPTIONS"])
def comment_report(request, comment_id):
    if request.method == "OPTIONS":
        return _cors_json(HttpResponse())

    user = require_authenticated_user(request)
    if user is None:
        return _unauthorized()

    try:
        comment = PostComment.objects.get(pk=comment_id)
    except PostComment.DoesNotExist:
        return _cors_json(JsonResponse({"error": "Comment not found"}, status=404))

    if comment.user_id == user.id:
        return _cors_json(JsonResponse({"error": "You cannot report your own comment"}, status=400))

    try:
        body = json.loads(request.body or b'{}')
    except Exception:
        body = {}

    reason = body.get("reason", "other").strip()
    valid_reasons = {r[0] for r in CommentReport.REASONS}
    if reason not in valid_reasons:
        reason = "other"

    # Ensure CommentReport table exists
    from django.db import connection as _conn
    with _conn.cursor() as _cursor:
        existing = set(_conn.introspection.table_names(_cursor))
    if CommentReport._meta.db_table not in existing:
        with _conn.schema_editor() as _editor:
            _editor.create_model(CommentReport)

    CommentReport.objects.get_or_create(
        comment=comment,
        reporter=user,
        defaults={"reason": reason},
    )
    return _cors_json(JsonResponse({"ok": True}))


@csrf_exempt
@require_http_methods(["POST", "OPTIONS"])
def comment_pin(request, comment_id):
    if request.method == "OPTIONS":
        return _cors_json(HttpResponse())

    user = require_authenticated_user(request)
    if user is None:
        return _unauthorized()

    try:
        comment = PostComment.objects.select_related('post').get(pk=comment_id)
    except PostComment.DoesNotExist:
        return _cors_json(JsonResponse({"error": "Comment not found"}, status=404))

    post = comment.post
    is_admin = getattr(getattr(user, 'profile', None), 'is_admin', False)
    if post.user_id != user.id and not is_admin:
        return _cors_json(JsonResponse({"error": "Only the post owner can pin comments"}, status=403))
    if comment.parent_id is not None:
        return _cors_json(JsonResponse({"error": "Only top-level comments can be pinned"}, status=400))

    try:
        body = json.loads(request.body or b'{}')
    except Exception:
        body = {}

    if body.get("pinned", True):
        PostComment.objects.filter(post=post, pinned=True).exclude(pk=comment.pk).update(pinned=False)
        comment.pinned = True
    else:
        comment.pinned = False
    comment.save(update_fields=["pinned"])

    return _cors_json(JsonResponse(_post_to_dict(post, viewer=user)))


@csrf_exempt
@require_http_methods(["POST", "OPTIONS"])
def post_report(request, post_id):
    if request.method == "OPTIONS":
        return _cors_json(HttpResponse())

    user = require_authenticated_user(request)
    if user is None:
        return _unauthorized()

    post = _get_post_or_404(post_id)
    if post is None:
        return _cors_json(JsonResponse({"error": "Post not found"}, status=404))

    try:
        body = json.loads(request.body or b'{}')
    except Exception:
        body = {}

    reason = body.get("reason", "").strip()
    valid_reasons = {r[0] for r in PostReport.REASONS}
    if reason not in valid_reasons:
        return _cors_json(JsonResponse({"error": "Invalid reason"}, status=400))

    sub_reason = body.get("sub_reason", "").strip()[:200]

    _, created = PostReport.objects.get_or_create(
        post=post,
        reporter=user,
        defaults={"reason": reason, "sub_reason": sub_reason},
    )
    if not created:
        PostReport.objects.filter(post=post, reporter=user).update(
            reason=reason, sub_reason=sub_reason
        )

    return _cors_json(JsonResponse({"ok": True}))


# How far above its own normal a city has to run to read as fully hot. At 2.0,
# twice the usual hourly traffic saturates the pin at red.
_HEAT_HOT_MULTIPLE = 2.0
# Days of history the per-city hourly average is taken over.
_HEAT_BASELINE_DAYS = 7
# Added to every denominator so heat means something in a quiet town.
#
# On a purely relative scale a village averaging 0.2 events an hour goes solid
# red the moment two people do anything, and its pin then spends most of its
# life red for no real reason. This is the absolute floor that a burst has to
# clear before it can register, so small cities still warm up when something is
# genuinely happening but don't flicker on background noise. It also removes
# the divide-by-zero for a city with no history at all.
_HEAT_SMOOTHING = 3.0


def _activity_by_city(since, until=None):
    """Posts + likes + comments per city in a window, as one number each.

    Engagement is counted against the city of the post it lands on, not the
    city of whoever produced it — a pin is meant to show how busy that place
    is, and a like from three cities away is still attention paid to it.
    """
    counts = {}

    def add(rows, key):
        for row in rows:
            city = (row[key] or '').strip()
            if city:
                counts[city] = counts.get(city, 0) + row['n']

    def window(qs, field='created'):
        qs = qs.filter(**{f'{field}__gte': since})
        if until is not None:
            qs = qs.filter(**{f'{field}__lt': until})
        return qs

    add(window(Post.objects).values('city').annotate(n=Count('id')), 'city')
    add(window(PostLike.objects).values('post__city').annotate(n=Count('id')), 'post__city')
    add(window(PostComment.objects).values('post__city').annotate(n=Count('id')), 'post__city')
    return counts


@csrf_exempt
@require_http_methods(["GET", "OPTIONS"])
def city_heat(request):
    """Per-city hotness, 0.0–1.0, for the map pins.

    Heat is relative, not absolute: each city is measured against its own
    hourly average rather than against Athens. An absolute scale would leave
    every pin outside the two biggest cities permanently green no matter what
    was happening there, which is the opposite of what the map is for.

    Cities with no activity are simply absent from the response — the client
    treats a missing city as cold, so there is no reason to send zeroes for
    every town in Greece.
    """
    if request.method == 'OPTIONS':
        return _cors_json(HttpResponse())

    _ensure_posts_table()
    now = timezone.now()
    hour_ago = now - datetime.timedelta(hours=1)
    baseline_start = now - datetime.timedelta(days=_HEAT_BASELINE_DAYS)

    current = _activity_by_city(hour_ago)
    if not current:
        return _cors_json(JsonResponse({}))

    # Baseline excludes the live hour, so a busy hour doesn't inflate the very
    # average it is being judged against.
    history = _activity_by_city(baseline_start, until=hour_ago)
    hours = _HEAT_BASELINE_DAYS * 24 - 1

    heat = {}
    for city, count in current.items():
        average = (history.get(city, 0) / hours) if hours else 0.0
        ratio = count / (average * _HEAT_HOT_MULTIPLE + _HEAT_SMOOTHING)
        value = max(0.0, min(1.0, ratio))
        if value > 0:
            heat[city] = round(value, 3)

    return _cors_json(JsonResponse(heat))
