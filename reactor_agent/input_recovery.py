"""Pre-check owns the questions; a model may only propose targeted repairs."""
from __future__ import annotations

import copy
import json

from .extraction import EXTRACTION_SCHEMA, grounding_failures
from .llm import LlmError

GROUPS = [('feed_total', 'feed_unit'), ('feed_pressure', 'feed_pressure_unit'),
          ('feed_temperature', 'feed_temperature_unit'), ('feed_composition', 'composition_basis'),
          ('case_pressures', 'case_pressure_unit'), ('outlet_temperatures', 'outlet_temperature_unit')]
PATHS = {'feeds[0].total_flow': GROUPS[0], 'feeds[0].pressure': GROUPS[1],
         'feeds[0].temperature': GROUPS[2], 'feeds[0].fractions': GROUPS[3],
         'fluid_package.components': ('species',)}
CONFIRMATIONS = {'q-coal-definition', 'q-volumetric-flow'}


def recover_explicit_units(text, facts):
    """Fill absent units from a matching, labelled literal in the source only."""
    import re
    from .extraction import _NUMBER, _unit_at_position
    units = [('feed_total', 'feed_unit', 'flow', r'流量|feed\s+(?:flow|rate)'),
             ('feed_pressure', 'feed_pressure_unit', 'pressure', r'压力|feed\s+pressure'),
             ('feed_temperature', 'feed_temperature_unit', 'temperature', r'进料温度|入口温度|feed\s+temperature')]
    repaired, records = copy.deepcopy(facts), []
    for value_field, unit_field, quantity, label in units:
        if facts.get(value_field) is None or str(facts.get(unit_field) or '').strip():
            continue
        candidates = []
        for match in _NUMBER.finditer(text):
            unit, kind = _unit_at_position(text, match.end())
            prefix = re.split(r'[，。；;\n]', text[:match.start()])[-1]
            matches_quantity = kind == quantity or (quantity == 'flow' and kind in ('mass_flow', 'molar_flow', 'volume_flow'))
            if (matches_quantity and float(match.group().replace(',', '')) == facts[value_field]
                    and re.search(label, prefix, re.I)
                    and not re.search(r'出口|工况|产物|outlet|case|product', prefix, re.I)):
                suffix = re.split(r'[，。；;\n]', text[match.start():])[0]
                candidates.append((unit, prefix + suffix))
        if len({unit for unit, _ in candidates}) == 1:
            repaired[unit_field] = candidates[0][0]
            records.append({'field': unit_field, 'value': candidates[0][0],
                'evidence': candidates[0][1],
                'reason': '原文匹配数值与明确物理量，单位唯一；仅补单位，不改数值。'})
    return repaired, records


def precheck_inputs(text, facts, context=None):
    from .nodes.plan import make_plan_node
    from .nodes.state import initial_state
    state = dict(context or initial_state(text))
    state.update(text=text, facts=facts, review={}, extraction_error=None,
                 ungrounded=grounding_failures(facts, text,
                     set(state.get('allowed_ungrounded') or [])), problems=[])
    result = make_plan_node()(state)
    targets = set()
    for question in result.get('blocking') or []:
        if question['id'] in CONFIRMATIONS:
            continue
        path = question['field']
        if path in PATHS:
            targets.update(PATHS[path])
        elif path.startswith('reactions'):
            targets.add('reactions')
        elif path in EXTRACTION_SCHEMA['properties']:
            targets.add(path)
        for group in GROUPS:
            if path in group:
                targets.update(group)
    if result['status'] == 'FAILED' and any('element balanced' in p for p in result['problems']):
        targets.add('reactions')
    return {'status': result['status'], 'decision': result.get('decision'),
            'blocking': result.get('blocking') or [], 'problems': result.get('problems') or []}, sorted(targets)


