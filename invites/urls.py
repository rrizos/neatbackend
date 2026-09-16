from django.urls import path

from . import views

# Mounted at /api/invites/. The two public pages (/invite, /<user>/invite and
# /invites) are wired in neatbackend/urls.py, where their root-level paths
# have to be spelled out anyway.
urlpatterns = [
    path('sent/', views.invite_sent, name='invite_sent'),
    path('joined/', views.invite_joined, name='invite_joined'),
    path('claim/', views.invite_claim, name='invite_claim'),
]
