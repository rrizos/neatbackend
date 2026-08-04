"""Fetch Open Graph metadata for a user-supplied URL.

Everything here runs against URLs that *any* user can type into a post, a
comment or a DM, so the fetcher is written defensively:

  * only http/https, and only on the default ports,
  * every hostname is resolved up-front and every resulting IP checked against
    the private/reserved ranges before a socket is opened,
  * the connection is made to the *validated* IP with the Host header and SNI
    set by hand, so a DNS entry that changes between the check and the connect
    (DNS rebinding) can't get us to talk to an internal address anyway,
  * redirects are followed manually so each hop gets the same treatment,
  * the body read is capped and the whole thing is on a short timeout.

The blocked ranges matter more than usual here: the API runs on EC2, where
169.254.169.254 hands out IAM credentials to anything that asks.
"""

import gzip
import html
import http.client
import ipaddress
import re
import socket
import ssl
import zlib
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

from . import oembed

# Enough to cover <head> on any sane page; we stop reading once </head> shows up.
MAX_BODY_BYTES = 512 * 1024
TIMEOUT_SECONDS = 6
MAX_REDIRECTS = 3

# Identify honestly as a link-preview bot rather than as Chrome.
#
# This is not cosmetic. Instagram serves a JS shell with no og: tags to
# anything that looks like a browser, and the full crawler HTML — caption,
# author, real thumbnail — to anything that doesn't. Verified: this UA gets
# og:title from Instagram, in.gr, YouTube, Wikipedia, GitHub and the BBC,
# while a Chrome UA gets nothing at all from Instagram.
#
# Deliberately our own name and not `facebookexternalhit`: the crawler HTML
# is served to any non-browser agent, so there is nothing to gain from
# impersonating someone else's crawler, and this leaves site owners a real
# contact if they want to block us.
USER_AGENT = 'Mozilla/5.0 (compatible; neatbot/1.0; +https://neatapp.gr)'


class UnsafeUrl(Exception):
    """The URL points somewhere we refuse to fetch from."""


