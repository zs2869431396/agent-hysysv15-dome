"""Bounded real-model CLI validation; credentials come only from the environment.

No --execute is passed: this checks extraction and compiled plans, not HYSYS.
Stop on the first unexpected result, retaining the transcripts for offline review.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from reactor_agent.extraction import allowed_keys, validate_facts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    root = Path(args.out).resolve()
    root.mkdir(parents=True, exist_ok=True)
    key = os.environ.get('TR_KEY', '')
    if not key:
        raise SystemExit('TR_KEY must be set in the execution environment')
    summary = []
    for label, scenario, flag, expected in (
            ('toluene', 'toluene', '--no-input', 0),
            ('smr', 'smr', '--no-input', 0),
            ('gas-no-input', 'gasification', '--no-input', 3),
            ('gas-defaults', 'gasification', '--accept-defaults', 0)):
        folder = root / label
        if folder.exists():
            raise SystemExit('Use a new output directory; previous runs are preserved')
        transcript = root / ('transcript-' + label + '.jsonl')
        environment = dict(os.environ, TR_TRANSCRIPT=str(transcript),
                           TR_ATTEMPTS='1', TR_MAX_REQUESTS='4', PYTHONUTF8='1')
        completed = subprocess.run(
            [sys.executable, '-m', 'reactor_agent', '--scenario', scenario, flag,
             '--out', str(folder)], cwd=ROOT, env=environment,
            capture_output=True, text=True, encoding='utf-8', errors='replace')
        output = (completed.stdout + completed.stderr).replace(key, '[REDACTED]')
        (root / ('cli-' + label + '.txt')).write_text(output, encoding='utf-8')
        rows = []
        if transcript.exists():
            raw = transcript.read_text(encoding='utf-8')
            if key in raw:
                transcript.write_text(raw.replace(key, '[REDACTED]'), encoding='utf-8')
                raise SystemExit('Credential containment check failed; transcript redacted')
            rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
        errors = []
        if completed.returncode != expected:
            errors.append('unexpected exit code')
        if len(rows) != 1:
            errors.append('expected one model request on the normal path')
        for row in rows:
            try:
                content = json.loads(row['body'])['choices'][0]['message']['content']
                facts = json.loads(content)
                if set(facts) != set(allowed_keys()) or validate_facts(facts):
                    errors.append('response did not satisfy the exact contract')
            except (ValueError, KeyError, TypeError):
                errors.append('response was not a contract JSON document')
        questions = []
        if label == 'gas-no-input' and (folder / 'paused.json').exists():
            questions = json.loads((folder / 'paused.json').read_text(encoding='utf-8'))['questions']
            if {q['id'] for q in questions} != {'q-volumetric-flow', 'q-coal-definition'}:
                errors.append('expected exactly the two independent confirmation questions')
            if any(q.get('default') is None for q in questions):
                errors.append('confirmation default missing')
        elif label == 'gas-no-input':
            errors.append('missing paused state')
        elif (folder / 'state.json').exists():
            state = json.loads((folder / 'state.json').read_text(encoding='utf-8'))
            if state.get('status') != 'READY':
                errors.append('compiled plan not READY')
        else:
            errors.append('missing final state')
        if label == 'gas-defaults' and not errors:
            specs = list(folder.glob('spec-*.json'))
            if len(specs) != 1:
                errors.append('expected one gasification spec')
            else:
                spec = json.loads(specs[0].read_text(encoding='utf-8'))
                feed = spec['feeds']
                feed = feed[0] if isinstance(feed, list) else feed
                if feed['fractions'] != {'Carbon': 0.62, 'Water': 0.38}:
                    errors.append('slurry composition is not coal 62%, water 38%')
                if feed['basis'] != 'mass_fraction':
                    errors.append('wrong slurry composition basis')
                if spec['reactor'].get('solid_carbon') != 'saturation':
                    errors.append('missing saturated solid-carbon route')
        item = {'scenario': label, 'model': os.environ.get('TR_MODEL'),
                'requests': len(rows), 'exit_code': completed.returncode,
                'questions': [q['id'] for q in questions], 'errors': errors}
        summary.append(item)
        (root / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
        print(json.dumps(item), flush=True)
        if errors:
            return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
