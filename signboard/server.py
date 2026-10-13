"""HTTP face for the sign, read-only for now.

  GET /status        bridge state (transport, whether the sign was reachable last time)
  GET /api/now       parsed view: caption text, colour, font size, background, panel size, last read time
  GET /api/program   raw project JSON from the sign
  POST /api/power    501: needs the controller login (see PROTOCOL.md); the vendor app is still the writer

The BLE link is opened per request and dropped after IDLE_S, so the vendor app on the phone can still
connect in between (the controller accepts one central at a time).

  python -m signboard.server --ble <sign address>                    # on a BLE-capable host
"""
import argparse
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from signboard import protocol

IDLE_S = 8          # drop the BLE link this long after the last request
CACHE_S = 3         # serve a cached read if it's this fresh (dashboards poll)
SEP = '-R)H(X-'     # field separator inside the app's "para" strings


class Bridge:
    def __init__(self, open_transport, name):
        self._open, self.name = open_transport, name
        self.lock = threading.Lock()
        self._tr = None
        self._last_use = 0.0
        self.last_ok = None          # epoch of last successful read
        self.last_error = None
        self._cache = None
        threading.Thread(target=self._reaper, daemon=True).start()

    def _transport(self):
        if self._tr is None or not getattr(self._tr, 'connected', True):
            self._tr = self._open()
        self._last_use = time.time()
        return self._tr

    def _reaper(self):
        while True:
            time.sleep(1)
            with self.lock:
                if self._tr is not None and time.time() - self._last_use > IDLE_S:
                    close = getattr(self._tr, 'close', None)
                    if close:
                        try:
                            close()
                        except Exception:
                            pass
                    self._tr = None

    def program(self):
        with self.lock:
            if self._cache and time.time() - self._cache[0] < CACHE_S:
                return self._cache[1]
            try:
                proj = protocol.read_program(self._transport().request)['project']
            except Exception as e:
                self.last_error = f'{type(e).__name__}: {e}'
                self._tr = None
                raise
            self.last_ok, self.last_error = time.time(), None
            self._cache = (time.time(), proj)
            return proj


def parse_now(project: dict) -> dict:
    """Pull the human-facing bits out of the app's project JSON."""
    screen = project.get('Screen', {})
    prog = (screen.get('Program') or [{}])[0]
    rect = (prog.get('Rect') or [{}])[0]
    info = (rect.get('Info') or [{}])[0]
    f = info.get('para', '').split(SEP)
    r = rect.get('para', '').split(SEP)
    spara = screen.get('para', '')
    m = re.match(r'(\d+),(\d+),(\d+),(\d+),(\d+),(\d+),(\d+),(\d+),(\d+)', spara)
    return {
        'text': f[1] if len(f) > 1 else None,
        'font_size': int(f[2]) if len(f) > 2 and f[2].isdigit() else None,
        'color': f[5] if len(f) > 5 else None,
        'font': f[6] if len(f) > 6 else None,
        'rect': r[1] if len(r) > 1 else None,
        'background': r[3] if len(r) > 3 else None,     # e.g. "gif-169-2" = custom gif slot
        'panel': {'width': int(m.group(7)), 'height': int(m.group(9))} if m else None,
        'scan': screen.get('ScanType'),
    }


def make_handler(bridge):
    class H(BaseHTTPRequestHandler):
        def _send(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False, indent=2).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == '/status':
                self._send(200, {'transport': bridge.name, 'last_ok': bridge.last_ok,
                                 'last_error': bridge.last_error, 'write': 'not implemented (controller login)'})
            elif self.path in ('/api/program', '/api/now'):
                try:
                    proj = bridge.program()
                except Exception as e:
                    return self._send(502, {'error': str(e)})
                if self.path == '/api/program':
                    self._send(200, proj)
                else:
                    out = parse_now(proj)
                    out['read_at'] = bridge.last_ok
                    self._send(200, out)
            else:
                self._send(404, {'error': 'not found'})

        def do_POST(self):
            if self.path in ('/api/power', '/api/upload', '/api/text'):
                self._send(501, {'error': 'writes need the controller login, which is not implemented; '
                                          'use the vendor app for now'})
            else:
                self._send(404, {'error': 'not found'})

        def log_message(self, *a):
            pass
    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ble', metavar='ADDR', required=True)
    ap.add_argument('--port', type=int, default=8080)
    ap.add_argument('--bind', default='0.0.0.0')
    a = ap.parse_args()
    from signboard.ble import BleakTransport
    bridge = Bridge(lambda: BleakTransport(a.ble, timeout=10), f'ble:{a.ble}')
    print(f'signboard bridge on {a.bind}:{a.port} via {bridge.name}', flush=True)
    ThreadingHTTPServer((a.bind, a.port), make_handler(bridge)).serve_forever()


if __name__ == '__main__':
    main()
