from django.urls import path

from . import views

urlpatterns = [
    path('health/', views.health, name='health'),
    path('signup/', views.signup, name='signup'),
    path('login/', views.login, name='login'),
    path('logout/', views.logout, name='logout'),
    path('me/', views.me, name='me'),
    path('profiles/<str:username>/', views.profile_detail, name='profile_detail'),
    path('profiles/<str:username>/followers/', views.followers_list, name='followers_list'),
    path('profiles/<str:username>/following/', views.following_list, name='following_list'),
    path('profiles/<str:username>/follow/', views.follow_toggle, name='follow_toggle'),
    path('suggestions/', views.suggestions, name='suggestions'),
    path('notifications/', views.notifications, name='notifications'),
]
