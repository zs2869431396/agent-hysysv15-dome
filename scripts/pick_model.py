"""Pick the best model for the extraction stage, using the improved call pattern.

Earlier benchmarking established three things that this script bakes in:

  1. the model-facing schema must be flat with arrays, not open dictionaries;
  2. a stronger prompt ("leaving a stated value empty is an error") is worth about
     +11 points at no cost;
  3. gaps are RANDOM, so validate the required fields and ask again - measured
     83% first-try completeness, 100% within three tries.

It also excludes the gasification scenario from scoring: that request is genuinely
under-specified, so a model that cannot fill it is behaving correctly. What matters
there is only whether `Nm3/h` survives untouched, which is checked separately.

Calls are SERIAL with a gap, because the endpoint rate-limits bursts.

Credentials come from the environment (TR_BASE / TR_KEY). Results go to _demo/.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bench_llm as B                                     # noqa: E402

OUT = ROOT / '_demo' / 'model-pick.json'
GAP = float(os.environ.get('TR_GAP', '2.5'))
ROUNDS = int(os.environ.get('TR_ROUNDS', '2'))

# Stronger system prompt: the earlier experiment showed this alone moves accuracy
# from 78% to 89% and removes the occasional collapse.
B.SYSTEM = (
    B.SYSTEM
    + ' CRITICAL: every figure the user DID state - flow, temperature, pressure, '
      'conversion, and every named substance - MUST appear in its field. Only use '
      'missing_information for things the user truly did not state. Leaving a '
      'stated value empty is an error.')

REQUIRED = ('feed_total', 'feed_temperature', 'feed_pressure', 'conversion_percent')

# Wider net than the flash round: include the max/pro tiers, since the user asked
# for the best model rather than only the fastest. Override with TR_MODELS=a,b,c to
# probe a smaller set - a full sweep is slow because calls must be serial.
_DEFAULT_CANDIDATES = [
    'deepseek-v4-flash-0731',     # the current baseline (fast)
    'deepseek-v4-pro-0813',
    'qwen3.8-max', 'qwen3.7-max', 'qwen3.8-flash', 'qwen3.7-flash',
    'glm-5.3', 'glm-5.2', 'glm-5.3-flash',
    'kimi-k3', 'kimi-k2.6',
    'mimo-v2.6-pro', 'mimo-v2.6-flash',
    'seed-2.1-pro', 'seed-2.1-turbo',
    'longcat-2.0',
]
CANDIDATES = [m.strip() for m in os.environ.get('TR_MODELS', '').split(',')
              if m.strip()] or _DEFAULT_CANDIDATES

# Gasification is scored on the single thing it can be scored on.
GAS_CRITICAL = 'Nm3 preserved, not converted'


def gaps(obj: dict) -> list[str]:
    return [k for k in REQUIRED if obj.get(k) is None]


def extract_validated(model: str, text: str, max_tries: int = 3):
    """Ask, check the required fields, ask again if the model left one empty."""
    last = None
    obj = None
    for attempt in range(max_tries):
        obj, _, note = B.extract(model, text)
        if obj is not None and not gaps(obj):
            return obj, attempt + 1, None
        last = ('missing %s' % gaps(obj)) if obj is not None else note
        time.sleep(GAP)
    return obj, max_tries, last


def run_model(model: str) -> dict:
    row: dict = {'model': model, 'cases': {}}
    for name, text, scorer in B.CASES:
        results = []
        for _ in range(ROUNDS):
            obj, tries, note = extract_validated(model, text)
            if obj is None:
                results.append({'score': 0.0, 'tries': tries, 'note': note,
                                'seconds': 0.0, 'failed': []})
            else:
                checks = scorer(obj)
                results.append({
                    'score': sum(1 for _, ok in checks if ok) / len(checks),
                    'tries': tries, 'note': None, 'seconds': 0.0,
                    'failed': [k for k, ok in checks if not ok]})
            time.sleep(GAP)
        row['cases'][name] = results

    # Score on the two scenarios that are actually answerable.
    scoring = [c for n, c in row['cases'].items() if n != 'gasification']
    ratios = [r['score'] for case in scoring for r in case]
    row['score'] = round(sum(ratios) / len(ratios), 3) if ratios else 0.0
    gas = row['cases'].get('gasification', [])
    row['gasification_kept_nm3'] = all(
        GAS_CRITICAL not in r.get('failed', []) for r in gas) if gas else False
    all_runs = [r for c in row['cases'].values() for r in c]
    row['mean_tries'] = round(sum(r['tries'] for r in all_runs) / len(all_runs), 2) \
        if all_runs else 0
    row['crashes'] = sum(1 for r in all_runs if r['note'])
    return row


def main() -> int:
    if not B.BASE or not B.KEY:
        print('TR_BASE and TR_KEY must be set in the environment')
        return 2

    try:
        import urllib.request
        request = urllib.request.Request(B.BASE.rstrip('/') + '/models')
        request.add_header('Authorization', 'Bearer ' + B.KEY)
        with urllib.request.urlopen(request, timeout=60) as response:
            available = {m.get('id') for m in json.loads(response.read().decode())['data']}
    except Exception as exc:                        # noqa: BLE001
        print('could not list models: %s' % exc)
        return 2

    models = [m for m in CANDIDATES if m in available]
    print('probing %d models, serial with %.1fs gaps, %d rounds per scenario'
          % (len(models), GAP, ROUNDS))
    print('this takes a while; the endpoint throttles bursts\n')

    rows = []
    for index, model in enumerate(models, 1):
        started = time.time()
        row = run_model(model)
        row['wall_seconds'] = round(time.time() - started, 1)
        rows.append(row)
        print('  [%2d/%d] %-24s score=%.2f  tries=%.2f  nm3=%s  %5.1fs'
              % (index, len(models), model, row['score'], row['mean_tries'],
                 'ok' if row['gasification_kept_nm3'] else 'LOST',
                 row['wall_seconds']))

    rows.sort(key=lambda r: (-r['score'], r['mean_tries'], r['wall_seconds']))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({'gap_seconds': GAP, 'rounds': ROUNDS,
                               'required_fields': list(REQUIRED),
                               'results': rows}, indent=2, ensure_ascii=False),
                   encoding='utf-8')

    print()
    print('%-24s %6s %6s %6s %7s  %s'
          % ('model', 'score', 'tries', 'crash', 'wall_s', 'toluene / smr'))
    for row in rows:
        parts = []
        for name in ('toluene', 'smr'):
            runs = row['cases'].get(name, [])
            parts.append('%s' % ','.join('%.0f%%' % (r['score'] * 100) for r in runs))
        print('%-24s %6.2f %6.2f %6d %7.1f  %s  (nm3 %s)'
              % (row['model'], row['score'], row['mean_tries'], row['crashes'],
                 row['wall_seconds'], ' / '.join(parts),
                 'ok' if row['gasification_kept_nm3'] else 'LOST'))
    print()
    print('score excludes the under-specified gasification scenario; for that one only')
    print('"Nm3 preserved" is checked, since filling it in would be a fabrication.')
    print('full results: %s' % OUT)
    return 0


if __name__ == '__main__':
    sys.exit(main())
