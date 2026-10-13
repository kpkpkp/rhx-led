"""Picture sequences: N whole 128x32 pictures that the controller plays in order (an animation without a GIF).

Layout (from a vendor GIF export, camera-verified 2026-10-12): picture block image count (0xbf+4) = N, effect 25,
speed 10 (~83 ms per picture), frames byte 1; rows are image-major, 3*(g+1)*32 per picture, after one 32-row
background item. Identical rows are stored once, so held frames cost only their 4-byte row pointers.

  from signboard.sequence import build
  img = build(frames, mode='low-gray')       # frames: list of 128x32 PIL images
"""
import struct
from PIL import Image
from signboard import picture

PICTURE_MS = 83          # at speed 10 (measured)


def build(frames, mode='low-gray', effect=25, speed=10, stop=0, background=None):
    t = picture.template(mode); g = t[0x37]; B = picture.BLK
    W = struct.unpack_from('>H', t, B + 5)[0]; bpr = (W + 7) // 8
    table = struct.unpack_from('>I', t, B + 10)[0] - 4 * 3 * (g + 1) * 32
    item1 = picture._rows(background or Image.new('RGB', (W, 32)), g, bpr)
    rows = list(item1)
    for f in frames:
        rows += picture._rows(f if f.size == (W, 32) else f.convert('RGB').resize((W, 32)), g, bpr)
    head = bytearray(t[:table])
    head[B], head[B + 1], head[B + 2] = effect, speed, stop
    head[B + 4] = len(frames)
    struct.pack_into('>H', head, B + 7, 32)
    head[B + 9] = 1
    struct.pack_into('>I', head, B + 10, table + 4 * len(item1))
    ds = table + 4 * len(rows); data = bytearray(); where = {}; tab = bytearray()
    for r in rows:
        if r not in where:
            where[r] = ds + len(data); data += picture._zip_row(r)
        tab += struct.pack('>I', where[r])
    body = head + tab + data + struct.pack('>II', len(rows) * bpr, len(data)) + b'\x04'
    struct.pack_into('>I', body, 0x11, len(body) - 4 + 6)
    return bytes(body) + bytes(6)


def from_durations(frames, durations_ms, **kw):
    """Hold each frame for its duration by repeating it (repeats cost 4 B per row, not pixel data)."""
    out = []
    for f, d in zip(frames, durations_ms):
        out += [f] * max(1, round(d / PICTURE_MS))
    return build(out, **kw)
