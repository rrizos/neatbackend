from django.urls import path

from . import views

urlpatterns = [
    path('', views.events_list, name='events_list'),
    path('<int:event_id>/attend/', views.event_attend, name='event_attend'),
    path('<int:event_id>/attendees/', views.event_attendees, name='event_attendees'),
    path('<int:event_id>/comments/', views.event_comments, name='event_comments'),
    path('<int:event_id>/delete/', views.event_delete, name='event_delete'),
    path('<int:event_id>/update/', views.event_update, name='event_update'),
    path('<int:event_id>/report/', views.event_report, name='event_report'),
    path('comments/<int:comment_id>/report/', views.event_comment_report, name='event_comment_report'),
    path('comments/<int:comment_id>/pin/', views.event_comment_pin, name='event_comment_pin'),
    path('comments/<int:comment_id>/like/', views.event_comment_like, name='event_comment_like'),
]
