"""RHX8-Q1C program image (the `dc02` upload) — container parser. Pure functions, no I/O.

Layout as stored in the sign (sign address = upload offset + 4; the wire frame carries 4 prefix bytes 00 00 00 ff):

  0x00  00 00 00 ff            prefix (part of the wire payload)
  0x04  u32 BE 0x75            version/kind (constant in all captures)
  0x08  u16 BE width, u16 BE height
  0x0c  u8 00
  0x0d  u32 BE  -> history section: u32 BE len, then "ProPhoneMsg:..." records (appended on every send)
  0x11  u32 BE  total length (through the trailer)
  0x15  03 03 fc fc 00 00 00 2d 00 00 00 0c
  0x21  u32 BE  -> project section: u16 0001, u32 BE json_len, then "ProPhoneMsg-R)H(X-..." + JSON
  0x25  9 zero bytes
  0x2e  72 B    controller config block (same bytes the `8204` command returns)
  0x76  ...     render area: pointer/row tables (LE u32, 0x1ccb = empty-row sentinel), RLE bitmaps (0xa5 escapes)
  json  the app's project JSON (what Readback returns), then 4 bytes (purpose unknown)
  hist  send-history log, appended to on every send
  ...   more tables/bitmaps
  end   u32 BE sum(all preceding bytes) , u16 BE (sum >> 16)

Rendering the bitmaps (text + background frames) is NOT implemented; `split`/`join` round-trip captured
images byte-exact so the known parts can be edited and the trailer regenerated.
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
