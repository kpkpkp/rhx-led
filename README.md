# rhx-led

Drive an **RHX-series LED sign controller** (developed on an RHX8-Q1C with a 128×32 RGB panel) straight from a
computer over Bluetooth LE: no vendor app, no phone. Send text, photos, animations and patterns, switch the panel
on and off, set the brightness, and run it all from a small LAN web service.

> **Use it on signs you own.** The controller protects writes with a password. This project logs in with *your*
> password, read from your own config file; it does not guess or bypass anything. Controllers ship with a well-known
> factory password, so **change yours in the RHX app before using this** (or anything else) near other people.

Not affiliated with the controller's manufacturer. Reverse-engineered for interoperability with hardware we own.

**Tested hardware:** one RHX8-Q1C (card 216) driving a 128×32 RGB panel made of two chained 64×32 HUB75 modules at
1/16 scan, from Windows 11. **Untested:** other RHX models and panel sizes, Linux/macOS BLE (should connect; the fast
upload path is Windows-only), Wi-Fi control, firmware updates, password changes. `docs/controller-capabilities.md`
maps what the controller family can do and what is still unknown.

## What works (tested on real hardware, with a camera watching the sign)

| | |
|---|---|
| Text | 7-colour scrolling text over an animated background; text-only programs upload in about 0.6 s |
| Pictures | all six work modes: seven-colour, seven-colour lengthening, low / middle / high gray, true colour |
| Animations | sequences of whole pictures played by the controller (~83 ms per picture) |
| Pans | one picture scrolled by the controller itself, up / down / left / right, a few KB |
| Patterns | seamless tessellations panned in a square loop with clean corners (`signboard.patterns`) |
| Control | power on/off and brightness 1–16 without disturbing the stored program |
| Speed | ~16 KB/s uploads on Windows (throughput-optimised BLE link); programs of 178 KB verified |
| Service | `signd`: HTTP API + web UI on the LAN, mDNS name, job queue, schedules, optional camera verification |

What the controller does **not** do (tested): partial writes or live updates. Every change is a full program upload,
so small programs are fast and big animations are slow. See `docs/PROTOCOL.md`.

## Quick start

Requirements: Python 3.11+ and a Bluetooth LE adapter. Install with `pip install .` (add `".[mdns]"` for the
`sign.local`-style LAN name). This gives two commands: `signd` (the LAN service) and `rhx-sign` (direct control).

1. **Config.** Copy `signboard.example.toml` to `~/.signboard.toml` and fill in your controller's BLE address and
   password.
2. **Templates.** Picture and text programs are built on templates made from your own RHX Plus exports (they are
   the vendor's files, so they are not shipped here). Follow `docs/templates.md`: one "USB drive" export per mode,
   then `python -m signboard.import_ledrom C:\LedRom` (the export is one file).
3. **Try it.**

   ```
   python -m signboard.direct status          # read what the controller holds (no login needed)
   python -m signboard.direct off             # then: on, brightness 1
   python -m signboard.patterns houndstooth hound.bin && python -m signboard.direct upload hound.bin
   python -m signboard.signd --port 8090      # web UI + API on http://<this host>:8090/
   ```

   **Without templates** these already work: `status`, `on`/`off`, `brightness N`, `read-image` (do this first: it is
   your backup), uploading an existing `.bin`, and `python -m signboard.selftest`. Text, pictures, sequences,
   patterns and pans need the matching template.

4. `python -m signboard.selftest` runs the offline checks (more of them once templates are installed).

**signd has no login.** It refuses requests for unknown host names and cross-site browser requests, but anyone on
your network can use it. Run it only on a network you trust, or with `--bind 127.0.0.1`.

## Layout

```
signboard/
  protocol.py  crypto.py  direct.py       BLE frames, login, uploads, power, brightness
  image.py  render.py                     program image container; text + background rendering
  picture.py  sequence.py  pan.py         picture modes, picture sequences, controller pans
  patterns.py                             seamless panned tessellations
  import_ledrom.py                        turn your RHX Plus export into a template
  signd.py  web/index.html                LAN service and UI
  camera.py  verify.py  morning.py        optional: phone camera checks, timed shows
  config.py                               all site settings (from ~/.signboard.toml)
docs/  PROTOCOL.md  templates.md
tools/ scan_private.py  pre-commit        keep personal data out of commits
```

## Contributing

See `CONTRIBUTING.md`. Findings about other RHX controllers, panel sizes and modes are very welcome, especially
anything verified on real hardware.

## License

MIT, see `LICENSE`.
