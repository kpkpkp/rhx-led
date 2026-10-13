"""A software RHX8-Q1C for tests: speaks the frames in docs/PROTOCOL.md over a fake GATT link, no radio.

It models what the real controller was seen to do, including the awkward parts the client code works around:
  - 78e1 answers a 59a0 challenge until the link has logged in; two 89a1 answers (crypto.solve_challenge) unlock it
  - 78e1 <size> with size > 0 erases the program area; size 0 (a power/brightness login) keeps it
  - an accepted upload stays pending until dc04 or a reconnect: 78e1 and ae01 meanwhile answer the stale 81dc02
  - dc02 data packets larger than 20 B are only understood after ae01 negotiated 500-byte packets on this link
  - the stored image is body + S1 (see image.wire_stream); reads (0e01, ac01, 0e02) need no login
Fault injection: dead_transfer (the stuck state after an oversized upload), drop_after (packets into the next stream), corrupt_next (flip a byte of the next stream),
corrupt_if (flip a byte of every stream it matches).

Where the real controller's behaviour is unknown the emulator picks something simple and says so (# model:).
"""
import asyncio
import os
import struct

from signboard import crypto, protocol

MAGIC = protocol.MAGIC + b'\x00\x00'
ACK = lambda cmd, sub: bytes([0xa5, 0x00, 0xa5, 0x00, cmd, sub, 0x88, 0x00])


class LinkDropped(Exception):
    pass


