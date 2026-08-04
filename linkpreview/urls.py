from django.urls import path

from . import views

urlpatterns = [
    path('', views.link_preview, name='link_preview'),
]
