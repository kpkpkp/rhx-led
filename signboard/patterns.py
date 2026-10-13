"""Seamless tessellations panned in a square loop by pre-rendered frames: each leg moves exactly one pattern period,
so the corners and the loop's wrap line up by construction (camera-verified 2026-10-12).

  python -m signboard.patterns houndstooth out.bin     # or: list
A pattern is f(x, y) -> (r, g, b) with periods (PX, PY). Low gray shows 4 levels per channel (0, 85, 170, 255).
"""
import math
import sys
from PIL import Image

K, W_, R_, G_, B_ = (0, 0, 0), (255, 255, 255), (255, 0, 0), (0, 255, 0), (0, 85, 255)
Y_, C_, M_ = (255, 170, 0), (0, 255, 255), (255, 0, 170)
NAVY, GOLD, TEAL, ORANGE = (0, 0, 170), (255, 170, 0), (0, 170, 170), (255, 85, 0)


def houndstooth(x, y, a=W_, b=K):
    N, P = 8, 16; bx, by = (x % P) < N, (y % P) < N
    if bx and by: return a
    if not bx and not by: return b
    return a if ((x - y) % N) < N // 2 else b


def checker_diag(x, y):
    return (Y_ if ((x // 8 + y // 8) % 2) else B_) if ((x + y) % 16) else W_


def herringbone(x, y):
    u, v = x % 16, y % 16
    if u < 8: return ORANGE if ((u + v) % 8) < 4 else K
    return TEAL if ((v - u) % 8) < 4 else K


def argyle(x, y):
    u, v = x % 32, y % 16
    if abs((x + 2 * y) % 32 - 16) < 1 or abs((x - 2 * y) % 32 - 16) < 1: return W_
    return M_ if abs(u - 16) / 16 + abs(v - 8) / 8 < 1 else TEAL


def scales(x, y):
    u, v = (x + (8 if (y // 8) % 2 else 0)) % 16, y % 8
    r = math.hypot(u - 8, v - 8)
    if r < 7: return C_ if r < 4 else B_
    return NAVY if r < 8.5 else K


def plaid(x, y):
    def band(t):
        t %= 32
        return 2 if t < 6 else 1 if 12 <= t < 14 else 3 if 20 <= t < 22 else 0
    pal = {0: (85, 0, 0), 1: GOLD, 2: (0, 85, 0), 3: W_}
    bx, by = band(x), band(y)
    return pal[bx] if bx == by or (x + y) % 2 == 0 else pal[by]


def rainbow_chevron(x, y):
    return [R_, ORANGE, Y_, G_, B_, M_][((y + abs((x % 16) - 8)) // 4) % 6]


PATTERNS = {'houndstooth': (houndstooth, 16, 16), 'houndstooth-red': (lambda x, y: houndstooth(x, y, R_, NAVY), 16, 16),
            'checker-diag': (checker_diag, 16, 16), 'herringbone': (herringbone, 16, 16), 'argyle': (argyle, 32, 16),
            'scales': (scales, 16, 16), 'plaid': (plaid, 32, 32), 'rainbow-chevron': (rainbow_chevron, 16, 24)}


def frame(f, dx, dy, w=128, h=32):
    im = Image.new('RGB', (w, h)); px = im.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = f(x + dx, y + dy)
    return im


def loop(f, PX, PY, step=1):
    """Frames panning right, down, left, up by one period each (the picture moves the opposite way on screen)."""
    path = [(i, 0) for i in range(0, PX, step)] + [(PX, i) for i in range(0, PY, step)] + \
           [(PX - i, PY) for i in range(0, PX, step)] + [(0, PY - i) for i in range(0, PY, step)]
    return [frame(f, dx, dy) for dx, dy in path]


def program(name, mode='low-gray'):
    from signboard import sequence
    f, PX, PY = PATTERNS[name]
    return sequence.build(loop(f, PX, PY, 2 if PX * PY > 512 else 1), mode=mode)


if __name__ == '__main__':
    if sys.argv[1:2] == ['list'] or len(sys.argv) < 3:
        print('\n'.join(PATTERNS)); sys.exit()
    open(sys.argv[2], 'wb').write(program(sys.argv[1])); print(sys.argv[2])
