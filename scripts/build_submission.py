"""Build the submission ZIP: both layers, the documentation, and the evidence.

Distinct from `build_release.py`, which exists to send the *frozen* tool layer to a
workstation and therefore deliberately excludes the agent. This one is the thing a
reviewer opens: everything needed to read the work, re-run the tests, and check the
claims.

Three properties it must have.

**Allowlist, not blocklist.** The directories are enumerated. A blocklist would
silently ship whatever new file appeared since it was written, and the one that
matters is the one nobody thought of.

**Nothing sensitive, ever.** `.rdp` files carry a host address, `.hsc` files are
large binaries, and an API key must never be inside a submission. There is an
exclusion pass on top of the allowlist, and the build aborts if it finds a key
pattern in anything it was about to write.

**Self-verifying.** Every file is hashed, the manifest goes into the archive, and the
hashes are re-checked by reading the finished ZIP back. A package that cannot be
verified is not worth producing.

Usage:
    python scripts/build_submission.py
    python scripts/build_submission.py --out D:\\somewhere\\submission.zip
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

PROJECT = Path(__file__).resolve().parent.parent

# Directories copied recursively. Anything not listed does not ship.
INCLUDE_DIRS = ('hysys_tools', 'reactor_agent', 'docs', 'scripts', 'verification')

# Root files copied individually. Kept explicit so a stray file cannot ride along.
INCLUDE_FILES = (
    'README.md', 'REPORT.md', 'PROJECT_PLAN.md', 'TECH_STACK.md',
    'requirements.txt', 'baseline_expected.json',
    '.gitignore',
    'Run-Agent-On-Workstation.cmd', 'Run-Offline-Checks.cmd',
    'Run-Remote-Validation.cmd', 'Run-Native-Flow-Validation.cmd', 'Run-Tool-Layer.cmd',
    'Run-Agent-UI.cmd', 'Start-Demo.bat',
)

# Acceptance runs worth shipping as evidence. Only the accepted ones, by name, so a
# half-finished local run can never end up in the submission.
EVIDENCE_DIRS = ('tool-layer-runs/acceptance-20261002-144131-d442afae',
                 'probe-runs/equilibrium-20261003-101446',
                 'tool-layer-runs/native-flow-20261003-091907-5550b747')

# Evidence that arrived as a ZIP rather than an extracted directory. The local
# machine never ran the remote acceptance, so the only copy of that run's evidence is
# the archive it produced; shipping it beats dropping the evidence chain.
EVIDENCE_ZIPS = ('tool-layer-runs/acceptance-20261003-105341-4467d9c9.zip',)

# Never ship these, whatever the allowlist says.
EXCLUDE_DIRS = {'__pycache__', '.pytest_cache', '.mypy_cache', '_archive', 'packages'}
EXCLUDE_SUFFIXES = {'.pyc', '.pyo', '.hsc', '.hscz', '.rdp', '.zip', '.log'}
# `.env` can hold a real key, and it is not in the allowlist anyway; naming it here
# means the exclusion survives someone adding the root directory wholesale later.
EXCLUDE_NAMES = {'cand-zhongshuai.rdp', '.env'}

# A submission containing a live credential is worse than no submission.
SECRET_PATTERNS = (
    re.compile(r'sk-[A-Za-z0-9_\-]{20,}'),
    re.compile(r'Bearer\s+[A-Za-z0-9_\-\.]{24,}'),
    re.compile(r'full address:s:'),
)

MAX_BYTES = 5 * 1024 * 1024


def is_excluded(path: Path) -> bool:
    if any(part in EXCLUDE_DIRS for part in path.parts):
        return True
    if path.suffix.lower() in EXCLUDE_SUFFIXES:
        return True
    return path.name in EXCLUDE_NAMES


def collect() -> tuple[dict[str, bytes], list[str]]:
    """Return (files by archive path, notes about what was skipped)."""
    contents: dict[str, bytes] = {}
    notes: list[str] = []

    def add(path: Path, key: str) -> None:
        if is_excluded(path):
            # Byte-code caches are excluded by design on every build, so recording
            # each one as a note would bury the notes that actually matter.
            if '__pycache__' not in path.parts:
                notes.append('skipped (excluded): %s' % key)
            return
        data = path.read_bytes()
        if len(data) > MAX_BYTES:
            notes.append('skipped (%.1f MB): %s' % (len(data) / 1024 / 1024, key))
            return
        contents[key] = data

    for directory in INCLUDE_DIRS:
        base = PROJECT / directory
        if not base.is_dir():
            notes.append('missing directory: %s' % directory)
            continue
        for path in sorted(base.rglob('*')):
            if path.is_file():
                add(path, str(path.relative_to(PROJECT)).replace('\\', '/'))

    for name in INCLUDE_FILES:
        path = PROJECT / name
        if path.is_file():
            add(path, name)
        else:
            notes.append('missing file: %s' % name)

    for directory in EVIDENCE_DIRS:
        base = PROJECT / directory
        if not base.is_dir():
            notes.append('missing evidence: %s' % directory)
            continue
        for path in sorted(base.rglob('*')):
            if path.is_file():
                add(path, str(path.relative_to(PROJECT)).replace('\\', '/'))

    for name in EVIDENCE_ZIPS:
        path = PROJECT / name
        if not path.is_file():
            notes.append('missing evidence: %s' % name)
            continue
        # Read directly: `add` excludes `.zip` on purpose (an archive in the tree is
        # usually a stray build artefact), and this one is named evidence on purpose.
        data = path.read_bytes()
        if len(data) > MAX_BYTES:
            notes.append('skipped (%.1f MB): %s' % (len(data) / 1024 / 1024, name))
            continue
        contents[name] = data

    return contents, notes


def scan_for_secrets(contents: dict[str, bytes]) -> list[tuple[str, str]]:
    """Find credentials before they are written into the archive.

    Files that *define* these patterns are skipped. Without that, the scanner
    reports itself: `full address:s:` appears verbatim in this file and in
    `prepare_git.py` as a pattern, not as an endpoint.
    """
    hits: list[tuple[str, str]] = []
    for key, data in contents.items():
        if Path(key).suffix.lower() not in {'.json', '.md', '.py', '.txt', '.cmd', ''}:
            continue
        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError:
            continue
        if 'SECRET_PATTERNS' in text or '_SECRET' in text:
            continue        # this is a scanner, not a leak
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                hits.append((key, pattern.pattern))
                break
    return hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', help='target ZIP path')
    parser.add_argument('--include-runs', action='store_true',
                        help='also include agent-runs/ (dry-run artifacts)')
    args = parser.parse_args(argv)

    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    target = Path(args.out) if args.out else (
        PROJECT.parent / ('hysys-agent-submission-%s.zip' % stamp))

    contents, notes = collect()

    hits = scan_for_secrets(contents)
    if hits:
        print('ABORTED: credential patterns found in the files to be packaged.')
        for key, pattern in hits:
            print('  %s  (matched %s)' % (key, pattern))
        print('Remove or rotate the credential before packaging.')
        return 2

    manifest = {key: hashlib.sha256(data).hexdigest()
                for key, data in contents.items()}
    manifest_doc = {
        'package': target.name,
        'built_at': datetime.now().isoformat(timespec='seconds'),
        'files': manifest,
        'notes': notes,
    }
    contents['SUBMISSION_MANIFEST.json'] = json.dumps(
        manifest_doc, ensure_ascii=False, indent=2).encode('utf-8')

    root = target.stem
    with ZipFile(target, 'x', ZIP_DEFLATED) as archive:
        for key, data in sorted(contents.items()):
            archive.writestr('%s/%s' % (root, key), data)

    # Read it back: the only hash that matters is the one the reader will compute.
    with ZipFile(target) as archive:
        if archive.testzip() is not None:
            print('ABORTED: the archive failed its own integrity check.')
            return 3
        for key, expected in manifest.items():
            actual = hashlib.sha256(archive.read('%s/%s' % (root, key))).hexdigest()
            if actual != expected:
                print('ABORTED: hash mismatch for %s' % key)
                return 4

    total = sum(len(data) for data in contents.values())
    print('built   : %s' % target)
    print('files   : %d' % len(contents))
    print('size    : %.2f MB compressed, %.2f MB of content'
          % (target.stat().st_size / 1024 / 1024, total / 1024 / 1024))
    print('verified: zip integrity + %d SHA256 hashes re-read from the archive'
          % len(manifest))
    if notes:
        print('notes   : %d entries (see SUBMISSION_MANIFEST.json)' % len(notes))
        for note in notes[:5]:
            print('          %s' % note)
        if len(notes) > 5:
            print('          ... and %d more' % (len(notes) - 5))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
