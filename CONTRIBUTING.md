# Contributing

- **Keep personal data out.** Install the hook once: `cp tools/pre-commit .git/hooks/pre-commit`. It blocks commits
  containing BLE/MAC addresses, LAN IPs, emails, user paths, password literals, and any word in your private
  `~/.signboard-private-words` list. Settings belong in `~/.signboard.toml`, never in code.
- **Never commit vendor files**: apps, exports, templates, captured programs. Describe formats instead.
- **Verify on hardware** before documenting behaviour as fact, and say how (photo, video, readback). Mark guesses
  as guesses in `docs/PROTOCOL.md`.
- **Safe experiments only.** Never send firmware-update, factory-init, WiFi-credential or password-change commands
  to someone else's controller, or to your own without a recovery plan. A bad program upload is harmless: upload a
  good one.
- `python -m signboard.selftest` must pass.