def _ssl_context():
    """A verifying context that works wherever this runs.

    ssl's default trust store is whatever the host Python was built against,
    which is empty on some installs (Homebrew/python.org builds on macOS).
    certifi ships its own CA bundle and comes along with firebase-admin, so
    prefer it and fall back to the system store. Verification stays on either
    way — a link preview is not worth accepting forged certificates for.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _ip_is_blocked(ip):
    """True for anything not on the public internet."""
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local      # 169.254.0.0/16 — the EC2 metadata service
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or (ip.version == 6 and ip.ipv4_mapped is not None
            and _ip_is_blocked(ip.ipv4_mapped))
    )


def _resolve_and_validate(host, port):
    """Resolve [host] and return one safe (family, sockaddr, ip) to connect to.

    Raises UnsafeUrl if the host doesn't resolve or *any* address it resolves
    to is non-public — all of them, not just the one we'd pick, so a host with
    one public and one internal A record doesn't slip through.
    """
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeUrl(f'could not resolve {host}') from exc
    if not infos:
        raise UnsafeUrl(f'could not resolve {host}')

    chosen = None
    for family, _type, _proto, _canon, sockaddr in infos:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            raise UnsafeUrl(f'unparseable address for {host}')
        if _ip_is_blocked(ip):
            raise UnsafeUrl(f'{host} resolves to a non-public address ({ip})')
        if chosen is None:
            chosen = (family, sockaddr, ip)
    return chosen


def normalise_url(raw):
    """Give a scheme-less URL one, so "www.in.gr" is fetchable.

    People type bare hosts far more often than they type schemes, and the
    client extracts them verbatim from message text. urlsplit reads "www.in.gr"
    as a *path* with no host at all, so everything downstream — the SSRF
    checks, the oEmbed provider match, redirect joining — has to be given a
    real absolute URL first.
    """
    raw = (raw or '').strip()
    if not raw:
        return raw
    # "//host/path" is protocol-relative, not scheme-less; and a bare host must
    # not be mistaken for a scheme by a colon later in the path ("in.gr/a:b").
    if raw.startswith('//'):
        return 'https:' + raw
    if not urlsplit(raw).scheme:
        return 'https://' + raw
    return raw


def _validate_url(raw):
    """Normalise [raw] and check the parts we can check without a socket."""
    parts = urlsplit(normalise_url(raw))
    if parts.scheme not in ('http', 'https'):
        raise UnsafeUrl('only http and https are supported')
    if not parts.hostname:
        raise UnsafeUrl('no host in URL')
    # Only the standard ports. Anything else is far more likely to be someone
    # probing an internal service than a real page with a preview.
    port = parts.port or (443 if parts.scheme == 'https' else 80)
    if port not in (80, 443):
        raise UnsafeUrl('only ports 80 and 443 are supported')
    # A bare IP literal is checked here; hostnames get checked at resolve time.
    try:
        ip = ipaddress.ip_address(parts.hostname)
    except ValueError:
        pass
    else:
        if _ip_is_blocked(ip):
            raise UnsafeUrl('address is not public')
    return parts, port


class _PinnedConnection(http.client.HTTPConnection):
    """An HTTP(S) connection that talks to a pre-validated address.

    http.client is what parses the response — status line, headers, and
    crucially chunked transfer-encoding, which almost every large site uses.
    Only the socket setup is ours, so the connection still goes to the IP we
    checked rather than re-resolving the hostname (which would reopen the DNS
    rebinding window) while `self.host` keeps the Host header and SNI correct.
    """

    def __init__(self, hostname, port, family, sockaddr, ssl_context=None):
        super().__init__(hostname, port, timeout=TIMEOUT_SECONDS)
        self._family = family
        self._sockaddr = sockaddr
        self._ssl_context = ssl_context

    def connect(self):
        sock = socket.socket(self._family, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._sockaddr)
        if self._ssl_context is not None:
            sock = self._ssl_context.wrap_socket(sock, server_hostname=self.host)
        self.sock = sock


def _decompress(body, encoding):
    """Undo Content-Encoding. Servers send it even when we ask for identity."""
    encoding = (encoding or '').lower().strip()
    try:
        if encoding == 'gzip':
            return gzip.decompress(body)
        if encoding == 'deflate':
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)
    except Exception:
        # A truncated body (we cap the read) can't always be inflated. Falling
        # back to the raw bytes lets the parser salvage whatever is readable.
        return body
    return body


def _fetch_once(url, accept='text/html,application/xhtml+xml'):
    """One hop. Returns (status, headers, body_bytes)."""
    parts, port = _validate_url(url)
    family, sockaddr, _ip = _resolve_and_validate(parts.hostname, port)
    ctx = _ssl_context() if parts.scheme == 'https' else None

    conn = _PinnedConnection(parts.hostname, port, family, sockaddr, ctx)
    try:
        path = urlunsplit(('', '', parts.path or '/', parts.query, '')) or '/'
        conn.request('GET', path, headers={
            'Host': parts.netloc,
            'User-Agent': USER_AGENT,
            'Accept': accept,
            'Accept-Language': 'el,en;q=0.8',
            # We decompress gzip/deflate ourselves but can't do brotli from the
            # stdlib, so don't advertise it.
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'close',
        })
        resp = conn.getresponse()
        headers = {k.lower(): v for k, v in resp.getheaders()}
        status = resp.status

        body = b''
        ctype = headers.get('content-type', '')
        # Only read a body we could actually parse. A 40MB PDF behind a link
        # shouldn't be pulled down just to find it has no og: tags.
        if 300 <= status < 400:
            pass
        elif 'html' in ctype or 'json' in ctype or not ctype:
            body = _decompress(resp.read(MAX_BODY_BYTES),
                               headers.get('content-encoding'))
        return status, headers, body
    finally:
        try:
            conn.close()
        except OSError:
            pass


def fetch_head_html(url):
    """Follow redirects (safely) and return (final_url, html_text).

    Raises UnsafeUrl if any hop is unsafe or the page isn't fetchable HTML.
    """
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        status, headers, body = _fetch_once(current)
        if 300 <= status < 400 and headers.get('location'):
            # Relative Location headers are legal and common.
            current = urljoin(current, headers['location'])
            continue
        if status != 200:
            raise UnsafeUrl(f'upstream returned {status}')
        charset = 'utf-8'
        ctype = headers.get('content-type', '')
        if 'charset=' in ctype:
            charset = ctype.split('charset=')[-1].split(';')[0].strip() or 'utf-8'
        try:
            text = body.decode(charset, 'replace')
        except LookupError:
            text = body.decode('utf-8', 'replace')
        return current, text
    raise UnsafeUrl('too many redirects')


class _MetaParser(HTMLParser):
    """Pulls og:/twitter:/bare title+description out of <head>."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.title = ''
        self._in_title = False
        self._done = False

    def handle_starttag(self, tag, attrs):
        if self._done:
            return
        if tag == 'title':
            self._in_title = True
            return
        if tag != 'meta':
            return
        a = {k.lower(): (v or '') for k, v in attrs}
        # og: and twitter: live on `property`, plain description on `name`.
        key = (a.get('property') or a.get('name') or '').lower()
        content = a.get('content', '').strip()
        if key and content and key not in self.meta:
            self.meta[key] = content

    def handle_endtag(self, tag):
        if tag == 'title':
            self._in_title = False
        elif tag == 'head':
            self._done = True

    def handle_data(self, data):
        if self._in_title and not self.title:
            self.title = data.strip()


