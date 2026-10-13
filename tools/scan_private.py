"""scan_private.py [ROOT] : flag anything that identifies this household before it goes public.
Generic patterns live here; site-specific words come from ~/.signboard-private-words (one per line, never committed).
Exit 1 if anything is found (use as a pre-commit hook)."""
import re, sys
from pathlib import Path
GENERIC = [
    (r'\b(?:[0-9A-F]{2}:){5}[0-9A-F]{2}\b', 'BLE/MAC address'),
    (r'\b(?:192\.168|10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b', 'private IP address'),
    (r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}', 'email address'),
    (r'adb-[A-Z0-9]{8,}', 'adb device name'),
    (r'(?i)C:[\/]+Users[\/]+\w+', 'Windows user path'),
    (r'(?i)/home/\w+|/Users/\w+', 'home path'),
    (r'''(?i)(password|passwd|pwd)\w*\s*[:=]\s*["'][^"']{4,}["']''', 'password literal'),
]
SKIP_EXT = {'.png', '.jpg', '.gif', '.bin', '.apk', '.ico', '.pyc'}
def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else '.')
    words = []
    wf = Path.home() / '.signboard-private-words'
    if wf.exists():
        words = [w.strip() for w in wf.read_text(encoding='utf-8').splitlines() if w.strip() and not w.startswith('#')]
    pats = GENERIC + [(re.escape(w), 'site word') for w in words]
    hits = 0
    for f in sorted(root.rglob('*')):
        if not f.is_file() or '.git' in f.parts: continue
        if f.suffix.lower() in SKIP_EXT:
            print(f'BINARY  {f.relative_to(root)} (check by hand)'); continue
        try: text = f.read_text(encoding='utf-8')
        except UnicodeDecodeError: print(f'BINARY  {f.relative_to(root)} (not text)'); continue
        for n, line in enumerate(text.splitlines(), 1):
            if 'scan:ok' in line: continue
            for p, what in pats:
                m = re.search(p, line, flags=0 if what != 'site word' else re.I)
                if m:
                    hits += 1; print(f'{what:18s} {f.relative_to(root)}:{n}: {line.strip()[:110]}')
    print(f'{hits} finding(s)'); sys.exit(1 if hits else 0)
if __name__ == '__main__':
    main()
