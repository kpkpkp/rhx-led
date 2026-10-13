"""Test setup: an isolated signboard config (never ~/.signboard.toml), template/sample gating, the hardware marker.

  pytest                         offline tests: no radio, no phone; template tests skip unless templates are given
  pytest --templates DIR         also build every installed work mode from your own templates (docs/templates.md)
  pytest --samples DIR           also check the reference captures (template.bin, tire.bin, captured_chunks.bin)

Panel tests are not pytest tests: see tools/hil.py.
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

# signboard.config reads its file once at import, so point it at a throwaway config before anything imports it.
_TMP = Path(tempfile.mkdtemp(prefix='signboard-test-'))
for k in [k for k in os.environ if k.startswith('SIGNBOARD_')]:
    del os.environ[k]


def _templates_arg():
    for i, a in enumerate(sys.argv):
        if a == '--templates' and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if a.startswith('--templates='):
            return a.split('=', 1)[1]
    return os.environ.get('RHX_TEST_TEMPLATES')


_tpl = _templates_arg()
(_TMP / 'test.toml').write_text(
    'sign_address = "EMU"\nsign_password = "testpass1"\nsign_name = "Test Sign"\n'  # scan:ok test-only password
    f'state_dir = "{_TMP.as_posix()}"\n' + (f'templates_dir = "{Path(_tpl).as_posix()}"\n' if _tpl else ''),
    encoding='utf-8')
os.environ['SIGNBOARD_CONFIG'] = str(_TMP / 'test.toml')
sys.path.insert(0, str(Path(__file__).resolve().parent))


def pytest_addoption(parser):
    parser.addoption('--templates', default=None, help='directory of your RHX Plus templates (<mode>.bin)')
    parser.addoption('--samples', default=os.environ.get('RHX_TEST_SAMPLES'),
                     help='directory with template.bin, tire.bin, captured_chunks.bin')


@pytest.fixture
def samples(request):
    d = request.config.getoption('--samples')
    need = ('template.bin', 'tire.bin', 'captured_chunks.bin')
    if not d or not all((Path(d) / f).is_file() for f in need):
        pytest.skip('reference samples not given (--samples DIR)')
    return Path(d)


def installed_modes():
    from signboard import picture
    return picture.installed()


@pytest.fixture(autouse=True)
def no_settle(monkeypatch):
    """The controller needs seconds between an upload and the next login; the emulator does not."""
    from signboard import direct
    monkeypatch.setattr(direct, 'UPLOAD_SETTLE_S', 0.0)


def synthetic_image(kind=0x75, payload=b'', json_text=b'{"test": true}'):
    """A structurally valid program image built with image.join (not displayable: no real render area)."""
    from signboard import image
    cfg = bytearray(72)
    cfg[0x72 - 0x2e] = sum(cfg[1:0x72 - 0x2e]) & 0xff
    return image.join({'kind': kind, 'width': 128, 'height': 32, 'config': bytes(cfg),
                       'render_a': payload or bytes(range(256)) * 4, 'project_tag': b'', 'json': json_text,
                       'gap': b'\x00' * 4, 'history': b'', 'render_b': b'\x12\x34' * 50})
