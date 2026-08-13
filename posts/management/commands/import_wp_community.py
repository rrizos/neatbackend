"""Import the FluentCommunity feed from the old WordPress site into Neat.

Scope is deliberately narrow: **profiles and posts only** -- users, their
profile fields (name, bio, avatar, city, verified), follows, and the feed
itself (posts, media, polls, comments, likes, comment likes, saves). Nothing
else from the WordPress database is read.

The import is purely additive and idempotent: it never deletes or overwrites
existing rows, and re-running it skips everything already imported (matched on
natural keys -- email/username for people, author+text+timestamp for content).

Usage:
    python manage.py import_wp_community \
        --sql "/path/to/wordpress-db.sql" \
        --uploads "/path/to/wordpress/wp-content/uploads/fluent-community" \
        [--dry-run]
"""

import base64
import html
import io
import os
import re
import shutil
from datetime import datetime, timezone as dt_timezone

from PIL import Image
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.avatars import AVATAR_MAX_PX
from accounts.models import Follow, Profile
from posts.models import (
    CommentLike,
    Poll,
    PollOption,
    PollVote,
    Post,
    PostComment,
    PostLike,
    PostMedia,
    PostSave,
)

from ._wpdump import extract_tables, parse_rows, php_unserialize

User = get_user_model()

TABLES = (
    'wp_users',
    'wp_fcom_xprofile',
    'wp_fcom_posts',
    'wp_fcom_post_comments',
    'wp_fcom_post_reactions',
    'wp_fcom_spaces',
    'wp_fcom_space_user',
    'wp_bp_followers',
)

# Column orders, taken from the CREATE TABLE statements in the dump.
COLS = {
    'wp_users': 'ID user_login user_pass user_nicename user_email user_url '
                'user_registered user_activation_key user_status display_name',
    'wp_fcom_xprofile': 'id user_id total_points username status is_verified display_name '
                        'avatar short_description last_activity meta created_at updated_at',
    'wp_fcom_posts': 'id user_id parent_id title slug message message_rendered type '
                     'content_type space_id privacy status featured_image meta is_sticky '
                     'comments_count reactions_count priority expired_at scheduled_at '
                     'created_at updated_at',
    'wp_fcom_post_comments': 'id user_id post_id parent_id reactions_count message '
                             'message_rendered meta type content_type status is_sticky '
                             'created_at updated_at',
    'wp_fcom_post_reactions': 'id user_id object_id parent_id object_type type ip_address '
                              'created_at updated_at',
    'wp_fcom_spaces': 'id user_id parent_id title slug description logo settings type '
                      'privacy status serial meta created_at updated_at',
    'wp_fcom_space_user': 'id space_id user_id status role meta created_at updated_at',
    'wp_bp_followers': 'id follower_id following_id',
}

# WordPress space names that don't exist verbatim in the app's city list
# (neat/lib/src/map/greece_cities.dart). Without this those posts would import
# with a city no Neat account can hold, so they'd never appear in any feed.
# Each maps to the app city that actually covers that place.
CITY_ALIASES = {
    # Spelling differences between the two sites.
    'Λάρισσα': 'Λάρισα',
    'Σαντορίνι': 'Σαντορίνη',
    'Μεσσολόγγι': 'Μεσολόγγι',
    # Island capital -> the island as the app names it.
    'Μυτιλήνη': 'Λέσβος',
    'Αργοστόλι': 'Κεφαλονιά',
    # Places the app's list doesn't carry -> the listed city that covers them.
    'Περαία': 'Θεσσαλονίκη',
    'Νέα Μουδανία': 'Θεσσαλονίκη',
    'Αλεξάνδρεια': 'Βέροια',
    'Σαμοθράκη': 'Αλεξανδρούπολη',
    'Άνδρος': 'Σύρος',
    'Τήνος': 'Σύρος',
    'Σαλαμίνα': 'Αθήνα',
    'Καρπενήσι': 'Λαμία',
    'Σκόπελος': 'Βόλος',
    'Ικαρία': 'Σάμος',
    'Θάσος': 'Καβάλα',
    'Τήλος': 'Ρόδος',
    'Σύμη': 'Ρόδος',
}

