"""signd: one HTTP API and web UI for an RHX LED sign, running on a LAN host near the sign.

signd renders programs itself (render.py, picture.py) and uploads them straight to the controller over BLE
(direct.py): ~1-5 s from request to the sign showing it. An optional Android phone on adb can photograph the sign
to verify each change (camera.py, verify.py).

  GET  /                              the web UI (web/index.html)
  GET  /api/health                    sign/camera status, queue depth, last job
  GET  /api/now                       what the sign holds (BLE read, cached 60 s)
  GET  /api/program                   the controller's project JSON from the same read
  POST /api/text   {"text": "...", "color": "white"|"#rrggbb", "outline": "red"|null,
                    "background": "keep"|"none"|"sunshine"|"<stored image>", "font": "bold", "size": 22}
  POST /api/image                     GIF/PNG/JPEG body -> stored background name for /api/text
  POST /api/picture[?mode=auto|seven-lengthen|low-gray|middle-gray|high-gray|true-color&still=1&effect=N]
                                      image (raw body, multipart "image", or JSON {"image": base64}) -> picture job
  POST /api/picture/recommend         same body; the mode that fits the content and why (no upload)
  POST /api/program                   a program image (.bin) uploaded as-is
  POST /api/sign   {"text":..,"color":..,"power": true|false}   desired state: no-op when the sign already holds it
  POST /api/brightness {"level": 1..16}
  POST /api/power  {"on": true|false}
  GET  /api/schedule                  timed jobs; POST {"at": "HH:MM"|ISO, "text"...} or {"at":..., "power": false}
  DELETE /api/schedule/<id>           (persisted in the state directory)
  POST /api/look                      photograph the sign now and judge it (job; needs the camera phone)
  GET  /api/camera                    latest photo
  GET  /api/jobs, /api/jobs/<id>      recent jobs / one job: queued | running | recovering | done | failed

Requests are refused when the Host header is not this machine (DNS rebinding) or a browser sends them from
another site (Origin). There is no login: run it only on a network you trust, or bind 127.0.0.1.
Jobs run one at a time on a worker thread (the controller takes one BLE central).

  python -m signboard.signd --port 8090 [--also-port 80] [--mdns sign] [--auto-light]
"""
import argparse
import functools
import json
import queue
import re
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

COLORS = ('red', 'green', 'yellow', 'blue', 'cyan', 'magenta', 'white')
from signboard import config

print = functools.partial(print, flush=True)   # the task redirects stdout to a file; flush so the log is live

PHOTO_DIR = config.state_file('photos')
PHOTO_DIR.mkdir(exist_ok=True)


BLE_LOCK = threading.Lock()      # the controller accepts one central: one job on the link at a time


def _judge(photo, color):
    """Camera verdict for a job result; never lets a bad photo fail the job itself."""
    try:
        from signboard.verify import judge_photo
        return judge_photo(photo, color)
    except Exception as e:
        return {'error': f'{type(e).__name__}: {e}'}


class SignState:
    """What the sign is showing, as far as signd knows: the last successful job of each kind, kept in
    <state_dir>/signd-state.json so it survives restarts. Every web UI visitor sees the same state (it is the sign's,
    not a user's); per-user preferences stay in each browser. Changes made outside signd (the vendor app,
    morning.py) are not seen here: compare content.text with the /api/now read-back to notice them.

      {"power": true|false|null, "brightness": 1..16|null,
       "content": {"kind": "text"|"picture"|"program", "text", "color", "background", "mode", "name"},
       "changed_at": t, "by": {"power": origin, "brightness": origin, "content": origin}}
    """

    def __init__(self, path=None):
        self.path = path or config.state_file('state.json')
        self.lock = threading.Lock()
        try:
            self.val = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            self.val = {}
        for k, v in (('power', None), ('brightness', None), ('content', None), ('by', {})):
            self.val.setdefault(k, v)

    def get(self):
        with self.lock:
            return json.loads(json.dumps(self.val))

    def record(self, kind, a, result, origin=None):
        with self.lock:
            v, by = self.val, self.val['by']
            if kind == 'power':
                v['power'] = bool(a.get('on')); by['power'] = origin
            elif kind == 'brightness':
                v['brightness'] = int(a['level']); by['brightness'] = origin
            elif kind in ('text', 'picture', 'program'):
                old = v.get('content') or {}
                if kind == 'text':
                    bg = a.get('background') or 'keep'
                    if bg == 'keep':                       # the text went onto the previous text program's background
                        bg = old.get('background') if old.get('kind') == 'text' else 'unknown'
                    c = {'kind': 'text', 'text': a.get('text') or '', 'color': a.get('color') or 'white', 'background': bg}
                elif kind == 'picture':
                    c = {'kind': 'picture', 'mode': (result or {}).get('mode'), 'name': a.get('name') or Path(a.get('file', '')).name}
                else:
                    c = {'kind': 'program', 'name': Path(a.get('file', '')).name}
                v['content'] = c; by['content'] = origin
                v['power'] = True; by['power'] = origin    # an upload always wakes the display
            else:
                return
            v['changed_at'] = time.time()
            tmp = self.path.with_suffix('.tmp')
            tmp.write_text(json.dumps(v, indent=1), encoding='utf-8')
            tmp.replace(self.path)


