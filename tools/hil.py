"""hil.py : hardware-in-the-loop checks on a real RHX8 panel, judged by camera photos. Run before a release or a merge.

  python tools/hil.py --out DIR [--templates DIR] [--keep-on]

Needs: your signboard config (sign_address, sign_password, camera), an adb camera phone pointed at the panel, and
templates (docs/templates.md). Runs the code in *this* checkout, with its own state directory (DIR), so a signd that
is already running keeps its files. That signd must stay idle meanwhile: the controller takes one BLE central.

What it does, at brightness 1, judging each step by a real photo:
  backup     read the program out of the sign and photograph the panel (was it on?)
  text       signd API text job (render, upload, camera verdict, BLE read-back)
  picture    signd API picture job, low gray
  pans       controller-native pans up and left (raw layout)
  power      off -> dark, on -> lit with the program kept (the size-0 login)
  restore    upload the backed-up program and put the power back the way it was
Writes DIR/hil-report.json and the photos. Exit 0 only if every step passed.
"""
import argparse
import io
import json
import os
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--out', required=True, help='state + report directory (created)')
    ap.add_argument('--templates', default=None, help='your templates (default: templates_dir from your config)')
    ap.add_argument('--keep-on', action='store_true', help='leave the panel on afterwards even if it was off')
    a = ap.parse_args()
    out = Path(a.out).resolve(); out.mkdir(parents=True, exist_ok=True)
    os.environ['SIGNBOARD_STATE_DIR'] = str(out)           # never touch a running signd's state
    if a.templates:
        os.environ['SIGNBOARD_TEMPLATES_DIR'] = str(Path(a.templates).resolve())
    sys.path.insert(0, str(ROOT))

    from signboard import direct, signd, picture, verify, image, pan, render
    from signboard.camera import Camera
    from PIL import Image, ImageDraw

    sha = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', '--short', 'HEAD'], capture_output=True, text=True).stdout.strip() or 'nogit'
    report = {'commit': sha, 'started': time.strftime('%Y-%m-%d %H:%M:%S'), 'steps': []}
    sign = direct.DirectSign(verbose=False)
    cam = Camera(verbose=False)
    state = {}

    def photo(tag, color=None):
        p = cam.take(str(out / f'{len(report["steps"]):02d}-{tag}.jpg'))
        return p, verify.judge_photo(p, color)

    def step(name, fn):
        t0 = time.time(); rec = {'step': name}
        try:
            rec.update(fn() or {}); rec.setdefault('ok', True)
        except Exception as e:
            rec.update(ok=False, error=f'{type(e).__name__}: {e}', trace=traceback.format_exc(limit=3))
        rec['seconds'] = round(time.time() - t0, 1)
        report['steps'].append(rec)
        print(f"{'PASS' if rec['ok'] else 'FAIL'}  {name:10s} {rec['seconds']:5.1f} s  {rec.get('error') or rec.get('note', '')}", flush=True)
        return rec['ok']

    # ---- steps -------------------------------------------------------------------------------------------------
    def backup():
        state['img'] = sign.read_image()
        (out / 'backup.bin').write_bytes(state['img'])
        p, v = photo('before')
        state['was_on'] = v['lit']
        sign.brightness(1)
        return {'ok': True, 'was_on': v['lit'], 'bytes': len(state['img']), 'photo': p}

    # an in-process signd on a private port, writing through the same link
    jobs = signd.Jobs(verbose=False)
    jobs.direct = signd.DirectBackend(sign)
    jobs.direct._cam = cam
    jobs.reads = reads = signd.ReadCache(sign)
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(('127.0.0.1', 0), signd.make_handler(jobs, reads))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    def api(method, path, body=None, ctype='application/json'):
        import http.client
        c = http.client.HTTPConnection('127.0.0.1', port, timeout=60)
        data = json.dumps(body).encode() if isinstance(body, dict) else body
        c.request(method, path, body=data, headers={'Host': f'localhost:{port}', 'Content-Type': ctype})
        r = c.getresponse(); return r.status, json.loads(r.read())

    def run_job(path, body, ctype='application/json', timeout=120):
        st, j = api('POST', path, body, ctype)
        if st != 202:
            raise RuntimeError(f'{path} -> {st} {j}')
        end = time.time() + timeout
        while time.time() < end:
            _, j = api('GET', f"/api/jobs/{j['id']}")
            if j['state'] in ('done', 'failed'):
                return j
            time.sleep(0.5)
        raise RuntimeError(f'job {j["id"]} timed out')

    def text():
        if not any((d / 'text.bin').is_file() for d in picture.template_dirs()):
            raise RuntimeError('no text template (docs/templates.md)')
        (signd.PROGRAMS / 'current.bin').write_bytes(picture.template('text'))
        caption = f'PR {sha}'
        j = run_job('/api/text', {'text': caption, 'color': 'green', 'background': 'none'})
        if j['state'] != 'done':
            raise RuntimeError(j.get('error'))
        cam_v = j['result'].get('camera', {})
        ble = verify.judge_ble(reads.now(max_age=0), caption, 'green')
        return {'ok': bool(cam_v.get('lit')) and bool(cam_v.get('color_ok')) and ble['match'],
                'camera': {k: cam_v.get(k) for k in ('lit', 'color_seen', 'color_ok')}, 'ble': ble,
                'latency': j['result']['latency'], 'photo': j['result'].get('photo')}

    def picture_job():
        im = Image.new('RGB', (128, 32))
        im.putdata([(x * 2, 40, 255 - x * 2) for y in range(32) for x in range(128)])
        buf = io.BytesIO(); im.save(buf, 'PNG')
        j = run_job('/api/picture?mode=low-gray&still=1', buf.getvalue(), 'image/png')
        if j['state'] != 'done':
            raise RuntimeError(j.get('error'))
        cam_v = j['result'].get('camera', {})
        return {'ok': bool(cam_v.get('lit')) and j['result']['mode'] == 'low-gray',
                'camera': {k: cam_v.get(k) for k in ('lit', 'color_seen')}, 'latency': j['result']['latency']}

    def pans():
        im = Image.new('RGB', (128, 32), (0, 0, 60)); d = ImageDraw.Draw(im)
        d.text((4, 2), f'PAN {sha}', fill=(255, 200, 0), font=render.load_font('bold', 22))
        res = {}
        for direction in ('up', 'left'):
            prog = pan.build(im, direction)
            sign.upload(prog)
            time.sleep(1.0)
            p, v = photo(f'pan-{direction}')
            res[direction] = {'lit': v['lit'], 'bytes': len(prog), 'photo': p}
        return {'ok': all(r['lit'] for r in res.values()), **res,
                'note': 'motion is not judged from stills; watch the panel or film it'}

    def power():
        sign.power(False); time.sleep(1.5)
        p_off, v_off = photo('off')
        sign.power(True); time.sleep(1.5)
        p_on, v_on = photo('on')
        return {'ok': not v_off['lit'] and v_on['lit'], 'off_lit': v_off['lit'], 'on_lit': v_on['lit'],
                'note': 'on must show the program, not the white ID screen'}

    def restore():
        if 'img' in state:
            sign.upload(state['img'])
            time.sleep(1.0)
        if state.get('was_on') is False and not a.keep_on:
            time.sleep(direct.UPLOAD_SETTLE_S)
            sign.power(False)
        p, v = photo('after')
        want = state.get('was_on', True) or a.keep_on
        return {'ok': v['lit'] == want, 'lit': v['lit'], 'photo': p}

    try:
        if step('backup', backup):
            step('text', text)
            step('picture', picture_job)
            step('pans', pans)
            step('power', power)
    finally:
        step('restore', restore)                 # always: put the sign back
        sign.close(); srv.shutdown()
    report['ok'] = all(s['ok'] for s in report['steps'])
    (out / 'hil-report.json').write_text(json.dumps(report, indent=2, default=str), encoding='utf-8')
    print('HIL', 'PASSED' if report['ok'] else 'FAILED', '->', out / 'hil-report.json')
    sys.exit(0 if report['ok'] else 1)


if __name__ == '__main__':
    main()
