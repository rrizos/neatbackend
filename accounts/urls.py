from django.urls import path

from . import views, admin_views

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
    path('profiles/<str:username>/block/', views.block_toggle, name='block_toggle'),
    path('blocked/', views.blocked_users, name='blocked_users'),
    path('suggestions/', views.suggestions, name='suggestions'),
    path('search/', views.search_users, name='search_users'),
    path('notifications/', views.notifications, name='notifications'),
    path('search-history/', views.search_history, name='search_history'),
    path('search-history/<str:query>/', views.search_history, name='search_history_item'),
    path('forgot-password/', views.forgot_password, name='forgot_password'),
    path('reset-password/', views.reset_password, name='reset_password'),
    # Admin endpoints
    path('admin/analytics/', admin_views.admin_analytics, name='admin_analytics'),
    path('admin/reports/', admin_views.admin_reports, name='admin_reports'),
    path('admin/reports/<int:report_id>/', admin_views.admin_dismiss_report, name='admin_dismiss_report'),
    path('admin/posts/<int:post_id>/', admin_views.admin_delete_post, name='admin_delete_post'),
    path('admin/users/', admin_views.admin_users, name='admin_users'),
    path('admin/users/<str:username>/verify/', admin_views.admin_verify_user, name='admin_verify_user'),
    path(
        'admin/users/<str:username>/official-eligibility/',
        admin_views.admin_set_official_eligibility,
        name='admin_set_official_eligibility',
    ),
    path('admin/users/<str:username>/delete/', admin_views.admin_delete_user, name='admin_delete_user'),
    path('admin/messages/<int:message_id>/', admin_views.admin_delete_message, name='admin_delete_message'),
    path('admin/comments/', admin_views.admin_comments, name='admin_comments'),
    path('admin/comments/<int:comment_id>/', admin_views.admin_delete_comment, name='admin_delete_comment'),
]
