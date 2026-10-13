"""Offline checks for the program-image code. No BLE, no phone.

Always runs the template-free checks. Checks against reference samples (template.bin, tire.bin, captured_chunks.bin)
and picture templates (templates/*.bin) run only when those private files are present; they are not distributed.

  python -m signboard.selftest
"""
import pickletools
import struct
from pathlib import Path

from signboard import content, image, render

HERE = Path(__file__).resolve().parent


def decode_rows(img):
    """All background rows of an image, decoded (frames x 3 planes x 32 rows of ceil(BW/8) bytes)."""
    ra = image.parse(img)['render_a']
    t, F, BW = (struct.unpack_from('>H', ra, o)[0] for o in (0x49, 0x3b, 0x3d))
    n = (BW + 7) // 8
    rows = []
    for k in range(F * 96):
        i = struct.unpack_from('>I', img, t + 4 * k)[0]; o = bytearray()
        if i == t - 11:                              # the shared empty row (00 a5 ff ...) is longer than any width
            rows.append(bytes(n)); continue
        while len(o) < n:
            if img[i] == 0xa5: o += bytes([o[-1]]) * img[i + 1]; i += 2
            else: o.append(img[i]); i += 1
        assert len(o) == n, 'row overruns its width'
        rows.append(bytes(o))
    return F, BW, rows


def check_samples():
    tpl = b'\x00\x00\x00\xff' + (HERE / 'template.bin').read_bytes()
    tire = b'\x00\x00\x00\xff' + (HERE / 'tire.bin').read_bytes()
    caps = [a for o, a, _ in pickletools.genops((HERE / 'captured_chunks.bin').read_bytes())
            if o.name in ('SHORT_BINBYTES', 'BINBYTES', 'BINBYTES8')]
    assert image.wire_stream(tpl)[0] == caps, 'wire stream differs from the app capture'
    assert image.join(image.parse(tpl))[:-6] == tpl[:-6], 'join(parse()) not byte-exact'
    pl = render.unpack_planes(image.parse(tpl)['render_a'])
    assert render.set_caption(tpl, pl)[:-6] == tpl[:-6], 'set_caption identity failed'
    for img in (tpl, tire):
        F, BW, rows = decode_rows(img)
        tail = img[-6 - 9:-6]
        assert struct.unpack('>I', tail[:4])[0] == F * 96 * ((BW + 7) // 8) and tail[-1] == 4, 'footer'
    planes = render.compose([(render.text_mask('Self test, a longer caption', 'bold', 22), 'white', 'red')])
    new = render.set_background(render.set_caption(tpl, planes, text='Self test', color='white'), content.sunshine())
    F, BW, rows = decode_rows(new)
    assert (F, BW) == (16, 128)
    want = []
    for fr in content.sunshine():
        for p in render.from_image(fr, 128):
            want.append(render.pack_planes([p])[1])      # one plane: 32 rows x 16 bytes
    got = [b''.join(rows[k * 32:(k + 1) * 32]) for k in range(F * 3)]
    assert [bytes(render._no_a5(bytearray(w[i:i + 16]))) for w in want for i in range(0, 512, 16)] == \
           [g[i:i + 16] for g in got for i in range(0, 512, 16)], 'background did not round-trip'
    assert image.check_trailer(new) and image.parse(new)['json'].find(b'Self test') > 0


def check_free():
    """Needs no sample or template: frame builders, text rendering, pattern periodicity."""
    from signboard import protocol, patterns, pan
    assert protocol.power(True).hex() == 'aa55aa5500000804' and protocol.power(False).hex() == 'aa55aa5500000904'
    assert protocol.open_session().hex() == 'aa55aa5500000e0104'
    m = render.text_mask('Hi', 'bold', 22); assert m.size[1] == 32 and m.getbbox()
    for name, (f, PX, PY) in patterns.PATTERNS.items():
        a = patterns.frame(f, 3, 5, 32, 16)
        assert a.tobytes() == patterns.frame(f, 3 + PX, 5 + PY, 32, 16).tobytes(), f'{name} not periodic'
    assert set(pan.EFFECT.values()) == {17, 18, 19, 20}


def main():
    check_free(); done = ['frames, text, patterns']
    if all((HERE / f).exists() for f in ('template.bin', 'tire.bin', 'captured_chunks.bin')):
        check_samples(); done.append('reference samples')
    done += check_pictures()
    print('selftest ok:', ', '.join(done))


def check_pictures():
    """Every work mode: build a picture program from a synthetic gradient, check the config checksum, the size limit,
    the footer, and that decoding the picture gives back roughly what went in."""
    from PIL import Image
    from signboard import picture
    src = Image.new('RGB', (128, 64))
    src.putdata([(x * 2, y * 4, 255 - x * 2) for y in range(64) for x in range(128)])
    done = []
    for mode in picture.MODES:
        try:
            picture.template(mode)
        except FileNotFoundError:
            continue
        done.append(mode)
        g, W = picture.info(mode)['grey'], picture.info(mode)['width']
        img = picture.build(src, mode)
        assert sum(img[0x2f:0x72]) & 0xff == img[0x72], f'{mode}: config checksum'
        assert len(img) - 6 <= picture.MAX_UPLOAD, f'{mode}: over the upload limit'
        assert picture.mode_of(img) == mode and img[-7] == 4, f'{mode}: mode byte or footer'
        rows = 3 * (g + 1) * (32 + struct.unpack_from('>H', img, picture.BLK + 7)[0])
        assert struct.unpack_from('>I', img, len(img) - 15)[0] == rows * ((W + 7) // 8), f'{mode}: footer size'
        got = picture.decode(img)
        want, _ = picture.fit(src, W)
        mask = 0xff << (7 - g) & 0xff
        err = sum(abs((a & mask) - (b & mask)) for a, b in zip(got.tobytes(), want.tobytes()))
        tol = 24 if g == 0 else 1                  # seven-colour dithers; the grey modes keep the top g+1 bits exactly
        assert err / (got.width * got.height * 3) <= tol, f'{mode}: picture did not round-trip ({err})'
    return [f'{m} template' for m in done]


if __name__ == '__main__':
    main()