class Prefs:
    """Each visitor's web UI choices (message, colour, background and picture files, picture mode...), kept by signd so
    they survive browser sessions and the different names of this host (sign.local, the IP, the router name: each is
    its own browser storage). Keyed by the client's address, which at home means one device = one user."""
    KEYS = {'msg': str, 'color': str, 'bgNone': bool, 'bgStored': str, 'bgName': str, 'picStored': str, 'picName': str,
            'picMode': str, 'picStill': bool, 'theme': str}

    def __init__(self, path=None):
        self.path = path or config.state_file('prefs.json')
        self.lock = threading.Lock()
        try:
            self.val = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            self.val = {}

    def get(self, who):
        with self.lock:
            return dict(self.val.get(who) or {})

    def update(self, who, a):
        clean = {}
        for k, v in a.items():
            t = self.KEYS.get(k)
            if t is None or v is None or not isinstance(v, t) or (t is str and len(v) > 500):
                continue
            if k in ('bgStored', 'picStored') and v and not STORED_NAME.fullmatch(v):
                continue
            clean[k] = v
        with self.lock:
            cur = self.val.setdefault(who, {})
            cur.update(clean); cur['saved_at'] = time.time()
            tmp = self.path.with_suffix('.tmp')
            tmp.write_text(json.dumps(self.val, indent=1), encoding='utf-8')
            tmp.replace(self.path)
            return dict(cur)


STORED_NAME = re.compile(r'(bg|src)-\d+-[0-9a-f]{4,8}\.(gif|png|jpg|webp)')   # uploads a visitor may fetch or reuse
KEEP_UPLOADS = 60                                                            # newest stored uploads kept per kind


def store_upload(kind, data, ext):
    name = f'{kind}-{int(time.time())}-{uuid.uuid4().hex[:6]}{ext}'
    (PHOTO_DIR / name).write_bytes(data)
    for old in sorted(PHOTO_DIR.glob(f'{kind}-*'), key=lambda f: f.stat().st_mtime)[:-KEEP_UPLOADS]:
        try: old.unlink()
        except OSError: pass
    return name


def keep_panel_only(photo, pad=0.15):
    """Crop a saved photo to the panel (camera_panel_box plus a margin) once it has been judged: the camera sees a room
    with people in it, and only the panel is evidence. No box configured: the photo is kept as taken."""
    box = config.get('camera_panel_box')
    if not box:
        return
    from PIL import Image
    with Image.open(photo) as im:
        W, H = im.size
        xs, ys = box[0::2], box[1::2]
        x0, x1, y0, y1 = min(xs) * W, max(xs) * W, min(ys) * H, max(ys) * H
        px, py = (x1 - x0) * pad, (y1 - y0) * pad * 2
        crop = im.crop((max(0, int(x0 - px)), max(0, int(y0 - py)), min(W, int(x1 + px)), min(H, int(y1 + py))))
        crop.load()
    crop.save(photo, quality=92)


class Jobs:
    MAX_KEPT = 200                           # finished jobs kept for /api/jobs

    def __init__(self, verbose=True, ble_addr=None):
        self.q, self.jobs, self.verbose, self.ble_addr = queue.Queue(), {}, verbose, ble_addr
        self.last = None
        self.state = SignState()
        threading.Thread(target=self._worker, daemon=True).start()

    current_origin = threading.local()     # set per request thread by the HTTP handler; 'auto-light' etc. set it by hand

    def submit(self, kind, args):
        jid = uuid.uuid4().hex[:8]
        shown = dict(args)
        origin = getattr(Jobs.current_origin, 'value', None) or 'internal'
        self.jobs[jid] = {'id': jid, 'kind': kind, 'args': shown, 'state': 'queued', 'queued_at': time.time(), 'origin': origin}
        self._real_args = getattr(self, '_real_args', {}); self._real_args[jid] = args
        self.q.put(jid)
        for old in list(self.jobs)[:-self.MAX_KEPT]:
            if self.jobs[old]['state'] in ('done', 'failed'): self.jobs.pop(old, None)
        return self.jobs[jid]

    def _worker(self):
        while True:
            jid = self.q.get(); job = self.jobs[jid]
            job['state'], job['started_at'] = 'running', time.time()
            args = self._real_args.pop(jid, job['args'])
            for attempt in range(2):                 # unattended: one reconnect and retry
                try:
                    with BLE_LOCK:
                        job['result'] = self._run(job['kind'], args)
                    job['state'] = 'done'; job.pop('error', None)
                    try:
                        self.state.record(job['kind'], args, job['result'], job.get('origin'))
                    except Exception as e:
                        print('state record failed:', e)
                    break
                except Exception as e:
                    job['state'], job['error'] = 'failed', f'{type(e).__name__}: {e}'
                    if attempt == 0 and job['kind'] in ('text', 'power', 'look', 'camera'):
                        job['retried'] = job['error']; job['state'] = 'recovering'
                        print('job', jid, 'failed, recovering:', job['error'])
                        self.direct.sign.close()          # drop the link; the retry reconnects from scratch
                        time.sleep(3)
                        job['state'] = 'running'
            job['finished_at'] = time.time(); self.last = job
            if job['state'] == 'done' and job['kind'] == 'text' and getattr(self, 'reads', None):
                job['ble'] = {'state': 'pending'}
                threading.Thread(target=self._post_verify, args=(job, args), daemon=True).start()

    def _post_verify(self, job, a):
        """Second witness: read the controller's program back once the link frees. Retries for up to 3 min; the job record gets job['ble']."""
        from signboard.verify import judge_ble
        deadline = time.time() + 180; last_err = None
        while time.time() < deadline:
            try:
                v = judge_ble(self.reads.now(max_age=0), a.get('text'), a.get('color'))
                if v['match'] or time.time() > deadline - 30:
                    job['ble'] = dict(v, state='done', read_at=time.time())
                    if not v['match'] and not a.get('_retry'):      # the controller is the ground truth: try once more
                        Jobs.current_origin.value = 'retry-of-' + job['id']
                        again = self.submit('text', dict(a, _retry=True))
                        job['ble']['retry_job'] = again['id']
                    return
                last_err = 'read ok but no match yet'
            except Exception as e:
                last_err = f'{type(e).__name__}: {e}'
            time.sleep(10)
        job['ble'] = {'state': 'failed', 'error': last_err, 'match': False}

    def _run(self, kind, a):
        if kind not in DIRECT_KINDS:
            raise ValueError(f'unknown job kind {kind!r}')
        return self.direct.run(kind, a)


