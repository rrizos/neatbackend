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
DESIGN_VERSION = 6

#: The poster's printed width. The SVG used to declare its size in pixels,
#: which a print dialog reads at 96 per inch: 880px came out 233mm wide and
#: 293mm tall, larger than A4's printable area (about 190 x 277mm) in both
#: directions, so it split across two pages. A physical size is the fix —
#: printed at 100% on A4, it now fits one page with room to spare, and any
#: print shop can still scale the vector to whatever size it likes.
PRINT_WIDTH_MM = 180

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


def svg(url, slogan=None):
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
        f'width="{PRINT_WIDTH_MM}mm" height="{PRINT_WIDTH_MM * height / total:.1f}mm" '
        f'viewBox="0 0 {total} {height:.3f}" '
        f'role="img" aria-label="{slogan}">',
        f'<rect width="{total}" height="{height:.3f}" fill="#ffffff"/>',
        '<g fill="#0a0d18" shape-rendering="crispEdges">',
    ]

    # One rect per run of dark modules rather than per module: the same
    # picture in a fraction of the bytes, which matters when this is inlined
    # into a page.
    #
    # Each rect is drawn a hair larger than its cell. Rects that merely touch
    # leave a seam where their edges meet, and a PDF renderer anti-aliases that
    # seam into a visible white hairline through every row — invisible on
    # screen, where crispEdges is honoured, and printed onto the paper, where
    # it is not. The overlap closes the seams; at a few hundredths of a module
    # it is far below anything a scanner can see.
    bleed = 0.04
    for y, row in enumerate(matrix):
        x = 0
        while x < modules:
            if row[x]:
                run = 1
                while x + run < modules and row[x + run]:
                    run += 1
                parts.append(
                    f'<rect x="{x + QUIET}" y="{y + QUIET}" '
                    f'width="{run + bleed:.2f}" height="{1 + bleed:.2f}"/>')
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


# ── A4, as a PDF ──────────────────────────────────────────────────────────────
#
# Why a PDF and not a print page: printing a web page lets the browser add its
# own margins, and a header and footer carrying the page's URL and page
# numbers. No page can switch those off — they are the reader's print-dialog
# setting — and the space they take is what pushed the poster onto a second
# sheet, with the URL (private key included) printed under each. A PDF prints
# as exactly the page it contains, with nothing added.
#
# Drawn with Pillow onto an A4 canvas rather than converted from the SVG, which
# has three advantages: no new dependency, text measured with the real font
# instead of estimated, and modules on an exact integer pixel grid, so there
# is no seam between rows for anything to anti-alias.

import io as _io
import zlib

from PIL import Image, ImageDraw, ImageFont

PDF_DPI = 300
A4_MM = (210, 297)
#: The poster's width on the sheet. Well inside A4, so a printer's own
#: unprintable edge never reaches it.
PDF_POSTER_MM = 170
FONT_BOLD = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
FONT_REGULAR = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
INK = (10, 13, 24)


def _mm(value):
    return round(value / 25.4 * PDF_DPI)


def _logo_image(on_dark):
    from web.views import WEB_ROOT

    name = 'logo-dark.png' if on_dark else 'logo-light.png'
    try:
        return Image.open(os.path.join(WEB_ROOT, 'brand', name)).convert('RGBA')
    except OSError:
        return None


def _fit_font(path, text, max_width, start):
    """The largest size, from [start] down, at which [text] fits [max_width]."""
    size = int(start)
    while size > 8:
        font = ImageFont.truetype(path, size)
        if font.getlength(text) <= max_width:
            return font
        size -= 2
    return ImageFont.truetype(path, 8)


