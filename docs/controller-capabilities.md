# RHX8-Q1C controller: capability map

Compiled 2026-10-12. Read-only research (vendor site, vendor spec sheet and manual, the RHX Plus card tables, app QML and string dumps). No device was contacted. "Verified" means verified on our sign (camera photos or readback), as recorded in `docs/PROTOCOL.md`.

Confidence tags: **[spec]** = vendor spec sheet or manual (marketing, but specific to this card), **[table]** = the app's machine-readable card table, **[app]** = app UI or command code (the feature exists somewhere in the family, not necessarily on Q1C), **[inferred]** = our reading of photos or strings.

## 1. Hardware

| Item | Finding | Confidence |
|---|---|---|
| Vendor / line | Zhengzhou Ruihexin (瑞合信) 8th-generation "门楣全彩" (shop-front full-colour) card. Comms: **WiFi + Bluetooth**. Requires app V8.0.76 or later (BLE support was added in 8.0.76, 2022-11-18). | spec |
| SoC | Card table entry 216 `RHX8-Q1C` has `supportType: LN882H_F`, as does sibling 217 `RHX8-Q2C`. The Q2C firmware embedded in RHXPlus.exe is LN882H (`LN882H WiFi Lib Ver`, BLE stack `gapm_/gattc_`, `rtc_thread`, `/LEDROM`). The product photo shows one QFN32 WiFi+BLE chip. Other LN882H firmware in the exe is for `RHX8-W2m`; the ESP32-S2 firmware is for `RHX8-Q4A1`. | table + strings: high. Q1C-specific firmware not seen. |
| LN882H (chip) | Cortex-M4F at 160 MHz, about 296 KB SRAM, 802.11b/g/n 2.4 GHz, BLE 5.1. QFN32: 20 GPIO, 2 UART, I2C, SPI, ADC, RTC, WDT, DMA. Package flash is 1 MB (LN882HKG) or 2 MB (LN882HKI). The SDK has an OTA partition. | datasheet-level community docs |
| Program storage | Spec sheet: "储存空间 1M" (1 MB). Card table `romSize: 2`. We have uploaded 178 KB programs successfully. | spec/table disagree |
| Board I/O | From the official photo: **1× HUB75 (16-pin, "75-1")**, 5 V screw terminal, red power LED, **one push button** (P1, probably test/init), **coin-cell holder + 32 kHz crystal + SOIC-8 chip** (almost certainly a battery-backed RTC), and two buffer ICs (U3/U4). No USB, no RS485, no sensor header, no IR receiver, no light sensor. | photo: inferred |
| Power | 3.5–5.5 V DC, under 2 W, -40 to 75 °C. Range 20 m WiFi/BLE. AP SSID `RHX8-Q1C#...`. | spec |
| Panel interface | HUB75 only (Model.json `interface 1*2`, `color 2` = full colour, `communicate 0,4` = WiFi, BLE). No mono/bi-colour. Scan: static, 1/2 … 1/32, plus custom "trace-point" scan configs. Supports regular drivers (ICN2037, FM6124, MBI5124 and others) and "special" ones (ICN2038S, SM16188, FM6126/6127, DP3246). **PWM high-refresh chips (ICN2053, FM6363, etc.) are NOT supported.** | spec |

**Max area per work mode (H×W, "length/width complementary" = fixed pixel budget):**

| Mode (para) | Spec sheet | Configure.json card 216 | Model.json | At 32 rows | Our 128×32 |
|---|---|---|---|---|---|
| 0 Seven-colour lengthening (no bg) | — | 32×1024 | 32×1024 | 1024 | fits |
| 1 Seven-colour background + colourful text | 32×768 | 32×1024 | 32×768 | 768–1024 | fits |
| 2 Low gray ("standard") | 32×384 | 32×512 | 32×384 | 384–512 | fits |
| 3 Middle gray | 32×192 | 32×192 | 32×192 | 192 | fits |
| 4 High gray (no bg) | 32×128 | 32×128 | 32×128 | 128 | exactly fits |
| 5 True colour (no bg) | **32×96** | 32×96 | 32×96 | 96 | **does not fit** |

Our camera tests agree: true colour only draws 96 columns (`docs/PROTOCOL.md`, screen width < 128). For our panel this is a hardware/firmware limit, not a builder bug. "Background + colourful text together" lowers these numbers on small cards (manual, §屏参设置 item 7).

