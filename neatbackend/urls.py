from django.contrib import admin
from django.urls import include, path, re_path
from media_serve import serve_media
from web import views as web_views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/auth/', include('accounts.urls')),
    path('api/auth/admin/security/', include('security.urls')),
    path('api/posts/', include('posts.urls')),
    path('api/messages/', include('dm_messages.urls')),
    path('api/events/', include('events.urls')),
    path('api/push/', include('push.urls')),
    path('api/link-preview/', include('linkpreview.urls')),
    # Shared post links. Server-rendered so a crawler gets real meta tags and a
    # human gets the post itself — this replaced the Netlify edge functions.
    path('post/', include('web.urls')),
    # Root-level, not under post/: it is its own page, behind an admin login.
    path('analytics', web_views.analytics, name='analytics'),
    path('analytics/', web_views.analytics),
    # Operational health. The page itself is behind the same admin login; the
    # two liveness endpoints are public and say a single word, because an
    # uptime monitor cannot log in.
    path('health', web_views.health, name='health'),
    path('health/', web_views.health),
    path('health/live', web_views.health_live, name='health_live'),
    path('health/ready', web_views.health_ready, name='health_ready'),
    # What to do when it is slow or down. Admin-gated for the same reason
    # /health is: it spells out exactly how little it takes to overwhelm the box.
    path('runbook', web_views.runbook, name='runbook'),
    path('runbook/', web_views.runbook),
    # Data and account pages. Served by Django rather than as static files
    # because the deletion form has to POST somewhere and send mail.
    path('safetyportal', web_views.safety_portal, name='safety_portal'),
    path('safetyportal/', web_views.safety_portal),
    path('deleteaccount', web_views.delete_account, name='delete_account'),
    path('deleteaccount/', web_views.delete_account),
    re_path(r'^media/(?P<path>.*)$', serve_media),
]
