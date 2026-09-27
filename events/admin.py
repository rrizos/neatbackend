"""The review queue.

Crawled events land as `pending` and are invisible until someone says
otherwise. This is that someone's screen — Django's admin rather than a page
of our own, because the job is "read a row, check it against the source, press
one of two buttons", and the admin already does that well.

What a reviewer is actually checking, in order: is the date right (the field
sources get wrong most often, and the one that matters most on a page about
today), is the city right, and is this a real event rather than a listing page
that parsed like one.
"""

from django.contrib import admin
from django.utils.html import format_html

from .models import Event


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ('title', 'city', 'when', 'status', 'source', 'source_link')
    list_filter = ('status', 'city', 'source', 'event_type')
    search_fields = ('title', 'location', 'organizer', 'description')
    date_hierarchy = 'date'
    ordering = ('date',)
    actions = ('publish', 'reject')
    readonly_fields = ('content_hash', 'external_id', 'created', 'updated')

    @admin.display(description='when', ordering='date')
    def when(self, event):
        if not event.date:
            return '—'
        # Midnight almost always means the source gave a day and no time, which
        # is the thing to fix before publishing rather than a real start.
        stamp = event.date.strftime('%d/%m %H:%M')
        if event.date.hour == 0 and event.date.minute == 0:
            return format_html('<span style="color:#b45309">{} (no time?)</span>', stamp)
        return stamp

    @admin.display(description='source')
    def source_link(self, event):
        if not event.source_url:
            return '—'
        return format_html('<a href="{}" target="_blank" rel="noopener">check</a>',
                           event.source_url)

    @admin.action(description='Publish — show these in the app')
    def publish(self, request, queryset):
        undated = queryset.filter(date__isnull=True).count()
        published = queryset.exclude(date__isnull=True).update(status=Event.PUBLISHED)
        self.message_user(request, f'{published} published.')
        if undated:
            self.message_user(
                request,
                f'{undated} left alone: an event with no date cannot go on a day.',
                level='warning',
            )

    @admin.action(description='Reject — hide, and do not re-add on the next crawl')
    def reject(self, request, queryset):
        count = queryset.update(status=Event.REJECTED)
        self.message_user(request, f'{count} rejected.')
