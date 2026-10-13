"""Picture programs in any of the controller's work modes, and a content-based mode recommendation.

Each mode is built on that mode's own export from RHX Plus for Windows (`templates/<mode>.bin`, image form). Only the
picture block (effect, speed, stop, height, pointer), the row-pointer table, the RLE rows and the footer change; the
embedded config, the scan entry and the config checksum (image 0x72 = sum8 of 0x2f..0x71) are kept as exported.

Layout (verified on the panel 2026-10-12): grey depth g = image 0x37 (Screen para[10]); picture block at 0xbf =
effect, speed, stop, 00, n, w(2), h(2), frames, ptr(4); two items share one table: item 1 (a 32-row frame) and the
picture, each 3*(g+1) channel-major planes (R bits 7..7-g, then G, then B) of h rows of ceil(w/8) bytes.

  python -m signboard.picture IMAGE OUT.bin [MODE|auto]
"""
import struct
from pathlib import Path

from PIL import Image, ImageOps

from signboard import render

HERE = Path(__file__).resolve().parent
MODES = ('seven-lengthen', 'low-gray', 'middle-gray', 'high-gray', 'true-color')     # shallow -> deep
LABELS = {'seven-lengthen': 'Seven colour', 'low-gray': 'Low gray', 'middle-gray': 'Middle gray',
          'high-gray': 'High gray', 'true-color': 'True color'}
BLK = 0xbf
MAX_UPLOAD = 256 * 1024                 # 78e1/dc02 sizes are 32-bit; 178 KB uploads verified (2026-10-12). A practical cap.
SCROLL, STILL = 17, 1                    # ShowWay: Up_Continue scrolls a tall picture; 1 shows it in place


def template(mode):
    if mode != 'text' and mode.removeprefix('raw-') not in MODES:
        raise ValueError(f'unknown mode {mode!r}; one of {", ".join(MODES)}')
    for d in template_dirs():
        if (d / f'{mode}.bin').is_file():
            return (d / f'{mode}.bin').read_bytes()
    raise FileNotFoundError(f'no {mode} template in {", ".join(map(str, template_dirs()))}. Templates come from your '
                            f'own RHX Plus exports: see docs/templates.md (python -m signboard.import_ledrom FILE)')


def template_dirs():
    """Where templates are looked for: templates_dir (config), <state_dir>/templates, then the package folder."""
    from signboard import config
    out = [Path(config.get('templates_dir')).expanduser()] if config.get('templates_dir') else []
    return out + [config.state_dir() / 'templates', HERE / 'templates']


def info(mode):
    """Grey depth and picture width the mode's template was exported with."""
    t = template(mode)
    return {'grey': t[0x37], 'width': struct.unpack_from('>H', t, BLK + 5)[0]}


def _rows(im, g, bpr):
    if g == 0:                            # 1 bit per channel: dither, or photos posterise into blobs
        planes = [ch.convert('1') for ch in im.convert('RGB').split()]
    else:
        planes = render.grey_planes(im, g)
    out = []
    for pl in planes:
        px = pl.load()
        for y in range(im.height):
            r = bytearray(bpr)
            for x in range(im.width):
                if px[x, y]:
                    r[x >> 3] |= 0x80 >> (x & 7)
            out.append(bytes(r))
    return out


def _zip_row(r):
    """The app's zipData rules: X a5 n = X plus n more copies; a literal a5 is a5 00."""
    o = bytearray(); i = 0
    while i < len(r):
        j = i + 1
        while j < len(r) and r[j] == r[i] and j - i < 256:
            j += 1
        n = j - i
        if r[i] == 0xa5:
            o += (b'\xa5\x00' * n) if n < 4 else (b'\xa5\x00' + bytes([0xa5, n - 1]))
        elif n >= 4:
            o += bytes([r[i], 0xa5, n - 1])
        else:
            o += bytes([r[i]]) * n
        i = j
    return bytes(o)


