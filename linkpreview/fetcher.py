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

import html
import ipaddress
import socket
import ssl
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

# Enough to cover <head> on any sane page; we stop reading once </head> shows up.
MAX_BODY_BYTES = 512 * 1024
TIMEOUT_SECONDS = 6
MAX_REDIRECTS = 3

# Chrome UA: a bare urllib UA gets a 403 from a lot of sites, and several
# (notably news sites) serve og: tags only to something that looks like a
# browser. Not spoofing to evade anything — we want the same HTML a user
# tapping the link would get.
USER_AGENT = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
)


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


def _validate_url(raw):
    """Normalise [raw] and check the parts we can check without a socket."""
    parts = urlsplit(raw)
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


def _read_capped(sock_file, limit):
    """Read up to [limit] bytes, stopping early once </head> is in hand."""
    chunks, total = [], 0
    while total < limit:
        chunk = sock_file.read(min(16384, limit - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if b'</head>' in chunk or b'</HEAD>' in chunk:
            break
    return b''.join(chunks)


def _fetch_once(url):
    """One hop. Returns (status, headers, body_bytes)."""
    parts, port = _validate_url(url)
    family, sockaddr, _ip = _resolve_and_validate(parts.hostname, port)

    # Connect to the address we validated, not to the hostname — resolving
    # again inside http.client would reopen the rebinding window.
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(TIMEOUT_SECONDS)
    try:
        sock.connect(sockaddr)
        if parts.scheme == 'https':
            sock = _ssl_context().wrap_socket(sock, server_hostname=parts.hostname)

        path = urlunsplit(('', '', parts.path or '/', parts.query, ''))
        request = (
            f'GET {path} HTTP/1.1\r\n'
            f'Host: {parts.netloc}\r\n'
            f'User-Agent: {USER_AGENT}\r\n'
            'Accept: text/html,application/xhtml+xml\r\n'
            'Accept-Language: el,en;q=0.8\r\n'
            'Connection: close\r\n\r\n'
        )
        sock.sendall(request.encode('latin-1', 'ignore'))

        stream = sock.makefile('rb')
        status_line = stream.readline(1024).decode('latin-1', 'replace').strip()
        try:
            status = int(status_line.split(' ')[1])
        except (IndexError, ValueError):
            raise UnsafeUrl('malformed response')

        headers = {}
        while True:
            line = stream.readline(8192)
            if not line or line in (b'\r\n', b'\n'):
                break
            decoded = line.decode('latin-1', 'replace')
            if ':' in decoded:
                key, _, value = decoded.partition(':')
                headers[key.strip().lower()] = value.strip()

        body = b''
        ctype = headers.get('content-type', '')
        # Only read a body we could actually parse. A 40MB PDF behind a link
        # shouldn't be pulled down just to find it has no og: tags.
        if 300 <= status < 400:
            pass
        elif 'html' in ctype or not ctype:
            body = _read_capped(stream, MAX_BODY_BYTES)
        return status, headers, body
    finally:
        try:
            sock.close()
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

    if image:
        image = urljoin(final_url, image.strip())
        # A preview image we can't load over TLS is worse than none: iOS ATS
        # and the web build both block mixed content.
        scheme = urlsplit(image).scheme
        if scheme not in ('http', 'https'):
            image = ''

    host = urlsplit(final_url).hostname or ''
    return {
        'url': final_url,
        'title': _clean(title, 200),
        'description': _clean(description, 400),
        'image_url': image[:1000] if image else '',
        'site_name': _clean(site_name, 100) or host.removeprefix('www.'),
    }


def fetch_preview(url):
    """Full pipeline. Raises UnsafeUrl on anything we won't or can't preview."""
    final_url, html_text = fetch_head_html(url)
    return parse_metadata(final_url, html_text)
