"""signd's HTTP API on a local port, writing to the software controller. No radio, no camera."""
import http.client
import io
import json
import threading
import time
from http.server import ThreadingHTTPServer

import pytest
from PIL import Image

from conftest import installed_modes
from fake_controller import FakeController, emulated_sign
from signboard import picture, signd


@pytest.fixture(scope='module')
def server():
    ctrl = FakeController()
    sign = emulated_sign(ctrl)
    jobs = signd.Jobs(verbose=False)
    jobs.direct = signd.DirectBackend(sign)
    jobs.direct.look = lambda out, color, t0=None: dict(out, camera={'skipped': 'test'})   # no phone in tests
    srv = ThreadingHTTPServer(('127.0.0.1', 0), signd.make_handler(jobs, signd.ReadCache(sign), signd.Schedule(jobs)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], ctrl, jobs
    srv.shutdown()


def call(port, method, path, body=None, headers=None, host=None):
    c = http.client.HTTPConnection('127.0.0.1', port, timeout=20)
    h = {'Host': host or f'localhost:{port}'}
    h.update(headers or {})
    if isinstance(body, (dict, list, int, str)) and not isinstance(body, bytes):
        body = json.dumps(body).encode(); h.setdefault('Content-Type', 'application/json')
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    data = r.read()
    try:
        return r.status, json.loads(data)
    except ValueError:
        return r.status, data


def wait_job(port, jid, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        _, j = call(port, 'GET', f'/api/jobs/{jid}')
        if j['state'] in ('done', 'failed'):
            return j
        time.sleep(0.1)
    raise AssertionError(f'job {jid} still {j["state"]}')


# ---- request guard ----------------------------------------------------------------------------------------------

def test_unknown_host_name_refused(server):
    port, *_ = server
    assert call(port, 'GET', '/api/jobs', host='evil.example')[0] == 421


def test_ip_literal_hosts_pass(server):
    """#7: an IP literal cannot be a DNS-rebinding name, and the LAN address may change after startup."""
    port, *_ = server
    assert call(port, 'GET', '/api/jobs', host='192.168.1.77')[0] == 200  # scan:ok example LAN address
    assert call(port, 'GET', '/api/jobs', host=f'[::1]:{port}')[0] == 200


def test_cross_origin_refused_same_origin_allowed(server):
    port, *_ = server
    assert call(port, 'POST', '/api/brightness', {'level': 1}, {'Origin': 'http://evil.example'})[0] == 403
    assert call(port, 'POST', '/api/brightness', {'level': 99}, {'Origin': 'http://localhost:3000'})[0] == 403   # #6
    assert call(port, 'POST', '/api/brightness', {'level': 99}, {'Origin': f'http://localhost:{port}'})[0] == 400


def test_body_cap(server):
    port, *_ = server
    c = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    c.putrequest('POST', '/api/text'); c.putheader('Host', f'localhost:{port}')
    c.putheader('Content-Length', str(signd.MAX_BODY + 1)); c.endheaders()
    assert c.getresponse().status == 400


# ---- validation -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('body', [
    {'text': 'x' * (signd.MAX_TEXT + 1)},
    {'text': 'hi', 'color': 5},                 # #5: non-strings must be a 400, not a dropped connection
    {'text': 'hi', 'color': 'chartreuse'},
    {'text': 'hi', 'outline': 'chartreuse'},
    {'text': 'hi', 'font': [1]},
    {'text': 'hi', 'font': 'C:/Windows/Fonts/x.ttf'},
    {'text': 'hi', 'size': True},
    {'text': 'hi', 'size': 400},
    {'text': 'hi', 'background': '../../etc/passwd'},
    {'text': ['hi']},
])
def test_text_validation(server, body):
    port, *_ = server
    status, r = call(port, 'POST', '/api/text', body)
    assert status == 400, r


