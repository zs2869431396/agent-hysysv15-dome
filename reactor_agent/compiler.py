"""Compile a ModelingPlan into strict hysys-agent/spec/1 documents.

This is the boundary where a modelling decision becomes something the tool layer
will accept. Two rules make it trustworthy:

  * Nothing is invented. Every value in the spec comes from the request, from a
    recorded assumption, or from the case's own overrides. If a required value is
    missing, compilation raises or raises a blocking question - it never fills a
    plausible default.
  * Nothing ambiguous is resolved silently. A total flow in Nm3/h is carried
    through verbatim so the tool layer's own pre-check rejects it; the compiler
    does NOT convert it to kg/h, because the conversion factor depends on which
    stream and which standard conditions the user meant, and a wrong choice would
    look like a successful run with a wrong CO yield.

Component and property-package names are resolved with the same functions the
executor uses (`hysys_tools.core.library_name`, `property_package_name`), so
"passes compilation" and "runs" cannot disagree.
"""
from __future__ import annotations

import re

from hysys_tools.core import (
    MASS_FLOW_UNITS,
    MOLAR_FLOW_UNITS,
    SPEC_SCHEMA,
    library_name,
    property_package_name,
)
from hysys_tools import precheck

from .schemas import ModelingPlan, OperatingCase, ProcessRequest, Question

# Windows refuses these names regardless of extension, and a name carrying a path
# separator would let a model choose where files land.
_WINDOWS_RESERVED = {
    'CON', 'PRN', 'AUX', 'NUL',
    *(f'COM{i}' for i in range(1, 10)),
    *(f'LPT{i}' for i in range(1, 10)),
}
_UNSAFE = re.compile(r'[^A-Za-z0-9._-]+')


class CompileError(Exception):
    """The plan cannot be turned into an executable specification."""


def safe_case_name(raw: str | None, fallback: str = 'agent-case') -> str:
    """A case name that is safe as a file name under the run directory.

    The plan requires that names and paths are produced by the adapter rather than
    by the model (plan 3.2), so anything unexpected collapses to a known shape
    instead of being passed through.
    """
    text = _UNSAFE.sub('-', str(raw or '')).strip('-._')
    if not text:
        return fallback
    stem = text.split('.')[0].upper()
    if stem in _WINDOWS_RESERVED:
        return fallback
    return text[:60]


def _flow_unit_is_convertible(unit: str) -> bool:
    key = str(unit or '').strip().casefold()
    return key in MOLAR_FLOW_UNITS or key in MASS_FLOW_UNITS


def feed_questions(request: ProcessRequest) -> list[Question]:
    """Blocking questions raised by the feeds themselves.

    The gasification case is the reason this exists: the exam says the feed is
    "煤炭和水, 流量80000Nm3/h". A coal-water slurry is a solid plus a liquid, and
    Nm3 is a gas-equivalent volume, so the figure cannot be converted without
    knowing which stream it describes and at what standard conditions. Choosing an
    interpretation would produce a confident, unverifiable CO yield.
    """
    questions: list[Question] = []
    for index, feed in enumerate(request.feeds):
        if feed.total_flow is not None and not _flow_unit_is_convertible(
                feed.total_flow_unit):
            questions.append(Question(
                id='q-flow-basis-%d' % index,
                field='feeds[%d].total_flow_unit' % index,
                blocking=True,
                question=(
                    '进料流量 %g %s 指的是哪一股物流、标准状态是什么？'
                    '（例如 0°C/101.325 kPa 还是 20°C，以及它描述的是进料总量、'
                    '水蒸气量还是出口合成气量）'
                    % (feed.total_flow, feed.total_flow_unit)),
                reason=(
                    '体积单位无法换算成摩尔或质量流量：Nm³ 只对气体有意义，而'
                    '煤是固体、水是液体。缺少基准时换算结果取决于假设，不同假设'
                    '会给出完全不同的 CO 收率，因此不能自行选定一种。')))
        if feed.total_flow is None and not feed.flows:
            questions.append(Question(
                id='q-flow-missing-%d' % index,
                field='feeds[%d]' % index, blocking=True,
                question='进料流量是多少？请给出数值与单位。',
                reason='没有流量就无法建立物料衡算。'))
        if feed.temperature is None:
            questions.append(Question(
                id='q-temp-missing-%d' % index,
                field='feeds[%d].temperature' % index, blocking=True,
                question='进料温度是多少？',
                reason='进料温度是物料与能量衡算的必需条件。'))
        if feed.pressure is None:
            questions.append(Question(
                id='q-press-missing-%d' % index,
                field='feeds[%d].pressure' % index, blocking=True,
                question='操作压力是多少？',
                reason='压力是物料与能量衡算的必需条件。'))
    return questions


