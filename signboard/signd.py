"""signd: one HTTP API for the sign, running on a LAN host next to the sign. Callers never see what's behind it.

Writer (default, --writer direct): signd renders the program itself (render.py) and uploads it straight to the
controller over BLE (direct.py): ~1-5 s from request to the sign showing it. The shelf phone (Pixel 2) is only the
camera that photographs the result (camera.py, real JPEGs). --writer app falls back to driving the vendor app on the
phone over adb/SignAgent (app_driver.py). If the controller is ever replaced, swap the backend; the API stays.

  GET  /api/now                       what the sign shows (direct BLE read, cached 60 s in direct mode)
  GET  /api/program                   the controller's raw project JSON from the same read
  POST /api/program                   raw program image (.bin) uploaded as-is
  POST /api/picture[?mode=auto|seven-lengthen|low-gray|middle-gray|high-gray|true-color&still=1&effect=N]
                                      any image (raw body, multipart field "image", or JSON {"image": base64, "mode":..})
                                      -> picture program in the mode that fits the content (signboard.picture); job
  POST /api/picture/recommend         same body; just the recommended mode and why (no upload)
  GET  /api/health                    phone reachable? app connected? last job? queue depth?
  POST /api/text   {"text": "...", "color": "white"|"#rrggbb", "outline": "red"|null,
                    "background": "keep"|"none"|"sunshine"|"<uploaded gif/png>", "font": "bold", "size": 22}
                                      queue a change + send; result.latency = {build_s, upload_s, total_s, photo_s}
  GET  /api/schedule                  timed jobs; POST {"at": "HH:MM"|ISO, "text"...} or {"at":..., "power": false};
  DELETE /api/schedule/<id>           persisted in ~/signd-schedule.json
  POST /api/look                      take a photo now (job)
  POST /api/sign   {"text":..,"color":..,"power":..}   desired state: no-op when the controller already holds it
  POST /api/brightness {"level": 1..16}
  POST /api/power  {"on": true|false}
  POST /api/image  (multipart or raw gif/png, name=...)  push a 128x32 image to the phone's Download/
  POST /api/readback                  open the app, connect to the sign (the app pulls the program), screenshot it
  GET  /api/jobs/<id>                 job status: queued | running | recovering | done | failed (+ photo path)
                                      result.camera = {lit, color_seen, color_ok} from the phone's camera;
                                      text jobs also get ble = {state, match, got, want}: the controller's own
                                      program read back once the app releases the link (pending for ~1-3 min)
  GET  /                              the LAN web UI (web/index.html); /classic = the old inline dashboard
  GET  /api/jobs                      the last 30 jobs, newest first
  POST /api/park                      show the dashboard on the phone (done automatically after each job)
  GET  /api/camera                    latest photo (jpeg/png; X-Photo header = filename)
  GET  /api/look[?color=green]        the vision rig: take a photo now and return its verdict (waits up to 90 s)

Jobs run one at a time on a worker thread (the controller takes one BLE central; the phone is one camera).

  python -m signboard.signd --port 8090                                                    # direct BLE writer
  python -m signboard.signd --writer app --serial <phone ip> --port 8090 [--ble <sign address>]   # legacy: vendor app on a phone
"""
import argparse
import functools
import json
import queue
import re
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from signboard import protocol
try:                                   # legacy writer: drive the vendor app on a phone (not needed for --writer direct)
    from signboard.app_driver import App, Phone, COLORS
except ImportError:
    App = Phone = None
    COLORS = {c: None for c in ('red', 'green', 'yellow', 'blue', 'cyan', 'magenta', 'white')}
from signboard import config

print = functools.partial(print, flush=True)   # the task redirects stdout to a file; flush so the log is live

PHOTO_DIR = config.state_file('photos')
PHOTO_DIR.mkdir(exist_ok=True)


