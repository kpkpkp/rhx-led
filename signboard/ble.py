"""BLE transport over bleak. Service is the Nordic UART UUID, but the two characteristics are vendor-custom.
Needs a BLE-capable host."""
import asyncio
import threading

NUS_WRITE = '28be4a4a-cd67-11e9-a332-2a2ae2dbccff'     # handle 0x12, props write/write-no-rsp/read
NUS_NOTIFY = '28be4cb6-cd67-11e9-a323-2a2ae2dbccee'    # handle 0x14, props notify/read


class BleakTransport:
    def __init__(self, address: str, timeout: float = 5.0):
        from bleak import BleakClient          # imported lazily so the package works without bleak
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True).start()
        self._client = BleakClient(address)
        self._buf = bytearray()
        self._timeout = timeout
        self._run(self._connect())

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(self._timeout + 5)

    async def _connect(self):
        await self._client.connect()
        await self._client.start_notify(NUS_NOTIFY, lambda _h, data: self._buf.extend(data))

    async def _exchange(self, frame: bytes, need: int) -> bytes:
        self._buf.clear()
        await self._client.write_gatt_char(NUS_WRITE, frame, response=True)
        async with asyncio.timeout(self._timeout):
            while len(self._buf) < need:
                await asyncio.sleep(0.01)
        return bytes(self._buf[:need])

    async def _write(self, data: bytes):
        await self._client.write_gatt_char(NUS_WRITE, data, response=False)

    def write(self, data: bytes):
        """Write a stream chunk without waiting for a notification response."""
        return self._run(self._write(data))

    def request(self, frame: bytes, need: int) -> bytes:
        return self._run(self._exchange(frame, need))

    def send(self, frame: bytes, need: int = 1) -> bytes:
        """Write a command; power replies with a single 0x88 status byte."""
        return self.request(frame, need)

    def close(self):
        try:
            self._run(self._client.disconnect())
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)

    @property
    def connected(self) -> bool:
        return self._client.is_connected