@pytest.mark.parametrize('body', [[1, 2], 'text', 5])
def test_non_object_json(server, body):
    port, *_ = server
    assert call(port, 'POST', '/api/text', body)[0] == 400


def test_bad_json(server):
    port, *_ = server
    assert call(port, 'POST', '/api/text', b'{nope', {'Content-Type': 'application/json'})[0] == 400


def test_sign_endpoint_validates(server):
    """#4: /api/sign used to queue text jobs without the text checks."""
    port, *_ = server
    assert call(port, 'POST', '/api/sign', {'text': 'x' * (signd.MAX_TEXT + 1)})[0] == 400
    assert call(port, 'POST', '/api/sign', {'text': 'hi', 'color': 7})[0] == 400
    assert call(port, 'POST', '/api/sign', {'power': 'yes'})[0] == 400


@pytest.mark.parametrize('path,body', [('/api/power', {'on': 'true'}), ('/api/power', {}),
                                       ('/api/brightness', {'level': 0}), ('/api/brightness', {'level': 17}),
                                       ('/api/brightness', {'level': 'max'})])
def test_power_brightness_validation(server, path, body):
    port, *_ = server
    assert call(port, 'POST', path, body)[0] == 400


def test_image_upload(server):
    port, *_ = server
    assert call(port, 'POST', '/api/image', b'not an image')[0] == 400
    buf = io.BytesIO(); Image.new('RGB', (8, 8), 'red').save(buf, 'PNG')
    status, r = call(port, 'POST', '/api/image', buf.getvalue())
    assert status == 200 and r['stored'].startswith('bg-') and r['stored'].endswith('.png')


def test_removed_routes(server):
    port, *_ = server
    assert call(port, 'GET', '/files/anything.jpg')[0] == 404       # #10: served the photo folder to the LAN
    assert call(port, 'GET', '/classic')[0] == 404


def test_index_uses_sign_name(server):
    port, *_ = server
    status, body = call(port, 'GET', '/')
    assert status == 200 and b'Test Sign' in body and b'{{SIGN_NAME' not in body


def test_health(server):
    port, *_ = server
    status, r = call(port, 'GET', '/api/health')
    assert status == 200 and r['writer'] == 'direct' and r['name'] == 'Test Sign'


# ---- jobs end to end (need your templates) ------------------------------------------------------------------

def test_power_job(server):
    port, ctrl, _ = server
    status, j = call(port, 'POST', '/api/power', {'on': False, 'verify': False})
    assert status == 202 and wait_job(port, j['id'])['state'] == 'done'
    assert not ctrl.display_on
    call(port, 'POST', '/api/power', {'on': True, 'verify': False})


def test_text_job(server):
    if not any((d / 'text.bin').is_file() for d in picture.template_dirs()):
        pytest.skip('no text template')
    port, ctrl, _ = server
    signd.PROGRAMS.joinpath('current.bin').write_bytes(picture.template('text'))
    status, j = call(port, 'POST', '/api/text', {'text': 'Hello tests', 'color': 'green', 'background': 'none'})
    assert status == 202
    j = wait_job(port, j['id'])
    assert j['state'] == 'done', j.get('error')
    assert b'Hello tests' in ctrl.image() and ctrl.screen() == 'program'
    assert j['result']['latency']['upload_s'] < 10


@pytest.mark.skipif('low-gray' not in installed_modes(), reason='no low-gray template')
def test_picture_job(server):
    port, ctrl, _ = server
    buf = io.BytesIO(); Image.new('RGB', (128, 32), (200, 40, 40)).save(buf, 'PNG')
    status, j = call(port, 'POST', '/api/picture?mode=low-gray&still=1', buf.getvalue(), {'Content-Type': 'image/png'})
    assert status == 202
    j = wait_job(port, j['id'])
    assert j['state'] == 'done', j.get('error')
    assert j['result']['mode'] == 'low-gray' and ctrl.image()[7] == 0x73
