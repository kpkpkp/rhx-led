"""Render text into the RHX8-Q1C caption bitmap and splice it into a program image. Pure functions apart from fonts.

Render area A (image offset 0x76) = an 89-byte header + three 1-bit planes (R, G, B), each 32 rows x ceil(W/8) bytes,
MSB = leftmost pixel, row-major. A pixel's colour is its (R, G, B) bits, so 7 colours: the app's palette. The app's
"tlei" font draws the fill in the caption colour and a 1-px outline in a second colour (#ffff00 in our programs).
Header u16 BE at +0x50 = W (text strip width in pixels), +0x52 = 32. The caption scrolls across the 128-px panel.
"""
import struct

from PIL import Image, ImageDraw, ImageFont

HDR = 89
H = 32
FONTS = {
    'bold': r'C:\Windows\Fonts\arialbd.ttf',
    'narrow': r'C:\Windows\Fonts\ARIALNB.TTF',
    'impact': r'C:\Windows\Fonts\impact.ttf',
    'black': r'C:\Windows\Fonts\seguibl.ttf',
    'verdana': r'C:\Windows\Fonts\verdanab.ttf',
}
NAMED = {'red': '#ff0000', 'green': '#00ff00', 'yellow': '#ffff00', 'blue': '#0000ff',
         'cyan': '#00ffff', 'magenta': '#ff00ff', 'white': '#ffffff', 'black': '#000000'}


def rgb_bits(color):
    """'#rrggbb' or a palette name -> (r, g, b) bits; each channel is on when >= 0x80."""
    c = NAMED.get(str(color).lower(), color)
    c = c.lstrip('#')
    return tuple(int(c[i:i + 2], 16) >= 0x80 for i in (0, 2, 4))


def text_mask(text, font='bold', size=26, y=None):
    """Text -> 1-bit PIL image, height 32, width = ink width + 2 px (thresholded, no anti-aliasing)."""
    f = ImageFont.truetype(FONTS.get(font, font), size)
    x0, y0, x1, y1 = f.getbbox(text)
    w = max(1, x1 - x0) + 2
    im = Image.new('L', (w, H), 0)
    d = ImageDraw.Draw(im)
    ty = (H - (y1 - y0)) // 2 - y0 if y is None else y
    d.text((1 - x0, ty), text, font=f, fill=255)
    return im.point(lambda v: 255 if v >= 110 else 0).convert('1')


def outline_of(mask):
    """1-px ring around the ink (8-neighbourhood dilation minus the ink)."""
    from PIL import ImageFilter, ImageChops
    grown = mask.convert('L').filter(ImageFilter.MaxFilter(3))
    return ImageChops.subtract(grown, mask.convert('L')).point(lambda v: 255 if v else 0).convert('1')


