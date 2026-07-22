from django.urls import path

from . import views

urlpatterns = [
    path('logs/', views.security_logs, name='security_logs'),
    path('summary/', views.security_summary, name='security_summary'),
    path('actions/', views.security_action, name='security_action'),
]
