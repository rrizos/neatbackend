import json
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from .models import Post


@csrf_exempt
@require_http_methods(["GET", "POST", "OPTIONS"])
def posts_list(request):
    # Simple CORS support for development
    if request.method == "OPTIONS":
        resp = HttpResponse()
        resp["Access-Control-Allow-Origin"] = "*"
        resp["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
        resp["Access-Control-Allow-Headers"] = "Content-Type"
        return resp

    if request.method == "GET":
        posts = Post.objects.all().order_by("-created")
        data = [p.to_dict() for p in posts]
        resp = JsonResponse(data, safe=False)
        resp["Access-Control-Allow-Origin"] = "*"
        return resp

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
    resp = JsonResponse(post.to_dict(), status=201)
    resp["Access-Control-Allow-Origin"] = "*"
    return resp
