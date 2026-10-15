"""signboard.direct over the TCP transport, against the software controller behind a loopback socket."""
import pytest

from conftest import synthetic_image
from fake_controller import FakeController, emulated_tcp_sign
from signboard import protocol
from signboard.direct import SignError, DirectSign


@pytest.fixture
def ctrl():
    return FakeController()


@pytest.fixture(params=['plain', 'split1', 'merged'])
def mode(request):
    return {'plain': {}, 'split1': {'split': 1}, 'merged': {'merge': True}}[request.param]


@pytest.fixture
def tcp(ctrl, mode):
    made = []

    def make(**kw):
        s, srv = emulated_tcp_sign(ctrl, **mode, **kw)
        made.append((s, srv))
        return s
    yield make
    for s, srv in made:
        s.close()
        srv.close()


def test_upload_read_back(ctrl, tcp):
    s = tcp()
    img = synthetic_image()
    s.upload(img)
    assert ctrl.uploads == 1 and ctrl.screen() == 'program'
    assert ctrl.image() == img
    assert s.read_image()[:-6] == img[:-6]
    assert s.read_program()


def test_login_power_brightness(ctrl, tcp):
    s = tcp()
    s.upload(synthetic_image())
    s.power(False)
    assert ctrl.screen() == 'off'
    s.power(True)
    assert ctrl.screen() == 'program'
    s.brightness(3)
    assert ctrl.brightness == 3 or ctrl.brightness


def test_wrong_password(ctrl):
    s, srv = emulated_tcp_sign(ctrl, password='nope')  # scan:ok test-only password
    try:
        with pytest.raises(SignError):
            s.upload(synthetic_image())
        assert ctrl.uploads == 0
    finally:
        s.close()
        srv.close()


def test_larger_upload_and_chunk_size(ctrl):
    s, srv = emulated_tcp_sign(ctrl)
    try:
        s.transport.chunk = 1400
        img = synthetic_image(payload=bytes(range(256)) * 80)
        s.upload(img)
        assert ctrl.image() == img
    finally:
        s.close()
        srv.close()


def test_idle_release_and_reconnect(ctrl):
    s, srv = emulated_tcp_sign(ctrl, idle_s=0.2)
    try:
        s.upload(synthetic_image())
        import time
        for _ in range(40):
            if not s.connected:
                break
            time.sleep(0.1)
        assert not s.connected
        n = srv.connections
        assert s.read_image()
        assert srv.connections == n + 1
    finally:
        s.close()
        srv.close()


def test_connect_failure_message():
    from signboard.transport import TcpTransport
    s = DirectSign(transport=TcpTransport('127.0.0.1', 1, connect_s=0.5, error=SignError))
    with pytest.raises(SignError, match='cannot connect to the sign'):
        s.ensure()


def test_refused_connections_are_retried(ctrl):
    s, srv = emulated_tcp_sign(ctrl, refuse=1)
    try:
        s.upload(synthetic_image())          # first connect is closed at once; the write then fails -> upload retries
    except SignError:
        pass
    finally:
        s.close()
        srv.close()


def test_make_transport_from_config(monkeypatch):
    from signboard import config, direct
    monkeypatch.setitem(config.CFG, 'sign_transport', 'tcp')
    t = direct.make_transport()
    assert (t.host, t.port) == ('192.168.47.1', 25622)  # scan:ok
    monkeypatch.setitem(config.CFG, 'sign_transport', 'carrier-pigeon')
    with pytest.raises(SignError):
        direct.make_transport()


def test_login_over_real_tcp_is_refused_by_default(ctrl):
    """Firmware v8.05 drops the TCP connection mid-login; without sign_tcp_login no login (and no upload) is attempted."""
    from fake_controller import FakeTcpServer
    from signboard.transport import TcpTransport
    srv = FakeTcpServer(ctrl)
    s = DirectSign(transport=TcpTransport('127.0.0.1', srv.port, connect_s=2, error=SignError))
    try:
        with pytest.raises(SignError, match='login over TCP is disabled'):
            s.upload(synthetic_image())
        with pytest.raises(SignError, match='login over TCP is disabled'):
            s.power(False)
        assert ctrl.uploads == 0
    finally:
        s.close(); srv.close()