def coal_questions(request: ProcessRequest) -> list[Question]:
    """Questions about representing a complex solid by a single component.

    Only raised when the request actually names coal: assuming pure carbon is a
    modelling decision with a large effect on the carbon balance, and the exam only
    says ash may be neglected, which is not the same as saying the coal is pure
    carbon.
    """
    text = request.source_text.casefold()
    if not any(token in text for token in ('煤', 'coal', '焦', 'char')):
        return []
    # Already answered: if the user said to treat it as pure carbon, asking again
    # would be the repeated-question failure the plan warns about (plan 5.5).
    if any(token in text for token in (
            '纯碳', '纯固体碳', '按碳处理', '按纯碳', '按碳算',
            'pure carbon', 'as carbon', 'as pure carbon')):
        return []
    return [Question(
        id='q-coal-definition',
        field='feeds[0].fractions',
        blocking=True,
        question='煤能否按纯固体碳处理？如果方便，请给出工业分析或元素分析结果。',
        reason=('题目只说"灰分不做考虑"，未给出煤的组成。按纯碳处理是一个会影响'
                '碳平衡与 CO 收率分母的假设，需要明确认可后才能执行。'))]


def _reaction_entry(request: ProcessRequest, index: int) -> dict:
    reaction = request.reactions[index]
    entry: dict = {
        'name': reaction.name or 'RXN-%d' % (index + 1),
        'stoichiometry': {str(k): float(v)
                          for k, v in reaction.stoichiometry.items()},
    }
    # A conversion figure belongs to a reaction; match it by name when the request
    # names one, otherwise apply it to the single reaction.
    for constraint in request.conversion_constraints:
        if constraint.reaction and constraint.reaction != reaction.name:
            continue
        if len(request.reactions) > 1 and not constraint.reaction:
            continue
        entry['conversion_percent'] = float(constraint.percent)
        if constraint.base_component:
            entry['base_component'] = constraint.base_component
        break
    entry['phase'] = {
        'gas': 'vapour', 'liquid': 'liquid', 'mixed': 'combined',
    }.get(request.phase, 'combined')
    return entry


def _feed_entry(request: ProcessRequest, index: int) -> dict:
    feed = request.feeds[index]
    entry: dict = {
        'name': feed.name or 'FEED',
        'basis': feed.basis,
        'temperature': feed.temperature,
        'temperature_unit': feed.temperature_unit,
        'pressure': feed.pressure,
        'pressure_unit': feed.pressure_unit,
    }
    if feed.fractions:
        entry['fractions'] = {str(k): float(v) for k, v in feed.fractions.items()}
    if feed.flows:
        entry['flows'] = {str(k): float(v) for k, v in feed.flows.items()}
    if feed.total_flow is not None:
        entry['total_flow'] = float(feed.total_flow)
        # Carried through unchanged, even when the tool layer will refuse it.
        entry['total_flow_unit'] = feed.total_flow_unit
    return entry


def _case_request(plan: ModelingPlan, case: OperatingCase):
    """The OperatingCaseRequest this case corresponds to, matched by case_id.

    Keeps the operating point stated by the user as the single source of truth:
    `OperatingCase.overrides` only needs to carry what differs from it, so the two
    cannot drift apart and silently lose an outlet temperature.
    """
    for item in plan.request.operating_cases:
        if item.case_id == case.case_id:
            return item
    return None


