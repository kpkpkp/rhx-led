"""Verdicts for a sign change, from the two independent witnesses signd has.

camera: the camera phone's rear camera sees the sign. A photo is judged for "is the sign lit"
and "is the lit colour the one we asked for". No OCR: LED text scrolls and the camera is uncalibrated, so the
dominant hue of the bright saturated pixels is the reliable signal. The sign is the only saturated light source
in a room at night; by day the thresholds still hold because LEDs saturate the sensor.

ble: the controller's own program (text/colour) read back over BLE after the app lets go of the link.
"""
import colorsys

from PIL import Image

HUES = {  # name -> (centre hue in degrees, half-width); tuned on one phone camera looking at one sign (green = 160-175)
    'red': (0, 18), 'yellow': (55, 25), 'green': (135, 38), 'cyan': (186, 13), 'blue': (230, 25), 'magenta': (300, 25),
}


def _name(h):
    h = h * 360
    if h > 342: h -= 360
    for n, (c, w) in HUES.items():
        if abs(h - c) <= w: return n
    return None


def panel_outline(im, box, k=3, need=40):
    """Is the panel itself in view? Its dark bezel against the wall makes a sharp brightness step on each side.
    For each row crossing the left/right edges (each column crossing top/bottom) take the largest step near the
    expected edge, then the median per side. Saved crops (2026-10-13): panel in view 3-4 sides at 48-119; a sleeve
    in front of the lens or the phone knocked off its aim (a blurred white surface) at most one side over 40.
    Returns {'seen': True/False/None, 'sides': [left, right, top, bottom], 'wall': mean luma}; seen is None
    when the wall is too dark to show an outline (night, panel off)."""
    import statistics
    W, H = im.size
    xs, ys = box[0::2], box[1::2]
    if len(box) == 8:                               # four corners: each side from its own two corners
        cs = sorted(zip(xs, ys))
        x0, x1 = (cs[0][0] + cs[1][0]) / 2, (cs[2][0] + cs[3][0]) / 2
        cs = sorted(zip(ys, xs))
        y0, y1 = (cs[0][0] + cs[1][0]) / 2, (cs[2][0] + cs[3][0]) / 2
    else:
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    x0, x1, y0, y1 = x0 * W, x1 * W, y0 * H, y1 * H
    s = 400 / max(1.0, x1 - x0)                     # work with the panel ~400 px wide
    mx, my = (x1 - x0) * 0.2, (y1 - y0) * 0.6
    cx, cy = max(0, int(x0 - mx)), max(0, int(y0 - my))
    reg = im.crop((cx, cy, min(W, int(x1 + mx)), min(H, int(y1 + my)))).convert('L')
    g = reg.resize((max(1, round(reg.width * s)), max(1, round(reg.height * s))))
    px = g.load()
    x0, x1, y0, y1 = (x0 - cx) * s, (x1 - cx) * s, (y0 - cy) * s, (y1 - cy) * s
    bw, bh = x1 - x0, y1 - y0
    sx, sy = max(int(bw * 0.08), 2 * k + 2), max(int(bh * 0.25), 2 * k + 2)

    def step(vals):
        b = 0
        for i in range(k, len(vals) - k):
            b = max(b, abs(sum(vals[i - k:i]) - sum(vals[i:i + k])))
        return b / k

    def side(c, rows):
        out = []
        if rows:
            for y in range(int(y0 + bh * .2), int(y1 - bh * .2), 2):
                out.append(step([px[x, y] for x in range(max(0, int(c - sx)), min(g.width, int(c + sx)))]))
        else:
            for x in range(int(x0 + bw * .2), int(x1 - bw * .2), 2):
                out.append(step([px[x, y] for y in range(max(0, int(c - sy)), min(g.height, int(c + sy)))]))
        return round(statistics.median(out)) if out else 0

    sides = [side(x0, True), side(x1, True), side(y0, False), side(y1, False)]
    wall = [px[x, y] for y in range(max(0, int(y0)), min(g.height, int(y1)))
            for x in list(range(max(0, int(x0 - 2 * sx)), max(0, int(x0 - sx)))) + list(range(min(g.width, int(x1 + sx)), min(g.width, int(x1 + 2 * sx))))]
    wall = round(sum(wall) / len(wall)) if wall else 0
    seen = sum(v >= need for v in sides) >= 3
    return {'seen': True if seen else (None if wall < 40 else False), 'sides': sides, 'wall': wall}


