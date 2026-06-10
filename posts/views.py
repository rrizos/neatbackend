import json
from django.db import connection
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from accounts.auth import get_authenticated_user, require_authenticated_user
from .models import Post, PostComment, PostLike


def _cors_json(response):
    response["Access-Control-Allow-Origin"] = "*"
    response["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
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


def _post_to_dict(post, viewer=None):
    data = post.to_dict()
    row_comments = list(post.comment_rows.select_related("user").all())
    if row_comments:
        data["comments"] = [comment.to_dict() for comment in row_comments]
    data["likes"] = post.like_rows.count() or post.likes
    data["liked"] = False
    if viewer and viewer.is_authenticated:
        data["liked"] = PostLike.objects.filter(post=post, user=viewer).exists()
        data["following"] = post.user_id == viewer.id or post.user_id is not None
    else:
        data["following"] = post.user_id is not None
    return data


def _ensure_posts_table():
    table_name = Post._meta.db_table
    with connection.cursor() as cursor:
        existing_tables = connection.introspection.table_names(cursor)
    if table_name in existing_tables:
        return

    with connection.schema_editor() as schema_editor:
        schema_editor.create_model(Post)


@csrf_exempt
@require_http_methods(["GET", "POST", "OPTIONS"])
def posts_list(request):
    # Simple CORS support for development
    if request.method == "OPTIONS":
        return _cors_json(HttpResponse())

    _ensure_posts_table()

    if request.method == "GET":
        viewer = get_authenticated_user(request)
        posts = Post.objects.select_related("user").prefetch_related("comment_rows__user", "like_rows").all().order_by("-created")
        data = [_post_to_dict(p, viewer=viewer) for p in posts]
        return _cors_json(JsonResponse(data, safe=False))

    # POST
    user = require_authenticated_user(request)
    if user is None:
        return _unauthorized()

    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return _cors_json(JsonResponse({"error": "Invalid JSON"}, status=400))

    text = body.get("text") or body.get("content")
    if not text:
        return _cors_json(JsonResponse({"error": "Missing text"}, status=400))

    post = Post.objects.create(user=user, author=user.username, text=text)
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

    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return _cors_json(JsonResponse({"error": "Invalid JSON"}, status=400))

    liked = body.get("liked")
    if liked is None:
        return _cors_json(JsonResponse({"error": "Missing liked value"}, status=400))

    if bool(liked):
        PostLike.objects.get_or_create(post=post, user=user)
    else:
        PostLike.objects.filter(post=post, user=user).delete()
    post.likes = post.like_rows.count()
    post.save(update_fields=["likes"])
    return _cors_json(JsonResponse(_post_to_dict(post, viewer=user)))


@csrf_exempt
@require_http_methods(["POST", "OPTIONS"])
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

    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return _cors_json(JsonResponse({"error": "Invalid JSON"}, status=400))

    text = (body.get("text") or body.get("comment") or "").strip()
    if not text:
        return _cors_json(JsonResponse({"error": "Missing text"}, status=400))

    PostComment.objects.create(post=post, user=user, text=text)
    return _cors_json(JsonResponse(_post_to_dict(post, viewer=user)))
