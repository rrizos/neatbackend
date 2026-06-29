import json

from django.contrib.auth import get_user_model
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from accounts.auth import require_authenticated_user
from accounts.serializers import ensure_profile, user_to_dict
from posts.models import Post, PostReport

User = get_user_model()


def _cors_json(response):
    response["Access-Control-Allow-Origin"] = "*"
    response["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response["Access-Control-Allow-Methods"] = "GET,POST,DELETE,PATCH,OPTIONS"
    response["Cache-Control"] = "no-store"
    return response


def _unauthorized():
    return _cors_json(JsonResponse({"error": "Authentication required"}, status=401))


def _forbidden():
    return _cors_json(JsonResponse({"error": "Admin access required"}, status=403))


def _require_admin(request):
    user = require_authenticated_user(request)
    if user is None:
        return None, _unauthorized()
    profile = ensure_profile(user)
    if not profile.is_admin:
        return None, _forbidden()
    return user, None


@csrf_exempt
@require_http_methods(["GET", "OPTIONS"])
def admin_reports(request):
    if request.method == "OPTIONS":
        return _cors_json(HttpResponse())

    _, err = _require_admin(request)
    if err:
        return err

    reports = (
        PostReport.objects
        .select_related("post", "post__user", "reporter")
        .order_by("-created")
    )

    data = []
    for r in reports:
        post = r.post
        data.append({
            "id": r.id,
            "reason": r.reason,
            "subReason": r.sub_reason,
            "created": r.created.isoformat(),
            "reporter": {
                "id": r.reporter_id,
                "username": r.reporter.username,
            },
            "post": {
                "id": post.id,
                "author": post.user.username if post.user_id else post.author,
                "text": post.text,
                "created": post.created.isoformat(),
            },
        })

    return _cors_json(JsonResponse({"reports": data}))


@csrf_exempt
@require_http_methods(["DELETE", "OPTIONS"])
def admin_dismiss_report(request, report_id):
    if request.method == "OPTIONS":
        return _cors_json(HttpResponse())

    _, err = _require_admin(request)
    if err:
        return err

    try:
        report = PostReport.objects.get(pk=report_id)
    except PostReport.DoesNotExist:
        return _cors_json(JsonResponse({"error": "Report not found"}, status=404))

    report.delete()
    return _cors_json(JsonResponse({"ok": True}))


@csrf_exempt
@require_http_methods(["DELETE", "OPTIONS"])
def admin_delete_post(request, post_id):
    if request.method == "OPTIONS":
        return _cors_json(HttpResponse())

    _, err = _require_admin(request)
    if err:
        return err

    try:
        post = Post.objects.get(pk=post_id)
    except Post.DoesNotExist:
        return _cors_json(JsonResponse({"error": "Post not found"}, status=404))

    post.delete()
    return _cors_json(JsonResponse({"ok": True}))


@csrf_exempt
@require_http_methods(["GET", "OPTIONS"])
def admin_users(request):
    if request.method == "OPTIONS":
        return _cors_json(HttpResponse())

    admin_user, err = _require_admin(request)
    if err:
        return err

    query = request.GET.get("q", "").strip()
    qs = User.objects.select_related("profile").order_by("username")
    if query:
        qs = qs.filter(username__icontains=query)

    users = [user_to_dict(u, viewer=admin_user) for u in qs[:100]]
    return _cors_json(JsonResponse({"users": users}))


@csrf_exempt
@require_http_methods(["POST", "OPTIONS"])
def admin_verify_user(request, username):
    if request.method == "OPTIONS":
        return _cors_json(HttpResponse())

    _, err = _require_admin(request)
    if err:
        return err

    try:
        user = User.objects.select_related("profile").get(username=username)
    except User.DoesNotExist:
        return _cors_json(JsonResponse({"error": "User not found"}, status=404))

    try:
        body = json.loads(request.body or b'{}')
    except Exception:
        body = {}

    profile = ensure_profile(user)
    profile.is_verified = body.get("verified", not profile.is_verified)
    profile.save(update_fields=["is_verified"])

    return _cors_json(JsonResponse({"ok": True, "isVerified": profile.is_verified}))


@csrf_exempt
@require_http_methods(["DELETE", "OPTIONS"])
def admin_delete_user(request, username):
    if request.method == "OPTIONS":
        return _cors_json(HttpResponse())

    admin_user, err = _require_admin(request)
    if err:
        return err

    try:
        user = User.objects.get(username=username)
    except User.DoesNotExist:
        return _cors_json(JsonResponse({"error": "User not found"}, status=404))

    if user.id == admin_user.id:
        return _cors_json(JsonResponse({"error": "Cannot delete your own account"}, status=400))

    user.delete()
    return _cors_json(JsonResponse({"ok": True}))