def _clean(value, limit):
    if not value:
        return ''
    collapsed = ' '.join(html.unescape(value).split())
    return collapsed[:limit]


# "NASA on Instagram: "caption…"" — Instagram packs the author into og:title
# and the handle into og:url (/<handle>/reel/<id>/). Pulling them apart is what
# turns a generic card into one that names who posted.
#
# The connecting word is localised ("on" in English, "στο" in Greek, and we ask
# for Greek), so it is matched as "any one token" rather than spelled out. The
# leading group is greedy so a multi-word display name ("Zach King on
# Instagram:") keeps all of its words instead of stopping at the first.
_IG_TITLE = re.compile(r'^(.+)\s+\S+\s+Instagram\s*[::]', re.DOTALL)
_IG_PATH_HANDLE = re.compile(r'^/([A-Za-z0-9_.]+)/(?:p|reel|tv)/')


def _author_from_page(final_url, meta, title):
    """Best effort (name, handle, profile_url) for the person who posted this."""
    # og:url is the canonical permalink and carries the handle even when the
    # link that was pasted didn't (instagram.com/reel/<id>/ has no username in
    # it, instagram.com/<user>/reel/<id>/ does).
    canonical = (meta.get('og:url') or '').strip() or final_url
    host = (urlsplit(canonical).hostname or '').lower().removeprefix('www.')
    path = urlsplit(canonical).path

    if host.endswith('instagram.com'):
        handle = ''
        match = _IG_PATH_HANDLE.match(path)
        if match:
            handle = match.group(1)
        name = ''
        title_match = _IG_TITLE.match(title or '')
        if title_match:
            name = title_match.group(1).strip()
        if not handle and name:
            handle = name
        profile = f'https://www.instagram.com/{handle}/' if handle else ''
        return name or handle, handle, profile

    if host.endswith('tiktok.com'):
        # /@handle/video/<id>
        parts = [p for p in path.split('/') if p]
        if parts and parts[0].startswith('@'):
            handle = parts[0][1:]
            return handle, handle, f'https://www.tiktok.com/@{handle}'

    # The generic web: article bylines.
    author = (meta.get('author') or meta.get('article:author') or '').strip()
    if author.startswith('http'):
        return '', '', author
    return author, '', ''


def _strip_author_prefix(title, host):
    """Drop the "<author> on Instagram:" preamble so the card shows the caption.

    The author gets its own line on the card, so repeating it inside the title
    just costs two lines of a three-line clamp. Split on the "Instagram:"
    marker rather than on the author's name, because the connecting word is
    localised and the name may be styled differently in the two places.
    """
    if not title or not host.endswith('instagram.com'):
        return title
    marker = re.search(r'Instagram\s*[::]\s*', title)
    if not marker:
        return title
    rest = title[marker.end():].strip().strip('"“”').strip()
    return rest or title


