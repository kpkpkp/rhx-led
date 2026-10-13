"""Sun-driven brightness policy (location from signboard.config: latitude/longitude).

solar_elevation() is the NOAA low-precision solar position (about 0.3 degrees, plenty here). policy() maps the sun to
(power, level): nothing is lit before sunrise or in the dark, and the display steps down through twilight.

  elevation >= 10      level 16  full daylight
  0 .. 10              level 10  low sun
  -6 .. 0              level 4   civil twilight (dusk only)
  -12 .. -6            level 2   nautical twilight (dusk only; night-light territory)
  below -12            off
  morning, below 0     off       "do not illuminate until daylight"
"""
import math
from datetime import datetime, timezone

from signboard import config
LAT, LON = config.get('latitude'), config.get('longitude')   # signboard.config; both None disables auto-light


def solar_elevation(when=None, lat=LAT, lon=LON):
    """Degrees above the horizon (no refraction correction), and whether it is before solar noon."""
    t = (when or datetime.now(timezone.utc)).astimezone(timezone.utc)
    d = t.timetuple().tm_yday
    g = 2 * math.pi / 365 * (d - 1 + (t.hour - 12) / 24)
    eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g) - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    minutes = t.hour * 60 + t.minute + t.second / 60
    tst = (minutes + eqt + 4 * lon) % 1440            # true solar time, minutes
    ha = math.radians(tst / 4 - 180)
    la = math.radians(lat)
    elev = math.degrees(math.asin(math.sin(la) * math.sin(decl) + math.cos(la) * math.cos(decl) * math.cos(ha)))
    return elev, tst < 720


def policy(elev, morning):
    if elev >= 10: return True, 16
    if elev >= 0: return True, 10
    if morning: return False, None
    if elev >= -6: return True, 4
    if elev >= -12: return True, 2
    return False, None


if __name__ == '__main__':
    from datetime import timedelta
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    last = None
    for m in range(0, 24 * 60, 15):
        t = now + timedelta(minutes=m)
        e, morning = solar_elevation(t)
        p = policy(e, morning)
        if p != last:
            local = t - timedelta(hours=6)       # MDT, enough for a printout
            print(f'{local:%H:%M} MDT  sun {e:6.1f} deg  ->', 'off' if not p[0] else f'on, level {p[1]}')
            last = p
