"""Text rendering, patterns, sequences and pans. Template-free parts always run; the rest needs --templates/--samples."""
import json
import os
import struct
import sys

import pytest
from PIL import Image

from conftest import installed_modes
from signboard import image, patterns, pan, picture, render, selftest


def test_font_paths_are_clean():
    """Regression (#1): '\\a' and '\\v' in non-raw strings turned two Windows paths into BEL/VT bytes."""
    for name, paths in render.FONTS.items():
        for p in paths:
            assert p.isprintable(), f'{name}: control character in {p!r}'


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows font paths')
def test_windows_fonts_resolve():
    for name in ('bold', 'verdana', 'impact'):
        assert any(os.path.isfile(p) for p in render.FONTS[name]), name
    assert type(render.load_font('bold', 22)).__name__ == 'FreeTypeFont'


def test_load_font_clamps_and_falls_back():
    assert render.load_font('no-such-font', 400) is not None
    assert render.load_font('bold', -5) is not None


def test_text_mask():
    m = render.text_mask('Hi', 'bold', 22)
    assert m.size[1] == 32 and m.getbbox()


def test_caption_json_is_escaped():
    js = b'{"para": "0-R)H(X-old-R)H(X-1-R)H(X-2-R)H(X-3-R)H(X-#ff0000"}'
    out = render._edit_json(js, 'say "hi" \\ there\nnext', 'green')
    para = json.loads(out)['para']                             # still valid JSON
    assert "say 'hi' / there" in para and '#00ff00' in para.lower()


@pytest.mark.parametrize('name', sorted(patterns.PATTERNS))
def test_patterns_tile_seamlessly(name):
    f, PX, PY = patterns.PATTERNS[name]
    a = patterns.frame(f, 3, 5, 32, 16)
    assert a.tobytes() == patterns.frame(f, 3 + PX, 5 + PY, 32, 16).tobytes()


def test_pan_effects():
    assert pan.EFFECT == {'up': 17, 'down': 18, 'left': 19, 'right': 20}


# ---- needs your own templates (--templates DIR) ---------------------------------------------------------------

def gradient(w=128, h=64):
    im = Image.new('RGB', (w, h))
    im.putdata([(x * 2 % 256, y * 4 % 256, 255 - x * 2 % 256) for y in range(h) for x in range(w)])
    return im


@pytest.mark.parametrize('mode', installed_modes() or [pytest.param(None, marks=pytest.mark.skip('no templates'))])
def test_picture_mode(mode):
    """Build a picture in each installed mode: config checksum, size limit, footer, decode round trip."""
    img = picture.build(gradient(), mode)
    assert image.parse(img)['len_ok']
    image.wire_stream(img)
    assert sum(img[0x2f:0x72]) & 0xff == img[0x72]
    assert len(img) - 6 <= picture.MAX_UPLOAD
    assert picture.mode_of(img) == mode and img[-7] == 4
    g, W = picture.info(mode)['grey'], picture.info(mode)['width']
    got, (want, _) = picture.decode(img), picture.fit(gradient(), W)
    mask = 0xff << (7 - g) & 0xff
    err = sum(abs((a & mask) - (b & mask)) for a, b in zip(got.tobytes(), want.tobytes()))
    assert err / (got.width * got.height * 3) <= (24 if g == 0 else 1)


def _need(mode):
    if mode not in installed_modes() and not any((d / f'{mode}.bin').is_file() for d in picture.template_dirs()):
        pytest.skip(f'no {mode} template')


@pytest.mark.parametrize('direction', ['up', 'down', 'left', 'right'])
def test_pan_programs(direction):
    _need('low-gray')
    if direction in ('left', 'right'):
        _need('raw-low-gray')
    img = pan.build(patterns.frame(patterns.PATTERNS['houndstooth'][0], 0, 0), direction)
    assert image.parse(img)['len_ok'] and img[picture.BLK] == pan.EFFECT[direction]
    image.wire_stream(img)                                     # builders leave the trailer to the uploader
    assert len(img) < 16 * 1024                                # the point of a pan: a few KB, not a frame stack


def test_sequence_program():
    _need('low-gray')
    from signboard import sequence
    frames = [patterns.frame(patterns.PATTERNS['checker-diag'][0], i, 0) for i in range(4)]
    img = sequence.build(frames)
    assert image.parse(img)['len_ok'] and img[picture.BLK + 4] == 4
    image.wire_stream(img)


def test_text_program():
    _need('text')
    base = picture.template('text')
    planes = render.compose([(render.text_mask('Test 123', 'bold', 22), 'green', 'red')])
    img = render.set_caption(base, planes, text='Test 123', color='green')
    p = image.parse(img)
    assert p['trailer_ok'] and p['len_ok'] and b'Test 123' in p['json']
    image.wire_stream(img)


def test_reference_samples(samples, monkeypatch):
    """Byte-exact against the vendor app's captured upload, join/caption identities, background round trip."""
    monkeypatch.setattr(selftest, 'HERE', samples)
    selftest.check_samples()


def test_sequence_picture_limit():
    """The picture block counts images in one byte: more than 255 used to fail with 'byte must be in range'."""
    from signboard import sequence
    with pytest.raises(ValueError, match='1..255 pictures'):
        sequence.build([Image.new('RGB', (128, 32))] * 256)


def test_photo_check_ignores_things_outside_the_panel(tmp_path, monkeypatch):
    """A lit room with a yellow reference ball above a dark panel read as 'lit, yellow' before camera_panel_box."""
    from PIL import ImageDraw
    from signboard import config, verify
    im = Image.new('RGB', (800, 600), (200, 190, 180))                 # warm wall
    d = ImageDraw.Draw(im)
    d.polygon([(100, 300), (700, 250), (710, 470), (110, 450)], fill=(10, 10, 10))   # dark panel, seen at an angle
    d.ellipse((370, 130, 450, 210), fill=(250, 210, 0))                # the ball, above the panel
    p = tmp_path / 'room.jpg'; im.save(p)
    monkeypatch.setitem(config.CFG, 'camera_panel_box', None)
    assert verify.judge_photo(p)['lit']                                 # fooled by the ball
    monkeypatch.setitem(config.CFG, 'camera_panel_box', [0.15, 0.51, 0.87, 0.43, 0.88, 0.77, 0.16, 0.74])
    assert not verify.judge_photo(p)['lit']
    d.rectangle((300, 330, 500, 400), fill=(0, 255, 60))                # now light some green LEDs on the panel
    im.save(p)
    r = verify.judge_photo(p, 'green')
    assert r['lit'] and r['color_ok']
