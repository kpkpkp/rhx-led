"""Real photos from a camera phone's rear camera (Android + Google Camera over adb; tested on a Pixel 2), pulled as full-resolution JPEGs.

Never a screenshot: the viewfinder preview is a low-resolution, tone-mapped stand-in and is not evidence of what the
sign shows. If no new JPEG lands, take() raises.

GCam ignores KEYCODE_CAMERA, so the shutter is tapped. Its position depends on the phone's rotation, so it is found
by resource id in a uiautomator dump and cached per screen orientation.

  python -m signboard.camera out.jpg [--serial <adb serial>] [--crop] [--burst 3]
"""
import argparse
import json
import re
import subprocess
import time
from pathlib import Path

GCAM = 'com.google.android.GoogleCamera'
SHUTTER_ID = GCAM + ':id/shutter_button'
DCIM = '/sdcard/DCIM/Camera'
from signboard import config
CACHE = config.state_file('camera.json')     # rotation -> shutter centre, learned from uiautomator dumps
SIGN_ID = config.get('camera_serial')        # adb serial (USB serial is also part of its wireless-debugging mDNS name)


def find_serial(want=SIGN_ID):
    """The camera phone's current adb name: wireless-debugging names change after re-pairing, the USB serial does not."""
    out = subprocess.run(['adb', 'devices', '-l'], capture_output=True, text=True, timeout=15).stdout
    for line in out.splitlines():
        m = re.match(r'^(\S.*?)\s+device(\s|$)', line)
        if m and ((want and want in line) or any(f'device:{d}' in line or f'model:{d}' in line for d in config.get('camera_models') or [])):
            return m.group(1).strip()
    devs = [l.split()[0] for l in out.splitlines()[1:] if re.search(r'\sdevice(\s|$)', l)]
    if not want and not config.get('camera_models') and len(devs) == 1:
        return devs[0]
    raise RuntimeError(f'camera phone ({want or config.get("camera_models")}) not attached over adb: enable wireless debugging or plug it in, or set camera_serial')


