"""signboard.direct against the software controller (tests/fake_controller.py): login, uploads, retries, power."""
import pytest

from conftest import synthetic_image
from fake_controller import FakeController, emulated_sign
from signboard.direct import SignError


@pytest.fixture
def ctrl():
    return FakeController()


def test_upload_then_read_back(ctrl):
    s = emulated_sign(ctrl)
    img = synthetic_image()
    r = s.upload(img)
    assert r['bytes'] == len(img) - 6 - 4 + 6
    assert ctrl.uploads == 1 and ctrl.screen() == 'program'
    assert s.read_image()[:-6] == img[:-6]                   # memory holds body + S1; the trailer differs by design
    assert ctrl.image() == img


def test_unacked_fast_path(ctrl):
    s = emulated_sign(ctrl, fast=True)
    img = synthetic_image(payload=bytes(range(256)) * 40)    # ~10 KB, many packets
    s.upload(img)
    assert ctrl.image() == img


def test_wrong_password_is_refused(ctrl):
    s = emulated_sign(ctrl, password='not-it')  # scan:ok test-only password
    with pytest.raises(SignError):
        s.upload(synthetic_image())
    assert ctrl.uploads == 0


def test_power_cycle_keeps_the_program(ctrl):
    """Regression: a size-100 login (78e1) erased the program, and power-on then showed the white ID screen."""
    s = emulated_sign(ctrl)
    s.upload(synthetic_image())
    s.power(False)
    assert ctrl.screen() == 'off'
    s.power(True)
    assert ctrl.screen() == 'program'


def test_brightness(ctrl):
    s = emulated_sign(ctrl)
    s.brightness(1)
    assert ctrl.brightness == 1
    with pytest.raises(ValueError):
        s.brightness(17)


def test_upload_after_power_login(ctrl):
    """A power login leaves an upload pending on its link; the next upload must still get through."""
    s = emulated_sign(ctrl)
    s.power(True)
    img = synthetic_image()
    s.upload(img)
    assert ctrl.image() == img


def test_link_drop_falls_back_and_renegotiates(ctrl):
    """Regression (#3): after a dropped fast attempt the acked retry runs on a new link, which needs ae01 again."""
    s = emulated_sign(ctrl, fast=True)
    img = synthetic_image(payload=bytes(range(256)) * 20)
    ctrl.drop_after = 3
    s.upload(img)
    assert ctrl.image() == img
    assert sum(1 for d, b in ctrl.log if d == '>' and b[6:8] == b'\xae\x01') >= 2


def test_failed_upload_restores_the_last_good_program(ctrl):
    s = emulated_sign(ctrl)
    good = synthetic_image(json_text=b'{"good": 1}')
    s.upload(good)
    bad = synthetic_image(json_text=b'{"new": 2}')
    ctrl.corrupt_if = lambda stream: b'"new"' in stream      # every attempt at the new image arrives damaged
    with pytest.raises(SignError):
        s.upload(bad)
    assert ctrl.image() == good and ctrl.screen() == 'program'


def test_read_image_refuses_an_erased_sign(ctrl):
    s = emulated_sign(ctrl)
    with pytest.raises(SignError):
        s.read_image()