class FakeController:
    def __init__(self, password='testpass1', memory=b''):  # scan:ok test-only password
        self.password = password
        self.mem = bytearray(memory) + bytearray(b'\xff' * (1024 * 1024 - len(memory)))
        self.erased = not memory
        self.display_on, self.brightness = True, 16
        self.authed = False             # model: a login survives reconnects (the hello then answers 88 at once)
        self.pending = None             # size of an accepted, not yet closed upload
        self.rx = None                  # bytes of the dc02 stream being received
        self.drop_after = None
        self.corrupt_next = False
        self.corrupt_if = None          # callable(stream) -> True to damage that stream (every attempt)
        self.dead_transfer = False      # stuck after an oversized upload: 78e1 only gets 81dc03 progress until dc04
        self.log = []                   # (direction, bytes) for assertions
        self.responses = []             # response flag of every GATT write (flow-control checks)
        self.link = None
        self.uploads = 0

    # ---- link --------------------------------------------------------------------------------------
    def attach(self, link):
        self.link = link
        self.negotiated = False
        self.challenge = None
        self.rx = None                  # a dropped stream is abandoned
        self.pending = None             # a reconnect clears a pending upload (direct._reset_link relies on this)

    def send(self, data: bytes):
        self.log.append(('<', bytes(data)))
        self.link.notify(bytes(data))

    # ---- what the panel shows ------------------------------------------------------------------------
    def screen(self):
        if not self.display_on:
            return 'off'
        return 'id-screen' if self.erased else 'program'

    def image(self):
        """The stored program as an upload image would hold it (prefix + body, trailer recomputed)."""
        total = struct.unpack_from('>I', self.mem, 0x11)[0]
        from signboard import image
        body = bytes(self.mem[:total + 4 - 6])
        return body + image.trailer(body)

    # ---- frames -----------------------------------------------------------------------------------------
    def write(self, data: bytes):
        self.log.append(('>', bytes(data)))
        if self.rx is not None:
            return self._stream(data)
        if not data.startswith(MAGIC):
            return                                   # model: stray bytes are ignored
        cmd, body = data[6], data[7:]
        if cmd == 0x0d:
            return self.send(b'\x88') if self.authed else self._challenge(1)
        if cmd == 0x78 and body[:1] == b'\xe1':
            size = struct.unpack_from('>H', body, 7)[0] if len(body) == 10 else struct.unpack_from('>I', body, 5)[0]
            return self._begin(size)
        if cmd == 0x89 and body[:2] == b'\xa1\x10':
            return self._answer(body[2:18])
        if cmd == 0xae:
            if self.pending is not None:
                return self._accept(self.pending)    # stale upload: everything gets its 81dc02
            self.negotiated = True
            return self.send(bytes.fromhex('a500a500ae018800') + struct.pack('>HH', 500, 500))
        if cmd == 0xdc and body[:1] == b'\x02':
            if self.pending is None:
                return self.send(b'\xaa')
            self.rx = bytearray()
            return self._stream(data)
        if cmd == 0xdc and body[:1] == b'\x04':
            self.pending = None
            self.dead_transfer = False
            return
        if cmd in (0x08, 0x09):
            if not self.authed:
                return self.send(b'\xaa')
            self.display_on = cmd == 0x08
            return self.send(b'\x88')
        if cmd == 0x0a:
            if not self.authed or not 0xf0 <= body[0] <= 0xff:
                return self.send(b'\xaa')
            self.brightness = body[0] - 0xef
            return self.send(b'\x88')
        if cmd == 0x0e:
            return self.send(ACK(0x0e, body[0]))
        if cmd == 0xac and body[:1] == b'\x01':
            addr, n = struct.unpack_from('>II', body, 1)
            data = bytes(self.mem[addr:addr + n])
            return self.send(protocol.DATA_MAGIC + struct.pack('>II', addr, n) + data
                             + struct.pack('>I', sum(data) & 0xffffffff) + ACK(0xac, 0x01))
        self.send(bytes.fromhex('a500a500') + bytes([cmd]) + b'\x00\xaa\x00')   # model: "unknown command"

    def _challenge(self, step):
        nonce = os.urandom(2)
        self.challenge = bytes([0xca, step]) + nonce + os.urandom(4)
        self.send((b'\x59\xa0\x08' if step == 1 else b'\x59\xa3\x08') + crypto.encrypt(self.challenge))

    def _answer(self, resp):
        plain = crypto.decrypt(resp)
        step = self.challenge[1] if self.challenge else 0
        want = (self.password if step == 1 else crypto.SECONDARY_PASSWORD).encode()[:8]
        ok = (self.challenge and plain[:4] == self.challenge[:4] and plain[4] == len(want)
              and plain[5:5 + len(want)] == want)
        if not ok:
            self.challenge = None
            return self.send(b'\x59\xa2\x00')        # model: refusal
        if step == 1:
            return self._challenge(2)
        self.authed, self.challenge = True, None
        self.send(b'\x59\xa1\x00')
        if self.pending is None and getattr(self, '_after_login', None) is not None:
            self._open(self._after_login)            # model: the 78e1 that asked for the login now opens

    def _begin(self, size):
        if self.dead_transfer:
            return self.send(b'\x81\xdc\x03' + b'\x00' * 4 + struct.pack('>II', 735641, 601000))
        if not self.authed:
            self._after_login = size
            return self._challenge(1)
        if self.pending is not None:
            return self._accept(self.pending)
        self._open(size)

    def _open(self, size):
        self._after_login = None
        if size:                                     # the program area is erased by every real upload request
            self.mem[:] = b'\xff' * len(self.mem)
            self.erased = True
        self.pending = size                          # model: even a size-0 login request stays pending until dc04
        self._accept(size)

    def _accept(self, size):
        self.send(b'\x81\xdc\x02' + b'\x00' * 4 + struct.pack('>I', size))

    def _stream(self, data):
        if self.drop_after is not None:
            self.drop_after -= 1
            if self.drop_after < 0:
                self.drop_after = None
                self.rx = None
                self.link.drop()
                raise LinkDropped('fake link dropped mid-stream')
        if len(data) > 20 and not self.negotiated:
            self.rx = None                           # model: oversize packets on an un-negotiated link are garbage
            return self._verdict(False)
        self.rx += data
        need = self.pending + 25                     # dc02 header 16 + body (size - 2) + S1 + S2 + 00 00 04
        if len(self.rx) < need:
            return
        stream, self.rx = bytes(self.rx), None
        if self.corrupt_next or (self.corrupt_if and self.corrupt_if(stream)):
            self.corrupt_next = False
            stream = stream[:100] + bytes([stream[100] ^ 0x40]) + stream[101:]
        size = struct.unpack_from('>I', stream, 12)[0]
        body = stream[16:16 + size - 2]
        s1b = stream[16 + size - 2:16 + size + 2]
        s2 = struct.unpack_from('>I', stream, 16 + size + 2)[0]
        ok = (size == self.pending and struct.unpack('>I', s1b)[0] == sum(body) & 0xffffffff
              and s2 == (sum(body) + sum(s1b)) & 0xffffffff and stream[-3:] == b'\x00\x00\x04')
        if ok:
            self.mem[:len(body) + 4] = body + s1b
            self.erased = False
            self.display_on = True                   # an upload always wakes the display
            self.uploads += 1
        self._verdict(ok)

    def _verdict(self, ok):
        self.send(b'\x81\xdc\x04\x00\x00\x00' + (b'\x88' if ok else b'\xaa') + b'\x00\x00\x00')


class FakeLink:
    """Stands in for bleak.BleakClient: write_gatt_char / disconnect / is_connected / mtu_size."""
    mtu_size = 515

    def __init__(self, ctrl, on_notify):
        self.ctrl, self.on_notify, self.is_connected = ctrl, on_notify, True
        ctrl.attach(self)

    def notify(self, data):
        self.on_notify(None, bytearray(data))

    def drop(self):
        self.is_connected = False

    async def write_gatt_char(self, _uuid, data, response=True):
        if not self.is_connected:
            raise LinkDropped('not connected')
        self.ctrl.responses.append(bool(response))
        self.ctrl.write(bytes(data))

    async def disconnect(self):
        self.is_connected = False


def emulated_sign(ctrl, fast=False, **kw):
    """A signboard.direct.DirectSign wired to `ctrl` instead of a radio. fast=True also takes the unacked path."""
    from signboard.direct import DirectSign
    pw = kw.pop('password', 'testpass1')

    class EmuSign(DirectSign):
        connects = 0

        async def _connect(self):
            self._rx_event = asyncio.Event()
            self._client = FakeLink(ctrl, self._on_notify)
            EmuSign.connects += 1

        def _fast_link(self):
            return fast

        def password(self):
            return pw

    return EmuSign('EMU', **kw)