class Camera:
    def __init__(self, serial=None, verbose=True):
        self.serial = serial or find_serial()
        self.verbose = verbose
        self._shutter = {}                       # rotation -> (x, y)

    def log(self, *a):
        if self.verbose:
            print(time.strftime('%H:%M:%S'), 'camera:', *a, flush=True)

    def sh(self, *args, timeout=30):
        r = subprocess.run(['adb', '-s', self.serial, 'shell', *args], capture_output=True, timeout=timeout)
        return (r.stdout or b'').decode('utf-8', 'replace')

    def pull(self, remote, timeout=120) -> bytes:
        return subprocess.run(['adb', '-s', self.serial, 'exec-out', 'cat', remote],
                              capture_output=True, timeout=timeout).stdout

    def _rotation(self):
        m = re.search(r'mCurrentRotation=(\S+)', self.sh('dumpsys', 'window', 'displays'))
        return m.group(1) if m else '?'

    def _focus(self):
        m = re.search(r'mCurrentFocus=Window\{[^ ]+ u0 ([^ /}]+)', self.sh('dumpsys', 'window'))
        return m.group(1) if m else ''

    def _listing(self):
        """{name: size} of the photos in DCIM/Camera (videos and pending .tmp files excluded)."""
        out = self.sh('ls', '-l', DCIM)
        files = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 8 and parts[-1].lower().endswith('.jpg') and parts[4].isdigit():
                files[parts[-1]] = int(parts[4])
        return files

    def _find_shutter(self):
        """Shutter centre for the current rotation. uiautomator often cannot get an idle state while the viewfinder
        animates, so a found position is remembered on disk (it only depends on rotation) and dumps are retried."""
        rot = self._rotation()
        if not self._shutter:
            try:
                self._shutter = {k: tuple(v) for k, v in json.loads(CACHE.read_text()).items()}
            except Exception:
                pass
        if rot in self._shutter:
            return self._shutter[rot]
        for _ in range(8):
            self.sh('uiautomator', 'dump', '/sdcard/ui.xml')
            xml = self.pull('/sdcard/ui.xml').decode('utf-8', 'replace')
            m = re.search(r'resource-id="' + re.escape(SHUTTER_ID) + r'"[^>]*?bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
            if m:
                x0, y0, x1, y1 = map(int, m.groups())
                self._shutter[rot] = ((x0 + x1) // 2, (y0 + y1) // 2)
                try:
                    CACHE.write_text(json.dumps(self._shutter))
                except Exception:
                    pass
                return self._shutter[rot]
            time.sleep(1.0)
        raise RuntimeError('Google Camera shutter button not on screen (camera app not in photo mode?)')

    def ready(self):
        """Wake the phone and bring Google Camera to the front in photo mode."""
        self.sh('input', 'keyevent', 'KEYCODE_WAKEUP')
        if 'deviceLocked=1' in self.sh('dumpsys', 'trust'):
            raise RuntimeError('phone is locked with a PIN: unlock it and remove the screen lock')
        if not self._focus().startswith(GCAM):
            self.sh('am', 'start', '-a', 'android.media.action.STILL_IMAGE_CAMERA', '-p', GCAM)   # -p: no app chooser if another camera app is installed
            for _ in range(10):
                time.sleep(1)
                if self._focus().startswith(GCAM):
                    break
            else:
                raise RuntimeError(f'camera app did not come forward (focus: {self._focus()!r})')
            time.sleep(2)                        # exposure settles

    def take(self, path, timeout=20) -> str:
        """Shoot one photo and save the full-resolution JPEG at path. Returns the path.
        The screen goes off again afterwards (the phone runs on battery; ready() wakes it for the next photo)."""
        try:
            return self._take(path, timeout)
        finally:
            try:
                self.sh('input', 'keyevent', 'KEYCODE_SLEEP')
            except Exception:
                pass

    def _take(self, path, timeout=20) -> str:
        self.ready()
        before = self._listing()
        x, y = self._find_shutter()
        t0 = time.time()
        self.sh('input', 'tap', str(x), str(y))
        new, last_size = None, -1
        while time.time() - t0 < timeout:
            time.sleep(0.7)
            now = self._listing()
            fresh = [n for n in now if n not in before]
            if not fresh:
                continue
            new = max(fresh)                     # names are timestamps
            if now[new] == last_size and now[new] > 100_000:
                break                            # size stable across two polls: GCam has finished writing
            last_size = now[new]
        else:
            if new is None:
                self._shutter.clear()            # rotation may have changed under us; re-find next time
                raise RuntimeError('tapped the shutter but no new photo appeared')
        data = self.pull(f'{DCIM}/{new}')
        if len(data) < 100_000 or data[:2] != b'\xff\xd8':
            raise RuntimeError(f'pulled {new} is not a valid JPEG ({len(data)} bytes)')
        path = Path(path)
        if path.suffix.lower() != '.jpg':
            path = path.with_suffix('.jpg')
        path.write_bytes(data)
        self.log(f'{new} -> {path} ({len(data) // 1024} KB, {time.time() - t0:.1f} s)')
        self.sh('rm', f'{DCIM}/{new}')         # keep the phone's storage from filling up
        return str(path)


def crop_sign(src, dst=None, pad=0.08):
    """Crop a photo to the lit sign (bright saturated pixels). Returns the crop's path, or None if nothing is lit."""
    from PIL import Image, ImageOps
    im = ImageOps.exif_transpose(Image.open(src)).convert('RGB')
    small = im.resize((im.width // 8, im.height // 8))
    px = small.load()
    xs, ys = [], []
    for y in range(small.height):
        for x in range(small.width):
            r, g, b = px[x, y]
            if max(r, g, b) >= 180:
                xs.append(x); ys.append(y)
    if len(xs) < 30:
        return None
    xs.sort(); ys.sort()
    lo = lambda v: v[int(len(v) * 0.01)]
    hi = lambda v: v[int(len(v) * 0.99) - 1]
    x0, x1, y0, y1 = lo(xs) * 8, (hi(xs) + 1) * 8, lo(ys) * 8, (hi(ys) + 1) * 8
    px_, py_ = int((x1 - x0) * pad), int((y1 - y0) * pad * 2)
    box = (max(0, x0 - px_), max(0, y0 - py_), min(im.width, x1 + px_), min(im.height, y1 + py_))
    dst = dst or str(Path(src).with_name(Path(src).stem + '-sign.jpg'))
    im.crop(box).save(dst, quality=90)
    return dst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('out')
    ap.add_argument('--serial')
    ap.add_argument('--crop', action='store_true', help='also save <out>-sign.jpg cropped to the lit sign')
    ap.add_argument('--burst', type=int, default=1, help='take N photos (the sign scrolls), out-1.jpg, out-2.jpg, ...')
    a = ap.parse_args()
    cam = Camera(a.serial)
    outs = [a.out] if a.burst == 1 else [str(Path(a.out).with_name(f'{Path(a.out).stem}-{i + 1}.jpg')) for i in range(a.burst)]
    for o in outs:
        p = cam.take(o)
        print(p)
        if a.crop:
            print(crop_sign(p))


if __name__ == '__main__':
    main()
