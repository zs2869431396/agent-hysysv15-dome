"""Build a small, allowlisted remote-test ZIP without caches or connection files.

Lives in scripts/, so the project root is one level up. Everything inside the ZIP
is addressed relative to that root, which is also where the validation runner
expects to find it.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

PROJECT = Path(__file__).resolve().parent.parent


def main():
    name = 'hysys-agent-repair-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:6]
    target = PROJECT.parent / (name + '.zip')
    paths = sorted((PROJECT / 'hysys_tools').glob('*.py'))
    paths += sorted((PROJECT / 'hysys_tools' / 'golden').glob('*.json'))
    paths += [PROJECT / 'probe-runs/equilibrium-20261003-101446/probe-equilibrium-7.json']
    paths += [PROJECT / 'tool-layer-runs/native-flow-20261003-091907-5550b747/gasification/result.json']
    paths += sorted((PROJECT / 'docs').glob('*.md'))
    paths += [PROJECT / 'scripts' / 'build_release.py']
    paths += [PROJECT / 'scripts' / 'validate_native_flow.py']
    paths += [PROJECT / filename for filename in (
        'README.md', 'PROJECT_PLAN.md', 'requirements.txt', 'baseline_expected.json',
        'Run-Remote-Validation.cmd', 'Run-Native-Flow-Validation.cmd', 'Run-Offline-Checks.cmd', 'Run-Tool-Layer.cmd')]
    historical = PROJECT / 'tool-layer-runs' / '20261002-111439'
    paths += sorted(historical.glob('*/result.json'))
    contents = {str(path.relative_to(PROJECT)).replace('\\', '/'): path.read_bytes()
                for path in paths}
    # Local offline verification snapshot, taken straight from verification/ rather
    # than by hunting for a matching acceptance directory under tool-layer-runs.
    # Those directories get archived once their evidence has been extracted, and a
    # release builder must not silently drop the snapshot when that happens.
    verification = PROJECT / 'verification'
    if (verification / 'local-summary.json').is_file():
        for path in sorted(verification.rglob('*')):
            if path.is_file():
                key = ('verification/'
                       + str(path.relative_to(verification)).replace('\\', '/'))
                contents[key] = path.read_bytes()
    manifest = {filename: hashlib.sha256(data).hexdigest() for filename, data in contents.items()}
    contents['RELEASE_MANIFEST.json'] = json.dumps(manifest, indent=2).encode('utf-8')
    with ZipFile(target, 'x', ZIP_DEFLATED) as archive:
        for filename, data in contents.items():
            archive.writestr(name + '/' + filename, data)
    with ZipFile(target) as archive:
        assert archive.testzip() is None
        for filename, expected in manifest.items():
            assert hashlib.sha256(archive.read(name + '/' + filename)).hexdigest() == expected
    print(str(target).encode('ascii', 'backslashreplace').decode('ascii'))
    print('Files: %d; ZIP integrity and file hashes verified.' % len(contents))


if __name__ == '__main__':
    main()
