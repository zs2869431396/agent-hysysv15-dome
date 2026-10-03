"""Benchmark candidate models on SPEED and on CORRECTNESS.

Design note (learned the hard way): the schema handed to a model must be SMALL and
must avoid open dictionaries. Handing a model the full three-layer contract made it
emit mangled keys, because `dict[str, float]` becomes `additionalProperties`, and
under `strict` mode the model has to invent the keys itself. The contract that works
is a flat object whose repeating parts are ARRAYS of {name, value}:

    stoichiometry: dict[str, float]      ->  species: [{name, coefficient}]
    fractions: dict[str, float]          ->  [{name, fraction}]

Two further rules follow from the same lesson:
  * ask the model to COPY numbers and units as written, never to normalise them;
    one candidate turned "50%" into 0.5 and another read "62wt%" as a conversion;
  * give it a `missing_information` array, so "do not invent" becomes something it
    can positively do rather than something it must remember not to do.

Unit normalisation belongs in deterministic code, not in the prompt.

Credentials come from the environment (TR_BASE / TR_KEY). Results go to
_demo/llm-eval.json.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BASE = os.environ.get('TR_BASE', '')
KEY = os.environ.get('TR_KEY', '')
OUT = ROOT / '_demo' / 'llm-eval.json'
MAX_TOKENS = int(os.environ.get('TR_MAX_TOKENS', '3000'))

CANDIDATES = [
    'deepseek-v4-flash-0731', 'deepseek-flash', 'qwen3.8-flash', 'qwen3.7-flash',
    'glm-5.3-flash', 'glm-5.3-flashx', 'mimo-v2.6-flash', 'mimo-v2.5-pro',
    'minimax-m2.7', 'seed-2.1-turbo',
]

SYSTEM = (
    'Extract ONLY what the user literally stated. '
    'Copy every number and unit EXACTLY as written - do not convert units, '
    'do not turn a percentage into a fraction or vice versa. '
    'List everything the user did NOT state in missing_information. '
    'Never invent a value.')


def _obj(properties: dict, required: list[str] | None = None) -> dict:
    return {'type': 'object', 'properties': properties,
            'required': required if required is not None else list(properties),
            'additionalProperties': False}


# The model-facing contract. Deliberately flat, array-based, and free of anything
# the model should not decide (no reactor type, no assumptions, no unit conversion).
#
# Field names must cover the SEMANTICS the source text actually uses, or the model
# has nowhere to put a value. Steam reforming was the case that proved it: the exam
# gives "压力 13.5 bar" per operating case, not as a feed pressure, so with only
# `feed_pressure` in the schema the model left pressure empty and the value was lost.
EXTRACTION_SCHEMA = _obj({
    'species': {'type': 'array', 'items': {'type': 'string'}},
    'conversion_percent': {'type': ['number', 'null']},
    'feed_total': {'type': ['number', 'null']},
    'feed_unit': {'type': 'string'},
    'feed_temperature': {'type': ['number', 'null']},
    'feed_temperature_unit': {'type': 'string'},
    'feed_pressure': {'type': ['number', 'null']},
    'feed_pressure_unit': {'type': 'string'},
    # Pressure stated per operating case rather than for the feed. Deterministic
    # code decides later whether the two mean the same thing here.
    'case_pressures': {'type': 'array', 'items': {'type': 'number'}},
    'case_pressure_unit': {'type': 'string'},
    'outlet_temperatures': {'type': 'array', 'items': {'type': 'number'}},
    'outlet_temperature_unit': {'type': 'string'},
    'missing_information': {'type': 'array', 'items': {'type': 'string'}},
})

TOLUENE = (
    '请帮我完成甲苯歧化反应的模拟，甲苯原料进入转化率反应器，发生歧化反应：'
    '2C₇H₈ → C₆H₆ + C₈H₁₀。甲苯进料流量10000kg/h，进料温度为380℃，'
    '操作压力2.5MPa，甲苯转化率为50%，反应产物为苯和二甲苯（邻、间、对三种异构体）')

SMR = (
    '我需要模拟甲烷蒸汽重整。进料是甲烷和水蒸气（摩尔比 1:2.7）有两个反应，'
    '主反应甲烷和水反应生成一氧化碳和氢气；副反应一氧化碳和水蒸汽反应生成'
    '二氧化碳和氢气，请分析以下两种情况下反应炉的组分分布：'
    '1、重整炉出口气温度为 710°C，压力 13.5 bar，进料温度520℃；'
    '2、重整炉出口气温度为600℃，压力13.5bar，进料温度520℃。'
    '进料流量可以自定，要求符合一个工厂一年正常的处理量')

GASIFICATION = (
    '我要模拟水煤浆的气化过程。进料为煤炭和水，流量80000Nm3/h，压力40bar，'
    '水煤浆进料浓度62wt%，进料温度40摄氏度，主要反应：C+H2O → CO+H2。'
    '请帮我计算一下气化炉出口温度为1400度时出口组成及CO的收率，反应器灰分不做考虑')


def post(payload: dict, timeout: int = 300):
    request = urllib.request.Request(BASE.rstrip('/') + '/chat/completions',
                                     data=json.dumps(payload).encode('utf-8'),
                                     method='POST')
    request.add_header('Authorization', 'Bearer ' + KEY)
    request.add_header('Content-Type', 'application/json')
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode('utf-8')), time.time() - started, None
    except urllib.error.HTTPError as exc:
        return None, time.time() - started, 'HTTP %s: %s' % (
            exc.code, exc.read().decode('utf-8', 'replace')[:110])
    except Exception as exc:                        # noqa: BLE001
        return None, time.time() - started, '%s: %s' % (type(exc).__name__, exc)


def extract(model: str, text: str):
    body, seconds, error = post({
        'model': model, 'max_tokens': MAX_TOKENS,
        # Thinking must be OFF. Measured on qwen3.7-flash: with reasoning enabled the
        # model spent the whole budget on reasoning_tokens and either returned an
        # empty content or a truncated one - 28s and 60% on the reformer. With it
        # disabled the same call took 2.6s and scored higher. Reasoning hurt both
        # latency and accuracy here.
        'enable_thinking': False,
        'response_format': {'type': 'json_schema',
                            'json_schema': {'name': 'Extraction', 'strict': True,
                                            'schema': EXTRACTION_SCHEMA}},
        'messages': [{'role': 'system', 'content': SYSTEM},
                     {'role': 'user', 'content': text}]})
    if error or not body:
        return None, seconds, error or 'no body'
    choice = body['choices'][0]
    raw = choice['message'].get('content') or ''
    if not raw:
        details = (body.get('usage', {}).get('completion_tokens_details') or {})
        return None, seconds, ('empty content (finish=%s, reasoning_tokens=%s)'
                               % (choice.get('finish_reason'),
                                  details.get('reasoning_tokens', 0)))
    try:
        return json.loads(raw), seconds, None
    except Exception as exc:                        # noqa: BLE001
        return None, seconds, 'not JSON: %s' % exc


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _norm_unit(unit) -> str:
    """Normalise a unit the way deterministic code would, not the model."""
    text = str(unit or '').strip().casefold().replace('°', '')
    if text in ('c', 'degc', 'celsius', 'centigrade', '摄氏度', '度', '℃'):
        return 'c'
    return text


def _norm_temp(value, unit) -> float | None:
    """Convert to Celsius. Handles the K case the model may copy verbatim."""
    number = _num(value)
    if number is None:
        return None
    if _norm_unit(unit) in ('k', 'kelvin', '开尔文'):
        return number - 273.15
    return number


def _has(field, tokens) -> bool:
    text = ' '.join(str(x) for x in (field or [])).casefold()
    return any(t in text for t in tokens)


def score_toluene(obj: dict) -> list[tuple[str, bool]]:
    species = obj.get('species') or []
    return [
        ('50% conversion', _num(obj.get('conversion_percent')) == 50.0),
        ('10000 kg/h', _num(obj.get('feed_total')) == 10000.0
         and 'kg' in str(obj.get('feed_unit')).casefold()),
        ('380 C feed', _norm_temp(obj.get('feed_temperature'),
                                  obj.get('feed_temperature_unit')) == 380.0),
        ('2.5 MPa (or 2500 kPa)',
         _num(obj.get('feed_pressure')) in (2.5, 2500.0)),
        ('benzene present', _has(species, ('benzene', 'c6h6', '苯'))),
        ('xylene present', _has(species, ('xylene', 'c8h10', '二甲苯'))),
    ]


def score_smr(obj: dict) -> list[tuple[str, bool]]:
    outlets = [v for v in (_num(x) for x in (obj.get('outlet_temperatures') or []))
               if v is not None]
    # Pressure may arrive as a feed pressure or per operating case; deterministic
    # normalisation merges the two, so the scorer must accept either.
    pressures = [v for v in (_num(x) for x in (
        [obj.get('feed_pressure')] + list(obj.get('case_pressures') or [])))
        if v is not None]
    units = ' '.join(str(u or '') for u in (obj.get('feed_pressure_unit'),
                                            obj.get('case_pressure_unit'))).casefold()
    return [
        ('two outlet temperatures', len(outlets) == 2),
        ('600 and 710 captured', sorted(outlets) == [600.0, 710.0]),
        ('inlet 520 C', _norm_temp(obj.get('feed_temperature'),
                                   obj.get('feed_temperature_unit')) == 520.0),
        ('13.5 bar present',
         any(abs(p - 13.5) < 1e-9 for p in pressures) and 'bar' in units),
        ('reforming species present',
         _has(obj.get('species'), ('ch4', 'methane', '甲烷'))
         and _has(obj.get('species'), ('h2', 'hydrogen', '氢'))),
    ]


def score_gasification(obj: dict) -> list[tuple[str, bool]]:
    unit = str(obj.get('feed_unit') or '').casefold()
    missing = ' '.join(str(x) for x in (obj.get('missing_information') or [])).casefold()
    return [
        # The decisive one: the unknown unit must survive untouched.
        ('Nm3 preserved, not converted', 'nm3' in unit or 'nm³' in unit),
        ('1400 C outlet',
         any(_num(x) == 1400.0 for x in (obj.get('outlet_temperatures') or []))),
        ('40 bar', _num(obj.get('feed_pressure')) == 40.0),
        ('40 C feed', _norm_temp(obj.get('feed_temperature'),
                                 obj.get('feed_temperature_unit')) == 40.0),
        # And it should notice the ambiguity rather than resolve it.
        ('questions the Nm3 basis',
         _has(obj.get('missing_information'),
              ('nm3', 'nm³', 'standard', '标况', '标准状态', '基准', '哪一股', '物流'))),
    ]


CASES = [('toluene', TOLUENE, score_toluene),
         ('smr', SMR, score_smr),
         ('gasification', GASIFICATION, score_gasification)]


def evaluate(model: str) -> dict:
    row: dict = {'model': model, 'cases': {}}
    for name, text, scorer in CASES:
        obj, seconds, note = extract(model, text)
        if obj is None:
            checks = [(scorer.__name__, False)]
        else:
            checks = scorer(obj)
        passed = sum(1 for _, ok in checks if ok)
        row['cases'][name] = {
            'seconds': round(seconds, 2), 'note': note,
            'score': '%d/%d' % (passed, len(checks)),
            'ratio': round(passed / len(checks), 3),
            'checks': {label: ok for label, ok in checks},
            'missing_count': len(obj.get('missing_information') or []) if obj else 0,
        }
    scores = [c['ratio'] for c in row['cases'].values()]
    row['mean_score'] = round(sum(scores) / len(scores), 3)
    row['mean_seconds'] = round(
        sum(c['seconds'] for c in row['cases'].values()) / len(row['cases']), 2)
    row['useful'] = all(c['ratio'] >= 0.6 for c in row['cases'].values())
    return row


def main() -> int:
    if not BASE or not KEY:
        print('TR_BASE and TR_KEY must be set in the environment')
        return 2

    schema = EXTRACTION_SCHEMA          # flat + array-based; see the module docstring
    models = list(CANDIDATES)
    try:
        request = urllib.request.Request(BASE.rstrip('/') + '/models')
        request.add_header('Authorization', 'Bearer ' + KEY)
        with urllib.request.urlopen(request, timeout=60) as response:
            available = {m.get('id') for m in json.loads(response.read().decode())['data']}
        models = [m for m in models if m in available]
    except Exception as exc:                        # noqa: BLE001
        print('could not list models (%s); using the candidate list' % exc)

    print('evaluating %d models x %d scenarios (max_tokens=%d), in parallel...'
          % (len(models), len(CASES), MAX_TOKENS))
    started = time.time()
    with ThreadPoolExecutor(max_workers=len(models) or 1) as pool:
        rows = list(pool.map(evaluate, models))
    elapsed = time.time() - started

    # Correctness first, then speed: the user asked for the fastest one that is
    # actually right, not the fastest one full stop.
    rows.sort(key=lambda r: (-r['mean_score'], r['mean_seconds']))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({'max_tokens': MAX_TOKENS,
                               'wall_seconds': round(elapsed, 1),
                               'schema_style': 'flat_object_with_arrays',
                               'results': rows}, indent=2, ensure_ascii=False),
                   encoding='utf-8')

    print()
    print('%-24s %6s %8s %6s  %s'
          % ('model', 'score', 'avg_s', 'miss', 'per scenario'))
    for row in rows:
        parts = ['%s:%s/%.0fs' % (name[:4], row['cases'][name]['score'],
                                  row['cases'][name]['seconds'])
                 for name, _, _ in CASES]
        miss = sum(row['cases'][n]['missing_count'] for n, _, _ in CASES)
        print('%-24s %6.2f %8.1f %6d  %s%s'
              % (row['model'], row['mean_score'], row['mean_seconds'], miss,
                 '  '.join(parts), '' if row['useful'] else '   <- not usable'))
    print()
    print('score = field-level correctness against the known answers; miss = total')
    print('items the model itself flagged as unstated.')
    best = next((r['model'] for r in rows if r['useful']), None)
    print('wall time %.1fs' % elapsed)
    print('best usable (fastest among the accurate): %s' % best)
    print('full results: %s' % OUT)
    return 0


if __name__ == '__main__':
    sys.exit(main())
