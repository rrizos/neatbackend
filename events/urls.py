from django.urls import path

from . import views

urlpatterns = [
    path('', views.events_list, name='events_list'),
    path('<int:event_id>/attend/', views.event_attend, name='event_attend'),
    path('<int:event_id>/comments/', views.event_comments, name='event_comments'),
]
