"""The review queue.

Crawled events land as `pending` and are invisible until someone says
otherwise. This is that someone's screen — Django's admin rather than a page
of our own, because the job is "read a row, check it against the source, press
one of two buttons", and the admin already does that well.

What a reviewer is actually checking, in order: is the date right (the field
sources get wrong most often, and the one that matters most on a page about
today), is the city right, and is this a real event rather than a listing page
that parsed like one.

And now one more, which the score answers for them: is this worth showing at
all? `quality` (events/ranking.py) sorts the queue best first, `why` says what
earned it, and "Publish the ones worth showing" publishes only what clears the
tab's bar with a real start time. `editor pick` forces an event onto the tab
and to the top of it, whatever its score.
"""

from datetime import timezone as dt_timezone

from django.contrib import admin
from django.utils.html import format_html

from . import ranking
from .ingest.dedupe import has_no_time
from .models import Event


class QualityFilter(admin.SimpleListFilter):
    title = 'quality'
    parameter_name = 'quality'

    def lookups(self, request, model_admin):
        bar = ranking._setting('EVENTS_MIN_QUALITY', ranking.MIN_QUALITY)
        return (('high', '70 and up'), ('shown', f'{bar} and up (clears the bar)'),
                ('low', f'under {bar}'))

    def queryset(self, request, queryset):
        bar = int(ranking._setting('EVENTS_MIN_QUALITY', ranking.MIN_QUALITY))
        if self.value() == 'high':
            return queryset.filter(quality__gte=70)
        if self.value() == 'shown':
            return queryset.filter(quality__gte=bar)
        if self.value() == 'low':
            return queryset.filter(quality__lt=bar)
        return queryset


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ('title', 'city', 'when', 'status', 'quality', 'editor_pick',
                    'popularity', 'why', 'source', 'source_link')
    list_editable = ('editor_pick',)
    list_filter = ('status', QualityFilter, 'editor_pick', 'city', 'source', 'event_type')
    search_fields = ('title', 'location', 'organizer', 'description')
    date_hierarchy = 'date'
    ordering = ('date',)
    actions = ('publish', 'publish_worth_showing', 'reject', 'rescore')
    readonly_fields = ('content_hash', 'external_id', 'created', 'updated', 'quality', 'signals')

    def save_model(self, request, obj, form, change):
        # An edit (a fixed time, a venue, editor pick) changes the score.
        ranking.refresh(obj)
        super().save_model(request, obj, form, change)

    @admin.display(description='popularity')
    def popularity(self, event):
        value = ranking.popularity_of(event)
        return f'{value}/5' if value else '—'

    @admin.display(description='why')
    def why(self, event):
        points, reasons = ranking.score(event)
        signals = event.signals if isinstance(event.signals, dict) else {}
        evidence = signals.get('why') or ''
        text = ', '.join(reasons) or 'listed once'
        return format_html('{}<br><small>{}</small>', text, evidence) if evidence else text

    @admin.display(description='when', ordering='date')
    def when(self, event):
        if not event.date:
            return '—'
        # Dates are stored as the Athens wall clock labelled UTC (see
        # events.ingest.normalise.as_wall_clock), so the stored value *is* the
        # local time — converting it again would move every event by hours
        # and hide the midnight warning below.
        local = event.date.astimezone(dt_timezone.utc)
        stamp = local.strftime('%d/%m %H:%M')
        # Midnight almost always means the source gave a day and no time, which
        # is the thing to fix before publishing rather than a real start.
        if local.hour == 0 and local.minute == 0:
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

    @admin.action(description='Publish the ones worth showing (score clears the bar, has a time)')
    def publish_worth_showing(self, request, queryset):
        bar = int(ranking._setting('EVENTS_MIN_QUALITY', ranking.MIN_QUALITY))
        chosen, left = [], 0
        for event in queryset.exclude(date__isnull=True):
            ranking.refresh(event)
            if event.quality >= bar and not has_no_time(event.date):
                chosen.append(event.pk)
            else:
                left += 1
        published = Event.objects.filter(pk__in=chosen).update(status=Event.PUBLISHED)
        self.message_user(request, f'{published} published, {left} left as they were.')

    @admin.action(description='Rescore')
    def rescore(self, request, queryset):
        for event in queryset:
            ranking.refresh(event, save=True)
        self.message_user(request, f'{queryset.count()} rescored.')

    @admin.action(description='Reject — hide, and do not re-add on the next crawl')
    def reject(self, request, queryset):
        count = queryset.update(status=Event.REJECTED)
        self.message_user(request, f'{count} rejected.')
