import json
from django.db import connection
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from accounts.auth import get_authenticated_user, require_authenticated_user
from accounts.models import Follow, Notification
from accounts.serializers import user_to_dict
from .models import Post, PostComment, PostLike, PostSave, CommentLike


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


def _post_to_dict(post, viewer=None, viewer_following_ids=None):
    data = post.to_dict()
    row_comments = list(
        post.comment_rows
        .filter(parent__isnull=True)
        .select_related("user")
        .prefetch_related("comment_likes", "replies__user", "replies__comment_likes")
        .all()
    )
    if row_comments:
        data["comments"] = [comment.to_dict(viewer=viewer) for comment in row_comments]
    data["likes"] = post.like_rows.count() or post.likes
    data["liked"] = False
    data["saved"] = False
    data["likedByFollowing"] = []
    if viewer and viewer.is_authenticated:
        data["liked"] = PostLike.objects.filter(post=post, user=viewer).exists()
        data["saved"] = PostSave.objects.filter(post=post, user=viewer).exists()
        data["following"] = post.user_id == viewer.id or post.user_id is not None
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
    return data


def _notify(recipient, actor, verb, post):
    if recipient == actor or recipient is None:
        return
    Notification.objects.create(
        recipient=recipient,
        actor=actor,
        verb=verb,
        target_type='post',
        target_id=str(post.id),
        target_text=post.text[:255],
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
        posts = Post.objects.select_related("user").prefetch_related("comment_rows__user", "like_rows").all().order_by("-created")
        requested_city = (request.GET.get("city") or "").strip()
        if requested_city:
            posts = posts.filter(city=requested_city)
            if viewer_city and requested_city != viewer_city:
                viewer = None
        elif viewer_city:
            posts = posts.filter(city=viewer_city)
        viewer_following_ids = None
        if viewer and viewer.is_authenticated:
            viewer_following_ids = set(
                Follow.objects.filter(follower=viewer).values_list('following_id', flat=True)
            )
        data = [_post_to_dict(p, viewer=viewer, viewer_following_ids=viewer_following_ids) for p in posts]
        return _cors_json(JsonResponse(data, safe=False))

    # POST
    user = require_authenticated_user(request)
    if user is None:
        return _unauthorized()
    user_city = getattr(getattr(user, "profile", None), "city", "")
    if not user_city:
        return _cors_json(JsonResponse({"error": "Choose a city first"}, status=400))

    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return _cors_json(JsonResponse({"error": "Invalid JSON"}, status=400))

    text = body.get("text") or body.get("content")
    if not text:
        return _cors_json(JsonResponse({"error": "Missing text"}, status=400))
    image_url = (body.get("imageUrl") or body.get("image_url") or "").strip()

    post = Post.objects.create(
        user=user,
        author=user.username,
        text=text,
        city=user_city,
        image_url=image_url,
    )
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
        if comment.user_id != user.id:
            return _cors_json(JsonResponse({"error": "Cannot delete other user's comment"}, status=403))
        comment.delete()
        return _cors_json(JsonResponse(_post_to_dict(post, viewer=user)))

    # POST
    text = (body.get("text") or body.get("comment") or "").strip()
    image_url = (body.get("imageUrl") or body.get("image_url") or "").strip()
    parent_id = body.get("parentId")

    if not text and not image_url:
        return _cors_json(JsonResponse({"error": "Missing text or image"}, status=400))

    parent = None
    if parent_id is not None:
        try:
            parent = PostComment.objects.get(pk=int(parent_id), post=post)
        except (PostComment.DoesNotExist, ValueError):
            return _cors_json(JsonResponse({"error": "Parent comment not found"}, status=404))

    PostComment.objects.create(post=post, user=user, text=text, image_url=image_url, parent=parent)
    if parent is None:
        _notify(post.user, user, 'commented on your post', post)
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
        .prefetch_related('post__comment_rows__user', 'post__like_rows')
        .order_by('-created')
    )
    viewer_following_ids = set(
        Follow.objects.filter(follower=user).values_list('following_id', flat=True)
    )
    posts = [_post_to_dict(s.post, viewer=user, viewer_following_ids=viewer_following_ids) for s in save_rows]
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
    if post.user_id != user.id:
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
        CommentLike.objects.get_or_create(comment=comment, user=user)
    else:
        CommentLike.objects.filter(comment=comment, user=user).delete()

    return _cors_json(JsonResponse({
        "likes": comment.comment_likes.count(),
        "liked": CommentLike.objects.filter(comment=comment, user=user).exists(),
    }))
