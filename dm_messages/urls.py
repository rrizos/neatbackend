from django.urls import path

from . import views

urlpatterns = [
    path('inbox/', views.inbox, name='inbox'),
    path('start/', views.start_conversation, name='start_conversation'),
    path('<int:conversation_id>/', views.conversation_detail, name='conversation_detail'),
]

