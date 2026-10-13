"""Ready-made backgrounds for the 128x32 panel (7-colour palette: each channel on or off)."""
import math

from PIL import Image, ImageDraw

W, H = 128, 32
YELLOW, RED, WHITE, GREEN, CYAN, BLUE, MAGENTA = ((255, 255, 0), (255, 0, 0), (255, 255, 255), (0, 255, 0),
                                                  (0, 255, 255), (0, 0, 255), (255, 0, 255))


def sunshine(n=16, suns=((64, 34),)):
    """A sun rising over a green horizon, rays turning slowly, a few twinkling sparkles. n frames, loopable."""
    frames = []
    sparkles = [(9, 5), (27, 11), (101, 4), (118, 13), (44, 3), (84, 8)]
    for f in range(n):
        im = Image.new('RGB', (W, H), (0, 0, 0))
        d = ImageDraw.Draw(im)
        for cx, cy in suns:
            phase = 2 * math.pi * f / n / 6          # rays advance 1/6 of their spacing per... full turn of a gap per loop
            for k in range(12):
                a = math.pi + (k + f / n) * math.pi / 6   # upper half only: the sun sits on the horizon
                if math.sin(a) > -0.05:
                    continue
                r0, r1 = 15, 15 + (9 if k % 2 == 0 else 6)
                x0, y0 = cx + r0 * math.cos(a), cy + r0 * math.sin(a)
                x1, y1 = cx + r1 * math.cos(a), cy + r1 * math.sin(a)
                d.line((x0, y0, x1, y1), fill=YELLOW if k % 2 == 0 else RED, width=2)
            d.ellipse((cx - 12, cy - 12, cx + 12, cy + 12), fill=YELLOW)
            d.ellipse((cx - 7, cy - 9, cx - 3, cy - 6), fill=WHITE)       # highlight
        d.rectangle((0, 30, W, 31), fill=GREEN)
        for i, (x, y) in enumerate(sparkles):
            if (f + 3 * i) % 8 < 3:
                c = CYAN if i % 2 else WHITE
                d.point([(x, y), (x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)], fill=c)
        frames.append(im)
    return frames


def blank(n=1):
    return [Image.new('RGB', (W, H)) for _ in range(n)]
