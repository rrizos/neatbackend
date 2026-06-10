import json
from django.db import connection
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from .models import Post


def _cors_json(response):
    response["Access-Control-Allow-Origin"] = "*"
    response["Access-Control-Allow-Headers"] = "Content-Type"
    response["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return response


def _get_post_or_404(post_id):
    try:
        return Post.objects.get(pk=post_id)
    except Post.DoesNotExist:
        return None


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
        posts = Post.objects.all().order_by("-created")
        data = [p.to_dict() for p in posts]
        return _cors_json(JsonResponse(data, safe=False))

    # POST
    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    text = body.get("text") or body.get("content")
    author = body.get("author") or body.get("user") or "Anonymous"
    if not text:
        return JsonResponse({"error": "Missing text"}, status=400)

    post = Post.objects.create(author=author, text=text)
    return _cors_json(JsonResponse(post.to_dict(), status=201))


@csrf_exempt
@require_http_methods(["POST", "OPTIONS"])
def post_like(request, post_id):
    if request.method == "OPTIONS":
        return _cors_json(HttpResponse())

    _ensure_posts_table()

    post = _get_post_or_404(post_id)
    if post is None:
        return JsonResponse({"error": "Post not found"}, status=404)

    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    liked = body.get("liked")
    if liked is None:
        return JsonResponse({"error": "Missing liked value"}, status=400)

    if bool(liked):
        post.likes += 1
    else:
        post.likes = max(0, post.likes - 1)
    post.save(update_fields=["likes"])
    return _cors_json(JsonResponse(post.to_dict()))


@csrf_exempt
@require_http_methods(["POST", "OPTIONS"])
def post_comment(request, post_id):
    if request.method == "OPTIONS":
        return _cors_json(HttpResponse())

    _ensure_posts_table()

    post = _get_post_or_404(post_id)
    if post is None:
        return JsonResponse({"error": "Post not found"}, status=404)

    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    text = (body.get("text") or body.get("comment") or "").strip()
    if not text:
        return JsonResponse({"error": "Missing text"}, status=400)

    try:
        comments = json.loads(post.comments or "[]")
    except Exception:
        comments = []

    author = (body.get("author") or body.get("user") or "Anonymous").strip()
    comment_text = f"{author}: {text}" if author and author != "Anonymous" else text
    comments.append(comment_text)
    post.comments = json.dumps(comments, ensure_ascii=False)
    post.save(update_fields=["comments"])
    return _cors_json(JsonResponse(post.to_dict()))
