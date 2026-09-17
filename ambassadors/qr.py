"""The creator's link as something you can stick on a wall.

SVG rather than a raster: a poster is printed at whatever size the printer
feels like, and a QR made of rectangles stays sharp at A0. The `n` sits in the
middle, which is possible at all because the code is generated at error
correction level H — up to 30% of it can be obscured and still scan. The
white plate under the logo is slightly larger than the logo itself so the
boundary is clean rather than a fringe of half-covered modules.
"""

import base64
import hashlib
import os

import segno

#: Error correction. 'h' tolerates ~30% loss, which is what pays for the logo.
LEVEL = 'h'
#: How much of the code the centre mark covers, as a fraction of its width.
#: Deliberately small: the band below already carries the logo at full size,
#: and two of the same mark on one sticker reads as a mistake rather than as
#: branding. This one is here to say the code is Neat's when the poster is
#: cropped away, and for nothing else.
LOGO_FRACTION = 0.125
#: Quiet zone, in modules. Four is the spec's minimum and scanners want it.
QUIET = 4


#: Bumped whenever the poster's design changes. It rides in the image URL as
#: ?v=, which is the only thing that reliably retires a cached copy — the file
#: name stays the same, so without it a creator keeps seeing the old design
#: until their browser feels like asking again.
DESIGN_VERSION = 4

#: Neat's blue, and the band it paints across the bottom of a poster.
BLUE = '#2F80ED'

#: What the sticker says. A question, not a description: somebody walking past
#: a wall is not shopping for an app, and "what is happening here right now"
#: is a thing they might actually want to know. The instruction underneath
#: exists because a QR with no verb gets photographed and forgotten.
#: The call to action under every slogan. Constant on purpose: whatever the
#: line above it says, a passer-by still needs the verb.
SUBLINE = 'Σκάναρε και μπες στη Neat'

#: The plain one. Says what the app is, asks nothing of anybody.
BASIC_SLOGAN = 'Τι παίζει στην πόλη σου;'

#: The loud ones, and there is more than one of them on purpose. A wall with
#: the same sticker six times is wallpaper — the eye files it once and stops
#: seeing it. Six different lines on six walls read as six things worth
#: reading, and each is a fresh chance that this one is the one that lands.
#:
#: Which one you get changes on every look, so a creator can print a mix
#: rather than a hundred of the same sticker. The preview and the download
#: never disagree about it, because the line is addressed by index in the URL
#: rather than picked again on each request.
#:
#: The register is gossip, because that is what actually stops someone on a
#: pavement: nobody scans a description of an app, and everybody wants to know
#: what they have missed. They stay on the safe side of it — curiosity about
#: the town in general, never a claim about an identifiable person, because a
#: sticker naming someone is a different kind of thing entirely and not one
#: worth printing a hundred of.
AGGRESSIVE_SLOGANS = [
    'Μάντεψε ποιον είδαν μαζί χθες.',
    'Ποιος με ποιον; Ρώτα την πόλη.',
    'Το έμαθες ή είσαι ο τελευταίος;',
    'Όλοι το συζητάνε. Εκτός από σένα.',
    'Τι έγινε χθες βράδυ εδώ;',
    'Κάποιος το ανέβασε ήδη.',
    'Μην το μάθεις τελευταίος.',
    'Η πόλη σου έχει νέα. Πικάντικα.',
]

BASIC = 'basic'
AGGRESSIVE = 'aggressive'
CUSTOM = 'custom'
STYLES = (BASIC, AGGRESSIVE, CUSTOM)


def slogan_for(style, code='', custom='', index=None):
    """The line a given creator's poster carries.

    [index] addresses one of the loud lines directly; without it, the creator's
    code picks a stable starting point, so a link shared with no index still
    produces the same poster every time rather than a surprise.
    """
    if style == CUSTOM and (custom or '').strip():
        return (custom or '').strip()[:60]
    if style == AGGRESSIVE:
        if index is None:
            digest = hashlib.sha256((code or '').encode('utf-8')).digest()
            index = digest[0]
        return AGGRESSIVE_SLOGANS[int(index) % len(AGGRESSIVE_SLOGANS)]
    return BASIC_SLOGAN

#: Rough advance width of one glyph, as a fraction of the font size, for the
#: system sans this renders in. Used to size text so it cannot overflow the
#: poster — SVG has no layout engine, so a string that is too long simply
#: runs off the edge and prints that way.
GLYPH_WIDTH = 0.54


def _fit(text, available, cap):
    """The largest font size at which [text] still fits [available]."""
    if not text:
        return cap
    return min(cap, available / (GLYPH_WIDTH * len(text)))


