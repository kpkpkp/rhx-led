"""RHX8-Q1C BLE framing, read path. See ../PROTOCOL.md. Pure functions, no I/O.

Host -> sign:  aa 55 aa 55 00 00 <cmd> <body...> [04]
Memory read:   cmd ac, body 01 <addr:4 BE> <len:4 BE>, tail 04
Sign -> host:  a5 00 5a <addr:4> <len:4> <data> <sum32 of data> , then ack a5 00 a5 00 ac 01 88 00
"""
import json
import struct

MAGIC = bytes.fromhex('aa55aa55')
DATA_MAGIC = bytes.fromhex('a5005a')
ACK_LEN = 8
CHUNK = 0x106                       # largest read the app issues
PROGRAM_PTR_ADDR = 0x21             # u32 BE pointer to the program blob


def frame(cmd: int, body: bytes = b'', tail04: bool = True) -> bytes:
    return MAGIC + b'\x00\x00' + bytes([cmd]) + body + (b'\x04' if tail04 else b'')


def open_session() -> bytes:
    return frame(0x0e, b'\x01')


def close_session() -> bytes:
    return frame(0x0e, b'\x02')


def power(on: bool) -> bytes:
    """Display power. Captured from the app inside a logged-in session. Sent without login the sign
    replies 0xaa and ignores it (tested 2026-10-09), so this frame alone does not work yet."""
    return frame(0x08 if on else 0x09)


def read_request(addr: int, length: int) -> bytes:
    return frame(0xac, b'\x01' + struct.pack('>II', addr, length))


def read_reply_size(length: int) -> int:
    """Bytes the sign sends back for a read of `length`: data frame + ack."""
    return 11 + length + 4 + ACK_LEN


def parse_read_reply(buf: bytes, addr: int, length: int) -> bytes:
    if buf[:3] != DATA_MAGIC:
        raise ValueError(f'bad reply magic {buf[:3].hex()}')
    a, n = struct.unpack_from('>II', buf, 3)
    if (a, n) != (addr, length):
        raise ValueError(f'reply for {a:#x}+{n}, wanted {addr:#x}+{length}')
    data = buf[11:11 + n]
    (want,) = struct.unpack_from('>I', buf, 11 + n)
    if len(data) != n or sum(data) & 0xffffffff != want:
        raise ValueError('checksum mismatch')
    return data


def read_memory(request, addr: int, length: int) -> bytes:
    """request(frame, need_bytes) -> reply bytes. Chunks long reads."""
    out = b''
    for off in range(0, length, CHUNK):
        n = min(CHUNK, length - off)
        reply = request(read_request(addr + off, n), read_reply_size(n))
        out += parse_read_reply(reply, addr + off, n)
    return out


def read_program(request) -> dict:
    """Read the project JSON out of the sign. Returns {'raw': bytes, 'project': dict}."""
    request(open_session(), ACK_LEN)
    try:
        ptr = struct.unpack('>I', read_memory(request, PROGRAM_PTR_ADDR, 4))[0]
        n = struct.unpack('>I', read_memory(request, ptr, 6)[2:6])[0]
        raw = read_memory(request, ptr, 6 + n + 6)       # app over-reads the tail slightly
    finally:
        request(close_session(), ACK_LEN)
    start = raw.index(b'{')
    project, _ = json.JSONDecoder().raw_decode(raw[start:].decode('utf-8', 'replace'))
    return {'raw': raw, 'project': project}


def upload_request(size: int) -> bytes:
    """78e1 begin upload / challenge request: aa 55 aa 55 00 00 78 e1 00 00 00 00 00 00 <size:2 BE> 04"""
    return frame(0x78, b'\xe1\x00\x00\x00\x00\x00\x00' + struct.pack('>H', size & 0xffff))


def auth_response_frame(resp_16b: bytes) -> bytes:
    """89a1 challenge answer: aa 55 aa 55 00 00 89 a1 10 <16B> 04"""
    return frame(0x89, b'\xa1\x10' + resp_16b)


def login(request, password: str = None, upload_size: int = 100) -> bool:
    """
    password: the controller password (default: signboard.config "sign_password").
    Performs full 2-step challenge-response authentication against the RHX8-Q1C controller.
    Unlocks write, upload, and power-control commands.
    """
    if password is None:
        from signboard import config
        password = config.require('sign_password')
    from signboard import crypto

    # Step 1: send 78e1, sign replies with 59a0 + 8-byte challenge
    reply1 = request(upload_request(upload_size), 18)
    if b'\x59\xa0\x08' not in reply1:
        return False
    idx1 = reply1.index(b'\x59\xa0\x08') + 3
    chal1 = reply1[idx1:idx1 + 8]
    ans1 = crypto.solve_challenge(chal1, step=1, password=password)

    # Step 2: send 89a1, sign replies with 59a3 + 8-byte challenge
    reply2 = request(auth_response_frame(ans1), 18)
    if b'\x59\xa3\x08' not in reply2:
        return False
    idx2 = reply2.index(b'\x59\xa3\x08') + 3
    chal2 = reply2[idx2:idx2 + 8]
    ans2 = crypto.solve_challenge(chal2, step=2, password=password)

    # Final step: send second 89a1, sign replies with 59a1 (success)
    reply3 = request(auth_response_frame(ans2), 18)
    return b'\x59\xa1' in reply3


def finish_upload() -> bytes:
    """dc04 end of upload: aa 55 aa 55 00 00 dc 04 00 00 04"""
    return frame(0xdc, b'\x04\x00\x00')


def make_upload_stream(image_bytes: bytes, max_chunk: int = 500) -> list[bytes]:
    """
    Chunks a program image into ATT write frames for dc02 upload.
    Packet 0 begins with framing: aa 55 aa 55 00 00 dc 02 00 00 00 00 00 00 <size:2 BE> + data.
    The size field is len(image_bytes) - 4 (excluding the 00 00 00 ff prefix).
    """
    wire_size = len(image_bytes) - 4 if image_bytes.startswith(b'\x00\x00\x00\xff') else len(image_bytes)
    hdr = frame(0xdc, b'\x02\x00\x00\x00\x00\x00\x00' + struct.pack('>H', wire_size & 0xffff), tail04=False)
    p0_len = max_chunk - len(hdr)
    packets = [hdr + image_bytes[:p0_len]]
    
    pos = p0_len
    while pos < len(image_bytes):
        packets.append(image_bytes[pos:pos + max_chunk])
        pos += max_chunk

    return packets
