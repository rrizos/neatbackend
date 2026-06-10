from django.urls import path
from . import views

urlpatterns = [
    path('', views.posts_list, name='posts_list'),
    path('<int:post_id>/like/', views.post_like, name='post_like'),
    path('<int:post_id>/comments/', views.post_comment, name='post_comment'),
]
