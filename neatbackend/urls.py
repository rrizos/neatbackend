from django.contrib import admin
from django.urls import include, path, re_path
from media_serve import serve_media

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/auth/', include('accounts.urls')),
    path('api/posts/', include('posts.urls')),
    path('api/messages/', include('dm_messages.urls')),
    path('api/events/', include('events.urls')),
    path('api/push/', include('push.urls')),
    re_path(r'^media/(?P<path>.*)$', serve_media),
]