def parse_metadata(final_url, html_text):
    """Reduce a page's tags to the fields the preview card renders."""
    parser = _MetaParser()
    try:
        parser.feed(html_text)
    except Exception:
        # A malformed page shouldn't take the request down; whatever was
        # parsed before the error is still worth using.
        pass
    m = parser.meta

    def pick(*keys):
        for k in keys:
            if m.get(k):
                return m[k]
        return ''

    title = pick('og:title', 'twitter:title') or parser.title
    description = pick('og:description', 'twitter:description', 'description')
    image = pick('og:image', 'og:image:url', 'og:image:secure_url', 'twitter:image')
    site_name = pick('og:site_name', 'application-name')
    og_type = pick('og:type').lower()

    if image:
        image = urljoin(final_url, image.strip())
        # A preview image we can't load over TLS is worse than none: iOS ATS
        # and the web build both block mixed content.
        scheme = urlsplit(image).scheme
        if scheme not in ('http', 'https'):
            image = ''

    host = (urlsplit(final_url).hostname or '').lower().removeprefix('www.')
    canonical_path = urlsplit((m.get('og:url') or '').strip() or final_url).path

    title = _clean(title, 300)
    author_name, author_handle, author_url = _author_from_page(final_url, m, title)
    title = _clean(_strip_author_prefix(title, host), 200)

    # A page that declares a video, or carries og:video, is one the card should
    # badge with a play button.
    kind = ''
    if og_type.startswith('video') or pick('og:video', 'og:video:url'):
        kind = 'video'
    # Instagram labels reels og:type=article, so the badge has to come from the
    # permalink shape instead. Same for TikTok, whose crawler HTML says website.
    elif host.endswith('instagram.com') and '/reel/' in canonical_path:
        kind = 'video'
    elif host.endswith('tiktok.com') and '/video/' in canonical_path:
        kind = 'video'
    elif og_type in ('article', 'website'):
        kind = og_type

    def dimension(*keys):
        for key in keys:
            raw = m.get(key)
            if not raw:
                continue
            try:
                return max(0, int(float(raw)))
            except (TypeError, ValueError):
                continue
        return 0

    return {
        'url': final_url,
        'title': title,
        'description': _clean(description, 400),
        'image_url': image[:1000] if image else '',
        'image_width': dimension('og:image:width', 'twitter:image:width'),
        'image_height': dimension('og:image:height', 'twitter:image:height'),
        'site_name': _clean(site_name, 100) or host,
        'author_name': _clean(author_name, 100),
        'author_handle': _clean(author_handle, 100),
        'author_url': author_url[:500] if author_url else '',
        'kind': kind,
    }


def fetch_oembed(url):
    """oEmbed for [url], or None when the provider has none or it fails."""
    endpoint = oembed.endpoint_for(url)
    if endpoint is None:
        return None
    try:
        status, _headers, body = _fetch_once(endpoint, accept='application/json')
    except Exception:
        return None
    if status != 200 or not body:
        return None
    return oembed.parse(body.decode('utf-8', 'replace'))


def fetch_preview(url):
    """Full pipeline. Raises UnsafeUrl on anything we won't or can't preview.

    oEmbed and Open Graph each know things the other doesn't, so both run and
    the results are merged. TikTok's oEmbed has the caption, creator name and
    handle its HTML omits; YouTube's page has a description and a larger
    thumbnail its oEmbed omits. Taking the better of each is what makes a
    shared video look the same here as it does in Instagram.
    """
    url = normalise_url(url)
    card = fetch_oembed(url)

    try:
        final_url, html_text = fetch_head_html(url)
        page = parse_metadata(final_url, html_text)
    except UnsafeUrl:
        # No page, but oEmbed alone is still a perfectly good card.
        if card is None:
            raise
        page = None

    if page is None:
        return {
            'url': url,
            'title': card['title'],
            'description': '',
            'image_url': card['thumbnail_url'],
            'image_width': card['image_width'],
            'image_height': card['image_height'],
            'site_name': card['site_name'],
            'author_name': card['author_name'] or card['author_handle'],
            'author_handle': card['author_handle'],
            'author_url': card['author_url'],
            'kind': card['kind'],
        }

    if card is None:
        return page

    # Merge: oEmbed wins on the fields it is authoritative for (the caption and
    # the creator), the page fills in everything else.
    use_page_image = bool(page['image_url'])
    # For a provider with oEmbed, oEmbed is authoritative about the caption:
    # if it says there isn't one, the page <title> is the app's own shell
    # ("TikTok - Make Your Day"), never the post. Better to show no title and
    # let the creator and thumbnail speak than to caption a video with an ad.
    return {
        'url': page['url'],
        'title': card['title'],
        'description': page['description'],
        'image_url': page['image_url'] or card['thumbnail_url'],
        # Dimensions have to travel with whichever image won, or a portrait
        # oEmbed thumbnail would be drawn at the page image's landscape ratio.
        'image_width': page['image_width'] if use_page_image else card['image_width'],
        'image_height': page['image_height'] if use_page_image else card['image_height'],
        # The provider names itself better than our host-name fallback does
        # ("TikTok", not "tiktok.com").
        'site_name': card['site_name'] or page['site_name'],
        'author_name': card['author_name'] or card['author_handle'] or page['author_name'],
        'author_handle': card['author_handle'] or page['author_handle'],
        'author_url': card['author_url'] or page['author_url'],
        'kind': card['kind'] or page['kind'],
    }