DIRECT_KINDS = ('text', 'power', 'brightness', 'program', 'picture', 'look', 'camera')
PROGRAMS = config.state_file('programs')
PROGRAMS.mkdir(exist_ok=True)


class DirectBackend:
    """signd writes the controller itself over BLE (signboard.direct): text is rendered here (signboard.render), the
    background is kept, replaced by a stock animation, or built from an uploaded GIF/PNG. The camera phone only takes
    photos (signboard.camera, real JPEGs over adb). Every job result carries its timings."""

    def __init__(self, sign, camera_serial=None):
        self.sign = sign
        self.camera_serial = camera_serial
        self._cam = None

    def camera(self):
        if self._cam is None:
            from signboard.camera import Camera
            self._cam = Camera(self.camera_serial, verbose=False)
        return self._cam

    def current(self):
        """The program the controller holds (our last upload, else read out of the sign once)."""
        f = PROGRAMS / 'current.bin'
        if f.exists():
            return f.read_bytes()
        img = self.sign.read_image()
        if img[7] != 0x75:                       # the sign holds a picture program: start from the imported text template
            from signboard import picture
            img = picture.template('text')
        f.write_bytes(img)
        return img

    def build(self, a):
        from signboard import content, render
        base = self.current()
        text = a.get('text') or ''
        color = a.get('color') or 'white'
        outline = a.get('outline', None if a.get('background') in (None, 'none') else 'red')
        if text.strip():
            mask = render.text_mask(text, a.get('font', 'bold'), int(a.get('size', 22)))
            planes = render.compose([(mask, color, outline)], width=max(128, mask.width) if mask.width < 128 else None)
        else:
            from PIL import Image
            planes = [Image.new('1', (128, 32), 0) for _ in range(3)]
        img = render.set_caption(base, planes, text=text, color=color)
        bg = a.get('background')
        if bg in (None, '', 'keep'):
            return img
        if bg == 'none':
            frames = content.blank()
        elif bg == 'sunshine':
            frames = content.sunshine()
        else:
            frames = gif_frames(PHOTO_DIR / re.sub(r'[^A-Za-z0-9._-]', '_', bg))
        return render.set_background(img, frames)

    def show(self, img, a, t0):
        r = self.sign.upload(img)
        # current.bin is the base for the next text render; a picture program (kind 0x73, not 0x75) cannot be one
        (PROGRAMS / ('current.bin' if img[7] == 0x75 else 'picture.bin')).write_bytes(img)
        out = {'uploaded': r['bytes'], 'direct_ble': True,
               'latency': {'upload_s': r['seconds']}}
        # no power(True) here: an upload already wakes the display, and the power login (78e1) sent while the
        # controller is still taking in a fresh program left it on its broken-text screen (2026-10-12, camera)
        out['latency']['total_s'] = round(time.time() - t0, 2)
        if a.get('verify', True):
            self.look(out, a.get('color'), t0)
        return out

    def look(self, out, color, t0=None):
        if config.get('camera') is False:
            out['camera'] = {'skipped': 'camera = false in config'}
            return out
        try:
            photo = self.camera().take(str(PHOTO_DIR / f'{int(time.time())}.jpg'))
            out['photo'] = photo
            out['camera'] = _judge(photo, color)
            keep_panel_only(photo)
            if t0:
                out['latency']['photo_s'] = round(time.time() - t0, 2)
        except Exception as e:
            out['camera'] = {'error': f'{type(e).__name__}: {e}'}
        return out

    def run(self, kind, a):
        t0 = time.time()
        if kind == 'text':
            img = self.build(a)
            tb = round(time.time() - t0, 2)
            out = self.show(img, a, t0)
            out['latency']['build_s'] = tb
            return dict(out, text=a.get('text'))
        if kind == 'program':
            return self.show(Path(a['file']).read_bytes(), a, t0)
        if kind == 'picture':
            from PIL import Image
            from signboard import picture
            im = Image.open(a['file'])
            rec = picture.recommend(im)
            mode = rec['mode'] if a.get('mode', 'auto') == 'auto' else a['mode']
            notes = []
            img = picture.build(im, mode, effect=a.get('effect'), still=bool(a.get('still')), report=notes)
            tb = round(time.time() - t0, 2)
            out = self.show(img, a, t0)
            out['latency']['build_s'] = tb
            return dict(out, mode=picture.mode_of(img), requested=a.get('mode', 'auto'), notes=notes, recommendation=rec)
        if kind == 'power':
            self.sign.power(bool(a['on']))
            out = {'power': bool(a['on']), 'direct_ble': True, 'latency': {'total_s': round(time.time() - t0, 2)}}
            if a.get('verify', True):
                self.look(out, None, t0)
                if 'lit' in out.get('camera', {}):
                    out['camera']['power_ok'] = out['camera']['lit'] == bool(a['on'])
            return out
        if kind == 'brightness':
            self.sign.brightness(int(a['level']))
            return {'brightness': int(a['level']), 'direct_ble': True, 'latency': {'total_s': round(time.time() - t0, 2)}}
        if kind in ('look', 'camera'):
            return self.look({'latency': {}}, a.get('color'), t0)
        raise RuntimeError(f'direct backend cannot do {kind}')