def recover_inputs(client, text, facts, *, context=None):
    from .review import _review_reply, valid_value
    original, source_recoveries = recover_explicit_units(text, facts)
    original.pop('_review_questions', None)  # Model-authored questions never govern planning.
    before, targets = precheck_inputs(text, original, context)
    record = {'status': 'SKIPPED', 'mode': 'targeted_recovery', 'target_fields': targets,
              'precheck_before': before, 'corrections': [], 'rejected_corrections': [],
              'unchanged_corrections': [], 'ignored_questions': [], 'questions': [],
              'original_facts': copy.deepcopy(facts), 'source_recoveries': source_recoveries}
    if not targets:
        record['reason'] = '预检没有需要模型补漏的字段；建模假设确认由程序直接追问。'
        return original, record
    try:
        reply, attempts = _review_reply(client, text, original,
            recovery_context={'target_fields': targets, 'precheck': before})
    except LlmError as exc:
        from .review import review_failure_record
        record.update(status='WAITING_INPUT', recovery_error=review_failure_record(exc),
            reason='定向补漏未成功，保留原始事实；只询问程序预检仍未解决的条件。')
        return original, record
    record.update(status='PASS', format_attempts=attempts,
                  ignored_questions=copy.deepcopy(reply['questions']))
    candidate = copy.deepcopy(original)
    accepted, rejected, unchanged = [], [], []
    seen = set()
    for patch in reply['corrections']:
        name = patch['field']
        reason = None
        if name not in targets or name in seen:
            reason = '字段不属于本次预检缺口，或重复修改；未采用。'
        else:
            seen.add(name)
            try:
                value = json.loads(patch['value_json'])
            except (TypeError, ValueError):
                reason = '修正值不是有效 JSON。'
            if reason is None and not valid_value(value, EXTRACTION_SCHEMA['properties'][name]):
                reason = '修正值类型或结构无效。'
            if reason is None and name in original and value == original[name]:
                unchanged.append(patch)
                continue
            if reason is None and original.get(name) is not None and (value is None or value == [] or value == ''):
                reason = '不能清空已有事实；原始单位的物理解释由独立确认处理。'
            if reason is None and (not patch['evidence'].strip() or patch['evidence'] not in text):
                reason = '修正没有逐字连续的原文依据。'
            if reason is None and name == 'species':
                from .normalize import resolve_species
                old = {resolve_species(n) or n for n in original.get(name) or []}
                new = {resolve_species(n) or n for n in value}
                if not old.issubset(new):
                    reason = '不能缩减原有组分列表。'
            if reason is None:
                candidate[name] = value
                accepted.append(patch)
        if reason:
            rejected.append({**patch, 'reason': reason})
    candidate, later_units = recover_explicit_units(text, candidate)
    record['source_recoveries'].extend(later_units)
    # Numeric values are validated with their final associated units. A rejected
    # value/unit/basis group rolls back atomically; no model may reinterpret a number.
    for patch in accepted:
        name = patch['field']
        failures = grounding_failures(candidate, patch['evidence'])
        if any(f == name or f.startswith(name + '[') for f in failures):
            rejected.append({**patch, 'reason': '数值或量纲不能与原文引用对应。'})
    rollback = {p['field'] for p in rejected}
    for group in GROUPS:
        if rollback.intersection(group):
            rollback.update(group)
    for name in rollback:
        if name in original:
            candidate[name] = copy.deepcopy(original[name])
        else:
            candidate.pop(name, None)
    for patch in accepted:
        if patch['field'] in rollback and not any(p['field'] == patch['field'] for p in rejected):
            rejected.append({**patch, 'reason': '关联数值或单位未通过，已一并回退。'})
    after, _ = precheck_inputs(text, candidate, context)
    # An invalid candidate must not replace a still-usable original input.
    if after['status'] == 'FAILED' and before['status'] != 'FAILED':
        rejected += [{**p, 'reason': '候选修改造成预检失败，保留原始事实。'}
                     for p in accepted if p['field'] not in rollback]
        candidate, after = original, before
        rollback.update(p['field'] for p in accepted)
    record.update(corrections=[p for p in accepted if p['field'] not in rollback],
                  rejected_corrections=rejected, unchanged_corrections=unchanged,
                  precheck_after=after)
    return candidate, record