def render_poster(url, slogan=None):
    """The poster alone, at print resolution — no page around it."""
    slogan = slogan or BASIC_SLOGAN
    matrix = [list(row) for row in segno.make(url, error=LEVEL).matrix]
    total = len(matrix) + QUIET * 2
    # Whole pixels per module: the grid is exact, so neighbouring modules meet
    # edge to edge with nothing between them.
    module = _mm(PDF_POSTER_MM) // total
    poster_w = module * total
    band_h = round(poster_w * 0.26)
    page = Image.new('RGB', (poster_w, poster_w + band_h), 'white')
    draw = ImageDraw.Draw(page)
    left = top = 0

    for y, row in enumerate(matrix):
        for x, dark in enumerate(row):
            if dark:
                x0 = left + (x + QUIET) * module
                y0 = top + (y + QUIET) * module
                draw.rectangle([x0, y0, x0 + module - 1, y0 + module - 1], fill=INK)

    logo = _logo_image(on_dark=False)
    if logo is not None:
        span = round(poster_w * LOGO_FRACTION)
        plate = round(span * 1.42)
        cx, cy = left + poster_w // 2, top + poster_w // 2
        draw.rounded_rectangle(
            [cx - plate // 2, cy - plate // 2, cx + plate // 2, cy + plate // 2],
            radius=round(plate * 0.18), fill='white')
        mark = logo.resize((span, span), Image.LANCZOS)
        page.paste(mark, (cx - span // 2, cy - span // 2), mark)

    band_top = top + poster_w
    draw.rectangle([left, band_top, left + poster_w - 1, band_top + band_h - 1], fill=BLUE)

    pad = round(poster_w * 0.055)
    text_x = left + pad
    pale = _logo_image(on_dark=True)
    if pale is not None:
        size = round(band_h * 0.66)
        mark = pale.resize((size, size), Image.LANCZOS)
        page.paste(mark, (left + pad, band_top + (band_h - size) // 2), mark)
        text_x = left + pad + size + round(poster_w * 0.03)

    available = left + poster_w - pad - text_x
    title = _fit_font(FONT_BOLD, slogan, available, band_h * 0.30)
    sub = _fit_font(FONT_REGULAR, SUBLINE, available, title.size * 0.72)

    title_box = title.getbbox(slogan)
    sub_box = sub.getbbox(SUBLINE)
    title_h = title_box[3] - title_box[1]
    sub_h = sub_box[3] - sub_box[1]
    gap = round(title.size * 0.35)
    y = band_top + (band_h - (title_h + gap + sub_h)) // 2
    draw.text((text_x, y - title_box[1]), slogan, font=title, fill='white')
    draw.text((text_x, y + title_h + gap - sub_box[1]), SUBLINE, font=sub,
              fill=(225, 234, 250))
    return page


def _render_a4(url, slogan):
    poster = render_poster(url, slogan)
    width, height = _mm(A4_MM[0]), _mm(A4_MM[1])
    page = Image.new('RGB', (width, height), 'white')
    page.paste(poster, ((width - poster.width) // 2, (height - poster.height) // 2))
    return page


def poster_png(url, slogan=None):
    """The poster as a lossless PNG, for the print page to show and print."""
    buffer = _io.BytesIO()
    render_poster(url, slogan).save(buffer, 'PNG', dpi=(PDF_DPI, PDF_DPI), optimize=True)
    return buffer.getvalue()


def _one_page_pdf(image):
    """A minimal PDF: one A4 page holding one losslessly compressed image.

    Written by hand because Pillow's own PDF writer stores RGB as JPEG, which
    rings around every sharp edge of a QR code, or palette images as
    uncompressed hex, which is 17 MB for an A4 page. Flate is lossless, and a
    poster that is mostly white and flat blue compresses to very little.
    """
    w, h = image.size
    pixels = zlib.compress(image.tobytes(), 9)
    page_w = A4_MM[0] / 25.4 * 72
    page_h = A4_MM[1] / 25.4 * 72
    draw_ops = f'q {page_w:.2f} 0 0 {page_h:.2f} 0 0 cm /Im0 Do Q'.encode()

    objects = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        (f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_w:.2f} {page_h:.2f}] '
         f'/Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>').encode(),
        (f'<< /Type /XObject /Subtype /Image /Width {w} /Height {h} '
         f'/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode '
         f'/Length {len(pixels)} >>\nstream\n').encode() + pixels + b'\nendstream',
        f'<< /Length {len(draw_ops)} >>\nstream\n'.encode() + draw_ops + b'\nendstream',
    ]

    out = _io.BytesIO()
    out.write(b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n')
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f'{number} 0 obj\n'.encode() + body + b'\nendobj\n')
    xref = out.tell()
    out.write(f'xref\n0 {len(objects) + 1}\n0000000000 65535 f \n'.encode())
    for offset in offsets:
        out.write(f'{offset:010d} 00000 n \n'.encode())
    out.write(f'trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n'
              f'startxref\n{xref}\n%%EOF\n'.encode())
    return out.getvalue()


def pdf(url, slogan=None):
    """The poster on one A4 page, ready to print."""
    return _one_page_pdf(_render_a4(url, slogan))
