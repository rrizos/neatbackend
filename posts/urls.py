from django.urls import path
from . import views

urlpatterns = [
    path('cities/', views.cities_list, name='cities_list'),
    path('saved/', views.saved_posts, name='saved_posts'),
    path('', views.posts_list, name='posts_list'),
    path('<int:post_id>/like/', views.post_like, name='post_like'),
    path('<int:post_id>/comments/', views.post_comment, name='post_comment'),
    path('<int:post_id>/save/', views.post_save, name='post_save'),
    path('<int:post_id>/delete/', views.post_delete, name='post_delete'),
    path('comments/<int:comment_id>/like/', views.comment_like, name='comment_like'),
]