def gif_frames(path, limit=60):
    """All frames of a GIF/PNG, scaled to 32 px high and centred on 128 px (frame delays are not carried over)."""
    from PIL import Image, ImageSequence
    im = Image.open(path)
    out = []
    for fr in ImageSequence.Iterator(im):
        f = fr.convert('RGB')
        if f.size != (128, 32):
            f.thumbnail((128, 32))
            c = Image.new('RGB', (128, 32)); c.paste(f, ((128 - f.width) // 2, (32 - f.height) // 2)); f = c
        out.append(f)
        if len(out) >= limit:
            break
    return out


class Schedule:
    """Timed jobs, persisted in the state dir (signd-schedule.json): {id, at (local ISO), kind, args, state}. Checked every 15 s."""
    MAX_ITEMS = 500
    FILE = config.state_file('schedule.json')

    def __init__(self, jobs):
        self.jobs, self.lock = jobs, threading.Lock()
        try: self.items = json.loads(self.FILE.read_text())
        except Exception: self.items = []
        threading.Thread(target=self._loop, daemon=True, name='schedule').start()

    def _save(self):
        tmp = self.FILE.with_suffix('.tmp')                  # write-then-rename: a crash never leaves a torn file
        tmp.write_text(json.dumps(self.items, indent=1)); tmp.replace(self.FILE)

    def add(self, a):
        import datetime as dt
        at = a.get('at') or ''
        if re.fullmatch(r'\d{1,2}:\d{2}', at):
            h, m = map(int, at.split(':'))
            t = dt.datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)
            if t < dt.datetime.now():
                t += dt.timedelta(days=1)
        else:
            t = dt.datetime.fromisoformat(at)
            if t.tzinfo is not None:                          # compare in naive local time (the loop uses now())
                t = t.astimezone().replace(tzinfo=None)
        kind = 'power' if 'power' in a and not a.get('text') else 'text'
        args = {k: v for k, v in a.items() if k not in ('at',)}
        if kind == 'power':
            if not isinstance(a['power'], bool): raise ValueError('power must be true or false')
            args = {'on': a['power'], 'verify': True}
        else:
            err = text_args_error(args)
            if err: raise ValueError(err)
        if len([i for i in self.items if i['state'] == 'pending']) >= self.MAX_ITEMS:
            raise ValueError('too many pending schedule items')
        item = {'id': uuid.uuid4().hex[:8], 'at': t.isoformat(timespec='minutes'), 'kind': kind, 'args': args,
                'text': a.get('text'), 'color': a.get('color'), 'state': 'pending'}
        with self.lock:
            self.items.append(item); self._save()
        return item

    def remove(self, sid):
        with self.lock:
            n = len(self.items)
            self.items = [i for i in self.items if i['id'] != sid]; self._save()
            return len(self.items) != n

    def _loop(self):
        import datetime as dt
        def when(i):
            t = dt.datetime.fromisoformat(i['at'])
            return t.astimezone().replace(tzinfo=None) if t.tzinfo else t
        while True:
            time.sleep(15)
            try:                                              # one bad item or a disk error must not end the loop
                with self.lock:
                    due = [i for i in self.items if i['state'] == 'pending' and when(i) <= dt.datetime.now()]
                    for i in due:
                        Jobs.current_origin.value = 'schedule-' + i['id']
                        i['state'] = 'fired'; i['job'] = self.jobs.submit(i['kind'], i['args'])['id']
                    if due: self._save()
            except Exception as e:
                print('schedule error:', e)


class ReadCache:
    """The controller's program, read over the shared direct link (the sign's own lock serialises it with jobs)."""

    def __init__(self, sign):
        self.sign, self.lock, self.at, self.val, self.err = sign, threading.Lock(), 0, None, None

    def now(self, max_age=15):
        from signboard.server import parse_now
        with self.lock:
            if time.time() - self.at < max_age and self.val: return self.val
            if self.val and self.sign.want_on is False:
                return dict(self.val, power=False)         # powered off on purpose: a read session would wake it
            if max_age:
                max_age = max(max_age, 60)                 # dashboards poll; each read holds the only BLE slot
            if time.time() - self.at < max_age and self.val: return self.val
            try:
                proj = self.sign.read_program()['project']
                self.project = proj
                self.val = parse_now(proj); self.val['read_at'] = time.time()
                self.at, self.err = time.time(), None
            except Exception as e:
                self.err = f'{type(e).__name__}: {e}'
                if not self.val: raise
            return self.val


def auto_light(jobs):
    """Every 5 min: sun position -> (power, level) per signboard/daylight.py; act only when the target changes, so a
    manual change holds until the next transition. State persists in <state_dir>/signd-light.json across restarts."""
    from signboard.daylight import solar_elevation, policy
    f = config.state_file('light.json')
    try: last = json.loads(f.read_text())
    except Exception: last = {}
    Jobs.current_origin.value = 'auto-light'
    while True:
        try:
            e, morning = solar_elevation()
            on, level = policy(e, morning)
            if level is not None: level = min(level, int(config.get('max_brightness') or 16))   # site cap (signboard.config)
            tgt = {'on': on, 'level': level}
            if tgt != last:
                print(f'auto-light: sun {e:.1f} deg -> {tgt} (was {last})')
                if on:
                    if level != last.get('level'): jobs.submit('brightness', {'level': level, 'verify': False})
                    if not last.get('on'): jobs.submit('power', {'on': True, 'verify': False})
                elif last.get('on') or not last:
                    jobs.submit('power', {'on': False, 'verify': False})
                last = tgt
                f.write_text(json.dumps(last))
        except Exception as ex:
            print('auto-light error:', ex)
        time.sleep(300)


WEB = Path(__file__).resolve().parent / 'web' / 'index.html'


def text_args_error(a):
    """Validate a text job's arguments (API and schedules). Returns an error string or None."""
    from signboard import render
    for k in ('text', 'color', 'outline', 'font', 'background'):
        if a.get(k) is not None and not isinstance(a[k], str):
            return f'{k} must be a string'
    if isinstance(a.get('size'), bool) or not isinstance(a.get('size'), (int, str, type(None))):
        return 'size must be a number'
    if len(a.get('text') or '') > MAX_TEXT:
        return f'text longer than {MAX_TEXT} characters'
    for k in ('color', 'outline'):
        c = a.get(k)
        if c and c.lower() not in COLORS and not re.fullmatch(r'#[0-9a-fA-F]{6}', c):
            return f'{k} must be one of {sorted(COLORS)} or #rrggbb'
    if a.get('font') not in (None, '') and a['font'] not in render.FONTS:
        return f'font must be one of {sorted(render.FONTS)}'
    if a.get('size') not in (None, ''):
        try:
            sz = int(a['size'])
        except (TypeError, ValueError):
            return 'size must be a number'
        if not 8 <= sz <= 40:
            return 'size must be 8..40'
    bg = a.get('background')
    if bg and bg not in ('keep', 'none', 'sunshine') and not re.fullmatch(r'[A-Za-z0-9._-]{1,80}', bg):
        return 'background must be keep, none, sunshine or a stored image name'
    return None


MAX_BODY = 8 * 1024 * 1024          # largest request body (pictures, programs)
MAX_TEXT = 500                       # characters in one text job
ALLOWED_HOSTS = {'localhost'}       # plus this host's names and allowed_hosts, filled in by main()


def host_allowed(host):
    """DNS rebinding needs a name the attacker controls, so any IP literal is safe (the LAN address can change
    with DHCP after startup); names must be ours."""
    import ipaddress
    try:
        ipaddress.ip_address(host.strip('[]'))
        return True
    except ValueError:
        return host in ALLOWED_HOSTS


def make_handler(jobs: Jobs, reads, sched=None, prefs=None):
    prefs = prefs or Prefs()

    class H(BaseHTTPRequestHandler):
        def _json(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False, indent=2).encode()
            self.send_response(code); self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers(); self.wfile.write(body)

        def _guard(self):
            """Refuse DNS rebinding (unknown Host) and cross-site browser requests (Origin of another site).
            Same-origin pages and non-browser clients (curl, scripts: no Origin header) pass."""
            netloc = (self.headers.get('Host') or '').strip().lower()
            host = netloc if netloc.endswith(']') else re.sub(r':\d+$', '', netloc)   # a bare [::1] has no port
            if host and not host_allowed(host):
                return self._refuse(421, f'unknown host {host!r}; add it to allowed_hosts in signboard.config')
            origin = self.headers.get('Origin')
            if origin and origin.split('://', 1)[-1].rstrip('/').lower() != netloc:   # same origin = same host *and* port
                return self._refuse(403, 'cross-site request refused')
            return True

        def _refuse(self, code, error):
            # read a small unread body first: answering and closing with request bytes still unread makes Windows reset
            # the connection, and the client never sees the refusal (seen as a flaky test, 2026-10-13)
            try:
                n = int(self.headers.get('Content-Length') or 0)
            except ValueError:
                n = 0
            if 0 < n <= 64 * 1024:
                self.rfile.read(n)
            self.close_connection = True
            self._json(code, {'error': error})
            return False

        def _body(self):
            try:
                n = int(self.headers.get('Content-Length') or 0)
            except ValueError:
                n = -1
            if n < 0 or n > MAX_BODY:
                raise ValueError(f'request body must be 0..{MAX_BODY} bytes')
            return self.rfile.read(n) if n else b''

        def _picture_body(self):
            """Image + options from a raw image body (options in the query), JSON {"image": base64, ...} or multipart
            form-data (field "image" plus option fields)."""
            q = dict(x.split('=', 1) for x in self.path.split('?', 1)[1].split('&') if '=' in x) if '?' in self.path else {}
            ctype = self.headers.get('Content-Type') or ''
            data = self._body()
            if ctype.startswith('application/json'):
                import base64
                a = json.loads(data or b'{}')
                img = a.pop('image', '')
                return base64.b64decode(img.split(',', 1)[-1]), dict(q, **a)
            if ctype.startswith('multipart/form-data'):
                from email.parser import BytesParser
                from email.policy import HTTP
                msg = BytesParser(policy=HTTP).parsebytes(b'Content-Type: ' + ctype.encode() + b'\r\n\r\n' + data)
                img = None
                for part in msg.iter_parts():
                    name = part.get_param('name', header='content-disposition')
                    if name == 'image' or (img is None and part.get_filename()):
                        img = part.get_payload(decode=True)
                    elif name:
                        q[name] = part.get_payload(decode=True).decode(errors='replace')
                return img or b'', q
            return data, q

        def do_GET(self):
            if not self._guard(): return
            p = self.path.split('?')[0]
            Jobs.current_origin.value = f"{self.client_address[0]} {(self.headers.get('User-Agent') or '-')[:60]}"
            if p == '/api/now':
                if not reads: return self._json(501, {'error': 'no BLE read bridge configured'})
                try: return self._json(200, reads.now())
                except Exception as e: return self._json(502, {'error': str(e)})
            if p == '/api/program':
                if not reads: return self._json(501, {'error': 'no BLE read bridge configured'})
                try: reads.now(); return self._json(200, getattr(reads, 'project', None) or {'error': 'no project yet'})
                except Exception as e: return self._json(502, {'error': str(e)})
            if p == '/api/health':
                try:
                    if config.get('camera') is False: raise RuntimeError('disabled in config')
                    from signboard.camera import find_serial
                    cam_serial = jobs.direct.camera_serial or find_serial(); cam = 'ok'
                except Exception as e:
                    cam_serial, cam = None, ('disabled' if config.get('camera') is False else f'offline: {e}')
                return self._json(200, {'writer': 'direct', 'sign': 'ok' if not getattr(reads, 'err', None) else reads.err,
                                        'camera': cam, 'name': config.get('sign_name'), 'queue': jobs.q.qsize(), 'last_job': jobs.last,
                                        'read_error': getattr(reads, 'err', None)})
            if p == '/api/state':
                return self._json(200, jobs.state.get())
            if p == '/api/prefs':
                return self._json(200, prefs.get(self.client_address[0]))
            if p.startswith('/api/stored/'):
                name = p.rsplit('/', 1)[1]
                f = PHOTO_DIR / name
                if not STORED_NAME.fullmatch(name) or not f.is_file(): return self._json(404, {'error': 'not found'})
                data = f.read_bytes()
                ctype = {'.gif': 'image/gif', '.png': 'image/png', '.jpg': 'image/jpeg', '.webp': 'image/webp'}[f.suffix]
                self.send_response(200); self.send_header('Content-Type', ctype); self.send_header('Content-Length', str(len(data)))
                self.send_header('Cache-Control', 'private, max-age=86400'); self.end_headers(); self.wfile.write(data); return
            if p == '/api/jobs':
                return self._json(200, list(jobs.jobs.values())[-30:][::-1])
            if p == '/api/schedule':
                if not sched: return self._json(404, {'error': 'no scheduler'})
                return self._json(200, sorted(sched.items, key=lambda i: i['at']))
            if p in ('/', '/index.html'):
                name = config.get('sign_name') or 'LED Sign'
                body = WEB.read_text(encoding='utf-8').replace('{{SIGN_NAME_UPPER}}', name.upper()).replace('{{SIGN_NAME}}', name).encode()
                self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Cache-Control', 'no-cache')   # phones kept serving an old page after updates
                self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body); return
            if p.startswith('/api/jobs/'):
                j = jobs.jobs.get(p.rsplit('/', 1)[1]); return self._json(200 if j else 404, j or {'error': 'no such job'})
            if p == '/api/camera':
                shots = sorted(list(PHOTO_DIR.glob('*.png')) + list(PHOTO_DIR.glob('*.jpg')), key=lambda f: f.stat().st_mtime)
                if not shots: return self._json(404, {'error': 'no photos yet'})
                data = shots[-1].read_bytes()
                ctype = 'image/jpeg' if shots[-1].suffix == '.jpg' else 'image/png'
                self.send_response(200); self.send_header('Content-Type', ctype); self.send_header('Content-Length', str(len(data)))
                self.send_header('X-Photo', shots[-1].name); self.end_headers(); self.wfile.write(data); return
            if p == '/api/look':
                return self._json(405, {'error': 'use POST /api/look'})
            self._json(404, {'error': 'not found'})

        def do_POST(self):
            if not self._guard(): return
            try:
                return self._post()
            except ValueError as e:
                return self._json(400, {'error': str(e)})

        def _post(self):
            p = self.path.split('?')[0]
            Jobs.current_origin.value = f"{self.client_address[0]} {(self.headers.get('User-Agent') or '-')[:60]}"
            if p == '/api/image':                          # a background image for text jobs: {"background": <stored name>}
                data = self._body()
                if not data: return self._json(400, {'error': 'empty body'})
                try:
                    from PIL import Image
                    import io
                    im = Image.open(io.BytesIO(data)); info = {'size': list(im.size), 'frames': getattr(im, 'n_frames', 1)}
                    ext = {'GIF': '.gif', 'PNG': '.png', 'JPEG': '.jpg'}.get(im.format)
                except Exception as e:
                    return self._json(400, {'error': f'not an image: {e}'})
                if not ext: return self._json(400, {'error': 'GIF, PNG or JPEG only'})
                name = store_upload('bg', data, ext)
                return self._json(200, {'stored': name, 'use': {'background': name}, **info})
            if p in ('/api/picture', '/api/picture/recommend'):
                q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
                try:
                    from PIL import Image
                    import io
                    if q.get('stored'):                    # a picture this visitor uploaded before (its prefs keep the name)
                        if not STORED_NAME.fullmatch(q['stored']) or not (PHOTO_DIR / q['stored']).is_file():
                            return self._json(404, {'error': 'no such stored picture'})
                        self._body()                       # whatever the client sent is not needed
                        data, opts = (PHOTO_DIR / q['stored']).read_bytes(), q
                    else:
                        data, opts = self._picture_body()
                    im = Image.open(io.BytesIO(data)); im.load()
                except Exception as e:
                    return self._json(400, {'error': f'not an image: {e}'})
                from signboard import picture
                if p == '/api/picture/recommend':
                    out = dict(picture.recommend(im), size=list(im.size))
                    if q.get('keep') and not q.get('stored'):
                        ext = {'GIF': '.gif', 'PNG': '.png', 'JPEG': '.jpg', 'WEBP': '.webp'}.get(im.format, '.png')
                        if ext == '.png' and im.format != 'PNG':
                            buf = io.BytesIO(); im.convert('RGB').save(buf, 'PNG'); data = buf.getvalue()
                        out['stored'] = store_upload('src', data, ext)
                    return self._json(200, out)
                mode = opts.get('mode') or 'auto'
                if mode != 'auto' and mode not in picture.MODES:
                    return self._json(400, {'error': f'mode must be auto or one of {list(picture.MODES)}'})
                f = PHOTO_DIR / f'picture-{int(time.time())}-{uuid.uuid4().hex[:4]}.png'; im.convert('RGB').save(f)
                a = {'file': str(f), 'mode': mode, 'still': str(opts.get('still', '')).lower() in ('1', 'true', 'on', 'yes'),
                     'verify': str(opts.get('verify', 'true')).lower() not in ('0', 'false', 'no')}
                if opts.get('effect') not in (None, ''):
                    a['effect'] = int(opts['effect'])
                return self._json(202, jobs.submit('picture', a))
            if p == '/api/program':                         # raw program image (.bin, 00 00 00 ff ...) straight to the sign
                data = self._body()
                if data[:4] != b'\x00\x00\x00\xff': return self._json(400, {'error': 'not a program image'})
                f = PROGRAMS / f'upload-{int(time.time())}.bin'; f.write_bytes(data)
                return self._json(202, jobs.submit('program', {'file': str(f), 'verify': True}))
            try: a = json.loads(self._body() or b'{}')
            except json.JSONDecodeError: return self._json(400, {'error': 'bad json'})
            if not isinstance(a, dict): return self._json(400, {'error': 'body must be a JSON object'})
            if p == '/api/prefs':
                return self._json(200, prefs.update(self.client_address[0], a))
            if p == '/api/look':
                return self._json(202, jobs.submit('look', {'color': a.get('color')}))
            if p == '/api/schedule':
                if not sched: return self._json(404, {'error': 'no scheduler'})
                try: return self._json(201, sched.add(a))
                except Exception as e: return self._json(400, {'error': f'bad schedule: {e}'})
            if p == '/api/text':
                if not a.get('text') and not a.get('background'): return self._json(400, {'error': 'text required'})
                err = text_args_error(a)
                if err: return self._json(400, {'error': err})
                return self._json(202, jobs.submit('text', a))
            if p == '/api/sign':
                # desired-state, idempotent: {"text":..., "color":..., "background":..., "power":true|false}
                if not reads: return self._json(501, {'error': 'no BLE read bridge configured'})
                from signboard.verify import judge_ble
                out = {'actions': []}
                if 'power' in a and not isinstance(a['power'], bool):
                    return self._json(400, {'error': 'power must be true or false'})
                if a.get('text') is not None or a.get('color'):
                    args = {k: a[k] for k in ('text', 'color', 'background') if a.get(k)}
                    err = text_args_error(args)
                    if err: return self._json(400, {'error': err})
                    try: cur = reads.now(max_age=0)
                    except Exception as e: return self._json(502, {'error': f'cannot read the controller: {e}'})
                    v = judge_ble(cur, a.get('text'), a.get('color'))
                    if v['match']:
                        out['text'] = {'state': 'noop', 'holds': v['got']}
                    else:
                        j = jobs.submit('text', args | {'verify': True})
                        out['text'] = {'state': 'queued', 'job': j['id'], 'was': v['got']}
                        out['actions'].append(j['id'])
                if 'power' in a:
                    j = jobs.submit('power', {'on': a['power'], 'verify': True})
                    out['power'] = {'state': 'queued', 'job': j['id']}; out['actions'].append(j['id'])
                return self._json(200 if not out['actions'] else 202, out)
            if p == '/api/brightness':
                try: lv = int(a.get('level', 0))
                except (TypeError, ValueError): lv = 0
                if not 1 <= lv <= 16: return self._json(400, {'error': 'level 1..16'})
                return self._json(202, jobs.submit('brightness', {'level': lv, 'verify': False}))
            if p == '/api/power':
                on = a.get('on')
                if not isinstance(on, bool): return self._json(400, {'error': 'on must be true or false'})
                return self._json(202, jobs.submit('power', {'on': on, 'verify': bool(a.get('verify', True))}))
            if p == '/api/camera':
                return self._json(202, jobs.submit('camera', {}))
            self._json(404, {'error': 'not found'})

        def do_DELETE(self):
            if not self._guard(): return
            p = self.path.split('?')[0]
            if p.startswith('/api/schedule/') and sched:
                ok = sched.remove(p.rsplit('/', 1)[1])
                return self._json(200 if ok else 404, {'removed': ok})
            self._json(404, {'error': 'not found'})

        def log_message(self, *a): pass
    return H


