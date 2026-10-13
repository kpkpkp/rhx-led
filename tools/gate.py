"""gate.py : verify a commit or pull request before it goes to the public repo.

  python tools/gate.py                      # HEAD
  python tools/gate.py --pr 12 --hil        # GitHub PR #12, including the panel tests
  python tools/gate.py REF --templates DIR --samples DIR

Checks the exact tree of REF in a throwaway git worktree, never your working copy:
  privacy   tools/scan_private.py over every file of REF
  build     a fresh venv, `pip install REF`, the signd and rhx-sign entry points start
  tests     pytest against the *installed* package (the build), with your templates/samples when given
  hil       (--hil) tools/hil.py on the real panel, judged by camera photos
The result is stamped into .git/rhx-gate/<sha>.json; tools/pre-push refuses to push a commit without a passing
stamp. Templates and samples default to RHX_TEST_TEMPLATES / RHX_TEST_SAMPLES.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def git(*a, cwd=ROOT, check=True):
    return subprocess.run(['git', *a], cwd=cwd, capture_output=True, text=True, check=check).stdout.strip()


def run(name, cmd, cwd, log, env=None, timeout=1800):
    t0 = time.time()
    print(f'---- {name}: {" ".join(map(str, cmd))}', flush=True)
    with open(log, 'w', encoding='utf-8') as f:
        p = subprocess.run(list(map(str, cmd)), cwd=cwd, stdout=f, stderr=subprocess.STDOUT, env=env, timeout=timeout)
    tail = Path(log).read_text(encoding='utf-8', errors='replace').strip().splitlines()[-12:]
    print('\n'.join('    ' + l for l in tail))
    ok = p.returncode == 0
    print(f"{'PASS' if ok else 'FAIL'}  {name} ({time.time() - t0:.0f} s, log {log})", flush=True)
    return {'ok': ok, 'seconds': round(time.time() - t0), 'log': str(log)}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('ref', nargs='?', default='HEAD')
    ap.add_argument('--pr', type=int, help='GitHub pull request number (fetched from origin)')
    ap.add_argument('--hil', action='store_true', help='also run the panel tests (tools/hil.py)')
    ap.add_argument('--templates', default=os.environ.get('RHX_TEST_TEMPLATES'))
    ap.add_argument('--samples', default=os.environ.get('RHX_TEST_SAMPLES'))
    ap.add_argument('--keep', action='store_true', help='keep the worktree and venv')
    a = ap.parse_args()

    if a.pr:
        git('fetch', 'origin', f'pull/{a.pr}/head')
        a.ref = 'FETCH_HEAD'
    sha = git('rev-parse', a.ref)
    work = Path(tempfile.mkdtemp(prefix=f'rhx-gate-{sha[:8]}-'))
    tree, logs = work / 'tree', work / 'logs'
    logs.mkdir()
    git('worktree', 'add', '--detach', str(tree), sha)
    print(f'gate {sha[:8]} ({a.ref}{f", PR #{a.pr}" if a.pr else ""}) in {work}')
    res = {'sha': sha, 'ref': a.ref, 'pr': a.pr, 'at': time.strftime('%Y-%m-%d %H:%M:%S'), 'checks': {}}
    try:
        res['checks']['privacy'] = run('privacy', [sys.executable, ROOT / 'tools' / 'scan_private.py', tree], tree, logs / 'privacy.log')

        venv = work / 'venv'
        py = venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        b = run('build', [sys.executable, '-m', 'venv', venv], work, logs / 'venv.log')
        if b['ok']:
            b = run('build', [py, '-m', 'pip', 'install', '-q', str(tree), 'pytest'], work, logs / 'build.log')
        if b['ok']:
            scripts = venv / ('Scripts' if os.name == 'nt' else 'bin')
            for exe in ('signd', 'rhx-sign'):
                e = run(f'entry point {exe}', [scripts / exe, '--help'], work, logs / f'{exe}.log', timeout=60)
                b = b if e['ok'] else e
        res['checks']['build'] = b

        if b['ok']:
            cmd = [py, '-m', 'pytest', '-q', '-rs', '-p', 'no:cacheprovider', tree / 'tests']
            cmd += ['--templates', a.templates] if a.templates else []
            cmd += ['--samples', a.samples] if a.samples else []
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
            res['checks']['tests'] = run('tests (installed package)', cmd, work, logs / 'tests.log', env=env)   # cwd=work: imports the build, not the tree
            skipped = sum(l.count('SKIPPED') for l in (logs / 'tests.log').read_text(encoding='utf-8', errors='replace').splitlines())
            res['checks']['tests']['skipped'] = skipped
        else:
            res['checks']['tests'] = {'ok': False, 'error': 'build failed'}

        if a.hil:
            if all(c['ok'] for c in res['checks'].values()):
                cmd = [py, tree / 'tools' / 'hil.py', '--out', work / 'hil'] + (['--templates', a.templates] if a.templates else [])
                res['checks']['hil'] = run('hil (panel)', cmd, tree, logs / 'hil.log', timeout=900)
                rep = work / 'hil' / 'hil-report.json'
                if rep.exists():
                    keep = ROOT / '.git' / 'rhx-gate' / f'{sha}-hil'
                    shutil.copytree(work / 'hil', keep, dirs_exist_ok=True, ignore=shutil.ignore_patterns('signd-*'))
                    res['checks']['hil']['report'] = str(keep / 'hil-report.json')
            else:
                res['checks']['hil'] = {'ok': False, 'error': 'skipped: offline checks failed (the panel is not touched)'}
    finally:
        git('worktree', 'remove', '--force', str(tree), check=False)
        if not a.keep:
            shutil.rmtree(work / 'venv', ignore_errors=True)

    res['ok'] = all(c['ok'] for c in res['checks'].values())
    res['hil'] = bool(res['checks'].get('hil', {}).get('ok'))
    stamp = ROOT / '.git' / 'rhx-gate' / f'{sha}.json'
    stamp.parent.mkdir(exist_ok=True)
    stamp.write_text(json.dumps(res, indent=2), encoding='utf-8')
    print(f"\nGATE {'PASSED' if res['ok'] else 'FAILED'} for {sha[:8]}{' with panel tests' if res['hil'] else ' (no panel tests)'}; "
          f'stamp {stamp}; logs {logs}')
    sys.exit(0 if res['ok'] else 1)


if __name__ == '__main__':
    main()
