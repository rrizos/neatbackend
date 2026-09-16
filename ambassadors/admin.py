from django.contrib import admin

from .models import Ambassador, AmbassadorClick, AmbassadorSignup


@admin.register(Ambassador)
class AmbassadorAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'is_active', 'payout_per_signup', 'created')
    search_fields = ('name', 'code', 'contact')


@admin.register(AmbassadorSignup)
class AmbassadorSignupAdmin(admin.ModelAdmin):
    list_display = ('user', 'ambassador', 'status', 'post_count', 'retained_day3', 'created')
    list_filter = ('status',)
    raw_id_fields = ('user', 'click', 'ambassador', 'reviewed_by')


@admin.register(AmbassadorClick)
class AmbassadorClickAdmin(admin.ModelAdmin):
    list_display = ('ambassador', 'source', 'created', 'used_at')
    list_filter = ('source',)
