"""Offline stand-in for the sign: serves memory reads from a captured session (for tests and demos)."""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from btsnoop import att                      # noqa: E402
from signboard import protocol as p          # noqa: E402


def memory_from_capture(path):
    stream = b''.join(pdu[3:] for _, d, _, pdu in att(path) if d == 'RX' and pdu[0] == 0x1b)
    mem, pos = {}, 0
    while (pos := stream.find(p.DATA_MAGIC, pos)) >= 0 and pos + 11 <= len(stream):
        addr, n = struct.unpack_from('>II', stream, pos + 3)
        data = stream[pos + 11: pos + 11 + n]
        if len(data) == n and sum(data) & 0xffffffff == struct.unpack_from('>I', stream, pos + 11 + n)[0]:
            for i, b in enumerate(data):
                mem[addr + i] = b
            pos += 11 + n + 4
        else:
            pos += 1
    return mem


def fake_sign(mem):
    def request(frame, need):
        if frame[6] == 0x0e:
            return bytes.fromhex('a500a5000e018800')
        assert frame[6] == 0xac
        addr, n = struct.unpack_from('>II', frame, 8)
        data = bytes(mem[addr + i] for i in range(n))
        return (p.DATA_MAGIC + struct.pack('>II', addr, n) + data
                + struct.pack('>I', sum(data)) + bytes.fromhex('a500a500ac018800'))
    return request
