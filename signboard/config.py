"""Site settings: which sign, its password, the camera phone, where state lives, where the sign is on Earth.

Read once from a TOML file, then overridden by SIGNBOARD_<KEY> environment variables (upper-case key):
  $SIGNBOARD_CONFIG, else ./signboard.toml, else ~/.signboard.toml
Nothing personal lives in the code; see signboard.example.toml for every key.
"""
import os
import tomllib
from pathlib import Path

DEFAULTS = {
    'sign_address': None,        # BLE address of the RHX controller, e.g. "AA:BB:CC:DD:EE:FF"  # scan:ok
    'sign_password': None,       # the controller ("card") password; set your own in the vendor app first
    'camera_serial': None,       # adb serial (or part of it) of the phone used as a camera; None = first match below
    'camera_models': [],         # adb `device:` names accepted when camera_serial is unset (e.g. "walleye");
                                 # empty: use the only attached adb device
    'state_dir': '~/.signboard',  # signd-photos/, signd-programs/ and signd-*.json state files live here
    'templates_dir': None,       # picture-mode templates imported from your own RHX Plus exports; None = package templates/
    'sign_name': 'LED Sign',     # shown in the web UI and advertised over mDNS
    'allowed_hosts': [],         # extra host names signd answers to (it already accepts its LAN address and names)
    'max_brightness': 16,        # upper limit for automatic brightness (auto-light)
    'camera_panel_box': None,    # panel in the camera photo, fractions: [x0, y0, x1, y1] or 4 corners [x, y, ...]
    'latitude': None,            # for sunrise/sunset brightness (signd --auto-light); None disables it
    'longitude': None,
}


def _load():
    cfg = dict(DEFAULTS)
    env = os.environ.get('SIGNBOARD_CONFIG')
    if env and not Path(env).expanduser().is_file():
        raise RuntimeError(f'signboard: SIGNBOARD_CONFIG={env} does not exist')
    for p in ((env,) if env else ('signboard.toml', '~/.signboard.toml')):
        if p and Path(p).expanduser().is_file():
            with open(Path(p).expanduser(), 'rb') as f:
                cfg.update(tomllib.load(f))
            cfg['_file'] = str(Path(p).expanduser())
            break
    for k, v in DEFAULTS.items():
        env = os.environ.get('SIGNBOARD_' + k.upper())
        if env is not None:
            cfg[k] = ([float(x) for x in env.split(',')] if k == 'camera_panel_box' else float(env) if k in ('latitude', 'longitude') else int(env) if k == 'max_brightness'
                      else [x.strip() for x in env.split(',') if x.strip()] if k in ('camera_models', 'allowed_hosts') else env)
    return cfg


CFG = _load()


def get(key):
    return CFG.get(key)


def require(key, hint=''):
    v = CFG.get(key)
    if v in (None, ''):
        where = CFG.get('_file', '~/.signboard.toml (none found)')
        raise RuntimeError(f'signboard: "{key}" is not set. Add it to {where} or set SIGNBOARD_{key.upper()}. {hint}'.strip())
    return v


def state_dir() -> Path:
    d = Path(CFG['state_dir']).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_file(name: str) -> Path:
    """signd-<name> in the state directory (signd-photos, signd-programs, signd-schedule.json, ...)."""
    return state_dir() / f'signd-{name}'
