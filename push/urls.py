from django.urls import path

from . import views

urlpatterns = [
    path('devices/register/', views.register_device, name='register_device'),
    path('devices/unregister/', views.unregister_device, name='unregister_device'),
    # The view existed and was never routed, so the app's badge refresh has
    # been answering 404 on every launch and after every read — 394 times in a
    # single day — and the icon badge never updated.
    path('badge/', views.badge, name='badge'),
    path('badge/', views.badge, name='push_badge'),
]
