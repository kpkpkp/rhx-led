"""Direct BLE client for the RHX8-Q1C: this computer talks to the controller itself, no phone, no vendor app.

One persistent GATT link (the controller accepts a single central), reconnected on demand and dropped after an idle
period so the vendor app can still get in when it is needed. Every operation is serialised by one lock.

  login         78e1 (size 0) -> 59a0 challenge, 89a1 -> 59a3, 89a1 -> 59a1   (crypto.solve_challenge)
  power         login, then 08 (on) / 09 (off); reply 88
  brightness    login, then 0a <0xef + level> 04; reply 88
  read          0e01, ac01 reads, 0e02                                  (no login)
  upload        78e1 <size> (login if challenged) -> 81dc02, dc02 stream, 81dc04 verdict, dc04   (see upload())

  python -m signboard.direct status|on|off|brightness N|read-image OUT|upload IMAGE   (back up first: read-image)
"""
import argparse
import asyncio
import json
import struct
import threading
import time
from pathlib import Path

from signboard import crypto, config, protocol

ADDR = None                 # from signboard.config ("sign_address")
WRITE_UUID = '28be4a4a-cd67-11e9-a332-2a2ae2dbccff'
NOTIFY_UUID = '28be4cb6-cd67-11e9-a323-2a2ae2dbccee'
UPLOAD_SETTLE_S = 3.0  # no 78e1 login this soon after an upload (it can break the program just written)
IDLE_S = 5            # release the link quickly: the controller takes one central, and others (the app, cron jobs) need it


class SignError(RuntimeError):
    pass