def judge_photo(path, color=None, sample=4):
    """Return {'lit', 'color_seen', 'color_ok', 'white', 'bbox', 'brightness', 'pixels', 'hist'} (+ 'panel' with a box).
    color_ok is None when no colour was requested. lit and color_ok are None when the panel's outline is not in view. bbox is the lit region in full-frame pixel coordinates."""
    im = Image.open(path).convert('RGB')
    W, H = im.size
    from signboard import config
    box = config.get('camera_panel_box')            # judge only the panel, not the room: a lit room, a door trim or a
    outline = panel_outline(im, box) if box else None
    if box and len(box) == 8:                       # reference ball next to the panel fooled the hue rules (2026-10-12)
        # four corners (x, y fractions, any order around the panel): an obliquely seen panel is not a rectangle
        from PIL import ImageDraw
        pts = [(W * box[i], H * box[i + 1]) for i in range(0, 8, 2)]
        mask = Image.new('L', (W, H), 0); ImageDraw.Draw(mask).polygon(pts, fill=255)
        im = Image.composite(im, Image.new('RGB', (W, H)), mask)
        ox, oy = int(min(p[0] for p in pts)), int(min(p[1] for p in pts))
        im = im.crop((ox, oy, int(max(p[0] for p in pts)), int(max(p[1] for p in pts))))
    elif box:                                       # [x0, y0, x1, y1]
        ox, oy = int(W * box[0]), int(H * box[1])
        im = im.crop((ox, oy, int(W * box[2]), int(H * box[3])))
    elif W > H:                                     # a landscape viewfinder screenshot: the camera UI strip is on the right
        ox = oy = 0; im = im.crop((0, 0, int(W * 0.75), H))
    else:                                           # portrait: shutter, mode row and the "Try Night Sight" pill sit in the bottom 38 %
        ox = oy = 0; im = im.crop((0, 0, W, int(H * 0.62)))
    im = im.resize((max(1, im.width // sample), max(1, im.height // sample)))
    face = None
    if box:                                         # the panel's own face, bezel excluded: dark when off (2026-10-13, P2 at
        fw, fh = im.width, im.height                # ~12 ft, camera exposed for the room: off face max <= ~31, lit LEDs 60-130)
        f = im.crop((int(fw * 0.06), int(fh * 0.15), max(int(fw * 0.94), 1), max(int(fh * 0.85), 1))).convert('L')
        face = sum(1 for v in f.getdata() if v > 60) / max(1, f.width * f.height)
    bright = sat = clip = 0; hist = {}; xs = []; ys = []; vs = []
    mid = 0; mid_hist = {}; mxs = []; mys = []; mvs = []
    w = im.width
    lo, s_led = (60, 0.4) if box else (170, 0.5)   # inside a known panel box the face is dark when off, so dim LEDs count
    for i, (r, g, b) in enumerate(im.getdata()):
        m = max(r, g, b)
        if m < lo: continue
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        bright += 1
        if m >= 245: clip += 1                     # near-clipping: LEDs do this, a lit wall does not
        n = _name(h)
        if s >= s_led:                             # night: LEDs at s 0.6-1.0, warm walls under room light at 0.2-0.4
            sat += 1; xs.append(i % w); ys.append(i // w); vs.append(v)
            if n: hist[n] = hist.get(n, 0) + 1
        elif s >= 0.25:                            # daylight: the same LEDs photograph washed out, s 0.1-0.4
            mid += 1; mxs.append(i % w); mys.append(i // w); mvs.append(v)
            if n: mid_hist[n] = mid_hist.get(n, 0) + 1
    total = im.width * im.height
    need = min(100, max(20, total // 100)) if box else 100     # a small panel in the photo has fewer LED pixels
    # daylight rule: a small (not wall-sized), hue-concentrated washed-out population is the sign
    day = False
    if sat < need and min(150, need * 3 // 2) <= mid <= total * (0.6 if box else 0.25) and mid_hist:
        top = max(mid_hist.values())
        day = top >= 0.6 * sum(mid_hist.values())
    if day:
        sat, xs, ys, vs, hist = mid, mxs, mys, mvs, mid_hist
    seen = max(hist, key=hist.get) if hist else None
    bbox = [ox + min(xs) * sample, oy + min(ys) * sample, ox + (max(xs) + 1) * sample, oy + (max(ys) + 1) * sample] if sat >= need else None
    white = clip >= min(150, need * 3 // 2) and sat < need
    lit = sat >= need or white or (face is not None and face >= 0.05)
    out = {'lit': lit, 'face_lit': None if face is None else round(face, 3), 'pixels': {'bright': bright, 'saturated': sat, 'clipped': clip, 'of': total, 'daylight': day},
           'color_seen': seen, 'hist': hist, 'white': white, 'bbox': bbox,
           'brightness': round(sum(vs) / len(vs), 2) if vs else 0.0}
    if outline is not None:
        out['panel'] = outline
        if outline['seen'] is False:                # the photo shows something else: no verdict (2026-10-13: a blurred
            out.update(lit=None, color_seen=None, white=False, bbox=None,     # white surface passed as "lit")
                       note='panel not in view: camera moved or blocked')
            out['color_ok'] = None
            return out
    if color:
        color = color.lower()
        if color in ('white', 'yellow'):            # phone cameras white-balance yellow LEDs to white (s 0.0-0.1)
            out['color_ok'] = out['lit'] and (white or seen == color)
            if color == 'yellow' and white: out['note'] = 'yellow renders as white on this camera'
        else:
            out['color_ok'] = out['lit'] and seen == color
    else:
        out['color_ok'] = None
    return out


def judge_ble(now: dict, text=None, color=None):
    """Compare a /api/now read with what was requested. Text compares stripped (signd pads with spaces)."""
    want = {'text': text.strip() if text else None, 'color': color}
    got = {'text': (now.get('text') or '').strip(), 'color': now.get('color'), 'scan': now.get('scan')}
    ok = True
    if want['text'] is not None and got['text'] != want['text']: ok = False
    if color and got['color'] and got['color'].lower() != PALETTE.get(color.lower(), color.lower()): ok = False
    return {'match': ok, 'want': want, 'got': got}


# the app's palette strip writes pure primaries into the program (seen in /api/now reads)
PALETTE = {'red': '#ff0000', 'green': '#00ff00', 'yellow': '#ffff00', 'blue': '#0000ff',
           'cyan': '#00ffff', 'magenta': '#ff00ff', 'white': '#ffffff'}
