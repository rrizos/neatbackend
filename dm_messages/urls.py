from django.urls import path

from . import views

urlpatterns = [
    path('inbox/', views.inbox, name='inbox'),
    path('presence/', views.update_presence, name='update_presence'),
    path('start/', views.start_conversation, name='start_conversation'),
    path('<int:conversation_id>/', views.conversation_detail, name='conversation_detail'),
]