class DirectSign:
    def __init__(self, addr=ADDR, verbose=False, idle_s=IDLE_S):
        self.addr, self.verbose, self.idle_s = addr or config.require('sign_address', "The RHX controller's BLE address (a bleak or nRF Connect scan shows it)."), verbose, idle_s
        self.lock = threading.RLock()
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True, name='ble-loop').start()
        self._client = None
        self._last_upload = 0.0
        # size field of the 78e1 that opens a power/brightness login. It used to be 100: that login erased the stored
        # program (the display kept showing it from RAM until the next reload, then showed the white gibberish/ID
        # screen). With 0 the program survives an off/on cycle (camera-verified 2026-10-12).
        self.login_size = 0
        self._rx = bytearray()
        self._rx_event = None
        self._last_use = 0.0
        threading.Thread(target=self._reaper, daemon=True, name='ble-reaper').start()

    # ---- link ------------------------------------------------------------------------------------
    def log(self, *a):
        if self.verbose:
            print(time.strftime('%H:%M:%S'), 'ble:', *a, flush=True)

    def _run(self, coro, timeout=30):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def _on_notify(self, _h, data):
        self._rx.extend(data)
        if self.verbose:
            self.log('<<', bytes(data[:48]).hex() + ('…' if len(data) > 48 else ''))
        self._rx_event.set()

    async def _connect(self):
        from bleak import BleakClient, BleakScanner
        dev = await BleakScanner.find_device_by_address(self.addr, timeout=10)
        if dev is None:
            raise SignError(f'sign {self.addr} not advertising (powered? another central connected?)')
        c = BleakClient(dev)
        await c.connect(timeout=15)
        self._rx_event = asyncio.Event()
        await c.start_notify(NOTIFY_UUID, self._on_notify)
        self._client = c

    @property
    def connected(self):
        return bool(self._client and self._client.is_connected)

    def ensure(self):
        if not self.connected:
            t = time.time()
            for attempt in range(3):
                try:
                    self._run(self._connect(), timeout=40)
                    break
                except Exception as e:
                    self.log('connect failed:', e)
                    self._client = None
                    if attempt == 2:
                        raise SignError(f'cannot connect to the sign: {type(e).__name__}: {e}')
                    time.sleep(2)
            self.log(f'connected in {time.time() - t:.1f} s')
        self._last_use = time.time()

    def close(self):
        with self.lock:
            if self._client:
                try:
                    self._run(self._client.disconnect(), timeout=10)
                except Exception:
                    pass
                self._client = None

    def _reaper(self):
        while True:
            time.sleep(2)
            if self._client and time.time() - self._last_use > self.idle_s and self.lock.acquire(blocking=False):
                try:
                    if time.time() - self._last_use > self.idle_s:
                        self.log('idle: releasing the link')
                        self.close()
                finally:
                    self.lock.release()

    def password(self):
        return config.require('sign_password', 'The controller password set in the RHX app (change the factory default first).')

    # ---- raw I/O ---------------------------------------------------------------------------------
    def write(self, data: bytes, response=True):
        if self.verbose:
            self.log('>>', data[:48].hex() + ('…' if len(data) > 48 else ''))
        self._run(self._client.write_gatt_char(WRITE_UUID, data, response=response))
        self._last_use = time.time()

    def wait_for(self, pred, timeout=8.0):
        """Wait until pred(rx_bytes) returns something truthy; returns it. Does not clear the buffer."""
        deadline = time.time() + timeout
        while True:
            got = pred(bytes(self._rx))
            if got:
                return got
            if time.time() > deadline:
                raise SignError(f'timeout; rx so far: {bytes(self._rx[-64:]).hex()}')
            time.sleep(0.01)

    def request(self, frame: bytes, need: int, timeout=8.0) -> bytes:
        """protocol.py's transport interface: clear, write, return the first `need` bytes."""
        self._rx.clear()
        self.write(frame)
        return self.wait_for(lambda b: b[:need] if len(b) >= need else None, timeout)

    def expect(self, frame: bytes, marker: bytes, extra: int = 0, timeout=8.0) -> bytes:
        """Clear, write, wait until `marker` (plus `extra` following bytes) has arrived; return from the marker on."""
        self._rx.clear()
        self.write(frame)

        def got(b):
            i = b.find(marker)
            return b[i:] if i >= 0 and len(b) >= i + len(marker) + extra else None
        return self.wait_for(got, timeout)

    def ack(self, frame, timeout=6.0):
        """Write a command whose reply is a bare 88 (OK) or aa (refused). The controller sometimes pushes an unrelated
        notification (a5 00 a5 ...) first, so look for the status byte anywhere rather than trusting byte 0."""
        self._rx.clear()
        self.write(frame)
        try:
            self.wait_for(lambda b: b'\x88' in b or b'\xaa' in b, timeout)
        except SignError:
            return bytes(self._rx[:1])
        return b'\x88' if b'\x88' in self._rx else b'\xaa'

    # ---- operations ------------------------------------------------------------------------------
    def login_challenge(self, password=None):
        """Login that always runs the challenge: 78e1 (upload request) -> 59a0, 89a1 -> 59a3, 89a1 -> 59a1.
        Use it for power/brightness. The 0d hello can answer a bare 88 without a challenge, and after that the
        controller acks 08/09 but ignores them (2026-10-11: Off from the web UI left the sign lit, seen by camera);
        this sequence is the one whose power-off the camera confirmed.
        78e1 starts a new upload, which (Q2C firmware) erases the program area: sent within a few seconds of an
        upload it broke the fresh program twice (2026-10-12), so wait until the controller has settled."""
        settle = UPLOAD_SETTLE_S - (time.time() - self._last_upload)
        if settle > 0:
            self.log(f'waiting {settle:.1f} s for the last upload to settle')
            time.sleep(settle)
        self._rx.clear()
        self.write(protocol.upload_request(self.login_size))
        r = self.wait_for(lambda b: b if (b'\x59\xa0\x08' in b and len(b) >= b.find(b'\x59\xa0\x08') + 11)
                          or b'\x81\xdc\x02' in b else None)
        if b'\x59\xa0\x08' not in r:
            return            # already authorised on this link (e.g. right after an upload): it simply accepted
        r = r[r.find(b'\x59\xa0\x08'):]
        a1 = crypto.solve_challenge(r[3:11], 1, password or self.password())
        r = self.expect(protocol.auth_response_frame(a1), b'\x59\xa3\x08', 8)
        a2 = crypto.solve_challenge(r[3:11], 2, password or self.password())
        self.expect(protocol.auth_response_frame(a2), b'\x59\xa1')

    def login(self, password=None):
        """The app's login (8.0.90): 0d ff..ff 04 asks for the challenge, two 89a1 answers, then 0d ff..ff 04 -> 88."""
        hello = protocol.frame(0x0d, b'\xff' * 9)
        self._rx.clear()
        self.write(hello)
        r = self.wait_for(lambda b: b if (b'\x59\xa0\x08' in b and len(b) >= b.find(b'\x59\xa0\x08') + 11)
                          or b[:1] == b'\x88' else None)
        if r[:1] == b'\x88':
            return                                   # the controller still holds a login from a moment ago
        r = r[r.find(b'\x59\xa0\x08'):]
        a1 = crypto.solve_challenge(r[3:11], 1, password or self.password())
        r = self.expect(protocol.auth_response_frame(a1), b'\x59\xa3\x08', 8)
        a2 = crypto.solve_challenge(r[3:11], 2, password or self.password())
        self.expect(protocol.auth_response_frame(a2), b'\x59\xa1')
        r = self.request(hello, 1)
        if r != b'\x88':
            raise SignError(f'login not confirmed: {r.hex()}')

    def negotiate(self):
        """ae01: ask for 500-byte packets. The app does this first on every connection."""
        r = self.expect(protocol.frame(0xae, b'\x01\x01\xf4\x01\xf4', tail04=False), b'\xa5\x00\xa5\x00\xae\x01\x88', 5)
        return struct.unpack_from('>HH', r, 8)          # (500, 500) on BLE 4.2+ hosts; an old BLE 4.0 host got (20, 20)

    def begin_upload(self, size, password=None):
        """78e1 <size> until the controller accepts *this* size (81dc02 ... <size>).
        78e1 answers either a challenge (59a0: log in, then ask again) or an accept. An accept for another size means
        an upload is still pending from an earlier 78e1 (a login opens one and it survives reconnects): close it with
        dc04 and ask again. 78e1 erases the program area, so it waits UPLOAD_SETTLE_S after the previous upload."""
        settle = UPLOAD_SETTLE_S - (time.time() - self._last_upload)
        if settle > 0:
            time.sleep(settle)
        req = protocol.frame(0x78, b'\xe1' + b'\x00' * 4 + struct.pack('>I', size), tail04=False)
        mine = b'\x81\xdc\x02' + b'\x00' * 4 + struct.pack('>I', size)
        for _ in range(4):
            self._rx.clear()
            self.write(req)
            r = self.wait_for(lambda b: b if (b'\x59\xa0\x08' in b and len(b) >= b.find(b'\x59\xa0\x08') + 11)
                              or (b'\x81\xdc\x02' in b and len(b) >= b.find(b'\x81\xdc\x02') + 11) else None, 10)
            if mine in r:
                return
            if b'\x59\xa0\x08' in r:
                c = r[r.find(b'\x59\xa0\x08'):]
                r2 = self.expect(protocol.auth_response_frame(crypto.solve_challenge(c[3:11], 1, password or self.password())), b'\x59\xa3\x08', 8)
                self.expect(protocol.auth_response_frame(crypto.solve_challenge(r2[3:11], 2, password or self.password())), b'\x59\xa1')
                continue
            self.log('stale upload pending:', r[r.find(b'\x81\xdc\x02'):][:11].hex(), '-> closing it')
            self.write(protocol.finish_upload())
            time.sleep(0.5)
        raise SignError(f'controller did not accept an upload of {size} B')

    def _fast_link(self):
        """Ask Windows for a throughput-optimised connection (Win11 WinRT). 43 KB went from 6.3 s to ~2.7 s
        together with unacked writes (2026-10-12). Returns False where unavailable (other OSes, older bleak)."""
        try:
            from winrt.windows.devices.bluetooth import BluetoothLEPreferredConnectionParameters as P
            self._client._backend._requester.request_preferred_connection_parameters(P.throughput_optimized)
            return True
        except Exception as e:
            self.log('fast link not available:', e)
            return False

    def _stream(self, packets, unacked, progress):
        """78e1 already accepted: send the dc02 stream, wait for 81dc04, always close with dc04. Returns the verdict."""
        self._rx.clear()
        try:
            for i, p in enumerate(packets):
                self.write(p, response=not unacked)
                if progress:
                    progress(i + 1, len(packets))
            try:
                done = self.wait_for(lambda b: b[b.find(b'\x81\xdc\x04'):] if b'\x81\xdc\x04' in b and len(b) >= b.find(b'\x81\xdc\x04') + 10 else None, 15)
            except SignError:
                done = b''
        finally:
            try:
                self.write(protocol.finish_upload())
            except Exception:
                pass
        return done

    def upload(self, img: bytes, progress=None, fast=True, _restoring=False):
        """Write a full program image (with the 00 00 00 ff prefix) to the sign: ae01, login, 78e1 <size> -> 81dc02,
        dc02 stream of 500-byte writes, 81dc04 (checksum verdict) -> dc04 000004. fast=True sends the stream unacked on
        a throughput-optimised link (~2.4x; Windows only, needs MTU >= 503) and falls back to acked writes when the
        verdict is not 88 or the link drops. 78e1 erases the stored program, so if every attempt fails the last
        program that uploaded successfully is put back. The u32 before the size in 78e1/dc02 is not an offset: the
        controller always writes from address 0 (tested 2026-10-12), so there are no partial uploads."""
        from signboard import image
        packets, size = image.wire_stream(img)
        with self.lock:
            self.ensure()
            t0 = time.time()
            fast = fast and self._fast_link() and (getattr(self._client, 'mtu_size', 23) - 3) >= max(map(len, packets))
            self.negotiate()
            done, err = b'', None
            for unacked in ((True, False) if fast else (False,)):
                try:
                    if not self.connected:               # the last attempt dropped the link: a new one needs ae01 again
                        self.ensure()
                        self.negotiate()
                    self.begin_upload(size)
                    done = self._stream(packets, unacked, progress)
                    err = None
                except Exception as e:                   # link dropped mid-stream: reconnect and try the next way
                    done, err = b'', e
                    self.log(f'upload attempt failed: {type(e).__name__}: {e}')
                    self._reset_link()
                ok = done[6:7] == b'\x88'
                self.log(f'upload {size} B {"unacked" if unacked else "acked"} in {time.time() - t0:.1f} s; end {done[:11].hex()}')
                if ok:
                    break
                time.sleep(0.5)
            if not ok:
                good = getattr(self, '_last_good', None)
                if good and good != img and not _restoring:
                    self.log('upload failed: putting the last good program back')
                    try:
                        self.upload(good, fast=False, _restoring=True)
                    except Exception as e:
                        self.log('restore failed too:', e)
                raise SignError(f'controller rejected the upload: {done[:11].hex() or err}')
            self._last_upload = time.time()
            self._last_good = img
            self._remember(True)                     # an upload always wakes the display
            return {'bytes': size, 'seconds': round(time.time() - t0, 1)}

    # Display power we last asked for, kept on disk. A read session (0e01 ... 0e02) or an upload can switch the display
    # back on, so after a read we re-assert "off" when that is what was asked for (2026-10-11: Off from the web UI
    # was undone by the UI's own /api/now polling).
    POWER_FILE = config.state_file('power.json')

    @property
    def want_on(self):
        try:
            return json.loads(self.POWER_FILE.read_text()).get('on')
        except Exception:
            return None

    def _remember(self, on):
        try:
            self.POWER_FILE.write_text(json.dumps({'on': bool(on), 'at': time.time()}))
        except Exception:
            pass

    def power(self, on: bool):
        with self.lock:
            self.ensure()
            self.login_challenge()
            r = self.ack(protocol.power(on))
            self._reset_link()
            if r != b'\x88':
                raise SignError(f'power {"on" if on else "off"} rejected: {r.hex()}')
            self._remember(on)
            return True

    def _reset_link(self):
        """The 78e1 login leaves an upload request pending; on the same link the controller then answers everything
        (even ae01) with that stale 81dc02 and uploads stall. A reconnect clears it, so drop the link."""
        time.sleep(0.3)
        if self._client:
            try:
                self._run(self._client.disconnect(), timeout=10)
            except Exception:
                pass
            self._client = None

    def _reassert_off(self):
        if self.want_on is False:
            self.login_challenge()
            self.ack(protocol.power(False))
            self._reset_link()
            self.log('re-asserted power off after a read')

    def brightness(self, level: int):
        """Display brightness 1..16: login, then 0a <0xef + level> 04 -> 88 (from the app's HCI log: auto-light's
        levels 16/10/4/2 went out as ff/f9/f3/f1)."""
        if not 1 <= int(level) <= 16:
            raise ValueError('brightness level 1..16')
        with self.lock:
            self.ensure()
            self.login_challenge()
            r = self.ack(protocol.frame(0x0a, bytes([0xef + int(level)])))
            self._reset_link()
            if r != b'\x88':
                raise SignError(f'brightness {level} rejected: {r.hex()}')
            return True

    def read_program(self):
        with self.lock:
            self.ensure()
            try:
                return protocol.read_program(self.request)
            finally:
                self._reassert_off()

    def read_image(self):
        """The whole program image out of the sign's memory (with the 00 00 00 ff wire prefix prepended)."""
        with self.lock:
            self.ensure()
            self.request(protocol.open_session(), protocol.ACK_LEN)
            try:
                head = protocol.read_memory(self.request, 0, 0x76)      # sign address == image offset
                total = struct.unpack_from('>I', head, 0x11)[0]
                if not 0x76 <= total <= 1024 * 1024:             # erased flash reads ff..: don't loop over 4 GB
                    raise SignError(f'no valid program in the sign (length field {total:#x}): upload one first')
                body = protocol.read_memory(self.request, 4, total)
            finally:
                self.request(protocol.close_session(), protocol.ACK_LEN)
                self._reassert_off()
            return b'\x00\x00\x00\xff' + body


def _find_all(b, sub):
    i = b.find(sub)
    while i >= 0:
        yield i
        i = b.find(sub, i + 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--addr', default=ADDR)
    ap.add_argument('-v', action='store_true')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('status'); sub.add_parser('on'); sub.add_parser('off')
    sub.add_parser('brightness').add_argument('level', type=int)
    sub.add_parser('read-image').add_argument('out')
    sub.add_parser('upload').add_argument('image')
    a = ap.parse_args()
    s = DirectSign(a.addr, verbose=a.v)
    t = time.time()
    try:
        if a.cmd in ('on', 'off'):
            s.power(a.cmd == 'on'); print('power', a.cmd, 'ok')
        elif a.cmd == 'brightness':
            s.brightness(a.level); print('brightness', a.level, 'ok')
        elif a.cmd == 'status':
            from signboard.server import parse_now
            print(parse_now(s.read_program()['project']))
        elif a.cmd == 'read-image':
            img = s.read_image()
            open(a.out, 'wb').write(img); print(len(img), 'bytes ->', a.out)
        elif a.cmd == 'upload':
            print(s.upload(open(a.image, 'rb').read()))
    finally:
        s.close()
        print(f'{time.time() - t:.1f} s')


if __name__ == '__main__':
    main()