# The old site's media host is dead (netnest.net now 404s), so every image URL
# has to be re-pointed at a file copied out of the backup's uploads folder.
DEAD_MEDIA_HOST = 'netnest.net'
MEDIA_SUBDIR = 'imported/wp'

# Post images are served as files, but avatars can't be: the app decodes
# `Profile.avatar_url` with `decodeAvatarUrl()` (neat/lib/src/core/post_card.dart),
# which bails out on anything that isn't a base64 data URL and falls back to the
# username initial. Every avatar the app itself stores is a data URL, so imported
# ones have to be too -- downscaled to the same AVATAR_MAX_PX the upload path uses.
AVATAR_JPEG_QUALITY = 82

# FluentCommunity wraps bare links in angle brackets; Neat renders post text
# as-is, so they'd otherwise show as literal <...> around the URL.
_ANGLE_LINK_RE = re.compile(r'<(https?://[^>\s]+)>')

# Markdown escaping applied by the old editor, e.g. `\#eimaigay`.
_MD_ESCAPE_RE = re.compile(r'\\([#*_~`\[\]()>+\-.!])')

# Emoji destroyed by a latin1/utf8 conversion somewhere in the WordPress site's
# history: every 4-byte emoji became exactly four literal '?' characters. Runs
# of 4/8/12 dominate the data and real emoji survive elsewhere in the same
# rows, so a run of >=4 is lost emoji, not punctuation. The original character
# is unrecoverable, so drop it -- but keep any remainder, because "kafes?????"
# is one genuine question mark followed by one dead emoji.
_LOST_EMOJI_RE = re.compile(r'\?{4,}')

# Whitespace left stranded once entities/emoji are removed -- a dead emoji with
# a space on each side collapses to a visible double gap, and Flutter's Text
# does not collapse runs of spaces the way HTML would.
_INNER_GAP_RE = re.compile(r'[ \t]{2,}')
_LINE_TAIL_RE = re.compile(r'[ \t]+$', re.M)


def clean_text(value):
    """Strip the WordPress export's artifacts out of user-written text.

    Deliberately narrow -- it only removes things that are certainly encoding
    debris. It leaves `//` alone (that's inside real URLs), leaves `**` alone
    (users self-censoring, e.g. "γάμ****"), and leaves zero-width joiners alone
    (they hold multi-part emoji like 😮‍💨 together).
    """
    if not value:
        return ''
    text = html.unescape(value)                       # &#x20; -> space
    text = text.replace('\u00a0', ' ')                # NBSP after @mentions
    text = _MD_ESCAPE_RE.sub(r'\1', text)             # \# -> #
    text = _LOST_EMOJI_RE.sub(lambda m: '?' * (len(m.group(0)) % 4), text)
    text = _ANGLE_LINK_RE.sub(r'\1', text)            # <https://x> -> https://x
    text = _INNER_GAP_RE.sub(' ', text)
    text = _LINE_TAIL_RE.sub('', text)
    return text.strip()


def _dt(value):
    """Parse a dump timestamp into an aware UTC datetime."""
    if not value or value.startswith('0000'):
        return None
    return datetime.strptime(value, '%Y-%m-%d %H:%M:%S').replace(tzinfo=dt_timezone.utc)


def _rows(raw, table):
    """Parse one table's INSERT text into a list of dicts."""
    names = COLS[table].split()
    return [dict(zip(names, row)) for row in parse_rows(raw.get(table, ''))]


