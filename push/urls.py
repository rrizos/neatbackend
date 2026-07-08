from django.urls import path

from . import views

urlpatterns = [
    path('devices/register/', views.register_device, name='register_device'),
    path('devices/unregister/', views.unregister_device, name='unregister_device'),
]
