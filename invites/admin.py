from django.contrib import admin

from .models import InviteEvent


@admin.register(InviteEvent)
class InviteEventAdmin(admin.ModelAdmin):
    list_display = ('kind', 'inviter', 'city', 'joined_user', 'created')
    list_filter = ('kind', 'created')
    search_fields = ('inviter__username', 'joined_user__username', 'city')
    raw_id_fields = ('inviter', 'joined_user')