class Command(BaseCommand):
    help = 'Import profiles and the post feed from a WordPress/FluentCommunity SQL dump.'

    def add_arguments(self, parser):
        parser.add_argument('--sql', required=True, help='Path to wordpress-db.sql')
        parser.add_argument(
            '--uploads',
            default='',
            help='Path to wp-content/uploads/fluent-community (for avatars and post images)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Run the whole import in a transaction and roll it back, reporting the counts.',
        )
        parser.add_argument(
            '--refresh-avatars',
            action='store_true',
            help='Only re-apply avatars to already-imported profiles, then stop. '
                 'Repairs a run made before avatars were stored as data URLs.',
        )
        parser.add_argument(
            '--clean-text',
            action='store_true',
            help='Only re-clean the text of already-imported posts, comments and bios, '
                 'then stop. Skips any row whose text no longer matches what was '
                 'imported, so edited content is never touched.',
        )

    def handle(self, *args, **options):
        sql_path = options['sql']
        uploads = options['uploads']
        self.dry_run = options['dry_run']

        if not os.path.isfile(sql_path):
            raise CommandError(f'SQL dump not found: {sql_path}')
        if uploads and not os.path.isdir(uploads):
            raise CommandError(f'Uploads directory not found: {uploads}')
        if not uploads:
            self.stdout.write(self.style.WARNING(
                'No --uploads given: avatars and post images will be skipped '
                '(the old media host is offline, so their URLs cannot be reused).'
            ))

        self.stdout.write(f'Reading {sql_path} ...')
        raw = extract_tables(sql_path, TABLES)

        self.wp_users = _rows(raw, 'wp_users')
        self.xprofiles = _rows(raw, 'wp_fcom_xprofile')
        self.wp_posts = _rows(raw, 'wp_fcom_posts')
        self.wp_comments = _rows(raw, 'wp_fcom_post_comments')
        self.reactions = _rows(raw, 'wp_fcom_post_reactions')
        self.spaces = _rows(raw, 'wp_fcom_spaces')
        self.space_users = _rows(raw, 'wp_fcom_space_user')
        self.followers = _rows(raw, 'wp_bp_followers')
        del raw

        self.stdout.write(
            f'  {len(self.wp_users)} users, {len(self.xprofiles)} profiles, '
            f'{len(self.wp_posts)} posts, {len(self.wp_comments)} comments, '
            f'{len(self.reactions)} reactions, {len(self.followers)} follows'
        )

        self.media_map, self.avatar_map = self._prepare_media(uploads)
        self.poll_options = {}
        self.stats = {}

        if options['refresh_avatars'] or options['clean_text']:
            with transaction.atomic():
                if options['refresh_avatars']:
                    self._refresh_avatars()
                if options['clean_text']:
                    self._clean_imported_text()
                if self.dry_run:
                    transaction.set_rollback(True)
            for key, value in self.stats.items():
                self.stdout.write(f'  {key}: {value}')
            if self.dry_run:
                self.stdout.write(self.style.WARNING('\nDry run — rolled back, nothing written.'))
            return

        # One transaction: a half-finished import would leave posts without
        # their authors' profiles, which is worse than importing nothing.
        # --dry-run runs the identical code path and rolls back at the end, so
        # the reported counts are the real ones.
        with transaction.atomic():
            self._import_users()
            self._import_follows()
            self._import_posts()
            self._import_comments()
            self._import_reactions()
            if self.dry_run:
                transaction.set_rollback(True)

        self.stdout.write('')
        for key, value in self.stats.items():
            self.stdout.write(f'  {key}: {value}')
        if self.dry_run:
            self.stdout.write(self.style.WARNING('\nDry run — rolled back, nothing written.'))
        else:
            self.stdout.write(self.style.SUCCESS('\nImport complete.'))

    # ---------------------------------------------------------------- media

    def _prepare_media(self, uploads):
        """Resolve the backup's dead image URLs into ones the app can render.

        Post images are copied into MEDIA_ROOT and referenced by path; avatars
        are inlined as base64 data URLs (see AVATAR_JPEG_QUALITY above for why).
        Returns (post image map, avatar map), both {old URL: new value}.
        """
        post_images = {url for url in map(self._post_image_url, self.wp_posts) if url}
        avatars = {p['avatar'] for p in self.xprofiles if p['avatar']}

        external = {u for u in post_images | avatars if DEAD_MEDIA_HOST not in u}
        if external:
            # Link-preview thumbnails hotlinked from Instagram/TikTok CDNs.
            # Those URLs are signed and already expired, so importing them
            # would only produce broken images; the link itself stays in the
            # post text.
            self.stdout.write(self.style.WARNING(
                f'Skipping {len(external)} off-site preview image(s) — expired CDN links.'
            ))

        if not uploads:
            return {}, {}

        dest_dir = os.path.join(settings.MEDIA_ROOT, *MEDIA_SUBDIR.split('/'))
        if not self.dry_run:
            os.makedirs(dest_dir, exist_ok=True)

        image_map = {}
        missing = 0
        for url in sorted(post_images - external):
            source = self._source_file(uploads, url)
            if source is None:
                missing += 1
                continue
            filename = os.path.basename(source)
            if not self.dry_run:
                target = os.path.join(dest_dir, filename)
                if not os.path.exists(target):
                    shutil.copy2(source, target)
            image_map[url] = f'{settings.MEDIA_URL}{MEDIA_SUBDIR}/{filename}'

        avatar_map = {}
        for url in sorted(avatars - external):
            source = self._source_file(uploads, url)
            if source is None:
                missing += 1
                continue
            data_url = self._avatar_data_url(source)
            if data_url:
                avatar_map[url] = data_url

        verb = 'would copy' if self.dry_run else 'copied'
        self.stdout.write(
            f'Media: {verb} {len(image_map)} post image(s), '
            f'encoded {len(avatar_map)} avatar(s), {missing} missing from the backup'
        )
        return image_map, avatar_map

    def _source_file(self, uploads, url):
        path = os.path.join(uploads, url.rsplit('/', 1)[-1])
        return path if os.path.isfile(path) else None

    def _avatar_data_url(self, source):
        """Downscale an avatar file to a base64 data URL, as the app stores them."""
        try:
            with Image.open(source) as img:
                has_alpha = img.mode in ('RGBA', 'LA') or (
                    img.mode == 'P' and 'transparency' in img.info
                )
                img.thumbnail((AVATAR_MAX_PX, AVATAR_MAX_PX), Image.LANCZOS)
                out = io.BytesIO()
                if has_alpha:
                    img.convert('RGBA').save(out, format='PNG', optimize=True)
                    mime = 'image/png'
                else:
                    img.convert('RGB').save(
                        out, format='JPEG', quality=AVATAR_JPEG_QUALITY, optimize=True
                    )
                    mime = 'image/jpeg'
            encoded = base64.b64encode(out.getvalue()).decode('ascii')
            return f'data:{mime};base64,{encoded}'
        except Exception as exc:
            self.stdout.write(self.style.WARNING(
                f'Could not encode avatar {os.path.basename(source)}: {exc}'
            ))
            return ''

    def _post_image_url(self, post):
        """The uploaded image attached to a post, if any."""
        meta = php_unserialize(post['meta']) or {}
        preview = meta.get('media_preview') or {}
        if isinstance(preview, dict) and preview.get('is_uploaded'):
            return preview.get('image') or ''
        return ''

    # ---------------------------------------------------------------- users

    def _import_users(self):
        """Create a Django user + Profile per WordPress community member.

        Imported accounts get an unusable password -- WordPress phpass hashes
        can't be verified by Django -- so the people behind them sign in
        through the app's normal password-reset flow.

        A WordPress account whose email already exists in Neat is treated as
        the same person: their imported content is attached to the live
        account, whose profile is left exactly as it is.
        """
        by_wp_id = {u['ID']: u for u in self.wp_users}
        xp_by_user = {p['user_id']: p for p in self.xprofiles}

        # Worth importing if they have a community profile or authored
        # something we're importing.
        authors = {p['user_id'] for p in self.wp_posts if p['user_id']}
        authors |= {c['user_id'] for c in self.wp_comments if c['user_id']}
        wanted = (set(xp_by_user) | authors) & set(by_wp_id)

        cities = self._city_per_user()
        self.user_map = {}
        created = reused = renamed = 0

        for wp_id in sorted(wanted, key=int):
            wp = by_wp_id[wp_id]
            xp = xp_by_user.get(wp_id, {})
            email = (wp['user_email'] or '').strip()
            username = (xp.get('username') or wp['user_login'] or '').strip()[:150]
            if not username:
                continue

            existing = User.objects.filter(email__iexact=email).first() if email else None
            if existing is not None:
                # Already a Neat account — don't touch its profile.
                self.user_map[wp_id] = existing
                reused += 1
                continue

            if User.objects.filter(username=username).exists():
                # Someone else in Neat already holds this name; don't take it over.
                username = self._free_username(username)
                renamed += 1

            user = User.objects.create(username=username, email=email)
            user.set_unusable_password()
            joined = _dt(wp['user_registered'])
            if joined:
                user.date_joined = joined
            user.save(update_fields=['password', 'date_joined'])

            self._create_profile(user, xp, cities.get(wp_id, ''))
            self.user_map[wp_id] = user
            created += 1

        self.stats['users created'] = created
        self.stats['users matched to existing accounts'] = reused
        if renamed:
            self.stats['users renamed (username taken)'] = renamed

    def _refresh_avatars(self):
        """Re-apply avatars to profiles imported by an earlier run.

        Only touches a profile whose avatar is empty or still points at the
        imported-files path -- a real account's own uploaded avatar (always a
        data URL) is never overwritten.
        """
        stale_prefix = f'{settings.MEDIA_URL}{MEDIA_SUBDIR}/'
        by_wp_id = {u['ID']: u for u in self.wp_users}
        fixed = skipped = unmatched = 0

        for xp in self.xprofiles:
            data_url = self.avatar_map.get(xp.get('avatar') or '')
            if not data_url:
                continue
            wp = by_wp_id.get(xp['user_id'])
            if wp is None:
                continue
            email = (wp['user_email'] or '').strip()
            user = User.objects.filter(email__iexact=email).first() if email else None
            if user is None:
                unmatched += 1
                continue
            profile = Profile.objects.filter(user=user).first()
            if profile is None:
                unmatched += 1
                continue
            if profile.avatar_url and not profile.avatar_url.startswith(stale_prefix):
                skipped += 1  # the account has its own avatar -- leave it alone
                continue
            profile.avatar_url = data_url
            profile.save(update_fields=['avatar_url'])
            fixed += 1

        self.stats['avatars repaired'] = fixed
        self.stats['profiles left alone (own avatar)'] = skipped
        self.stats['avatars with no matching profile'] = unmatched

    def _clean_imported_text(self):
        """Re-clean text on rows a previous run imported before `clean_text` existed.

        A row is only rewritten when its current text is byte-for-byte what the
        old import wrote, so anything since edited by its author is left alone.
        """
        def legacy(message):
            return _ANGLE_LINK_RE.sub(r'\1', (message or '').strip())

        posts = comments = bios = names = 0
        samples = []

        for wp in self.wp_posts:
            was, now = legacy(wp['message']), clean_text(wp['message'])
            if was == now:
                continue
            changed = Post.objects.filter(text=was, created=_dt(wp['created_at'])).update(text=now)
            posts += changed
            if changed and len(samples) < 6:
                samples.append(('post', was, now))

        for wp in self.wp_comments:
            was, now = legacy(wp['message']), clean_text(wp['message'])
            if was == now:
                continue
            changed = PostComment.objects.filter(
                text=was, created=_dt(wp['created_at'])
            ).update(text=now)
            comments += changed
            if changed and len(samples) < 6:
                samples.append(('comment', was, now))

        by_wp_id = {u['ID']: u for u in self.wp_users}
        for xp in self.xprofiles:
            wp = by_wp_id.get(xp['user_id'])
            email = (wp['user_email'] or '').strip() if wp else ''
            if not email:
                continue
            user = User.objects.filter(email__iexact=email).first()
            profile = Profile.objects.filter(user=user).first() if user else None
            if profile is None:
                continue
            # The old import wrote the raw value, unstripped -- match that exactly.
            was_bio = xp.get('short_description') or ''
            now_bio = clean_text(xp.get('short_description'))
            if was_bio != now_bio and profile.bio == was_bio:
                profile.bio = now_bio
                profile.save(update_fields=['bio'])
                bios += 1
            was_name = (xp.get('display_name') or '')[:150]
            now_name = clean_text(xp.get('display_name'))[:150]
            if was_name != now_name and profile.full_name == was_name:
                profile.full_name = now_name
                profile.save(update_fields=['full_name'])
                names += 1

        for kind, was, now in samples:
            self.stdout.write(f'  e.g. {kind}: {was[-58:]!r}\n         -> {now[-58:]!r}')

        self.stats['posts cleaned'] = posts
        self.stats['comments cleaned'] = comments
        self.stats['bios cleaned'] = bios
        self.stats['display names cleaned'] = names

    def _free_username(self, base):
        base = base[:145]
        for suffix in range(2, 1000):
            candidate = f'{base}{suffix}'
            if not User.objects.filter(username=candidate).exists():
                return candidate
        raise CommandError(f'Could not find a free username for {base!r}')

    def _create_profile(self, user, xp, city):
        profile, _ = Profile.objects.get_or_create(user=user)
        profile.full_name = clean_text(xp.get('display_name'))[:150]
        profile.bio = clean_text(xp.get('short_description'))
        profile.city = city
        profile.avatar_url = self.avatar_map.get(xp.get('avatar') or '', '')
        profile.is_verified = xp.get('is_verified') == '1'
        profile.last_active = _dt(xp.get('last_activity'))
        profile.save(update_fields=[
            'full_name', 'bio', 'city', 'avatar_url', 'is_verified', 'last_active',
        ])

    def _city_per_user(self):
        """Pick one Neat city per WordPress user.

        Preference order: the city they posted in most, then the first city
        space they joined. The app's feed is city-scoped, so a member with no
        city sees nothing -- but guessing the wrong city is worse than leaving
        it blank for them to choose on first launch.
        """
        space_city = self._space_cities()
        counts = {}
        for post in self.wp_posts:
            city = space_city.get(post['space_id'])
            if post['user_id'] and city:
                tally = counts.setdefault(post['user_id'], {})
                tally[city] = tally.get(city, 0) + 1

        cities = {}
        for row in sorted(self.space_users, key=lambda r: int(r['id'])):
            city = space_city.get(row['space_id'])
            if city and row['user_id'] not in cities:
                cities[row['user_id']] = city
        for user_id, tally in counts.items():
            cities[user_id] = max(tally.items(), key=lambda kv: kv[1])[0]
        return cities

    def _space_cities(self):
        """{space id: Neat city name} for the spaces that represent a city."""
        out = {}
        for space in self.spaces:
            # The single top-level space ("Greece") is the group holding the
            # city spaces, not a city itself.
            if not space['parent_id']:
                continue
            title = (space['title'] or '').strip()
            if title:
                out[space['id']] = CITY_ALIASES.get(title, title)
        return out

    # -------------------------------------------------------------- follows

    def _import_follows(self):
        created = 0
        for row in self.followers:
            follower = self.user_map.get(row['follower_id'])
            following = self.user_map.get(row['following_id'])
            if not follower or not following or follower == following:
                continue
            _, made = Follow.objects.get_or_create(follower=follower, following=following)
            created += made
        self.stats['follows'] = created

    # ---------------------------------------------------------------- posts

    def _import_posts(self):
        space_city = self._space_cities()
        self.post_map = {}
        created = skipped = no_city = 0

        for wp in sorted(self.wp_posts, key=lambda p: int(p['id'])):
            if wp['status'] != 'published':
                continue
            user = self.user_map.get(wp['user_id'])
            text = clean_text(wp['message'])
            if not text or user is None:
                continue
            city = space_city.get(wp['space_id'], '')
            if not city:
                no_city += 1
            created_at = _dt(wp['created_at'])

            existing = Post.objects.filter(user=user, text=text, created=created_at).first()
            if existing is not None:
                self.post_map[wp['id']] = existing
                self._map_existing_poll(existing, wp)
                skipped += 1
                continue

            image_url = self.media_map.get(self._post_image_url(wp), '')
            post = Post.objects.create(
                user=user,
                author=user.username,
                text=text,
                city=city,
                image_url=image_url,
            )
            # `created` is auto_now_add, so it can only be set after insert.
            Post.objects.filter(pk=post.pk).update(created=created_at)
            post.created = created_at
            if image_url:
                PostMedia.objects.create(post=post, media_type='image', url=image_url, order=0)
            self._create_poll(post, wp)
            self.post_map[wp['id']] = post
            created += 1

        self.stats['posts'] = created
        self.stats['posts already present (skipped)'] = skipped
        if no_city:
            self.stats['posts with no city'] = no_city

    def _poll_config(self, wp):
        meta = php_unserialize(wp['meta']) or {}
        config = meta.get('survey_config')
        if not isinstance(config, dict):
            return None
        options = config.get('options')
        return options if isinstance(options, dict) and options else None

    def _create_poll(self, post, wp):
        options = self._poll_config(wp)
        if not options:
            return
        poll = Poll.objects.create(post=post)
        slugs = {}
        for order, option in enumerate(options.values()):
            slugs[option.get('slug')] = PollOption.objects.create(
                poll=poll,
                text=(option.get('label') or '')[:200],
                order=order,
            )
        self.poll_options[wp['id']] = slugs

    def _map_existing_poll(self, post, wp):
        """Re-derive slug -> PollOption for a post imported by an earlier run,
        so its votes still resolve on a re-run."""
        options = self._poll_config(wp)
        poll = getattr(post, 'poll', None)
        if not options or poll is None:
            return
        rows = list(poll.options.order_by('order'))
        slugs = [option.get('slug') for option in options.values()]
        self.poll_options[wp['id']] = dict(zip(slugs, rows))

    # ------------------------------------------------------------- comments

    def _import_comments(self):
        self.comment_map = {}
        created = skipped = 0
        # Parents first, so a reply can always resolve its parent.
        ordered = sorted(
            self.wp_comments,
            key=lambda c: (1 if c['parent_id'] else 0, int(c['id'])),
        )
        for wp in ordered:
            if wp['status'] != 'published':
                continue
            post = self.post_map.get(wp['post_id'])
            user = self.user_map.get(wp['user_id'])
            text = clean_text(wp['message'])
            if post is None or user is None or not text:
                continue
            parent = self.comment_map.get(wp['parent_id']) if wp['parent_id'] else None
            if wp['parent_id'] and parent is None:
                continue  # a reply whose parent was deleted before the backup
            created_at = _dt(wp['created_at'])

            existing = PostComment.objects.filter(
                post=post, user=user, text=text, created=created_at
            ).first()
            if existing is not None:
                self.comment_map[wp['id']] = existing
                skipped += 1
                continue

            comment = PostComment.objects.create(
                post=post, user=user, parent=parent, text=text
            )
            PostComment.objects.filter(pk=comment.pk).update(created=created_at)
            comment.created = created_at
            self.comment_map[wp['id']] = comment
            created += 1

        self.stats['comments'] = created
        self.stats['comments already present (skipped)'] = skipped

    # -------------------------------------------------- likes, saves, votes

    def _import_reactions(self):
        likes = comment_likes = saves = votes = 0

        for row in self.reactions:
            user = self.user_map.get(row['user_id'])
            if user is None:
                continue
            created_at = _dt(row['created_at'])
            kind = row['type']

            if kind == 'like' and row['object_type'] == 'feed':
                post = self.post_map.get(row['object_id'])
                if post is not None:
                    likes += self._touch(PostLike, created_at, post=post, user=user)

            elif kind == 'like' and row['object_type'] == 'comment':
                comment = self.comment_map.get(row['object_id'])
                if comment is not None:
                    comment_likes += self._touch(
                        CommentLike, created_at, comment=comment, user=user
                    )

            elif kind == 'bookmark':
                post = self.post_map.get(row['object_id'])
                if post is not None:
                    saves += self._touch(PostSave, created_at, post=post, user=user)

            elif kind == 'survey_vote':
                option = (self.poll_options.get(row['object_id']) or {}).get(row['object_type'])
                if option is not None:
                    votes += self._touch(
                        PollVote, created_at,
                        unique_on=('poll', 'user'),
                        poll=option.poll, option=option, user=user,
                    )

        # `Post.likes` is the fallback the feed reads when a post has no like
        # rows; keep it consistent with the rows we just created.
        for post in self.post_map.values():
            count = post.like_rows.count()
            if count and post.likes != count:
                Post.objects.filter(pk=post.pk).update(likes=count)

        self.stats['post likes'] = likes
        self.stats['comment likes'] = comment_likes
        self.stats['saved posts'] = saves
        self.stats['poll votes'] = votes

    def _touch(self, model, created_at, unique_on=None, **fields):
        """get_or_create `model`, back-dating `created`. Returns 1 if created."""
        lookup = {name: fields[name] for name in unique_on} if unique_on else fields
        obj, made = model.objects.get_or_create(**lookup, defaults=fields)
        if made and created_at:
            model.objects.filter(pk=obj.pk).update(created=created_at)
        return 1 if made else 0
