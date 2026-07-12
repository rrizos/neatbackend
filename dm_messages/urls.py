from django.urls import path

from . import views

urlpatterns = [
    path('inbox/', views.inbox, name='inbox'),
    path('presence/', views.update_presence, name='update_presence'),
    path('start/', views.start_conversation, name='start_conversation'),
    path('<int:conversation_id>/typing/', views.conversation_typing, name='conversation_typing'),
    path('<int:conversation_id>/', views.conversation_detail, name='conversation_detail'),
    path('<int:conversation_id>/messages/<int:message_id>/delete/', views.message_delete, name='message_delete'),
    path('<int:conversation_id>/messages/<int:message_id>/react/', views.message_react, name='message_react'),
    path('<int:conversation_id>/messages/<int:message_id>/report/', views.message_report, name='message_report'),
]

