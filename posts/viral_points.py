"""Neat Points: what a user earns by landing in a city's Virals top-10.

Two different formulas are at play here and they are deliberately not the same
one:

  * **Ranking** — `likes * 0.45 + comment_count * 0.55`, the existing charts
    ordering in `viral_posts`. It decides *which* ten posts are the top-10.
  * **Points**  — `(likes + comments + shares) * 0.33 * 100`, which decides
    *how much* a post in that top-10 is worth.

The app already computes points with the second formula client-side, over the
ten posts `viral_posts` hands back (see lib/src/profile/neat_pass_page.dart).
Reproducing both formulas exactly is what lets the app switch from its fallback
to this endpoint without the number on screen jumping.

One deliberate difference from that fallback: it only ever asks for the
*viewer's own city* charts, so it silently misses a post that charted somewhere
else. Since Spectator Mode forbids posting outside your own city that is
normally the same set — but it stops being the same the moment someone changes
city, at which point the fallback quietly drops the points they already earned.
This credits every city the user has posts in, so a move doesn't cost them
anything. Expect the endpoint to read slightly higher than the fallback for
those users, and identically for everyone else.
"""

from django.db.models import Count, ExpressionWrapper, F, FloatField
from django.db import transaction

from .models import NeatPointsAward, Post

TOP_N = 10

# Weight applied to each of likes, comments and shares before scaling to 100.
POINT_WEIGHT = 0.33
POINT_SCALE = 100


def period_key_for(moment):
    """The calendar day an award belongs to, in the server's UTC clock.

    Matches `_viral_period_start('daily')`, which is what the Neat Pass reads.
    """
    return moment.strftime('%Y-%m-%d')


def top_posts(city, since, limit=TOP_N):
    """The city's top posts for a window, ranked exactly as `viral_posts` does.

    Kept as its own queryset rather than shared with the view: the view also
    serialises, prefetches previews and applies viewer-specific blocking, none
    of which the points calculation wants.
    """
    return list(
        Post.objects.filter(city=city, created__gte=since)
        .select_related('user')
        .annotate(comment_count=Count('comment_rows', distinct=True))
        .annotate(
            score=ExpressionWrapper(
                (F('likes') * 0.45 + F('comment_count') * 0.55) * 100,
                output_field=FloatField(),
            )
        )
        .order_by('-score', '-created')[:limit]
    )


def points_for(post, comment_count=None):
    """What one top-10 post is worth, matching the app's own arithmetic."""
    if comment_count is None:
        comment_count = getattr(post, 'comment_count', None)
        if comment_count is None:
            comment_count = post.comment_rows.count()
    likes = post.like_rows.count() or post.likes or 0
    shares = post.shares or 0
    return (likes * POINT_WEIGHT + comment_count * POINT_WEIGHT
            + shares * POINT_WEIGHT) * POINT_SCALE


def _record(post, key, city):
    """Upsert one post's award, keeping the highest value it has ever reached."""
    if post.user_id is None:
        return
    earned = points_for(post)
    with transaction.atomic():
        award, created = NeatPointsAward.objects.get_or_create(
            user_id=post.user_id, post=post, period_key=key,
            defaults={'points': earned, 'city': city or ''},
        )
        # High-water mark: a post losing likes must not claw back points the
        # user has already been shown.
        if not created and earned > award.points:
            award.points = earned
            award.save(update_fields=['points', 'updated'])


def refresh_city(city, now, period_start):
    """Award every author currently in one city's top-10."""
    key = period_key_for(now)
    for post in top_posts(city, period_start):
        _record(post, key, city)


def active_cities(period_start):
    return [
        c for c in Post.objects.filter(created__gte=period_start)
        .values_list('city', flat=True).distinct() if (c or '').strip()
    ]


def refresh_all(now, period_start):
    """Snapshot every city's top-10. Driven by the hourly management command.

    Awards cannot only be computed when someone opens their Neat Pass: a post
    can chart on Monday and fall out again long before its author next looks,
    and the balance is meant to be cumulative. Running this on a timer is what
    makes the high-water mark in `_record` mean anything — it catches the peak
    while the post is actually up there.
    """
    cities = active_cities(period_start)
    for city in cities:
        refresh_city(city, now, period_start)
    return len(cities)


def refresh_awards(user, now, period_start):
    """Bring today's awards for `user` up to date, on read.

    Only ranks the cities the user actually posted in today — the top-10 of a
    city they have no entry in cannot award them anything, so computing it
    would be wasted work on a request the user is waiting for. The hourly
    command covers everyone else.
    """
    key = period_key_for(now)

    cities = list(
        Post.objects.filter(user=user, created__gte=period_start)
        .values_list('city', flat=True)
        .distinct()
    )
    for city in cities:
        for post in top_posts(city, period_start):
            if post.user_id == user.id:
                _record(post, key, city)


def total_points(user):
    """The user's cumulative balance across every period."""
    total = 0.0
    for value in NeatPointsAward.objects.filter(user=user).values_list('points', flat=True):
        total += value or 0.0
    return int(round(total))
