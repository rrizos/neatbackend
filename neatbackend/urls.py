from django.contrib import admin
from django.urls import include, path, re_path
from media_serve import serve_media
from ambassadors import views as ambassador_views
from lockedcities import views as lockedcities_views
from invites import views as invite_views
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
    path('api/invites/', include('invites.urls')),
    path('api/ambassadors/', include('ambassadors.urls')),
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
    # Invite links. /invites (the dashboard) is spelled out before the
    # generic pattern so a user called "invites" cannot take the page, and the
    # <username>/invite pattern is last because it matches two segments of
    # anything — every real route above it must win first.
    path('invites', invite_views.invites_dashboard, name='invites_dashboard'),
    path('invites/', invite_views.invites_dashboard),
    path('invite', invite_views.invite_page, name='invite_page'),
    path('invite/', invite_views.invite_page),
    re_path(r'^(?P<username>[A-Za-z0-9_.-]{1,30})/invite/?$',
            invite_views.invite_page, name='invite_page_user'),
    # The ambassador programme: two admin pages and the public link.
    # /a/<code> is short because people type it off a screen.
    path('ambassadors', ambassador_views.manage, name='ambassadors_manage'),
    path('ambassadors/', ambassador_views.manage),
    path('ambassadorsstats', ambassador_views.stats, name='ambassadors_stats'),
    path('ambassadorsstats/', ambassador_views.stats),
    re_path(r'^a/(?P<code>[A-Za-z0-9_-]{1,64})/?$',
            ambassador_views.ambassador_landing, name='ambassador_landing'),
    # The locked-cities kill switch.
    path('stoplocked', lockedcities_views.stop_locked, name='stop_locked'),
    path('stoplocked/', lockedcities_views.stop_locked),
    # A creator's own dashboard. The key in the URL is the credential:
    # ambassadors usually have no Neat account to log in with.
    re_path(r'^creator/(?P<key>[A-Za-z0-9_-]{16,64})/qr\.(?P<fmt>svg|png)$',
            ambassador_views.creator_qr, name='creator_qr'),
    re_path(r'^creator/(?P<key>[A-Za-z0-9_-]{16,64})/?$',
            ambassador_views.creator_dashboard, name='creator_dashboard'),
    re_path(r'^media/(?P<path>.*)$', serve_media),
]
