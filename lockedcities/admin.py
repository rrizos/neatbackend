from django.contrib import admin

from .models import LockedCitiesState


@admin.register(LockedCitiesState)
class LockedCitiesStateAdmin(admin.ModelAdmin):
    list_display = ('enabled', 'changed_by', 'changed_at')
