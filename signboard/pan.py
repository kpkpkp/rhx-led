"""Controller-driven pans: ONE 128x32 picture that the controller scrolls forever (ShowWay 17-20), a few KB.

Up/down (17/18) work with the normal row-table picture layout. Left/right (19/20) need the RAW layout of a vendor
Left_Continue export: picture block frames byte = 0, pointer 0xcd, then 3*(g+1) planes x 32 rows x 16 B uncompressed
right after the block. Import such an export as templates/raw-<mode>.bin (signboard.import_ledrom).
stop = 0 pans nonstop; the export's 3 paused 3 s after each screen width. Speed 3 measured at ~35 px/s.
Seamless only if the picture repeats across its own edges (tile period divides 128 and 32).
The direction is part of the program: changing it means a new upload (no live command exists on the RHX8-Q1C).

  python -m signboard.pan IMAGE left|right|up|down OUT.bin [speed]
"""
import struct
import sys
from PIL import Image
from signboard import picture, image as img_mod

EFFECT = {'up': 17, 'down': 18, 'left': 19, 'right': 20}


def build(im, direction, mode='low-gray', speed=3):
    im = im.convert('RGB').resize((128, 32)) if im.size != (128, 32) else im.convert('RGB')
    eff = EFFECT[direction]
    if direction in ('up', 'down'):
        return picture.build_fitted(im, im, mode, effect=eff, speed=speed, stop=0)
    src = picture.template('raw-' + mode)
    L = struct.unpack_from('>I', src, 0x11)[0] - 2
    t = bytearray(src[:L]); B = picture.BLK; g = t[0x37]
    if t[B + 9] != 0 or struct.unpack_from('>I', t, B + 10)[0] != 0xcd:
        raise ValueError('raw template is not a raw-layout (Left_Continue) export')
    raw = b''.join(picture._rows(im, g, 16))
    t[0xcd:0xcd + len(raw)] = raw
    p = img_mod.parse(bytes(t) + bytes(6)); hend = p['history_off'] + 4 + len(p['history'])
    # program-background item after the history: point every row at one blank shared row
    t[hend:hend + 7] = bytes.fromhex('00a50f00000000')
    tab = hend + 7; first = min(struct.unpack_from('>I', t, tab + 4 * k)[0] for k in range(8))
    for o in range(tab, first, 4):
        struct.pack_into('>I', t, o, hend)
    t[B], t[B + 1], t[B + 2] = eff, speed, 0
    return bytes(t) + bytes(6)


if __name__ == '__main__':
    im = Image.open(sys.argv[1])
    out = build(im, sys.argv[2], speed=int(sys.argv[4]) if len(sys.argv) > 4 else 3)
    open(sys.argv[3], 'wb').write(out); print(sys.argv[3], len(out))
