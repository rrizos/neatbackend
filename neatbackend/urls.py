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
    # Data and account pages. Served by Django rather than as static files
    # because the deletion form has to POST somewhere and send mail.
    path('safetyportal', web_views.safety_portal, name='safety_portal'),
    path('safetyportal/', web_views.safety_portal),
    path('deleteaccount', web_views.delete_account, name='delete_account'),
    path('deleteaccount/', web_views.delete_account),
    re_path(r'^media/(?P<path>.*)$', serve_media),
]