def _logo_data_uri(on_dark=False):
    """The `n`, in the variant that shows up on the background it lands on:
    the dark mark for white paper, the pale one for the blue band. Returns ''
    if the file is missing, and the poster is then drawn without it rather
    than not drawn at all."""
    from web.views import WEB_ROOT

    name = 'logo-dark.png' if on_dark else 'logo-light.png'
    path = os.path.join(WEB_ROOT, 'brand', name)
    try:
        with open(path, 'rb') as fh:
            return 'data:image/png;base64,' + base64.b64encode(fh.read()).decode('ascii')
    except OSError:
        return ''


def svg(url, slogan=None, size_px=880):
    """A poster: the code, then a blue band that says what it is for.

    Proportions are in QR modules rather than pixels, so the whole thing
    scales with the code instead of needing a second set of numbers. The band
    is a fifth of the width — enough to read from across a pavement, not so
    much that it eats the code.
    """
    slogan = slogan or BASIC_SLOGAN
    qr = segno.make(url, error=LEVEL)
    matrix = [list(row) for row in qr.matrix]
    modules = len(matrix)
    total = modules + QUIET * 2
    band = total * 0.26
    height = total + band

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'width="{size_px}" height="{size_px * height / total:.0f}" '
        f'viewBox="0 0 {total} {height:.3f}" '
        f'role="img" aria-label="{slogan}">',
        f'<rect width="{total}" height="{height:.3f}" fill="#ffffff"/>',
        '<g fill="#0a0d18" shape-rendering="crispEdges">',
    ]

    # One rect per run of dark modules rather than per module: the same
    # picture in a fraction of the bytes, which matters when this is inlined
    # into a page.
    for y, row in enumerate(matrix):
        x = 0
        while x < modules:
            if row[x]:
                run = 1
                while x + run < modules and row[x + run]:
                    run += 1
                parts.append(
                    f'<rect x="{x + QUIET}" y="{y + QUIET}" width="{run}" height="1"/>')
                x += run
            else:
                x += 1
    parts.append('</g>')

    logo = _logo_data_uri()
    if logo:
        span = total * LOGO_FRACTION
        plate = span * 1.42
        parts.append(
            f'<rect x="{(total - plate) / 2:.3f}" y="{(total - plate) / 2:.3f}" '
            f'width="{plate:.3f}" height="{plate:.3f}" rx="{plate * 0.18:.3f}" '
            f'fill="#ffffff"/>')
        parts.append(
            f'<image x="{(total - span) / 2:.3f}" y="{(total - span) / 2:.3f}" '
            f'width="{span:.3f}" height="{span:.3f}" '
            f'preserveAspectRatio="xMidYMid meet" xlink:href="{logo}"/>')

    # ── the band ────────────────────────────────────────────────────────────
    pad = total * 0.055
    mark = band * 0.66
    parts.append(
        f'<rect x="0" y="{total:.3f}" width="{total}" height="{band:.3f}" fill="{BLUE}"/>')

    pale = _logo_data_uri(on_dark=True)
    text_x = pad
    if pale:
        parts.append(
            f'<image x="{pad:.3f}" y="{total + (band - mark) / 2:.3f}" '
            f'width="{mark:.3f}" height="{mark:.3f}" '
            f'preserveAspectRatio="xMidYMid meet" xlink:href="{pale}"/>')
        text_x = pad + mark + total * 0.03

    # Sized to the space rather than to a guess. Both lines are measured, and
    # the second is tied to the first so the pair always reads as a pair.
    available = total - text_x - pad
    size = _fit(slogan, available, band * 0.34)
    sub_size = min(_fit(SUBLINE, available, size * 0.74), size * 0.74)
    block = size + sub_size * 1.5
    baseline = total + (band - block) / 2 + size * 0.85

    parts.append(
        f'<text x="{text_x:.3f}" y="{baseline:.3f}" fill="#ffffff" '
        f'font-family="-apple-system, BlinkMacSystemFont, Helvetica, Arial, sans-serif" '
        f'font-size="{size:.3f}" font-weight="700" '
        f'letter-spacing="-0.02em">{slogan}</text>')
    parts.append(
        f'<text x="{text_x:.3f}" y="{baseline + sub_size * 1.5:.3f}" '
        f'fill="#ffffff" fill-opacity="0.82" '
        f'font-family="-apple-system, BlinkMacSystemFont, Helvetica, Arial, sans-serif" '
        f'font-size="{sub_size:.3f}" font-weight="600">{SUBLINE}</text>')

    parts.append('</svg>')
    return ''.join(parts)


def png(url, scale=16):
    """A raster copy, for anywhere that will not take an SVG — a story, a chat,
    a print shop's upload form. No logo: segno writes this one itself, and a
    code that scans everywhere is worth more here than the mark."""
    import io as _io

    buffer = _io.BytesIO()
    segno.make(url, error=LEVEL).save(
        buffer, kind='png', scale=scale, border=QUIET, dark='#0a0d18', light='#ffffff')
    return buffer.getvalue()
