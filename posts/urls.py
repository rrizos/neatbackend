from django.urls import path
from . import views

urlpatterns = [
    path('cities/', views.cities_list, name='cities_list'),
    path('viral/', views.viral_posts, name='viral_posts'),
    path('city-heat/', views.city_heat, name='city_heat'),
    path('saved/', views.saved_posts, name='saved_posts'),
    path('exist/', views.posts_exist, name='posts_exist'),
    # Upload a file while the caption is still being written; the post that
    # follows refers to it by id instead of carrying the bytes.
    path('upload/', views.stage_upload, name='stage_upload'),
    path('', views.posts_list, name='posts_list'),
    path('<int:post_id>/', views.post_detail, name='post_detail'),
    path('<int:post_id>/like/', views.post_like, name='post_like'),
    path('<int:post_id>/share/', views.post_share, name='post_share'),
    path('<int:post_id>/poll/vote/', views.post_poll_vote, name='post_poll_vote'),
    path('<int:post_id>/likers/', views.post_likers, name='post_likers'),
    path('<int:post_id>/comments/', views.post_comment, name='post_comment'),
    path('<int:post_id>/save/', views.post_save, name='post_save'),
    path('<int:post_id>/delete/', views.post_delete, name='post_delete'),
    path('<int:post_id>/report/', views.post_report, name='post_report'),
    path('comments/<int:comment_id>/like/', views.comment_like, name='comment_like'),
    path('comments/<int:comment_id>/report/', views.comment_report, name='comment_report'),
    path('comments/<int:comment_id>/pin/', views.comment_pin, name='comment_pin'),
]
