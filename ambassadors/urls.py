from django.urls import path

from . import views

# Mounted at /api/ambassadors/. The public link (/a/<code>) and the two admin
# pages are wired in neatbackend/urls.py, where their root paths live.
urlpatterns = [
    path('click/', views.mint_token, name='ambassador_mint'),
    path('seen/', views.confirm_open, name='ambassador_seen'),
    path('claim/', views.claim, name='ambassador_claim'),
]
