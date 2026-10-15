"""TCP transport for DirectSign: the controller's Wi-Fi hotspot speaks the BLE frames unchanged on a TCP port.

The controller (open AP "RHX8-Q1C#...") listens on port 25622 at its hotspot address (verified 2026-10-14). A reader thread
appends whatever arrives to DirectSign's rx buffer; replies may be split or coalesced, which is fine because
DirectSign matches by marker in the buffer, never by "one notification = one reply".
"""
import socket
import threading

DEFAULT_HOST = '192.168.47.1'  # scan:ok
DEFAULT_PORT = 25622


class TcpTransport:
    name = 'tcp'
    read_chunk = 4000        # ac01 length per request; the controller answered 4000 B in ~0.1 s

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT, connect_s=5.0, chunk=500, error=RuntimeError):
        self.host, self.port, self.connect_s, self.chunk, self.error = host, int(port), float(connect_s), int(chunk), error
        self._sock = None
        self._alive = False

    @property
    def connected(self):
        return self._sock is not None and self._alive

    def connect(self, on_data):
        try:
            s = socket.create_connection((self.host, self.port), timeout=self.connect_s)
        except OSError as e:
            raise self.error(f'cannot reach the sign at {self.host}:{self.port} ({type(e).__name__}: {e}); '
                             "is this computer on the sign's Wi-Fi?")
        s.settimeout(None)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        self._sock, self._alive = s, True
        threading.Thread(target=self._reader, args=(s, on_data), daemon=True, name='tcp-reader').start()

    def _reader(self, s, on_data):
        try:
            while True:
                d = s.recv(4096)
                if not d:
                    break
                if s is self._sock:
                    on_data(d)
        except OSError:
            pass
        finally:
            if s is self._sock:
                self._alive = False

    def write(self, data: bytes):
        if not self.connected:
            raise self.error('not connected')
        try:
            self._sock.sendall(data)
        except OSError as e:
            self._alive = False
            raise self.error(f'send failed: {e}')

    def close(self):
        s, self._sock, self._alive = self._sock, None, False
        if s:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()
