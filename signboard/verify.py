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


def judge_photo(path, color=None, sample=4):
    """Return {'lit', 'color_seen', 'color_ok', 'white', 'bbox', 'brightness', 'pixels', 'hist'}.
    color_ok is None when no colour was requested. bbox is the lit region in full-frame pixel coordinates."""
    im = Image.open(path).convert('RGB')
    W, H = im.size
    from signboard import config
    box = config.get('camera_panel_box')            # judge only the panel, not the room: a lit room, a door trim or a
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
    bright = sat = clip = 0; hist = {}; xs = []; ys = []; vs = []
    mid = 0; mid_hist = {}; mxs = []; mys = []; mvs = []
    w = im.width
    for i, (r, g, b) in enumerate(im.getdata()):
        m = max(r, g, b)
        if m < 170: continue
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        bright += 1
        if m >= 245: clip += 1                     # near-clipping: LEDs do this, a lit wall does not
        n = _name(h)
        if s >= 0.5:                               # night: LEDs at s 0.6-1.0, warm walls under room light at 0.2-0.4
            sat += 1; xs.append(i % w); ys.append(i // w); vs.append(v)
            if n: hist[n] = hist.get(n, 0) + 1
        elif s >= 0.25:                            # daylight: the same LEDs photograph washed out, s 0.1-0.4
            mid += 1; mxs.append(i % w); mys.append(i // w); mvs.append(v)
            if n: mid_hist[n] = mid_hist.get(n, 0) + 1
    total = im.width * im.height
    # daylight rule: a small (not wall-sized), hue-concentrated washed-out population is the sign
    day = False
    if sat < 100 and 150 <= mid <= total * 0.25 and mid_hist:
        top = max(mid_hist.values())
        day = top >= 0.6 * sum(mid_hist.values())
    if day:
        sat, xs, ys, vs, hist = mid, mxs, mys, mvs, mid_hist
    seen = max(hist, key=hist.get) if hist else None
    bbox = [ox + min(xs) * sample, oy + min(ys) * sample, ox + (max(xs) + 1) * sample, oy + (max(ys) + 1) * sample] if sat >= 100 else None
    white = clip >= 150 and sat < 100
    out = {'lit': sat >= 100 or white, 'pixels': {'bright': bright, 'saturated': sat, 'clipped': clip, 'of': total, 'daylight': day},
           'color_seen': seen, 'hist': hist, 'white': white, 'bbox': bbox,
           'brightness': round(sum(vs) / len(vs), 2) if vs else 0.0}
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