def compile_case(plan: ModelingPlan, case: OperatingCase) -> dict:
    """Build one hysys-agent/spec/1 document for a single operating case."""
    request = plan.request
    if plan.decision.execution_reactor in (None, 'unsupported'):
        raise CompileError(
            'no executable reactor for this plan (preferred %r, status %r)'
            % (plan.decision.preferred_reactor, plan.decision.capability_status))
    if not request.feeds:
        raise CompileError('a spec needs at least one feed')

    components = [library_name(name) for name in plan.components]
    reactor: dict = {
        'name': 'AGENT-RX',
        'kind': plan.decision.execution_reactor,
        'thermal_mode': plan.thermal_mode,
        'pressure_drop_kPa': 0.0,
    }
    # Each case carries its own outlet temperature; this is what keeps the two
    # reformer conditions from collapsing into one. The value comes from the
    # operating point the user stated, unless the case overrides it.
    outlet = case.overrides.get('outlet_temperature')
    outlet_unit = case.overrides.get('outlet_temperature_unit')
    if outlet is None:
        source = _case_request(plan, case)
        if source is not None and source.outlet_temperature is not None:
            outlet = source.outlet_temperature
            outlet_unit = source.outlet_temperature_unit
    if outlet is not None:
        reactor['outlet_temperature'] = float(outlet)
        reactor['outlet_temperature_unit'] = str(outlet_unit or 'C')
    if plan.thermal_mode == 'isothermal' and 'outlet_temperature' not in reactor:
        raise CompileError(
            'case %r is isothermal but no outlet temperature was stated for it'
            % case.case_id)

    spec: dict = {
        'schema': SPEC_SCHEMA,
        'case_name': safe_case_name(
            case.overrides.get('case_name') or request.scenario_label
            or case.case_id or 'agent-case'),
        'scenario': request.scenario_label or case.label or '',
        'fluid_package': {
            'property_package': property_package_name(plan.property_package),
            'components': components,
        },
        'feeds': [_feed_entry(request, i) for i in range(len(request.feeds))],
        'reactions': [_reaction_entry(request, i)
                      for i in range(len(request.reactions))]
        if plan.decision.execution_reactor == 'conversion' else [],
        'reactor': reactor,
        'assumptions': ['%s = %r (%s)' % (a.field, a.value, a.source)
                        for a in plan.assumptions],
        'open_questions': [q.question for q in plan.questions if not q.blocking],
    }
    blocking = [q.question for q in plan.questions if q.blocking and q.is_open()]
    if blocking:
        spec['blocking_questions'] = blocking
    return spec


def compile_plan(plan: ModelingPlan) -> ModelingPlan:
    """Compile every case and pre-check it.

    Blocking questions are collected from feeds and the coal definition first; when
    any are open the plan is left in WAITING_INPUT and no spec is produced, which is
    what keeps the agent from spending a HYSYS run on a guess.
    """
    request = plan.request
    existing = {(q.id, q.field) for q in plan.questions}
    for question in feed_questions(request) + coal_questions(request):
        if (question.id, question.field) not in existing:
            plan.questions.append(question)
            existing.add((question.id, question.field))

    if plan.blocking_questions():
        plan.status = 'WAITING_INPUT'
        return plan

    if not plan.decision.is_executable():
        plan.status = 'UNSUPPORTED'
        return plan

    reports: list[dict] = []
    for case in plan.cases:
        spec = compile_case(plan, case)
        case.with_spec(spec)
        report = precheck.validate_spec(spec)
        reports.append({'case_id': case.case_id, 'ok': report['ok'],
                        'errors': report['errors']})
        if not report['ok']:
            # A pre-check failure here is a compiler bug or a genuinely bad value;
            # either way it must not be turned into a run.
            raise CompileError(
                'compiled spec for case %r failed the pre-check: %s'
                % (case.case_id, '; '.join(report['errors'])))

    plan.status = 'READY'
    return plan