class PhoneLink:
    """Keeps adb to the shelf phone alive. Wireless (ip or ip:port): the port changes after reboots, so rescan
    when lost. USB (a bare serial such as T063205VZB): nothing to rescan, just check the device is attached."""
    def __init__(self, host):
        self.host, self.serial = host, None
        self.agent = bool(host) and host.startswith('http')          # SignAgent on the phone: no adb at all
        self.usb = (not self.agent) and host and ':' not in host and not host.replace('.', '').isdigit()
        if self.usb or self.agent:
            self.serial = host

    def ensure(self):
        if self.agent:
            try:
                import json, urllib.request
                with urllib.request.urlopen(self.host.rstrip('/') + '/ping', timeout=8) as r:
                    if json.loads(r.read().decode()).get('ok'): return self.host
            except Exception as e:
                raise RuntimeError(f'SignAgent on the phone not answering at {self.host} ({e}); is the phone on Wi-Fi?')
            raise RuntimeError(f'SignAgent at {self.host} answered but not ok')
        if self.usb:
            if self._ok(self.serial):
                return self.serial
            alt = self._any_pixel2()             # wireless-debugging names change after re-pairing; USB serial does not
            if alt:
                print(f'phone {self.serial} gone; using {alt}'); self.serial = alt; return alt
            raise RuntimeError(f'shelf phone {self.serial} not attached over adb (wireless debugging off after a reboot? plug it into USB)')
        if self.serial and self._ok(self.serial):
            return self.serial
        out = subprocess.run(['adb', 'devices'], capture_output=True, text=True).stdout
        for line in out.splitlines():
            if line.startswith(self.host + ':') and line.strip().endswith('device'):
                self.serial = line.split()[0]; return self.serial
        for port in self._scan():
            r = subprocess.run(['adb', 'connect', f'{self.host}:{port}'], capture_output=True, text=True, timeout=15).stdout
            if 'connected' in r:
                self.serial = f'{self.host}:{port}'; return self.serial
        raise RuntimeError(f'shelf phone {self.host} not reachable over adb (wireless debugging off?)')

    def _any_pixel2(self):
        """Windows adb names a re-registered mDNS device 'adb-…-xxxxxx (2)._adb-tls-connect._tcp' — with a space —
        so take everything before the 'device' column, not the first word."""
        out = subprocess.run(['adb', 'devices', '-l'], capture_output=True, text=True, timeout=15).stdout
        for line in out.splitlines():
            m = re.match(r'^(\S.*?)\s+device(\s|$)', line)
            if m and (any(d in line for d in (config.get('camera_models') or [])) or (config.get('camera_serial') or '\x00') in line):
                return m.group(1).strip()
        return None

    def _ok(self, serial):
        r = subprocess.run(['adb', '-s', serial, 'get-state'], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() == 'device'

    def _scan(self):
        import socket, concurrent.futures as cf
        def t(p):
            s = socket.socket(); s.settimeout(1.0)
            try: s.connect((self.host, p)); return p
            except OSError: return None
            finally: s.close()
        with cf.ThreadPoolExecutor(400) as ex:
            return [p for p in ex.map(t, range(30000, 50000)) if p]


BLE_LOCK = threading.Lock()      # the controller accepts one central: never read over BLE while the phone app is working


DASHBOARD = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kitchen Sign</title>
<style>
:root{--bg:#0b0d10;--fg:#e8e6e1;--mut:#8a8f98;--acc:#3dd68c;--bad:#ff6b6b;--card:#151920}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,sans-serif;padding:16px;max-width:720px;margin:auto}
h1{font-size:18px;margin:0 0 12px;letter-spacing:.04em}h1 span{color:var(--mut);font-weight:400}
.card{background:var(--card);border-radius:10px;padding:14px;margin-bottom:12px}
img{width:100%;border-radius:8px;background:#000;display:block}
form{display:flex;gap:8px;flex-wrap:wrap}input[type=text]{flex:1;min-width:160px;font-size:16px;padding:10px;border-radius:8px;border:1px solid #333;background:#0f1216;color:var(--fg)}
select,button{font-size:15px;padding:10px 14px;border-radius:8px;border:1px solid #333;background:#1d2330;color:var(--fg)}button.go{background:var(--acc);color:#000;border:0;font-weight:600}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.ok{color:var(--acc)}.bad{color:var(--bad)}.mut{color:var(--mut)}
table{width:100%;border-collapse:collapse;font-size:13px}td{padding:6px 4px;border-top:1px solid #222;vertical-align:top;font-variant-numeric:tabular-nums}
</style></head><body>
<h1>KITCHEN SIGN <span id="hl">…</span></h1>
<div class="card"><img id="cam" src="/api/camera" alt="latest photo"><div class="row mut" style="margin-top:8px"><span id="now">reading…</span><button onclick="look()">look now</button></div></div>
<div class="card"><form onsubmit="return send(event)"><input id="t" type="text" placeholder="message" maxlength="60">
<select id="c"><option>red</option><option selected>green</option><option>yellow</option><option>blue</option><option>cyan</option><option>magenta</option><option>white</option></select>
<button class="go">send</button><button type="button" onclick="power(true)">on</button><button type="button" onclick="power(false)">off</button></form></div>
<div class="card"><table id="jobs"></table></div>
<script>
const $=id=>document.getElementById(id);
async function j(u,o){const r=await fetch(u,o);return r.json()}
async function refresh(){
 try{const h=await j('/api/health');$('hl').textContent=(h.phone==='ok'?'phone ok':'phone: '+h.phone)+(h.queue?' · '+h.queue+' queued':'')}catch(e){$('hl').textContent='signd unreachable'}
 try{const n=await j('/api/now');$('now').textContent='controller holds: '+JSON.stringify(n.text)+' '+(n.color||'')+(n.background?' + '+n.background:'')}catch(e){}
 try{const js=await j('/api/jobs');$('jobs').innerHTML=js.map(x=>{const r=x.result||{},c=r.camera||{},b=x.ble||{};
  const what=x.kind==='text'?JSON.stringify(x.args.text)+' '+(x.args.color||''):x.kind==='power'?(x.args.on?'power on':'power off'):x.kind;
  const t=x.finished_at?Math.round(x.finished_at-x.started_at)+'s':'';
  const cam=c.lit===undefined?'':' cam '+(c.lit?'lit':'dark')+(c.color_seen?' '+c.color_seen:'')+(c.color_ok===true?' ✓':c.color_ok===false?' ✗':'')+(c.power_ok===true?' ✓':c.power_ok===false?' ✗':'');
  const ble=b.state==='done'?' ble '+(b.match?'✓':'✗'):b.state==='pending'?' ble …':'';
  const st=x.state==='done'?'<span class=ok>done</span>':x.state==='failed'?'<span class=bad>failed</span>':'<span class=mut>'+x.state+'</span>';
  return '<tr><td>'+new Date(x.queued_at*1000).toLocaleTimeString()+'</td><td>'+what+'</td><td>'+st+' '+t+'</td><td>'+cam+ble+(x.error?'<div class=bad>'+x.error+'</div>':'')+'</td></tr>'}).join('')}catch(e){}
 $('cam').src='/api/camera?t='+Date.now();
}
async function send(ev){ev.preventDefault();const t=$('t').value.trim();if(!t)return false;await j('/api/text',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:t,color:$('c').value,verify:true})});$('t').value='';refresh();return false}
async function power(on){await j('/api/power',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({on,verify:true})});refresh()}
async function look(){$('now').textContent='looking…';try{const r=await j('/api/look');const c=r.result.camera;$('now').textContent='look: '+(c.lit?'lit':'dark')+(c.color_seen?' '+c.color_seen:'')+' brightness '+c.brightness}catch(e){}refresh()}
refresh();setInterval(refresh,6000);
</script></body></html>"""


def _judge(photo, color):
    """Camera verdict for a job result; never lets a bad photo fail the job itself."""
    try:
        from signboard.verify import judge_photo
        return judge_photo(photo, color)
    except Exception as e:
        return {'error': f'{type(e).__name__}: {e}'}


class Jobs:
    def __init__(self, link: PhoneLink = None, verbose=True, ble_addr=None):
        self.link, self.q, self.jobs, self.verbose, self.ble_addr = link, queue.Queue(), {}, verbose, ble_addr
        self.last = None
        threading.Thread(target=self._worker, daemon=True).start()

    current_origin = threading.local()     # set per request thread by the HTTP handler; 'auto-light' etc. set it by hand

    def submit(self, kind, args):
        jid = uuid.uuid4().hex[:8]
        shown = {k: ('***' if k == 'pwd' else v) for k, v in args.items()}
        origin = getattr(Jobs.current_origin, 'value', None) or 'internal'
        self.jobs[jid] = {'id': jid, 'kind': kind, 'args': shown, 'state': 'queued', 'queued_at': time.time(), 'origin': origin}
        self._real_args = getattr(self, '_real_args', {}); self._real_args[jid] = args
        self.q.put(jid)
        return self.jobs[jid]

    def _worker(self):
        while True:
            jid = self.q.get(); job = self.jobs[jid]
            job['state'], job['started_at'] = 'running', time.time()
            args = self._real_args.pop(jid, job['args'])
            for attempt in range(2):                 # unattended: one self-heal (force-stop + relaunch) and retry
                try:
                    with BLE_LOCK:
                        job['result'] = self._run(job['kind'], args)
                    job['state'] = 'done'; job.pop('error', None)
                    break
                except Exception as e:
                    job['state'], job['error'] = 'failed', f'{type(e).__name__}: {e}'
                    if attempt == 0 and 'locked with a PIN' not in job['error'] and job['kind'] in ('text', 'power', 'readback', 'look', 'camera'):
                        job['retried'] = job['error']; job['state'] = 'recovering'
                        print('job', jid, 'failed, recovering:', job['error'])
                        if getattr(self, 'direct', None) and job['kind'] in DIRECT_KINDS:
                            self.direct.sign.close()          # drop the link; the retry reconnects from scratch
                            time.sleep(3)
                        elif self.link:
                            try:
                                App(Phone(self.link.ensure(), verbose=self.verbose)).recover()
                            except Exception as e2:
                                print('recover failed, using direct BLE fallback:', e2)
                        job['state'] = 'running'
            job['finished_at'] = time.time(); self.last = job
            if getattr(self, 'park_url', None) and self.q.empty() and self.link:
                try: App(Phone(self.link.ensure(), verbose=False)).park(self.park_url)
                except Exception as e: print('park failed:', e)
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
        if getattr(self, 'direct', None) and kind in DIRECT_KINDS:
            return self.direct.run(kind, a)
        app = None
        if self.link:
            try:
                serial = self.link.ensure()
                app = App(Phone(serial, verbose=self.verbose))
            except Exception as e:
                print(f'phone unreachable ({e}); falling back to direct BLE backend!')

        if not app:
            from signboard.ble import BleakTransport
            from signboard import protocol
            tr = BleakTransport(self.ble_addr, timeout=15)
            try:
                if kind in ('power', 'brightness', 'text', 'readback', 'now'):
                    ok = protocol.login(tr.request)
                    if not ok:
                        raise RuntimeError('RC5 authentication failed')
                if kind == 'power':
                    reply = tr.request(protocol.power(bool(a['on'])), 1)
                    return {'power': bool(a['on']), 'reply': reply.hex(), 'direct_ble': True}
                if kind in ('readback', 'now'):
                    prog = protocol.read_program(tr.request)
                    return {'project': prog['project'], 'direct_ble': True}
                if kind == 'text':
                    prog = protocol.read_program(tr.request)
                    from signboard import image
                    new_img = image.join(image.split(prog['raw']))
                    chunks = protocol.make_upload_stream(new_img)
                    tr.request(protocol.upload_request(len(new_img)), 18)
                    for chunk in chunks:
                        tr.write(chunk)
                        time.sleep(0.04)
                    tr.request(protocol.finish_upload(), 14)
                    return {'text': a['text'], 'uploaded': len(new_img), 'direct_ble': True}
            finally:
                tr.close()
            return {}

        app = App(Phone(self.link.ensure(), verbose=self.verbose))
        if kind == 'camera':
            return {'photo': app.camera(str(PHOTO_DIR / f'{int(time.time())}.png'))}
        if kind == 'park':
            app.park(self.park_url or 'about:blank'); return {'parked': self.park_url}
        if kind == 'look':
            photo = app.camera(str(PHOTO_DIR / f'look-{int(time.time())}.png'))
            return {'photo': photo, 'camera': _judge(photo, a.get('color'))}
        if kind == 'pwd_enter':
            app.open(connect=False)
            app.ensure_connected(); app.open_menu(); app.p.tap(__import__('signboard.app_driver', fromlist=['MENU_CHANGE_PWD']).MENU_CHANGE_PWD, 4)
            shot = str(PHOTO_DIR / f'pwd-enter-{int(time.time())}.png')
            st = app.enter_card_password(a['pwd'], shot)
            return {'state': st, 'screenshot': shot, 'connected': app.connected()}
        if kind == 'pwd_form':
            app.open(connect=False)
            shot = str(PHOTO_DIR / f'pwd-form-{int(time.time())}.png')
            st = app.open_password_form(shot)
            return {'state': st, 'screenshot': shot, 'connected': app.connected()}
        if kind == 'debug_list':
            # bench aid: tap the connect button, then sample state()/rule/pixels for 30 s and screenshot
            from signboard.app_driver import CONNECT_BTN
            app.wake(); app.dismiss_dialogs()
            app.p.tap(CONNECT_BTN, 2)
            trace = []
            for i in range(10):
                px = app._px()
                trace.append({'t': 2 + i * 3, 'state': app.state(), 'rule': app._bt_list_rule(px),
                              'dim': px(60, 300), 'mid': px(540, 900), 'r300': px(300, 840), 'r540': px(540, 840), 'r780': px(780, 840)})
                time.sleep(3)
            shot = str(PHOTO_DIR / f'debug-list-{int(time.time())}.png'); app.p.screenshot(shot)
            return {'tap_device_px': app.p.S(CONNECT_BTN), 'size': [app.p.w, app.p.h], 'trace': trace, 'screenshot': shot}
        if kind == 'text':
            # Edit first with the app idle, connect last: the controller drops an idle link after ~70 s and the
            # Moto X's editing takes longer than that, so connecting before editing guaranteed a dead link at Send.
            app.open(connect=False)
            app.set_text(a['text'])
            if a.get('color'): app.set_color(a['color'])
            if a.get('background'): app.set_background(a['background'])
            app.ensure_connected()
            app.send()
            out = {}
            if a.get('verify', True):
                out['photo'] = app.camera(str(PHOTO_DIR / f'{int(time.time())}.png'))
                out['camera'] = _judge(out['photo'], a.get('color'))
            return out
        app.open()
        if kind == 'readback':
            shot = str(PHOTO_DIR / f'readback-{int(time.time())}.png')
            app.p.screenshot(shot)
            return {'connected': app.connected(), 'screenshot': shot}
        if kind == 'brightness':
            app.set_brightness(a['level'])
            return {'brightness': int(a['level'])}
        if kind == 'power':
            app.power(bool(a['on']))
        out = {}
        if a.get('verify', True):
            out['photo'] = app.camera(str(PHOTO_DIR / f'{int(time.time())}.png'))
            out['camera'] = _judge(out['photo'], None)
            if kind == 'power':                       # off => the camera must see nothing lit; on => something
                out['camera']['power_ok'] = out['camera']['lit'] == bool(a['on'])
        return out


DIRECT_KINDS = ('text', 'power', 'brightness', 'program', 'picture', 'look', 'camera')
PROGRAMS = config.state_file('programs')
PROGRAMS.mkdir(exist_ok=True)


class DirectBackend:
    """signd writes the controller itself over BLE (signboard.direct): text is rendered here (signboard.render), the
    background is kept, replaced by a stock animation, or built from an uploaded GIF/PNG. The shelf phone only takes
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
        try:
            photo = self.camera().take(str(PHOTO_DIR / f'{int(time.time())}.jpg'))
            out['photo'] = photo
            out['camera'] = _judge(photo, color)
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
    """Timed jobs, persisted in ~/signd-schedule.json: {id, at (local ISO), kind, args, state}. Checked every 15 s."""
    FILE = config.state_file('schedule.json')

    def __init__(self, jobs):
        self.jobs, self.lock = jobs, threading.Lock()
        try: self.items = json.loads(self.FILE.read_text())
        except Exception: self.items = []
        threading.Thread(target=self._loop, daemon=True, name='schedule').start()

    def _save(self):
        self.FILE.write_text(json.dumps(self.items, indent=1))

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
        kind = 'power' if 'power' in a and not a.get('text') else 'text'
        args = {k: v for k, v in a.items() if k not in ('at',)}
        if kind == 'power':
            args = {'on': bool(a['power']), 'verify': True}
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
        while True:
            time.sleep(15)
            with self.lock:
                due = [i for i in self.items if i['state'] == 'pending' and dt.datetime.fromisoformat(i['at']) <= dt.datetime.now()]
                for i in due:
                    Jobs.current_origin.value = 'schedule-' + i['id']
                    i['state'] = 'fired'; i['job'] = self.jobs.submit(i['kind'], i['args'])['id']
                if due: self._save()


class ReadCache:
    def __init__(self, ble_addr, sign=None):
        self.addr, self.lock, self.at, self.val, self.err = ble_addr, threading.Lock(), 0, None, None
        self.sign = sign

    def now(self, max_age=15):
        from signboard.ble import BleakTransport
        from signboard.server import parse_now
        with self.lock:
            if time.time() - self.at < max_age and self.val: return self.val
            if self.sign and self.val and self.sign.want_on is False:
                return dict(self.val, power=False)         # powered off on purpose: a read session would wake it
            if self.sign and max_age:
                max_age = max(max_age, 60)                 # dashboards poll; each read holds the only BLE slot
            if self.sign and time.time() - self.at < max_age and self.val: return self.val
            if self.sign:                                   # direct mode: share the one link (its lock serialises)
                try:
                    proj = self.sign.read_program()['project']
                    self.project = proj
                    self.val = parse_now(proj); self.val['read_at'] = time.time()
                    self.at, self.err = time.time(), None
                except Exception as e:
                    self.err = f'{type(e).__name__}: {e}'
                    if not self.val: raise
                return self.val
            if not BLE_LOCK.acquire(timeout=0.5):        # a phone job owns the link; serve stale rather than collide
                if self.val: return self.val
                raise RuntimeError('sign busy (phone job running)')
            tr = None
            try:
                tr = BleakTransport(self.addr, timeout=10)
                proj = protocol.read_program(tr.request)['project']
                self.project = proj
                self.val = parse_now(proj); self.val['read_at'] = time.time()
                self.at, self.err = time.time(), None
            except Exception as e:
                self.err = f'{type(e).__name__}: {e}'
                if not self.val: raise
            finally:
                if tr:
                    try: tr.close()
                    except Exception: pass
                BLE_LOCK.release()
                time.sleep(3)                               # give the controller time to accept the next central
            return self.val


def auto_light(jobs):
    """Every 5 min: sun position -> (power, level) per signboard/daylight.py; act only when the target changes, so a
    manual change holds until the next transition. State persists in ~/signd-light.json across restarts."""
    from signboard.daylight import solar_elevation, policy
    f = config.state_file('light.json')
    try: last = json.loads(f.read_text())
    except Exception: last = {}
    Jobs.current_origin.value = 'auto-light'
    while True:
        try:
            e, morning = solar_elevation()
            on, level = policy(e, morning)
            level = min(level, int(config.get('max_brightness') or 16))   # site cap (signboard.config)
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


def make_handler(jobs: Jobs, reads, link: PhoneLink, sched=None):
    class H(BaseHTTPRequestHandler):
        def _json(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False, indent=2).encode()
            self.send_response(code); self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body))); self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers(); self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get('Content-Length') or 0)
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
            if p == '/api/health' and getattr(jobs, 'direct', None):
                try:
                    from signboard.camera import find_serial
                    cam_serial = jobs.direct.camera_serial or find_serial(); cam = 'ok'
                except Exception as e:
                    cam_serial, cam = None, f'offline: {e}'
                return self._json(200, {'writer': 'direct', 'sign': 'ok' if not getattr(reads, 'err', None) else reads.err,
                                        'camera': cam, 'camera_serial': cam_serial,
                                        'phone': 'not needed (signd writes the sign over BLE)',
                                        'serial': 'direct-ble', 'queue': jobs.q.qsize(), 'last_job': jobs.last,
                                        'read_error': getattr(reads, 'err', None)})
            if p == '/api/health':
                if not link:
                    serial, phone = 'direct-ble', 'none (standalone direct BLE mode)'
                else:
                    try:
                        serial = link.ensure(); phone = 'ok'
                        if Phone(serial, verbose=False).locked(): phone = 'locked (PIN): unlock the phone and remove its screen lock'
                    except Exception:
                        serial, phone = 'direct-ble', 'direct BLE active (shelf phone offline)'
                return self._json(200, {'phone': phone, 'serial': serial, 'queue': jobs.q.qsize(), 'last_job': jobs.last,
                                        'read_error': getattr(reads, 'err', None)})
            if p.startswith('/files/'):                   # static files from the photo dir (lets the phone fetch a background itself)
                f = PHOTO_DIR / re.sub(r'[^A-Za-z0-9._-]', '_', p.split('/', 2)[2])
                if not f.is_file(): return self._json(404, {'error': 'no such file'})
                data = f.read_bytes()
                ctype = {'.gif': 'image/gif', '.png': 'image/png', '.jpg': 'image/jpeg'}.get(f.suffix.lower(), 'application/octet-stream')
                self.send_response(200); self.send_header('Content-Type', ctype); self.send_header('Content-Length', str(len(data)))
                self.end_headers(); self.wfile.write(data); return
            if p == '/api/jobs':
                return self._json(200, list(jobs.jobs.values())[-30:][::-1])
            if p == '/api/schedule':
                if not sched: return self._json(404, {'error': 'no scheduler'})
                return self._json(200, sorted(sched.items, key=lambda i: i['at']))
            if p in ('/', '/index.html', '/classic'):
                body = WEB.read_bytes() if WEB.exists() and p != '/classic' else DASHBOARD.encode()
                self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8')
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
                # the vision rig: take a photo now and judge it (optional ?color=green to check the hue)
                q = dict(x.split('=', 1) for x in self.path.split('?', 1)[1].split('&') if '=' in x) if '?' in self.path else {}
                j = jobs.submit('look', {'color': q.get('color')})
                for _ in range(90):
                    if j['state'] in ('done', 'failed'): break
                    time.sleep(1)
                return self._json(200 if j['state'] == 'done' else 504, j)
            self._json(404, {'error': 'not found'})

        def do_POST(self):
            p = self.path.split('?')[0]
            Jobs.current_origin.value = f"{self.client_address[0]} {(self.headers.get('User-Agent') or '-')[:60]}"
            if p == '/api/image':
                q = dict(x.split('=', 1) for x in self.path.split('?', 1)[1].split('&') if '=' in x) if '?' in self.path else {}
                name = re.sub(r'[^A-Za-z0-9._-]', '_', (self.headers.get('X-Name') or q.get('name') or 'image.gif'))
                data = self._body()
                if not data: return self._json(400, {'error': 'empty body'})
                tmp = PHOTO_DIR / name; tmp.write_bytes(data)
                if getattr(jobs, 'direct', None):            # direct mode: signd renders backgrounds itself
                    try:
                        from PIL import Image
                        im = Image.open(tmp); info = {'size': list(im.size), 'frames': getattr(im, 'n_frames', 1)}
                    except Exception as e:
                        return self._json(400, {'error': f'not an image: {e}'})
                    return self._json(200, {'stored': name, 'use': {'background': name}, **info})
                serial = link.ensure()
                if link.agent:                       # SignAgent saves it through MediaStore (Pictures/sign); no adb involved
                    import urllib.request
                    req = urllib.request.Request(serial.rstrip('/') + '/upload?name=' + name, data=data, method='POST')
                    with urllib.request.urlopen(req, timeout=60) as r:
                        res = json.loads(r.read().decode())
                    if not res.get('ok'): return self._json(502, {'error': 'agent upload failed', 'detail': res})
                    return self._json(200, {'stored': name, 'where': 'Pictures/sign', 'use': {'background': name}})
                subprocess.run(['adb', '-s', serial, 'push', str(tmp), f'/sdcard/Download/{name}'], check=True, capture_output=True, timeout=60)
                subprocess.run(['adb', '-s', serial, 'shell', 'am', 'broadcast', '-a', 'android.intent.action.MEDIA_SCANNER_SCAN_FILE',
                                '-d', f'file:///sdcard/Download/{name}'], capture_output=True, timeout=30)
                return self._json(200, {'stored': name, 'use': {'background': name}})
            if p in ('/api/picture', '/api/picture/recommend'):
                try:
                    data, opts = self._picture_body()
                    from PIL import Image
                    import io
                    im = Image.open(io.BytesIO(data)); im.load()
                except Exception as e:
                    return self._json(400, {'error': f'not an image: {e}'})
                from signboard import picture
                if p == '/api/picture/recommend':
                    return self._json(200, dict(picture.recommend(im), size=list(im.size)))
                mode = opts.get('mode') or 'auto'
                if mode != 'auto' and mode not in picture.MODES:
                    return self._json(400, {'error': f'mode must be auto or one of {list(picture.MODES)}'})
                if not getattr(jobs, 'direct', None):
                    return self._json(409, {'error': 'pictures need the direct BLE writer'})
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
            if p == '/api/look':
                return self._json(202, jobs.submit('look', {'color': a.get('color')}))
            if p == '/api/schedule':
                if not sched: return self._json(404, {'error': 'no scheduler'})
                try: return self._json(201, sched.add(a))
                except Exception as e: return self._json(400, {'error': f'bad schedule: {e}'})
            if p == '/api/text':
                if not a.get('text') and not a.get('background'): return self._json(400, {'error': 'text required'})
                hexok = bool(getattr(jobs, 'direct', None)) and re.fullmatch(r'#[0-9a-fA-F]{6}', a.get('color') or '')
                if a.get('color') and a['color'].lower() not in COLORS and not hexok:
                    return self._json(400, {'error': f'color must be one of {sorted(COLORS)}' + (' or #rrggbb' if getattr(jobs, 'direct', None) else '')})
                return self._json(202, jobs.submit('text', a))
            if p == '/api/sign':
                # desired-state, idempotent: {"text":..., "color":..., "background":..., "power":true|false}
                if not reads: return self._json(501, {'error': 'no BLE read bridge configured'})
                from signboard.verify import judge_ble
                out = {'actions': []}
                if a.get('text') is not None or a.get('color'):
                    try: cur = reads.now(max_age=0)
                    except Exception as e: return self._json(502, {'error': f'cannot read the controller: {e}'})
                    v = judge_ble(cur, a.get('text'), a.get('color'))
                    if v['match']:
                        out['text'] = {'state': 'noop', 'holds': v['got']}
                    else:
                        j = jobs.submit('text', {k: a[k] for k in ('text', 'color', 'background') if a.get(k)} | {'verify': True})
                        out['text'] = {'state': 'queued', 'job': j['id'], 'was': v['got']}
                        out['actions'].append(j['id'])
                if 'power' in a:
                    j = jobs.submit('power', {'on': bool(a['power']), 'verify': True})
                    out['power'] = {'state': 'queued', 'job': j['id']}; out['actions'].append(j['id'])
                return self._json(200 if not out['actions'] else 202, out)
            if p == '/api/brightness':
                lv = int(a.get('level', 0))
                if not 1 <= lv <= 16: return self._json(400, {'error': 'level 1..16'})
                return self._json(202, jobs.submit('brightness', {'level': lv, 'verify': False}))
            if p == '/api/power':
                return self._json(202, jobs.submit('power', {'on': a.get('on', True), 'verify': a.get('verify', True)}))
            if p == '/api/camera':
                return self._json(202, jobs.submit('camera', {}))
            if p == '/api/park':
                return self._json(202, jobs.submit('park', {}))
            if p == '/api/readback':
                return self._json(202, jobs.submit('readback', {'verify': False}))
            if p == '/api/debug_list':
                return self._json(202, jobs.submit('debug_list', {}))
            if p == '/api/pwd_form':
                return self._json(202, jobs.submit('pwd_form', {}))
            if p == '/api/pwd_enter':
                if not a.get('pwd'): return self._json(400, {'error': 'pwd required'})
                return self._json(202, jobs.submit('pwd_enter', {'pwd': a['pwd']}))
            self._json(404, {'error': 'not found'})

        def do_DELETE(self):
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
    info = ServiceInfo('_http._tcp.local.', f'Kitchen Sign._http._tcp.local.', port=port,
                       addresses=[socket.inet_aton(ip)], server=f'{name}.local.', properties={'path': '/'})
    zc = Zeroconf()
    zc.register_service(info, allow_name_change=True)
    print(f'mdns: http://{name}.local' + ('' if port == 80 else f':{port}') + f'/ -> {ip}', flush=True)
    return zc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--serial', default=None, help='shelf phone: http://ip:8091 (SignAgent), an adb serial, or omit for standalone direct BLE mode')
    ap.add_argument('--port', type=int, default=8090)
    ap.add_argument('--ble', default=None, help='sign BLE address (default: sign_address from signboard.config)')
    ap.add_argument('--bind', default='0.0.0.0')
    ap.add_argument('--also-port', type=int, action='append', default=[], help='serve on these ports too (e.g. 80)')
    ap.add_argument('--mdns', default=None, help='advertise http://NAME.local/ on the LAN (needs zeroconf)')
    ap.add_argument('--auto-light', action='store_true', help='drive power/brightness from the sun position (signboard/daylight.py)')
    ap.add_argument('--writer', choices=['direct', 'app'], default='direct',
                    help='direct: signd writes the controller over BLE (default); app: drive the vendor app on the phone')
    ap.add_argument('--camera', default=None, help='adb serial of the camera phone (default: find the Pixel 2)')
    ap.add_argument('--park', default=None, help='URL the phone shows between jobs (the dashboard), e.g. http://<this host>:8090/')
    a = ap.parse_args()
    a.ble = a.ble or config.get('sign_address')
    link = None
    if a.serial:
        link = PhoneLink(a.serial if a.serial.startswith('http') else a.serial.split(':')[0])
        if ':' in a.serial and not a.serial.startswith('http'): link.serial = a.serial
    jobs = Jobs(link, ble_addr=a.ble)
    sign = None
    if a.writer == 'direct':
        from signboard.direct import DirectSign
        sign = DirectSign(a.ble)
        jobs.direct = DirectBackend(sign, a.camera)
        if link is None or a.park is None:
            jobs.park_url = None
    reads = ReadCache(a.ble, sign) if a.ble else None
    sched = Schedule(jobs)
    jobs.reads = reads                      # text jobs get a BLE read-back verdict (job['ble']) once the link frees
    jobs.park_url = a.park
    if a.auto_light and config.get('latitude') is None:
        print('auto-light off: set latitude/longitude in signboard.config', flush=True)
    elif a.auto_light: threading.Thread(target=auto_light, args=(jobs,), daemon=True).start()
    print(f'signd on {a.bind}:{a.port}; writer {a.writer}; phone {a.serial or "none"}; reads {"via " + a.ble if a.ble else "off"}', flush=True)
    handler = make_handler(jobs, reads, link, sched)
    for extra in a.also_port:
        try:
            srv = ThreadingHTTPServer((a.bind, extra), handler)
            threading.Thread(target=srv.serve_forever, daemon=True, name=f'http-{extra}').start()
            print(f'signd also on {a.bind}:{extra}', flush=True)
        except OSError as e:
            print(f'cannot listen on {extra}: {e}', flush=True)
    if a.mdns:
        try:
            advertise(a.mdns, 80 if 80 in a.also_port or a.port == 80 else a.port)
        except Exception as e:
            print('mdns failed:', e, flush=True)
    ThreadingHTTPServer((a.bind, a.port), handler).serve_forever()


if __name__ == '__main__':
    main()