def lan_ip():
    """The address other LAN devices reach us on (the interface that routes to the internet gateway)."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # TEST-NET address: no packet is sent, the OS just picks the outbound interface
        s.connect(('192.0.2.1', 9)); return s.getsockname()[0]
    finally:
        s.close()


def advertise(name, port):
    """mDNS: answer <name>.local with our LAN address and announce an http service, so phones and laptops on the
    LAN can open http://<name>.local/ with no IP or port to remember (iOS, macOS, Linux, Android 12+, Windows 10+)."""
    import socket
    from zeroconf import ServiceInfo, Zeroconf
    ip = lan_ip()
    info = ServiceInfo('_http._tcp.local.', f"{config.get('sign_name') or 'LED Sign'}._http._tcp.local.", port=port,
                       addresses=[socket.inet_aton(ip)], server=f'{name}.local.', properties={'path': '/'})
    zc = Zeroconf()
    zc.register_service(info, allow_name_change=True)
    print(f'mdns: http://{name}.local' + ('' if port == 80 else f':{port}') + f'/ -> {ip}', flush=True)
    return zc, ip


def keep_advertising(name, port, every=60):
    """advertise(), retried until the LAN is up and redone when our address changes: after a reboot the Wi-Fi can
    come up after signd, and a failed first try used to leave <name>.local unanswered until a restart (2026-10-13)."""
    zc, ip, last_err = None, None, None
    while True:
        try:
            now = lan_ip()
            if now != ip:
                if zc:
                    zc.close()
                zc, ip = advertise(name, port)
            last_err = None
        except Exception as e:
            if str(e) != last_err:
                print('mdns: not advertising yet:', e, flush=True)
            last_err, zc, ip = str(e), None, None
        time.sleep(every)