def fit(img, width, scale=1.0):
    """Scale to the mode's width (times `scale`, centred), pad to whole 32-row bands. Returns (picture, frame)."""
    img = ImageOps.exif_transpose(img).convert('RGB')
    w = max(8, round(width * scale))
    h = max(1, round(img.height * w / img.width))
    small = img.resize((w, h), Image.LANCZOS)
    H = max(32, (h + 31) // 32 * 32)
    pic = Image.new('RGB', (width, H))
    pic.paste(small, ((width - w) // 2, (H - h) // 2 if h < 32 else 0))
    top = max(0, min(H - 32, h // 3 - 16))            # item 1: a 32-row frame from the upper third (faces, roofs)
    return pic, pic.crop((0, top, width, top + 32))


def build_fitted(pic, frame, mode, effect=None, speed=2, stop=0):
    t = template(mode)
    g = t[0x37]
    W = struct.unpack_from('>H', t, BLK + 5)[0]; bpr = (W + 7) // 8
    assert pic.width == W and frame.size == (W, 32), 'fit() the picture to the mode width first'
    assert sum(t[0x2f:0x72]) & 0xff == t[0x72], 'template config checksum'
    table = struct.unpack_from('>I', t, BLK + 10)[0] - 4 * 3 * (g + 1) * 32
    item1 = _rows(frame, g, bpr)
    rows = item1 + _rows(pic, g, bpr)
    if effect is None:
        effect = SCROLL if pic.height > 32 else STILL
    head = bytearray(t[:table])
    head[BLK], head[BLK + 1], head[BLK + 2] = effect, speed, stop
    struct.pack_into('>H', head, BLK + 7, pic.height)
    struct.pack_into('>I', head, BLK + 10, table + 4 * len(item1))
    ds = table + 4 * len(rows); data = bytearray(); where = {}; tab = bytearray()
    for r in rows:
        if r not in where:
            where[r] = ds + len(data); data += _zip_row(r)
        tab += struct.pack('>I', where[r])
    body = head + tab + data + struct.pack('>II', len(rows) * bpr, len(data)) + b'\x04'
    struct.pack_into('>I', body, 0x11, len(body) - 4 + 6)
    return bytes(body) + bytes(6)


def build(img, mode, effect=None, speed=2, stop=0, still=False, report=None):
    """PIL image -> program image (bytes) in `mode`. Tall pictures scroll (effect 17); 32 rows or less stand still.
    still=True shows one 32-row frame. Over MAX_UPLOAD the picture is scaled down, then the mode stepped down; what was
    done is appended to `report` (a list) when given."""
    report = [] if report is None else report
    have = installed()
    lower = [m for m in MODES[MODES.index(mode)::-1] if m in have]
    if not lower:
        raise FileNotFoundError(f'no template installed for {mode} or a shallower mode (docs/templates.md)')
    # mild shrinking first, then a shallower mode at full size, then shrink as far as it takes
    tries = [(m, s) for m in lower for s in (1.0, 0.85, 0.72)] + \
            [(m, 0.72 * 0.85 ** k) for m in lower for k in range(1, 12)]
    for m, scale in tries:
        pic, frame = fit(img, info(m)['width'], scale)
        if still:
            pic = frame
        out = build_fitted(pic, frame, m, effect, speed, stop)
        if len(out) - 6 <= MAX_UPLOAD:
            if m != mode:
                report.append(f'{LABELS[mode]} would not fit the upload limit; used {LABELS[m]}')
            if scale < 1:
                report.append(f'scaled to {round(scale * 100)}% width to fit the upload limit')
            return out
    raise ValueError('picture does not fit the upload limit in any mode')


GREY = {'seven-lengthen': 0, 'low-gray': 1, 'middle-gray': 3, 'high-gray': 5, 'true-color': 7}


def installed():
    """Modes whose template is installed (see docs/templates.md)."""
    dirs = template_dirs()
    return [m for m in MODES if any((d / f'{m}.bin').is_file() for d in dirs)]


MODE_BY_GREY = {g: m for m, g in GREY.items()}


def mode_of(img_bytes):
    """Which mode a picture program was built in (from its grey depth byte; no templates needed)."""
    return MODE_BY_GREY.get(img_bytes[0x37])


def decode(img_bytes):
    """Program image -> the picture item as an RGB image (for previews and the self-test)."""
    b = img_bytes
    g = b[0x37]; nb = g + 1
    w, h = struct.unpack_from('>HH', b, BLK + 5); ptr = struct.unpack_from('>I', b, BLK + 10)[0]
    bpr = (w + 7) // 8

    def unrle(p):
        o = bytearray()
        while len(o) < bpr:
            x = b[p]; p += 1
            if x == 0xa5:
                n = b[p]; p += 1
                o += b'\xa5' if n == 0 else bytes([o[-1]]) * n
            else:
                o.append(x)
        return bytes(o[:bpr])
    planes = [[unrle(struct.unpack_from('>I', b, ptr + 4 * (k * h + y))[0]) for y in range(h)] for k in range(3 * nb)]
    im = Image.new('RGB', (w, h)); px = im.load()
    for y in range(h):
        for x in range(w):
            c = []
            for ch in range(3):
                v = 0
                for i in range(nb):
                    v |= (planes[ch * nb + i][y][x >> 3] >> (7 - (x & 7)) & 1) << (7 - i)
                c.append(v)
            px[x, y] = tuple(c)
    return im


def recommend(img):
    """Look at the content and pick the mode that suits it.
    -> {mode, label, reason, challenging, message, alternatives, stats}"""
    im = ImageOps.exif_transpose(img).convert('RGB')
    im.thumbnail((256, 256))
    raw = im.tobytes(); px = [tuple(raw[i:i + 3]) for i in range(0, len(raw), 3)]; n = len(px)
    near_pure = sum(1 for r, g, b in px if all(v < 48 or v > 207 for v in (r, g, b))) / n
    sat = sum(max(p) - min(p) for p in px) / n / 255
    grey = sum(1 for p in px if max(p) - min(p) < 24) / n
    vivid = sum(1 for p in px if max(p) - min(p) > 80) / n
    q = {}
    for r, g, b in px:
        k = (r >> 5, g >> 5, b >> 5); q[k] = q.get(k, 0) + 1
    common = sorted(q.values(), reverse=True)
    colours = next(i + 1 for i in range(len(common)) if sum(common[:i + 1]) >= 0.95 * n)
    w = im.width; soft = 0; edges = 0; pairs = 0
    for y in range(im.height):
        for x in range(w - 1):
            a, c = px[y * w + x], px[y * w + x + 1]
            d = max(abs(a[i] - c[i]) for i in range(3)); pairs += 1
            if 2 < d < 32: soft += 1
            elif d >= 96: edges += 1
    soft /= pairs; edges /= pairs
    stats = {'pure_colour': round(near_pure, 2), 'saturation': round(sat, 2), 'grayscale': round(grey, 2),
             'vivid': round(vivid, 2), 'colours': colours, 'smooth_shading': round(soft, 2), 'hard_edges': round(edges, 3)}
    alts = {'seven-lengthen': 'crisp and brightest; only 7 colours, so photos dither',
            'low-gray': '4 levels per colour; posterised photos',
            'middle-gray': '16 levels per colour; the clearest face in the 2026-10-12 camera test',
            'high-gray': '64 levels per colour; smoother colour, a little paler, bigger upload',
            'true-color': '256 levels, but only 96 px wide and the biggest upload'}
    if near_pure > 0.9 and colours <= 8 or (soft < 0.12 and colours <= 6):
        mode, challenging = 'seven-lengthen', False
        reason = 'flat colours, text or a logo: the 7-colour mode shows it crisp and at full brightness'
    elif grey > 0.9:
        mode, challenging = 'middle-gray', True
        reason = 'a black-and-white photo with smooth shading: middle gray gives 16 grey levels and kept the most detail on camera'
    elif (sat > 0.35 or vivid > 0.2) and colours > 24:
        mode, challenging = 'high-gray', True
        reason = 'a colourful photo: high gray keeps smooth colour at the full 128 px width'
    elif soft > 0.12 or colours > 12:
        mode, challenging = 'middle-gray', True
        reason = 'a photo or gradient: middle gray shades it smoothly and stays sharp at 128 px'
    else:
        mode, challenging = 'low-gray', False
        reason = 'a few soft colours: low gray is enough and uploads fastest'
    msg = f'Best mode for this picture: {LABELS[mode]} ({reason}).' if challenging else None
    return {'mode': mode, 'label': LABELS[mode], 'reason': reason, 'challenging': challenging, 'message': msg,
            'alternatives': [{'mode': m, 'label': LABELS[m], 'note': alts[m]} for m in MODES if m != mode],
            'stats': stats}


if __name__ == '__main__':
    import sys
    src, out = sys.argv[1], sys.argv[2]
    mode = sys.argv[3] if len(sys.argv) > 3 else 'auto'
    im = Image.open(src)
    rec = recommend(im)
    if mode == 'auto':
        mode = rec['mode']
    notes = []
    data = build(im, mode, report=notes)
    Path(out).write_bytes(data)
    print(rec['message'] or rec['reason'])
    print(mode, len(data) - 6, 'bytes', *notes)
