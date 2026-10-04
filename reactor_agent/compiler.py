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
    NORMAL_VOLUME_UNITS,
    SPEC_SCHEMA,
    library_name,
    property_package_name,
)
from hysys_tools import precheck

from .schemas import (
    Assumption,
    ModelingPlan,
    OperatingCase,
    ProcessRequest,
    Question,
)

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


def _flow_unit_is_convertible(feed) -> bool:
    """Whether the tool layer can turn this total into a molar flow by itself.

    A normal-volume total is convertible once the standard conditions are on the
    spec, so it no longer needs the "which stream at what standard state" question
    that a bare Nm3/h does.
    """
    if getattr(feed, 'flow_input', 'local') == 'normal_volume':
        return True
    key = str(getattr(feed, 'total_flow_unit', '') or '').strip().casefold()
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
        if feed.total_flow is not None and not _flow_unit_is_convertible(feed):
            normal_volume_unit = (
                str(feed.total_flow_unit or '').strip().casefold()
                in NORMAL_VOLUME_UNITS)
            questions.append(Question(
                id='q-flow-basis-%d' % index,
                field='feeds[%d].total_flow_unit' % index,
                blocking=True,
                question=(
                    '进料流量 %g %s 按什么理解？默认：单股混合进料的总量，'
                    '标准状态 0°C / 101.325 kPa。也可以回答其他标准状态'
                    '（如 20°C），或直接给出质量或摩尔流量。'
                    % (feed.total_flow, feed.total_flow_unit)
                    if normal_volume_unit else
                    '进料流量 %g %s 指的是哪一股物流？请直接给出质量或摩尔流量'
                    '（例如 kg/h 或 kmol/h）。'
                    % (feed.total_flow, feed.total_flow_unit)),
                default='总进料，0°C/101.325 kPa' if normal_volume_unit else None,
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


# Words that say the user accepted the pure-carbon representation, either in the
# original request or in an answer to `q-coal-definition`.
COAL_CONFIRMATION = ('纯碳', '纯固体碳', '按碳处理', '按纯碳', '按碳算',
                     'pure carbon', 'as carbon', 'as pure carbon')


def _mentions_coal(request: ProcessRequest) -> bool:
    text = request.source_text.casefold()
    # Water-gas shift names a gas reaction, not a solid coal feed. Remove only
    # that term, so separately stated coal/coke still requires confirmation.
    text = text.replace('水煤气', '').replace('焦耳', '')
    return ('煤' in text or '焦' in text
            or bool(re.search(r'(?<![a-z])(?:coal|char|charcoal|coke)(?![a-z])', text)))


def _confirms_pure_carbon(request: ProcessRequest) -> bool:
    text = request.source_text.casefold()
    return any(token in text for token in COAL_CONFIRMATION)


def coal_questions(request: ProcessRequest) -> list[Question]:
    """Questions about representing a complex solid by a single component.

    Only raised when the request actually names coal: assuming pure carbon is a
    modelling decision with a large effect on the carbon balance, and the exam only
    says ash may be neglected, which is not the same as saying the coal is pure
    carbon.
    """
    if not _mentions_coal(request):
        return []
    # Already answered: if the user said to treat it as pure carbon, asking again
    # would be the repeated-question failure the plan warns about (plan 5.5).
    if _confirms_pure_carbon(request):
        return []
    return [Question(
        id='q-coal-definition',
        # A field of its own: sharing `feeds[0].fractions` with the missing-
        # composition question let de-duplication swallow one of the two.
        field='feeds[0].coal_definition',
        blocking=True,
        question='煤能否按纯固体碳处理？默认：按纯碳处理。'
                 '说明：工具层目前只验收了"碳 + 水"进料，'
                 '按元素分析建模需要另行扩展。',
        default='按纯碳处理',
        reason=('输入涉及煤或焦炭，但尚未确认其组成或是否按纯碳处理。该假设会影响'
                '碳平衡与 CO 收率分母，需要明确认可后才能执行。'))]


def coal_assumption(request: ProcessRequest) -> Assumption | None:
    """The pure-carbon assumption, once the request actually relies on it."""
    if not _mentions_coal(request) or not _confirms_pure_carbon(request):
        return None
    text = request.source_text.casefold()
    asked = any(token in text for token in
                ('按纯碳处理', '按纯碳', '按纯固体碳处理', '按碳处理'))
    return Assumption(
        id='a-coal-pure-carbon',
        field='feeds[0].fractions',
        value='按纯碳处理',
        source='user_answer' if asked else 'user_text',
        accepted=True,
        scope=('煤按纯固体碳处理；题目只说忽略灰分，这一项是建模假设，'
               '会影响碳平衡和 CO 收率的分母'))



def _reaction_entry(request: ProcessRequest, index: int, kind: str) -> dict:
    """One reaction block, shaped by the reactor that will consume it.

    The phase is not taken from `request.phase` any more. That field describes the
    feed, and mapping it onto the reaction block produced a liquid-phase reaction in
    a gas-phase reactor. The accepted specs fix it per reactor type: Conversion runs
    `combined` (the toluene spec HYSYS accepted), Equilibrium requires `vapour`.

    The name is always ASCII. The intake model names reactions in the user's
    language ("歧化反应"), and that name becomes a HYSYS reaction object name.
    Conversion constraints are still matched against the request's own name below.
    """
    reaction = request.reactions[index]
    entry: dict = {
        'name': 'RXN-%d' % (index + 1),
        'stoichiometry': {str(k): float(v)
                          for k, v in reaction.stoichiometry.items()},
    }
    # A conversion figure belongs to a reaction; match it by name when the request
    # names one, otherwise apply it to the single reaction.
    if kind == 'conversion':
        for constraint in request.conversion_constraints:
            if constraint.reaction and constraint.reaction != reaction.name:
                continue
            if len(request.reactions) > 1 and not constraint.reaction:
                continue
            entry['conversion_percent'] = float(constraint.percent)
            if constraint.base_component:
                entry['base_component'] = constraint.base_component
            break
    entry['phase'] = 'combined' if kind == 'conversion' else 'vapour'
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
    # The accepted normal-volume path needs all three keys, and the tool layer reads
    # exactly these names. A `local` feed writes none of them, so the toluene and
    # reformer specs stay byte-identical to what was already run and their spec
    # hashes in the execution ledger do not change.
    if feed.flow_input == 'normal_volume':
        entry['flow_input'] = 'normal_volume'
        entry['standard_temperature_C'] = float(feed.standard_temperature_C)
        entry['standard_pressure_kPa'] = float(feed.standard_pressure_kPa)
    return entry


def _case_name(plan: ModelingPlan, case: OperatingCase) -> str:
    """An ASCII case name that distinguishes the operating cases.

    The scenario label is the user's own words and is usually Chinese, which
    `safe_case_name` strips to nothing; the two reformer conditions would then share
    one name and the second case would overwrite the first. The outlet temperature
    is appended for the same reason: it is what actually differs between them.
    """
    named = case.overrides.get('case_name')
    if named:
        return safe_case_name(named)
    base = safe_case_name(plan.request.scenario_label, fallback='')
    if not base:
        base = 'agent-%s' % (plan.decision.execution_reactor or 'case')
    outlet = case.overrides.get('outlet_temperature')
    if outlet is None:
        source = _case_request(plan, case)
        if source is not None:
            outlet = source.outlet_temperature
    if outlet is not None:
        return safe_case_name('%s-%gC' % (base, float(outlet)))
    if len(plan.cases) > 1:
        return safe_case_name('%s-%s' % (base, case.case_id))
    return safe_case_name(base)


def _canonical_fractions(feed) -> dict[str, float]:
    """Feed composition keyed the way `hysys_tools.core.canonical` names things.

    The spec keeps the user's own spelling; this view exists only so the compiler
    can ask "is carbon present, and in what amount" without re-implementing the
    aliases. Accepts fractions or flows, because either can carry the composition.
    """
    from hysys_tools.core import canonical

    composition: dict[str, float] = {}
    for source in (feed.fractions, feed.flows):
        for name, value in source.items():
            composition[canonical(name)] = float(value)
    return composition


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

    # Plan 5.4: a Gibbs reactor cannot be handed library Carbon - the tool layer
    # refuses that before solving - so a carbon-bearing feed takes the accepted
    # saturated-carbon route instead. This is mandatory, not an improvement.
    if plan.decision.execution_reactor == 'gibbs':
        for name, fraction in _canonical_fractions(request.feeds[0]).items():
            if name == 'carbon' and fraction > 0:
                reactor['solid_carbon'] = 'saturation'
                break

    spec: dict = {
        'schema': SPEC_SCHEMA,
        'case_name': _case_name(plan, case),
        'scenario': request.scenario_label or case.label or '',
        'fluid_package': {
            'property_package': property_package_name(plan.property_package),
            'components': components,
        },
        'feeds': [_feed_entry(request, i) for i in range(len(request.feeds))],
        # Plan 5.1: conversion and equilibrium both carry their reaction equations;
        # only the Gibbs reactor is given a bare candidate set and no equations.
        'reactions': [_reaction_entry(request, i, plan.decision.execution_reactor)
                      for i in range(len(request.reactions))]
        if plan.decision.execution_reactor != 'gibbs' else [],
        'reactor': reactor,
        'assumptions': ['%s：%s' % (a.field, a.scope or a.value)
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
    # De-duplicate by FIELD, not by (id, field). Normalisation and the compiler both
    # ask about one feed's total flow, under different ids; keeping both questions
    # left the user answering one of them forever, because only the normaliser's id
    # is routable. One field, one question.
    existing = {q.field for q in plan.questions}
    for question in feed_questions(request) + coal_questions(request):
        if question.field not in existing:
            plan.questions.append(question)
            existing.add(question.field)

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