def compose(segments, width=None, align='center'):
    """segments: [(mask, fill_color, outline_color or None)] laid out left to right.
    Returns an RGB-bits canvas as three 1-bit images (R, G, B) of the given width (default: natural width)."""
    natural = sum(m.width for m, _, _ in segments)
    W = width or natural
    planes = [Image.new('1', (W, H), 0) for _ in range(3)]
    x = {'left': 0, 'center': max(0, (W - natural) // 2), 'right': max(0, W - natural)}[align]
    for mask, fill, ring in segments:
        layers = []
        if ring:
            layers.append((outline_of(mask), rgb_bits(ring)))
        layers.append((mask, rgb_bits(fill)))
        for m, bits in layers:
            for p, on in zip(planes, bits):
                # paint: set where the layer has ink and the channel is on; clear where it has ink and the channel is off
                p.paste(1 if on else 0, (x, 0), m)
        x += mask.width
    return planes


def from_image(im, width=None):
    """A 32-px-high RGB picture (e.g. a GIF frame) -> planes, thresholded per channel at 50 %."""
    im = im.convert('RGB')
    if im.height != H:
        im = im.resize((max(1, round(im.width * H / im.height)), H))
    if width and im.width != width:
        canvas = Image.new('RGB', (width, H)); canvas.paste(im, ((width - im.width) // 2, 0)); im = canvas
    return [ch.point(lambda v: 255 if v >= 128 else 0).convert('1') for ch in im.split()]


def pack_planes(planes):
    W = planes[0].width
    stride = (W + 7) // 8
    out = bytearray()
    for p in planes:
        px = p.load()
        for y in range(H):
            row = bytearray(stride)
            for x in range(W):
                if px[x, y]:
                    row[x >> 3] |= 0x80 >> (x & 7)
            out += row
    return W, bytes(out)


def unpack_planes(render_a):
    W = struct.unpack_from('>H', render_a, 0x50)[0]
    stride = (W + 7) // 8
    bm = render_a[HDR:]
    planes = []
    for k in range(3):
        im = Image.new('1', (W, H), 0); px = im.load()
        for y in range(H):
            row = bm[(k * H + y) * stride:(k * H + y + 1) * stride]
            for x in range(W):
                if row[x >> 3] & (0x80 >> (x & 7)):
                    px[x, y] = 1
        planes.append(im)
    return planes


def set_caption(base_img: bytes, planes, text=None, color=None) -> bytes:
    """Replace the caption bitmap of a program image (any width), keeping its background animation.

    Everything after render area A moves by the size change, and the background is addressed absolutely:
    header u16 at render_a+0x49 -> its row-pointer table (frames x 32 BE u32), whose entries point at RLE rows or at the
    shared empty row 11 bytes before the table. All of those are shifted by the same delta. The project JSON (what a
    readback returns) gets the new caption text/colour so /api/now tells the truth."""
    from signboard import image
    p = image.parse(base_img)
    ra = p['render_a']
    W, bm = pack_planes(planes)
    hdr = bytearray(ra[:HDR])
    struct.pack_into('>H', hdr, 0x50, W)
    old_json = p['json']
    if text is not None or color is not None:
        p['json'] = _edit_json(old_json, text, color)
        p['gap'] = struct.pack('>I', sum(p['json']) & 0xffffffff)      # the 4 bytes after the JSON = sum32(JSON)
    delta = (HDR + len(bm) - len(ra)) + (len(p['json']) - len(old_json))
    table = struct.unpack_from('>H', ra, 0x49)[0]
    struct.pack_into('>H', hdr, 0x49, table + delta)
    if table + delta > 0xffff:
        raise ValueError('background table would move past 64 KB')
    # the table sits in render_b (or straddles the history's tail); patch it in a copy of the whole image after join
    p['render_a'] = bytes(hdr) + bm
    out = bytearray(image.join(p))
    count = struct.unpack_from('>H', ra, 0x3b)[0] * 3 * H     # frames x (R, G, B) x 32 rows
    for k in range(count):
        v = struct.unpack_from('>I', base_img, table + 4 * k)[0]
        struct.pack_into('>I', out, table + delta + 4 * k, v + delta)
    body = bytes(out[:-6])
    return body + image.trailer(body)


def _rle_row(row: bytes) -> bytes:
    """Encode one plane row the way the app does: `X a5 n` = X followed by n more copies of X (runs of 4+ only).
    No literal 0xa5 escape exists in any capture, so callers must not pass 0xa5 bytes (see _no_a5)."""
    out = bytearray(); i = 0
    while i < len(row):
        b = row[i]; j = i + 1
        while j < len(row) and row[j] == b and j - i < 256:
            j += 1
        run = j - i
        if run >= 4:
            out += bytes([b, 0xa5, run - 1])
        else:
            out += bytes([b]) * run
        i = j
    return bytes(out)


def _no_a5(row: bytearray) -> bytearray:
    for k, b in enumerate(row):
        if b == 0xa5:
            row[k] = 0xa4                        # drop one pixel rather than emit an escape the firmware may not have
    return row


WORK_MODES = {   # card family F (RHX8-Q1C): para values from the app's table; setWork() writes them to para[9,10,11,14,15]
    'seven_lengthen': (0, (0, 0, 0, 1, 0)), 'seven_background': (1, (0, 0, 0, 0, 0)),
    'low_gray': (2, (0, 1, 0, 0, 0)), 'middle_gray': (3, (0, 3, 1, 0, 8)),
    'high_gray': (4, (1, 5, 3, 2, 8)), 'true_color': (5, (1, 7, 5, 2, 8)),
}


def set_work_mode(img: bytes, mode: str) -> bytes:
    """Switch the program's screen work mode: config block byte k = Screen para[k + 1]; work = para[31],
    mode values -> para[9, 10, 11, 14, 15]; para[10] is the grey depth (bits per channel = grey + 1)."""
    import json
    from signboard import image
    work, vals = WORK_MODES[mode]
    p = image.parse(img)
    js = json.loads(p['json'])
    para = [int(x) for x in js['Screen']['para'].split(',')]
    for i, v in zip((9, 10, 11, 14, 15), vals):
        para[i] = v
    para[31] = work
    cfg = bytearray(p['config'])
    # config byte k = para[k + 1], except that config[13..14] hold the controller's own values (3, 0 here, the same as
    # its 8204 read), not para[14..15]: writing para[14] there broke the scan (only a few rows lit). Leave them.
    for i in (9, 10, 11, 31):
        cfg[i - 1] = para[i]
    p['config'] = bytes(cfg)
    old = js['Screen']['para']
    p['json'] = p['json'].replace(old.encode(), ','.join(map(str, para)).encode())
    p['gap'] = struct.pack('>I', sum(p['json']) & 0xffffffff)
    return image.join(p)


def grey_planes(im, grey):
    """RGB image -> bit-planes in the app's getBmpData order: for R, G, B: bits 7 .. 7-grey (MSB first)."""
    im = im.convert('RGB')
    out = []
    for ch in im.split():
        for bit in range(7, 6 - grey, -1):
            out.append(ch.point(lambda v, b=bit: 255 if (v >> b) & 1 else 0).convert('1'))
    return out


def set_background(img: bytes, frames, grey: int = 0) -> bytes:
    """Replace the background animation of a program image with `frames` (RGB PIL images, 128x32).

    Layout after the history: 11-byte shared empty row (00 a5 ff ...), the row-pointer table (u16 at render_a+0x49
    points at it): frames x 3 planes (R, G, B) x 32 rows of absolute BE u32 pointers, the RLE rows (each decodes to
    ceil(BW/8) bytes, BW = background width, u16 at render_a+0x3d), then a 9-byte footer: u32 decoded size
    (frames*96*rowbytes), u32 RLE length, 04. Frame count = u16 at render_a+0x3b. Identical rows share one copy;
    all-zero rows point at the shared empty row."""
    from signboard import image
    p = image.parse(img)
    ra = bytearray(p['render_a'])
    table = struct.unpack_from('>H', ra, 0x49)[0]
    empty = table - 11
    BW = 128
    struct.pack_into('>H', ra, 0x3b, len(frames))
    struct.pack_into('>H', ra, 0x3d, BW)
    p['render_a'] = bytes(ra)
    head = bytearray(image.join(p)[:table])          # everything before the table (offsets unchanged)
    rows = []
    for fr in frames:
        planes = from_image(fr, BW) if grey == 0 else grey_planes(fr.convert('RGB').resize((BW, H)) if fr.size != (BW, H) else fr, grey)
        for pl in planes:
            px = pl.load()
            for y in range(H):
                row = bytearray(BW // 8)
                for x in range(BW):
                    if px[x, y]:
                        row[x >> 3] |= 0x80 >> (x & 7)
                rows.append(bytes(_no_a5(row)))
    data = bytearray(); where = {}
    base = table + 4 * len(rows)
    ptrs = []
    for r in rows:
        if not any(r):
            ptrs.append(empty); continue
        if r not in where:
            where[r] = base + len(data); data += _rle_row(r)
        ptrs.append(where[r])
    footer = struct.pack('>II', len(rows) * (BW // 8), len(data)) + b'\x04'
    body = head + b''.join(struct.pack('>I', v) for v in ptrs) + data + footer
    struct.pack_into('>I', body, 0x11, len(body) - 4 + 6)
    body = bytes(body)
    return body + image.trailer(body)


def _edit_json(js: bytes, text, color):
    """Swap the caption text and/or colour inside the first Info para (fields split by -R)H(X-)."""
    import re
    s = js.decode('utf-8')
    m = re.search(r'("para": ")(0-R\)H\(X-)(.*?)(-R\)H\(X-\d+-R\)H\(X-\d+-R\)H\(X-\d+-R\)H\(X-)(#[0-9a-fA-F]{6})', s)
    if not m:
        return js
    t = m.group(3) if text is None else text.replace('"', "'").replace('\\', '/')
    c = m.group(5) if color is None else NAMED.get(str(color).lower(), color)
    s = s[:m.start()] + m.group(1) + m.group(2) + t + m.group(4) + c + s[m.end():]
    return s.encode('utf-8')


def preview(planes, scale=6):
    """Planes -> an RGB PIL image of the strip, LED-dot style, for checking renders by eye."""
    W = planes[0].width
    rgb = Image.merge('RGB', [p.convert('L') for p in planes])
    big = rgb.resize((W * scale, H * scale), Image.NEAREST)
    return big
