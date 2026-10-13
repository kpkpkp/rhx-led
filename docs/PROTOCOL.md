# RHX8-Q1C protocol notes

Everything here was observed on one RHX8-Q1C (card 216, firmware reply `d8 18 05`) driving a 128×32 RGB panel
(two chained 64×32 HUB75 modules, 1/16 scan), and checked against the vendor app's traffic and a camera pointed at
the sign. "Verified" means seen on that hardware; anything else is marked.

## Frames

`aa55aa55 0000 <cmd> <body> [04]` over the vendor GATT service (write `28be4a4a-cd67-11e9-a332-2a2ae2dbccff`,
notify `28be4cb6-cd67-11e9-a323-2a2ae2dbccee`). Replies arrive as notifications; status bytes `88` = OK, `aa` = refused.
Unknown commands answer `a5 00 a5 00 <cmd> 00 00 00`.

| Command | Bytes | Notes |
|---|---|---|
| Read session open / close | `0e 01 04` / `0e 02 04` | no login |
| Memory read | `ac 01 <addr:4> <len:4> 04` | sign memory address = program-image offset |
| Packet size | `ae 01 <u16> <u16>` | controller answers 500/500 (BLE MTU 512) |
| Login challenge | `78 e1 00000000 <size:4>` → `59 a0` + 8-byte challenge | answer with `89 a1 10 <16 B>` twice (`crypto.py`) |
| Power on / off | `08 04` / `09 04` | after login; verified |
| Brightness | `0a <0xef+level> 04` | level 1..16, after login; verified |
| Upload | see below | |

**Login without harm.** The login starts with an upload request (`78 e1`). With a non-zero size it erases the
stored program: the display keeps showing it from RAM until the next reload (power cycle, brightness change), then
shows the controller's ID/error screen. Use size 0 for power and brightness logins (verified: the program survives).
The app's other hello (`0d ff×9 04`) answers `88` but power commands sent after it are acknowledged and ignored.

Every power or brightness command makes the controller reload its program: a brief black blink, and animations
restart from the first frame.

## Uploads

`ae01` → login → `78e1 00000000 <size:4>` → controller accepts with `81 dc 02 00000000 <size:4>` → the stream
`aa55aa55 0000 dc02 00000000 <size:4>` + body + S1 + S2 + `00 00 04`, sent as 500-byte writes → progress
`81 dc 03 …` → `81 dc 04 … 88` (checksum OK) → host sends `dc 04 00 00 04`.

- body = the program image (starting `00 00 00 ff`), S1 = sum32(body), S2 = sum32(body + S1 bytes), size = len(body) + 2.
- The size field is 32-bit: programs of 178 KB upload fine (verified).
- The u32 before the size is **not** an offset: the controller always writes from address 0. There is no partial
  write; every change is a full upload, and the sister RHX8-Q2C firmware erases the program area on each `78e1`.
- Writes without response on a throughput-optimised link (Windows `BluetoothLEPreferredConnectionParameters`) run at
  ~16 KB/s against ~7 KB/s acked; `81dc04` still reports the checksum verdict, so a failed fast upload can be retried.
- After an upload the controller restarts its display (sometimes flashing its ID screen) and shows the new program
  ≈0.4 s later. Do not log in again within ~3 s of an upload.
- Remote program switching (`16 …`, "play program N", in the app's hidden remote-control page) answers "unknown
  command" on the Q1C.

## Program image

- Header: kind (0x75 = seven-colour text program, 0x73 = picture program), width, height, section pointers, total
  length at 0x11 (= len(image) − 4 + 6).
- Config block 0x2f..0x71 (config byte k = the app's Screen para[k+1]); **8-bit checksum at 0x72** = sum of 0x2f..0x71.
  Work mode at 0x4d; grey depth g at 0x37 (para[10]).
- Project JSON (the app's description, used for readback) and a send-history log follow; the controller draws from
  the bitmaps.
- Bitmaps are RLE rows reached through a table of absolute big-endian u32 row pointers. Row RLE: `X a5 n` = X plus n
  more copies; a literal `a5` is `a5 00`. A footer (u32 decoded size, u32 RLE length, `04`) ends the data.

### Work modes

| Mode | Bits per channel (planes) | Notes |
|---|---|---|
| seven-colour background (1) | 1 | text programs: caption bitmap scrolls over an animated background |
| seven-colour lengthening (0) | 1 | picture programs |
| low gray (2) | 2 | |
| middle gray (3) | 4 | |
| high gray (4) | 6 | |
| true colour (5) | 8 | the app limits the area to 96 px wide on a 32-row panel |

Planes are channel-major: R bits 7..(7−g), then G, then B, each plane the full picture height.

### Picture block (picture programs, at 0xbf)

`effect, speed, stop, 00, image count, width:2, height:2, frames, pointer:4`

- **Row-table layout** (frames = 1): pointer → table of row pointers; item 1 is a 32-row background, then the picture
  (3·(g+1) planes × height rows). Tall pictures scroll with effect 17.
- **Sequence** (image count = N, effect 25, speed 10): N whole pictures, image-major, played at ≈83 ms each.
- **Raw layout** (frames = 0, pointer 0xcd): 3·(g+1) planes × 32 rows × 16 B uncompressed right after the block.
  Required for left/right continuous pans.

### Effects (ShowWay)

0 shinning, 1 static, 2 random, 3–6 curtain up/down/left/right, 7–10 close/open up-down/left-right, 11–12 shutter,
13–16 up/down/left/right one step at a time, 17–20 up/down/left/right continuous, 21–24 laser down/up/left/right,
25 picture sequence. `stop` = seconds to hold between moves (0 = none). Speed 3 continuous ≈ 35 px/s.

## Panel quirks

- A screen width smaller than the panel (e.g. 96 or 32 on a 128-wide panel) puts the picture on the right and
  repeats it to the left; each repeat is one scan line older (shifted down one more row, wrapping within each
  16-row half).
- Seven-colour text uses the background animation at ≈91 ms per frame.