def main():
    ap = argparse.ArgumentParser(description='LAN service for an RHX LED sign: HTTP API + web UI over the direct BLE writer')
    ap.add_argument('--port', type=int, default=8090)
    ap.add_argument('--ble', default=None, help='sign BLE address (default: sign_address from signboard.config)')
    ap.add_argument('--bind', default='0.0.0.0', help='address to listen on (127.0.0.1 = this computer only)')
    ap.add_argument('--also-port', type=int, action='append', default=[], help='serve on these ports too (e.g. 80)')
    ap.add_argument('--mdns', default=None, help='advertise http://NAME.local/ on the LAN (needs zeroconf)')
    ap.add_argument('--auto-light', action='store_true', help='drive power/brightness from the sun position (signboard/daylight.py)')
    ap.add_argument('--camera', default=None, help='adb serial of the camera phone (default: signboard.config camera_serial)')
    a = ap.parse_args()
    a.ble = a.ble or config.get('sign_address')
    import socket
    ALLOWED_HOSTS.update({socket.gethostname().lower(), socket.gethostname().lower() + '.local'})
    if a.mdns:
        ALLOWED_HOSTS.add(a.mdns.lower() + '.local')
    ALLOWED_HOSTS.update(h.lower() for h in (config.get('allowed_hosts') or []))
    from signboard.direct import DirectSign
    jobs = Jobs(ble_addr=a.ble)
    sign = DirectSign(a.ble)
    jobs.direct = DirectBackend(sign, a.camera)
    reads = ReadCache(sign) if a.ble else None
    sched = Schedule(jobs)
    jobs.reads = reads                      # text jobs get a BLE read-back verdict (job['ble']) once the link frees
    if a.auto_light and config.get('latitude') is None:
        print('auto-light off: set latitude/longitude in signboard.config', flush=True)
    elif a.auto_light:
        threading.Thread(target=auto_light, args=(jobs,), daemon=True).start()
    print(f'signd on {a.bind}:{a.port}; config {config.get("_file")}; reads {"via " + a.ble if a.ble else "off"}', flush=True)
    handler = make_handler(jobs, reads, sched)
    for extra in a.also_port:
        try:
            srv = ThreadingHTTPServer((a.bind, extra), handler)
            threading.Thread(target=srv.serve_forever, daemon=True, name=f'http-{extra}').start()
            print(f'signd also on {a.bind}:{extra}', flush=True)
        except OSError as e:
            print(f'cannot listen on {extra}: {e}', flush=True)
    if a.mdns:
        threading.Thread(target=keep_advertising, args=(a.mdns, 80 if 80 in a.also_port or a.port == 80 else a.port),
                         daemon=True, name='mdns').start()
    ThreadingHTTPServer((a.bind, a.port), handler).serve_forever()


if __name__ == '__main__':
    main()
