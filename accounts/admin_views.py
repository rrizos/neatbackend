import json

from django.contrib.auth import get_user_model
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from accounts.auth import require_authenticated_user
from accounts.serializers import ensure_profile, user_to_dict
from accounts.views import delete_user_and_content
from posts.models import Post, PostReport

try:
    from dm_messages.models import Message, MessageReport
    _messages_available = True
except Exception:
    _messages_available = False

try:
    from events.models import Event, EventReport
    _events_available = True
except Exception:
    _events_available = False

try:
    from posts.models import CommentReport
    from posts.models import PostComment
    _comments_available = True
except Exception:
    _comments_available = False

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

    data = []

    # Post reports
    post_reports = (
        PostReport.objects
        .select_related("post", "post__user", "reporter")
        .order_by("-created")
    )
    for r in post_reports:
        post = r.post
        data.append({
            "id": r.id,
            "type": "post",
            "reason": r.reason,
            "subReason": r.sub_reason,
            "created": r.created.isoformat(),
            "reporter": {
                "id": r.reporter_id,
                "username": r.reporter.username,
            },
            "content": {
                "id": post.id,
                "conversationId": 0,
                "author": post.user.username if post.user_id else post.author,
                "text": post.text,
            },
        })

    # Message reports
    if _messages_available:
        try:
            for r in MessageReport.objects.select_related("message__sender", "reporter").order_by("-created"):
                msg = r.message
                data.append({
                    "id": r.id,
                    "type": "message",
                    "reason": r.reason,
                    "subReason": "",
                    "created": r.created.isoformat(),
                    "reporter": {"id": r.reporter_id, "username": r.reporter.username},
                    "content": {
                        "id": msg.id,
                        "conversationId": msg.conversation_id,
                        "author": msg.sender.username,
                        "text": msg.text,
                    },
                })
        except Exception:
            pass

    # Event reports
    if _events_available:
        try:
            for r in EventReport.objects.select_related("event", "event__creator", "reporter").order_by("-created"):
                evt = r.event
                data.append({
                    "id": r.id,
                    "type": "event",
                    "reason": r.reason,
                    "subReason": "",
                    "created": r.created.isoformat(),
                    "reporter": {"id": r.reporter_id, "username": r.reporter.username},
                    "content": {
                        "id": evt.id,
                        "conversationId": 0,
                        "author": evt.creator.username if evt.creator_id else evt.organizer,
                        "text": evt.title,
                    },
                })
        except Exception:
            pass

    # Comment reports
    if _comments_available:
        try:
            for r in CommentReport.objects.select_related("comment__post", "comment__user", "reporter").order_by("-created"):
                c = r.comment
                data.append({
                    "id": r.id,
                    "type": "comment",
                    "reason": r.reason,
                    "subReason": "",
                    "created": r.created.isoformat(),
                    "reporter": {"id": r.reporter_id, "username": r.reporter.username},
                    "content": {
                        "id": c.id,
                        "conversationId": 0,
                        "author": c.user.username,
                        "text": c.text,
                    },
                })
        except Exception:
            pass

    data.sort(key=lambda x: x["created"], reverse=True)
    return _cors_json(JsonResponse({"reports": data}))


@csrf_exempt
@require_http_methods(["DELETE", "OPTIONS"])
def admin_dismiss_report(request, report_id):
    if request.method == "OPTIONS":
        return _cors_json(HttpResponse())

    _, err = _require_admin(request)
    if err:
        return err

    report_type = request.GET.get("type", "post")

    if report_type == "message" and _messages_available:
        try:
            report = MessageReport.objects.get(pk=report_id)
        except MessageReport.DoesNotExist:
            return _cors_json(JsonResponse({"error": "Report not found"}, status=404))
    elif report_type == "event" and _events_available:
        try:
            report = EventReport.objects.get(pk=report_id)
        except EventReport.DoesNotExist:
            return _cors_json(JsonResponse({"error": "Report not found"}, status=404))
    elif report_type == "comment" and _comments_available:
        try:
            report = CommentReport.objects.get(pk=report_id)
        except CommentReport.DoesNotExist:
            return _cors_json(JsonResponse({"error": "Report not found"}, status=404))
    else:
        try:
            report = PostReport.objects.get(pk=report_id)
        except PostReport.DoesNotExist:
            return _cors_json(JsonResponse({"error": "Report not found"}, status=404))

    report.delete()
    return _cors_json(JsonResponse({"ok": True}))


@csrf_exempt
@require_http_methods(["GET", "OPTIONS"])
def admin_comments(request):
    if request.method == "OPTIONS":
        return _cors_json(HttpResponse())

    _, err = _require_admin(request)
    if err:
        return err

    if not _comments_available:
        return _cors_json(JsonResponse({"comments": []}))

    query = request.GET.get("q", "").strip()
    qs = PostComment.objects.select_related("user", "post").order_by("-created")
    if query:
        from django.db.models import Q
        qs = qs.filter(Q(text__icontains=query) | Q(user__username__icontains=query))

    data = []
    for c in qs[:100]:
        data.append({
            "id": c.id,
            "author": c.user.username,
            "text": c.text,
            "postId": c.post_id,
            "postText": (c.post.text[:120] if c.post else ""),
            "created": c.created.isoformat(),
        })

    return _cors_json(JsonResponse({"comments": data}))


@csrf_exempt
@require_http_methods(["DELETE", "OPTIONS"])
def admin_delete_comment(request, comment_id):
    if request.method == "OPTIONS":
        return _cors_json(HttpResponse())

    _, err = _require_admin(request)
    if err:
        return err

    if not _comments_available:
        return _cors_json(JsonResponse({"error": "Comments not available"}, status=404))

    try:
        comment = PostComment.objects.get(pk=comment_id)
    except PostComment.DoesNotExist:
        return _cors_json(JsonResponse({"error": "Comment not found"}, status=404))

    comment.delete()
    return _cors_json(JsonResponse({"ok": True}))


@csrf_exempt
@require_http_methods(["DELETE", "OPTIONS"])
def admin_delete_message(request, message_id):
    if request.method == "OPTIONS":
        return _cors_json(HttpResponse())

    _, err = _require_admin(request)
    if err:
        return err

    if not _messages_available:
        return _cors_json(JsonResponse({"error": "Messages not available"}, status=404))

    try:
        message = Message.objects.get(pk=message_id)
    except Message.DoesNotExist:
        return _cors_json(JsonResponse({"error": "Message not found"}, status=404))

    message.delete()
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
@require_http_methods(["POST", "OPTIONS"])
def admin_set_official_eligibility(request, username):
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
    profile.can_create_official_events = body.get("eligible", not profile.can_create_official_events)
    profile.save(update_fields=["can_create_official_events"])

    return _cors_json(JsonResponse({
        "ok": True,
        "canCreateOfficialEvents": profile.can_create_official_events,
    }))


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

    delete_user_and_content(user)
    return _cors_json(JsonResponse({"ok": True}))