## 2. Feature map

Status key: VERIFIED / PARTIAL / NOT YET USED / UNSUPPORTED on Q1C / UNKNOWN.

| Feature | What sources claim | Evidence | Our status |
|---|---|---|---|
| Work modes / colour depth | 6 modes, from 7-colour (1 bit) to true colour (8 bit) | Configure.json `workModeF`; spec sheet | **VERIFIED**: all 6 build and show (`picture.py`, `templates/`) |
| Max area per mode | Table above | spec, Configure.json `cardF` num 216 | **VERIFIED** for true colour = 96 wide; others are within budget |
| Text: fonts, outline, multiline, spacing | Subtitle/text/animated-text areas; font size, bold/italic/underline, reverse/flip/mirror/stretch, char and line spacing (negative too), outline ("描边字"), multiline button (8.0.118), Android-only custom fonts | manual §4.1, 4.8, 4.9; changelog 8.0.60/76/118; QML `MuiltLine`, `AddFont` | **PARTIAL**: we render our own caption bitmap with fill + outline (`render.py`). Multiline and vendor fonts are not used. |
| Colourful/gradient/random-colour text | "炫彩字 / 渐变字 / 随机色" | spec items 9–11 | **NOT YET USED** (we can draw them ourselves) |
| Pictures / GIF | jpg/png/bmp and GIF in picture/animation areas | manual §4.2 | **VERIFIED**: picture programs and sequences (effect 25) |
| Video area | MP4 "only big cards like Q10, others may look bad" | manual §4.7 | **UNSUPPORTED in practice** (picture sequences cover it) |
| Programs: count, play modes | Up to 255/256 programs; sequential, timed (date/time/week), fixed-length play | spec "常用功能"; manual §概述; QML `beginDate/endDate/beginTime/endTime/Week/TimeLen/OrderPlay/TimerPlay/RepeatCount` | **NOT YET USED**: we always upload one program. The multi-program layout is unknown. |
| Areas / partitions | Free areas: subtitle, text, table, picture, animation, dial, digital time, lunar, count up/down; auto split; programme and area backgrounds | spec items 12, 18; QML `rc*` types | **PARTIAL**: we use area A (caption) over a background, or a full-screen picture. No multi-window layouts yet. |
| Clock / date / dial / lunar | Digital time (styles, time zone), analogue dial, Chinese lunar calendar | manual §4.3–4.5; changelog 8.0.60 (time zones) | **NOT YET USED**. Needs the RTC, which is probably on board. |
| Count up / countdown | Target date/time; counts up if past | manual §4.6; QML `rcTimer`, `targetDate` | **NOT YET USED** |
| Stopwatch, counter, button program switching | Physical keys or 433 MHz remote | manual Q12–14: "适用卡型 RHX8-T8 only" | **UNSUPPORTED on Q1C**: our 0x16 `programerCmd` got "unknown command" (`docs/PROTOCOL.md`) |
| Temperature / humidity / 8-in-1 / ModBus RS485 | External sensors, ModBus slave (PM2.5, wind, CO2 …) | manual Q15–16: T8 only; no ports on the Q1C board | **UNSUPPORTED** (no hardware) |
| Brightness, manual | 16 levels | spec; opcode `0a` | **VERIFIED** (levels 1..16) |
| Brightness, timed | "定时调亮" schedule | opcode `0c` + 12 bytes (`timeBright`); QML `timerBright` | **NOT YET USED** (signd's scheduler does this on the host) |
| Brightness, auto sensor | Not claimed for Q1C; no sensor on the board | photo | **UNSUPPORTED** (inferred) |
| Power-on soft start | Fades up over 15 s after power-up ("开机渐亮") | manual §高级功能; opcode `0b` | **NOT YET USED** |
| Power on/off now | Yes | opcodes `08`/`09` | **VERIFIED** |
| Power schedules | 4 on/off time pairs | spec "定时开关机 4组"; opcode `0d` + 9 bytes (`timePower`; the app's "power on" is `0d ff×9`) | **NOT YET USED** natively. We send `0d ff×9 04` as the login hello, which may also be "clear timers". See unknowns. |
| Clock set | "校时" | opcode `07` (`correctionTime`) | **NOT YET USED** |
| WiFi AP | Own hotspot `RHX8-Q1C#…`; name/password changeable (letters and digits, at least 8) | spec; opcodes `80`, `77`, `89c0` | **NOT YET USED** (no TCP transport in this repo yet) |
| WiFi STA (join router) / LAN | "WiFi cards can join a router, LAN comms (latest version)" | manual §核心特点; QML `setRouter`, `ConnectRHXWifi` | **UNKNOWN** for Q1C |
| TCP port | port 3000 at 192.168.4.1 assumed in an early, unpublished experiment | our guess only; the Q2C firmware also has 192.168.47.x addresses | **UNKNOWN** | <!-- scan:ok vendor default AP addresses -->
| Cloud server | 4G cards and network cards; "WiFi cards remote cloud (latest)" | manual ch. 7; QML `useRHXServer`, `setCloudServerIP` | **UNKNOWN**, probably not on Q1C |
| BLE | GATT write/notify, one central | our `direct.py` | **VERIFIED** (production path) |
| USB drive | "小卡不支持优盘" (small cards: no USB) | spec item 29; no port | **UNSUPPORTED** (vendor `C:\LedRom` export still useful as a template source) |
| Program readback | Read back program and screen settings | spec item 21; `ac01` | **VERIFIED** (`read_image`) |
| Read version / supported functions | `83` readVersion, `ac05` readSupportedFunctions, `82` readSetting, `ac02` getSetting | `rhxcmd.s` | **NOT YET USED**. Reads, low risk. |
| Passwords / phone binding | Card password, change history, bind phone, "card encryption not dependent on WiFi password" | spec 14, 15, 23, 27; `89a1` challenge | **PARTIAL**: we solve the login challenge (`crypto.py`). Password change not used. |
| Screen-parameter lock | Lock/unlock scan settings (advanced pwd 888) | manual §8.2 | **NOT YET USED** |
| Scan / timing settings | Scan code, data/OE polarity, RGB order, row blanking, "fine-tune" (`87`), refresh frequency (`86`), test patterns (`97`) | manual §8; opcodes | **NOT YET USED** (vendor scan entry kept as exported). Risky. |
| Rotation / mirror / double-sided vertical | 360° rotate, subtitle mirror, folded vertical panels | spec 17, 19 | **NOT YET USED** (we can rotate at render time) |
| Effects (ShowWay) | 25 in the QML list (0 shinning … 24 Laser Right), plus 25 used by GIF exports; spec claims 59 including animated-text effects | QML `0018a297.bin`; spec | **VERIFIED**: 1, 17–20, 25. Others are untested. |
| Borders ("花边") | 200+ programme and area borders, speed | spec "炫酷花边"; QML `Lace`, `LaceSpeed` | **NOT YET USED** |
| Backgrounds | 150+ animated backgrounds, custom images or animation | spec | **VERIFIED** (custom 7-colour background frames, `render.set_background`) |
| Tables | Excel-like table area | manual §4.10 | **NOT YET USED** (low value) |
| Firmware update | "升级控制卡", "特定固件升级"; cloud OTA for 4G cards | manual; opcode `70e1` + `02 41` | **NOT YET USED**. High risk. |
| Factory init | Normal init (`98a5`, `1b`); "super init" (power-cycle, then reconnect within 1 min) resets everything, SSID gets "AP1" | manual Q10 | **NOT YET USED**. Destructive. |
| Test / ageing mode | Panel test patterns, "extreme test" | spec 25; `97 xx` | **NOT YET USED** |
| Partial / delta upload | — | — | **UNSUPPORTED**: every 78e1 erases the program (`docs/PROTOCOL.md`) |

## 3. Not covered by us yet (prioritised)

1. **Read version and supported functions** (`83`, `ac05`, `82`, `ac02`). These are reads with no state change. They would answer half of the unknowns below (firmware version, capability bits, STA support). Test: send them over the existing BLE link while idle, log the raw replies, and compare with the app's parser in `rhxcmd.s` and `so.str`. Low risk.
2. **Set the clock (`07`) and a clock/date area.** This gives a self-running clock with no host involved. Test: (a) `07` with the current time, then read `82`; (b) export a vendor program with one time area from RHX Plus Windows ("USB drive" → `C:\LedRom`), diff it, and upload it. Low risk. Note that `07` may need a login, which reloads the display (a blink).
3. **Native countdown area.** This is the same as item 2: get a vendor sample and diff it. Low risk.
4. **Native power and brightness schedules** (`0d`, `0c`). The sign then keeps to its schedule when the host computer is down. Test: capture the app setting one schedule over BLE (HCI snoop), replay it, and photograph the sign at the boundary times. Medium risk: a wrong schedule can leave the panel dark. First confirm what our `0d ff×9` hello does to existing schedules.
5. **Multiple programs with timed or weekday play.** Lets the sign rotate content (morning/evening) with no host. Test: vendor export with 2 programs, one timed, then diff. Low risk, but the format work is large.
6. **Multi-area layouts** (caption plus clock, picture plus text). Use vendor samples per layout. Low risk.
7. **Borders (lace) and other ShowWay codes (0, 2–16, 21–24).** These are cheap to test: change one byte in a working picture or caption program and film it. Low risk.
8. **Power-on soft start (`0b`).** Cosmetic, low risk.
9. **WiFi STA / TCP transport.** A second link that doesn't fight BLE's single-central limit. Test: read only first (scan for the AP, then connect to its TCP port and send a `0e01`/`ac01` read). **Do not write router credentials until we know how to recover** (super init is the fallback). Medium risk.
10. **Risky. Do not do these without an explicit decision:** firmware upgrade (`70e1`); factory init (`98a5`, `1b`, super init), which wipes the program and password and renames the SSID; password changes (`89c0`, `77`), which could lock out the vendor app and our `crypto.py` if they change the challenge key; WiFi name or password changes (`80`, `77`); scan parameters, polarity and fine-tune (`97`, `86`, `87`), which can scramble the display; screen-parameter lock.

## 4. Known unknowns

- The exact Q1C firmware: version, build, and whether it matches the Q2C image embedded in RHXPlus.exe.
- Program flash: 1 MB (spec) or 2 MB (`romSize`)? What is the real maximum program size?
- Does sending the `0d ff×9 04` login hello (the app's "power on" = `timePower` with all slots 0xff) clear stored power timers?
- The format of the `07` time and of the `0c`/`0d` schedule payloads, and whether they need a login.
- Is there an RTC that survives power loss? (The coin cell suggests so, but this is unverified.)
- Does Q1C firmware support STA mode, LAN discovery (`02 24` UDP search) or cloud? What are the TCP port and AP IP (192.168.4.1 or 192.168.47.1)? <!-- scan:ok vendor default AP addresses -->
- The meaning of the `ac05` supported-functions bitmap.
- What does the on-board button do (test pattern, next program, init)?
- Why does the card table give 32×1024 for mode 1 while the spec gives 32×768? Is this the "background + colourful text" penalty?
- Can multiple programs or areas mix work modes? (The mode is a card-wide config, so probably not.)
- Is there a seven-colour-lengthen export 128 wide (to remove the 96-wide wrap)?

## 5. Sources

- Vendor product page, RHX8-Q1C: http://www.rhxled.com/chanpinzhongxin/230.html (Q2C: http://www.rhxled.com/chanpinzhongxin/231.html)
- Vendor spec sheet (规格书), RHX8-Q1C, 2022-12-05: http://mp.rhxled.com/index.php?g=Wap&m=Index&a=lists&classid=477&token=lsfxjd1431739670 (board photo `.../a/5/7/a/65e533af71513.jpg`, dimension drawing `.../8/3/a/2/65e2f0c18b2b5.png`)
- RHX Plus user manual 8.0 (v8.08 PDF): http://www.rhxled.com/wendangxiazai/108.html → http://alicdn.rhxled.com/wifi/soft/瑞合信Plus 用户手册（8.0版）v8.08.pdf (local copy: session scratchpad `web/manual/`)
- Software page and changelog (V8.0.118, 2026-07-30): http://www.rhxled.com/8dairuanjian/ (`ruanjianxiazai/251.html`)
- Full-colour card catalogue: http://www.rhxled.com/menmeiquancaikongzhixitong/
- LN882H: https://docs.libretiny.eu/boards/generic-ln882h/ , https://docs.libretiny.eu/docs/platform/lightning-ln882x/ , https://www.elektroda.com/rtvforum/topic4027545.html , https://docs.ai-thinker.com/_export/xhtml/en/ai_wb3
- Marketplace (sibling cards, generic claims): https://www.alibaba.com/product-detail/RHX8-Q4AM-LED-display-full-color-1600409900053.html , https://vattuled.com/mach-dieu-khien-led-ma-tran-rhx-8q2.html (behind a bot check, not read)
