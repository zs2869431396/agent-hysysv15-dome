"""Prepare the repository contents without creating a repository.

The user asked for the Git repository to be created only once everything else is
finished, and this machine has no `git` on PATH at all, so this script does the part
that can be done safely now and prints exactly what remains:

  * a pre-flight scan for credentials and other things that must not be committed
  * a pre-flight scan for files larger than a sensible limit
  * a summary of what would be committed, and what `.gitignore` excludes

It never runs `git init`, `git add` or `git commit`.

Usage:
    python scripts/prepare_git.py
    python scripts/prepare_git.py --check-only
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent

# Anything matching these must not be committed. The credential patterns are the
# important ones: the API key was pasted into the development conversation, so it
# exists in that transcript even though it appears in no file here.
SECRET_PATTERNS = [
    (re.compile(r'sk-[A-Za-z0-9_\-]{16,}'), 'API key'),
    (re.compile(r'TR_KEY\s*=\s*[\'"]?[A-Za-z0-9_\-]{16,}'), 'TR_KEY assignment'),
    (re.compile(r'Bearer\s+[A-Za-z0-9_\-\.]{20,}'), 'bearer token'),
    (re.compile(r'full address:s:'), 'RDP endpoint'),
    (re.compile(r'password', re.IGNORECASE), 'password (check by hand)'),
]

SKIP_DIRS = {'__pycache__', '.git', '.venv', 'venv', '_archive', 'packages'}
SKIP_SUFFIXES = {'.hsc', '.pyc', '.zip', '.rdp'}

SIZE_LIMIT_MB = 5.0


def walk(root: Path):
    for path in root.rglob('*'):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.is_file() and path.suffix.lower() not in SKIP_SUFFIXES:
            yield path


def scan_secrets(root: Path) -> list[tuple[Path, int, str, str]]:
    """Find credential-shaped lines, skipping the files that define the patterns.

    Without the skip, both scanners report themselves: `full address:s:` appears
    verbatim in this file and in `build_submission.py` as a *pattern*, and the
    packaging tests contain the same string as a fixture. Those are not leaks, and a
    check that cries wolf is a check people learn to ignore.
    """
    findings = []
    for path in walk(root):
        try:
            text = path.read_text(encoding='utf-8', errors='ignore')
        except Exception:                                   # noqa: BLE001
            continue
        if 'SECRET_PATTERNS' in text:
            continue        # a scanner, not a leak
        for number, line in enumerate(text.splitlines(), 1):
            for pattern, label in SECRET_PATTERNS:
                if pattern.search(line):
                    findings.append((path, number, label, line.strip()[:100]))
                    break
    return findings


def scan_large(root: Path) -> list[tuple[Path, float]]:
    limit = SIZE_LIMIT_MB * 1024 * 1024
    big = []
    for path in walk(root):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > limit:
            big.append((path, size / 1024 / 1024))
    return sorted(big, key=lambda item: -item[1])


def summarise(root: Path) -> dict:
    files = list(walk(root))
    by_suffix: dict[str, int] = {}
    for path in files:
        by_suffix[path.suffix or '(none)'] = by_suffix.get(path.suffix or '(none)', 0) + 1
    return {'files': len(files),
            'bytes': sum(p.stat().st_size for p in files if p.is_file()),
            'by_suffix': dict(sorted(by_suffix.items(), key=lambda kv: -kv[1])[:12])}


def git_status() -> tuple[str | None, str | None]:
    """Return (git executable, version) if git is usable, else (None, None).

    Checked through the shell rather than by looking for a known install path: PATH
    is what actually decides whether `git init` will work for the user, and a path
    check would pass while the command still failed.
    """
    import shutil
    import subprocess
    exe = shutil.which('git')
    if not exe:
        # A machine-scope install is not on the PATH of an already-running shell.
        for candidate in (r'C:\Program Files\Git\cmd\git.exe',
                          r'C:\Program Files (x86)\Git\cmd\git.exe'):
            if Path(candidate).is_file():
                exe = candidate
                break
    if not exe:
        return None, None
    try:
        out = subprocess.run([exe, '--version'], capture_output=True, text=True,
                             timeout=20)
        return exe, (out.stdout or '').strip() or None
    except Exception:                                   # noqa: BLE001
        return exe, None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only', action='store_true',
                        help='only run the pre-flight scans')
    args = parser.parse_args(argv)

    print('project: %s' % PROJECT)
    print()

    print('=== pre-flight: credentials ===')
    findings = scan_secrets(PROJECT)
    if findings:
        print('  !! %d suspicious line(s):' % len(findings))
        for path, number, label, line in findings[:20]:
            print('     %s:%d  [%s]  %s'
                  % (path.relative_to(PROJECT), number, label, line))
        print('  Review each one. A real credential must be rotated, not just ignored.')
    else:
        print('  OK  no credential patterns found outside .gitignore')

    print()
    print('=== pre-flight: large files ===')
    big = scan_large(PROJECT)
    if big:
        for path, size in big[:10]:
            print('  %6.1f MB  %s' % (size, path.relative_to(PROJECT)))
    else:
        print('  OK  nothing above %.1f MB' % SIZE_LIMIT_MB)

    print()
    print('=== what would be committed ===')
    summary = summarise(PROJECT)
    print('  files: %d   total: %.2f MB'
          % (summary['files'], summary['bytes'] / 1024 / 1024))
    for suffix, count in summary['by_suffix'].items():
        print('    %-10s %d' % (suffix, count))

    print()
    print('=== must NOT be committed (see .gitignore) ===')
    for name in ('*.rdp', '*.hsc', '.env', '_archive/', 'packages/',
                 'agent-runs/', '__pycache__/'):
        print('    %s' % name)

    if args.check_only:
        return 0

    exe, version = git_status()
    print()
    print('=== git ===')
    if exe:
        print('  found: %s' % exe)
        print('  %s' % (version or '(version could not be read)'))
    else:
        print('  !! git was not found. Install Git for Windows, or add it to PATH.')

    print()
    print('=== remaining manual steps ===')
    if not exe:
        print('  1. install Git for Windows (winget install --id Git.Git -e)')
        print('  2. open a NEW terminal so PATH picks it up')
        offset = 3
    else:
        offset = 1
    print('  %d. git init -b main && git add -A && git commit -m "..."' % offset)
    print('  %d. git status   - confirm no .rdp, .hsc, .env or __pycache__' % (offset + 1))
    print('  %d. git remote add origin <url> && git push -u origin main'
          % (offset + 2))
    print()
    print('  Git needs user.name and user.email before it will commit:')
    print('    git config --global user.name  "..."')
    print('    git config --global user.email "..."')
    print()
    print('  This script deliberately does not run git init/add/commit.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
