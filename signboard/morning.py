"""Hold the sign dark until a set time, then put a message up, verified by the camera phone.

  python -m signboard.morning --at 08:00 --image <program.bin> [--guard-from 07:25]

Until --at: every --every seconds, photograph the sign; if it is lit (e.g. signd's auto-light switched it on at
sunrise), power it off over direct BLE. At --at: upload the program (an upload also wakes the display), power on,
photograph, and retry up to 3 times if the camera does not see it lit. Everything is logged to <state_dir>/signd-morning.log.
"""
import argparse
import datetime as dt
import time
import traceback
from pathlib import Path

from signboard.camera import Camera, crop_sign
from signboard.direct import DirectSign

from signboard import config
LOG = config.state_file('morning.log')
SHOTS = config.state_file('photos') / 'morning'


def log(*a):
    line = time.strftime('%Y-%m-%d %H:%M:%S ') + ' '.join(str(x) for x in a)
    print(line, flush=True)
    with LOG.open('a', encoding='utf-8') as f:
        f.write(line + '\n')


def today_at(hhmm):
    h, m = map(int, hhmm.split(':'))
    now = dt.datetime.now()
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return t


def lit(cam, tag):
    """judge_photo, not crop_sign: in daylight a bright wall can pass a plain brightness test (at 07:52 and
    07:57 on 2026-10-11 the guard re-sent power-off to a dark sign); judge_photo needs saturated or clipped LED pixels."""
    from signboard.verify import judge_photo
    p = cam.take(str(SHOTS / f'{time.strftime("%H%M%S")}-{tag}.jpg'))
    return bool(judge_photo(p)['lit']), p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--at', default='08:00')
    ap.add_argument('--image', required=True)
    ap.add_argument('--guard-from', default=None, help='start the blackout guard at HH:MM (default: now)')
    ap.add_argument('--every', type=int, default=120)
    a = ap.parse_args()
    SHOTS.mkdir(parents=True, exist_ok=True)
    img = Path(a.image).expanduser().read_bytes()
    at = today_at(a.at)
    if dt.datetime.now() > at + dt.timedelta(hours=2):
        at += dt.timedelta(days=1)
    sign = DirectSign(verbose=False)
    cam = None
    try:
        cam = Camera(verbose=False)
    except Exception as e:
        log('camera unavailable, guard runs blind (power-off every cycle):', e)
    log(f'morning: dark until {at:%H:%M}, then {a.image} ({len(img)} B)')

    if a.guard_from:
        g = today_at(a.guard_from)
        while dt.datetime.now() < g:
            time.sleep(min(60, max(1, (g - dt.datetime.now()).total_seconds())))
    while dt.datetime.now() < at - dt.timedelta(seconds=a.every / 2):
        try:
            on, photo = (lit(cam, 'guard') if cam else (True, None))
            if on:
                sign.power(False)
                log('guard: sign was lit before', a.at, '-> powered off', photo or '')
        except Exception as e:
            log('guard error:', type(e).__name__, e)
        left = (at - dt.datetime.now()).total_seconds()
        time.sleep(max(1, min(a.every, left - 5)))
    while dt.datetime.now() < at:
        time.sleep(0.5)

    for attempt in range(1, 4):
        t0 = time.time()
        try:
            r = sign.upload(img)
            sign.power(True)
            log(f'show: uploaded {r["bytes"]} B in {r["seconds"]} s, power on (attempt {attempt}, {time.time() - t0:.1f} s)')
            if not cam:
                break
            time.sleep(2)
            on, photo = lit(cam, 'show')
            log('show: camera sees the sign', 'LIT' if on else 'DARK', photo)
            if on:
                break
        except Exception as e:
            log('show error:', type(e).__name__, e, traceback.format_exc(limit=2))
        time.sleep(20)
    sign.close()


if __name__ == '__main__':
    main()
