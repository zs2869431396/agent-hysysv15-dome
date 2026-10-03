"""Restore only line endings that reproduce the accepted runtime hashes."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from reactor_agent.capabilities import ACCEPTED_SHA256, acceptance_state


def accepted_bytes(raw: bytes, expected: str) -> bytes | None:
    lf = raw.replace(b'\r\n', b'\n')
    for candidate in (raw, lf, lf.replace(b'\n', b'\r\n')):
        if hashlib.sha256(candidate).hexdigest() == expected:
            return candidate
    return None


def restore(tool_dir: Path, expected: dict[str, str], apply: bool = False) -> bool:
    changes = {}
    bad = []
    for name, digest in expected.items():
        try:
            raw = (tool_dir / name).read_bytes()
        except OSError:
            bad.append(name)
            continue
        repaired = accepted_bytes(raw, digest)
        if repaired is None:
            bad.append(name)
        elif repaired != raw:
            changes[name] = repaired
    print('Differences beyond line endings:', bad)
    if bad:
        print('STOPPED. No files were changed.')
        return False
    print('Line endings to restore:', list(changes))
    if apply:
        for name, data in changes.items():
            (tool_dir / name).write_bytes(data)
        print('Restored accepted bytes; unchanged files were preserved.')
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='apply verified repairs')
    args = parser.parse_args()
    if not restore(ROOT / 'hysys_tools', ACCEPTED_SHA256, args.apply):
        return 1
    if args.apply:
        acceptance_state.cache_clear()
        state = acceptance_state()
        print('Acceptance holds:', state['holds'])
        print('Remaining mismatches:', state['mismatched'])
        return 0 if state['holds'] else 1
    print('Check only. Use --apply to restore verified line endings.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
