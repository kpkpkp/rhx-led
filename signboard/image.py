"""RHX program image (the `dc02` upload): container parser and wire stream. Pure functions, no I/O.

Sign memory address = image offset (the image starts with the 4 prefix bytes 00 00 00 ff). Big-endian throughout.

  0x00  00 00 00 ff            prefix
  0x04  u32 kind               0x75 = seven-colour text program, 0x73 = picture program
  0x08  u16 width, u16 height
  0x0d  u32 -> history section: u32 len, then the app's "ProPhoneMsg:..." send-history records
  0x11  u32 total length       = len(image) - 4 (an image ends with a 6-byte trailer)
  0x21  u32 -> project section: u16 0001, u32 json_len, project tag + JSON (the app's description, used on readback)
  0x2e  72 B controller config (byte 0x72 = 8-bit sum of 0x2f..0x71)
  0x76  render area / picture header, then row-pointer tables (u32 absolute addresses) and RLE rows
        (X a5 n = X plus n more copies), ending with a footer: u32 decoded size, u32 RLE length, 04

See docs/PROTOCOL.md. render.py, picture.py, sequence.py and pan.py build the contents; split/join here round-trip
captured images byte-exact.
"""
import struct

PREFIX = b'\x00\x00\x00\xff'
EMPTY_ROW = 0x1ccb


def trailer(body: bytes) -> bytes:
    s = sum(body) & 0xffffffff
    return struct.pack('>IH', s, s >> 16)


def check_trailer(img: bytes) -> bool:
    return img[-6:] == trailer(img[:-6])
def parse(img: bytes) -> dict:
    """img = full sign memory image (starts with PREFIX). Returns section offsets and decoded header fields."""
    if img[:4] != PREFIX:
        raise ValueError('missing 00 00 00 ff prefix')
    kind, w, h = struct.unpack_from('>IHH', img, 4)
    hist = struct.unpack_from('>I', img, 0x0d)[0]
    total = struct.unpack_from('>I', img, 0x11)[0]
    proj = struct.unpack_from('>I', img, 0x21)[0]
    ver, jlen = struct.unpack_from('>HI', img, proj)
    hlen = struct.unpack_from('>I', img, hist)[0]
    jstart = proj + 6
    jbytes = img[jstart:jstart + jlen]
    brace = jbytes.find(b'{')
    if brace == -1:
        raise ValueError('no JSON found in project section')
    out = {
        'kind': kind, 'width': w, 'height': h, 'total': total, 'len_ok': total == len(img) - 4,
        'config': img[0x2e:0x2e + 72],
        'project_off': proj, 'project_tag': jbytes[:brace], 'json': jbytes[brace:],
        'gap': img[jstart + jlen:hist],                 # bytes between project section and history (4 B in captures)
        'history_off': hist, 'history': img[hist + 4:hist + 4 + hlen],
        'render_a': img[0x76:proj],                      # between config and project section
        'render_b': img[hist + 4 + hlen:-6],
        'trailer_ok': check_trailer(img),
    }
    return out


split = parse


def wire_stream(img: bytes, chunk: int = 500):
    """The dc02 upload for an image -> (ATT write values, size). Byte-exact against the app's captured upload.

    The wire carries body, S1 = sum32(body), S2 = sum32(body + S1's bytes), then 00 00 04, where body is the image up
    to its trailer (00 00 00 ff prefix included). The sign stores body + S1 only; the header's total-length field and
    the 78e1/dc02 size field count len(body) - 4 + 6, i.e. through the high half of S2 (which is what join() writes
    as the u16 after S1). Trailing bytes of img past body (join's u16, or ff ff from a memory readback) are ignored."""
    body = img[:-6]
    if body[:4] != PREFIX:
        raise ValueError('image must start with 00 00 00 ff')
    if struct.unpack_from('>I', img, 0x11)[0] != len(img) - 4:
        raise ValueError('header length does not match the image (is the 6-byte trailer missing?)')
    s1 = sum(body) & 0xffffffff
    s1b = struct.pack('>I', s1)
    s2 = (s1 + sum(s1b)) & 0xffffffff
    size = len(body) - 4 + 6
    if size > 0xffffffff:
        raise ValueError(f'image too large ({size} B)')
    stream = (bytes.fromhex('aa55aa550000dc0200000000') + struct.pack('>I', size)
              + body + s1b + struct.pack('>I', s2) + b'\x00\x00\x04')
    return [stream[i:i + chunk] for i in range(0, len(stream), chunk)], size


def join(p: dict) -> bytes:
    """Rebuild an image from parsed parts (inverse of parse); recomputes total length and trailer."""
    proj_section = struct.pack('>HI', 1, len(p['project_tag']) + len(p['json'])) + p['project_tag'] + p['json']
    head = bytearray(PREFIX + struct.pack('>IHH', p['kind'], p['width'], p['height']) + b'\x00')
    head += b'\x00' * 4                                   # history ptr, patched below
    head += b'\x00' * 4                                   # total, patched below
    head += bytes.fromhex('0303fcfc0000002d0000000c')
    head += b'\x00' * 4                                   # project ptr, patched below
    head += b'\x00' * 9 + p['config']
    assert len(head) == 0x76
    body = bytearray(head) + p['render_a']
    proj_off = len(body); body += proj_section + p['gap']
    hist_off = len(body); body += struct.pack('>I', len(p['history'])) + p['history'] + p['render_b']
    struct.pack_into('>I', body, 0x0d, hist_off)
    struct.pack_into('>I', body, 0x21, proj_off)
    struct.pack_into('>I', body, 0x11, len(body) + 6 - 4)
    return bytes(body) + trailer(bytes(body))
