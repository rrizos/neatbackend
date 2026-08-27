from django.urls import path

from . import views

urlpatterns = [
    # og-image first: it is the more specific pattern under the same prefix.
    path('<int:post_id>/og-image', views.og_image, name='post_og_image'),
    path('<int:post_id>', views.post_page, name='post_page'),
    path('<int:post_id>/', views.post_page, name='post_page_slash'),
]
