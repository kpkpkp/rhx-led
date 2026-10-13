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
- **Tests.** `pip install -e .[test]` then `pytest`. No hardware needed: `tests/fake_controller.py` emulates the
  controller (login, uploads, erase-on-78e1, stale uploads, dropped links), and template tests skip unless you pass
  `--templates DIR` (your own exports, docs/templates.md). A bug fix should come with a test that fails without it.
  GitHub Actions runs the same suite on every pull request.

## Before anything reaches the public repo (maintainers)

`python tools/gate.py --pr N --hil` checks out the PR's exact commit in a throwaway worktree and runs, in order:
the privacy scan, a clean `pip install` with both entry points, the tests against the installed package (with your
templates and the reference captures: `--templates`, `--samples` or `RHX_TEST_TEMPLATES`, `RHX_TEST_SAMPLES`),
then `tools/hil.py` on the real panel. The panel run backs up the sign's program, works at brightness 1, judges every
step by a camera photo (text with a BLE read-back, a picture, two native pans, power off/on keeping the program), and
always puts the program and the power state back. Results are stamped in `.git/rhx-gate/<sha>.json`, and
`tools/pre-push` (`cp tools/pre-push .git/hooks/pre-push`) refuses to push a commit without a passing stamp.
