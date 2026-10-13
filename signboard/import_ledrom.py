"""Import an RHX Plus "USB drive" export (C:\LedRom) as a picture-mode template.

The vendor templates cannot be redistributed, so each user makes their own (docs/templates.md):
in RHX Plus set Setting -> WorkMode, add a picture, set the screen to the panel size, press "USB drive", then
  python -m signboard.import_ledrom C:\LedRom    # -> <state_dir>/templates/<mode>.bin (or raw-<mode>.bin, text.bin)
The file is the program image with a 5-byte prefix (00 xx xx 00 ff) and a trailer (timestamp + sums).
"""
import struct
import sys
from pathlib import Path
from signboard import config, picture

MODE_BY_GREY = {0: 'seven-lengthen', 1: 'low-gray', 3: 'middle-gray', 5: 'high-gray', 7: 'true-color'}


def convert(ledrom: bytes) -> bytes:
    img = bytes(3) + ledrom[4:]                              # -> 00 00 00 ff ... (image offsets)
    size = struct.unpack_from('>I', img, 0x11)[0]
    body = img[:size - 2]
    if body[:4] != b'\x00\x00\x00\xff' or body[-1:] != b'\x04':
        raise ValueError('not an RHX Plus program export')
    if sum(body[0x2f:0x72]) & 0xff != body[0x72]:
        raise ValueError('config checksum mismatch')
    return body + bytes(6)


def main():
    import argparse
    ap = argparse.ArgumentParser(description='Import an RHX Plus "USB drive" export (one file, e.g. C:\LedRom) as a template')
    ap.add_argument('export', help='the exported file (RHX Plus writes it to C:\LedRom on Windows)')
    ap.add_argument('--out-dir', help='where to save (default: templates_dir from config, else <state_dir>/templates)')
    a = ap.parse_args()
    img = convert(Path(a.export).read_bytes())
    g, B = img[0x37], picture.BLK
    if img[7] == 0x75:                                       # seven-colour text program: the base for text renders
        mode, raw = 'text', False
    else:
        mode = MODE_BY_GREY.get(g, f'grey{g}')
        raw = img[B + 9] == 0 and struct.unpack_from('>I', img, B + 10)[0] == 0xcd
    d = Path(a.out_dir or config.get('templates_dir') or config.state_dir() / 'templates').expanduser()
    d.mkdir(parents=True, exist_ok=True)
    out = d / (('raw-' if raw else '') + mode + '.bin')
    out.write_bytes(img)
    print(f'{out}: {mode}{" (raw layout, for left/right pans)" if raw else ""}, {len(img)} B')


if __name__ == '__main__':
    main()
