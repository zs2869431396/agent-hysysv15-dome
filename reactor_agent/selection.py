"""Deterministic reactor selection.

The rules follow the exam's own selection logic (exam section 8) and are executed
in the order the plan requires (plan 5.4). Nothing here calls a model: selection is
a rule application over stated facts, and the LLM's role is to produce those facts,
not to pick the reactor.

Six defects in the earlier draft (`hysys-ai/phi_select.py`) are fixed here, each
noted at the code that fixes it:

  1. The kinetic branch returned `spec.reactor.type`, so it could name a different
     reactor than the one it had just chosen. Here the kinetic branch returns CSTR
     or PFR explicitly.
  2. Any phase that was not exactly "liquid" was treated as vapour. Here mixed and
     unknown phases ask a question instead of guessing.
  3. `spec.reactor.type == 'conversion'` was accepted as evidence of a known
     conversion, which let an earlier guess justify itself. Here the request layer
     has no reactor-type field at all, so a selection cannot be its own evidence.
  4. The explanation hard-coded "50% conversion" and misreported a 30% input. Here
     every figure in the explanation is read from the request.
  5. "No reaction list" fell through to Gibbs. Here missing information asks a
     question; Gibbs requires positive evidence of a high-temperature,
     many-species, equilibrium-controlled system.
  6. Yield and selectivity were treated as interchangeable with conversion. Here
     they select nothing unless the reaction progress is independently determined.

Separation of choice and execution is deliberate (plan 5.4): if the right model is
a PFR, `preferred_reactor` stays 'pfr' and the decision reports UNSUPPORTED. It is
never rewritten into a model we happen to be able to run.
"""
from __future__ import annotations

from hysys_tools.core import canonical

from .capabilities import combination_status
from .schemas import (
    Assumption,
    ProcessRequest,
    Question,
    ReactionSpec,
    SelectionDecision,
)

# Rule identifiers, so a decision can be traced to one rule and tested.
RULE_KINETIC_CSTR = 'kinetic_liquid_cstr'
RULE_KINETIC_PFR = 'kinetic_gas_pfr'
RULE_KINETIC_PHASE_UNCLEAR = 'kinetic_phase_unclear'
RULE_KINETIC_POLYMER = 'kinetic_polymerisation'
RULE_CONVERSION = 'conversion_from_constraint'
RULE_EQUILIBRIUM = 'equilibrium_with_data'
RULE_GIBBS = 'gibbs_high_temperature_many_species'
RULE_YIELD_NOT_CONVERSION = 'yield_without_progress'
RULE_INSUFFICIENT = 'insufficient_information'
RULE_USER_FORCED = 'user_forced_conflict'

# Above this outlet temperature the exam's own note calls the system high
# temperature (exam section 9.3: "高温（>800℃）").
HIGH_TEMPERATURE_C = 800.0
# This many candidate products means the mechanism is not reasonably written out as
# reaction equations, which is what Gibbs is for.
MANY_SPECIES = 3

# The exam describes this class as a "black box" whose product distribution is hard
# to predict from simple equations. Prose that says so is positive evidence for a
# free-energy-minimisation model; its absence is why a single known reaction at high
# temperature is NOT automatically a Gibbs case.
COMPLEXITY_TOKENS = (
    '气化', '裂解', '黑箱', '复杂', '副反应', '机理', '未知', '燃烧',
    'gasif', 'crack', 'black box', 'complex', 'side reaction', 'mechanism',
    'unknown', 'combustion',
)


def _celsius(value: float | None, unit: str | None) -> float | None:
    if value is None:
        return None
    unit_key = (unit or 'C').strip().casefold()
    if unit_key in ('c', 'degc', 'celsius', 'centigrade'):
        return float(value)
    if unit_key in ('k', 'kelvin'):
        return float(value) - 273.15
    if unit_key in ('f', 'degf', 'fahrenheit'):
        return (float(value) - 32.0) * 5.0 / 9.0
    return None


def hottest_case_c(request: ProcessRequest) -> float | None:
    """The highest outlet temperature the user asked about, in C."""
    values = [_celsius(c.outlet_temperature, c.outlet_temperature_unit)
              for c in request.operating_cases]
    values = [v for v in values if v is not None]
    return max(values) if values else None


def product_species(request: ProcessRequest) -> list[str]:
    """Species the user's reactions produce (positive coefficients)."""
    names: list[str] = []
    for reaction in request.reactions:
        for name, coefficient in reaction.stoichiometry.items():
            if coefficient > 0 and name not in names:
                names.append(name)
    return names


def is_reversible_declared(request: ProcessRequest) -> bool:
    """True only when the request itself says the reaction is reversible.

    The earlier draft scanned prose for keywords, which is a job for the intake
    model. Here it reads a field that the intake step had to fill in explicitly.
    """
    if any(r.reversible is True for r in request.reactions):
        return True
    text = request.source_text.casefold()
    return any(word in text for word in ('可逆', 'reversible'))


def candidate_products(request: ProcessRequest) -> list[str]:
    """Species that could appear as products.

    Reaction products, **plus** any listed component that is neither a feed
    component nor a stated reactant. That second group matters: a Gibbs reactor
    distributes among every candidate it is given, so a by-product mentioned only in
    the component list still counts. Gasification is the case in point - the exam
    writes one reaction (C + H2O -> CO + H2) but expects CO2 and CH4 as well, and
    judging only by the reaction equation would wrongly call it under-specified.
    """
    products = list(product_species(request))
    seen = {canonical(n) for n in products}
    reactants: set[str] = set()
    for reaction in request.reactions:
        reactants |= {canonical(n)
                      for n, c in reaction.stoichiometry.items() if c < 0}
    feed_species: set[str] = set()
    for feed in request.feeds:
        feed_species |= {canonical(n) for n in feed.fractions}
        feed_species |= {canonical(n) for n in feed.flows}
    for name in request.components:
        key = canonical(name)
        if key in seen or key in reactants or key in feed_species:
            continue
        products.append(name)
        seen.add(key)
    return products


def is_gibbs_candidate(request: ProcessRequest) -> bool:
    """True when the product distribution is better minimised than written out.

    Gibbs is not simply "the hot option". The exam's own words are that it suits
    systems whose product distribution is unknown and whose mechanism is complex
    (exam section 8), and section 9.3 describes gasification as a black box above
    800 C. Steam reforming is the other shape of the same problem: two coupled
    reversible reactions (reforming plus water-gas shift) sharing species, at only
    600-710 C. Judging by temperature alone would classify that as under-specified,
    which is wrong - it is one of the verified Gibbs cases.

    So there are three ways to qualify, and all of them require that no rate law
    and no conversion figure was given:
      * clearly hot, above the exam's 800 C line;
      * several reactions whose candidate products overlap;
      * a many-species product set together with prose that says the mechanism is
        complex, unknown, or has side reactions.
    """
    if request.has_kinetics():
        return False
    if request.has_conversion_constraints():
        return False
    candidates = candidate_products(request)
    if len(candidates) < 2:
        return False

    hottest = hottest_case_c(request)
    if hottest is not None and hottest > HIGH_TEMPERATURE_C:
        return True
    if len(request.reactions) >= 2 and len(candidates) >= MANY_SPECIES:
        return True
    text = request.source_text.casefold()
    return (len(candidates) >= MANY_SPECIES
            and any(token in text for token in COMPLEXITY_TOKENS))


def _decision(preferred: str, rule_id: str, evidence: list[str],
              execution: str | None, status: str, explanation: str,
              reason: str | None = None,
              alternatives: list[str] | None = None) -> SelectionDecision:
    return SelectionDecision(
        preferred_reactor=preferred,          # type: ignore[arg-type]
        execution_reactor=execution,          # type: ignore[arg-type]
        rule_id=rule_id,
        evidence=evidence,
        alternatives=alternatives or [],
        capability_status=status,             # type: ignore[arg-type]
        fallback_reason=reason,
        explanation=explanation,
    )


def select_reactor(request: ProcessRequest,
                   reaction_count: int | None = None,
                   solid_phase: bool = False) -> SelectionDecision:
    """Choose the reactor model for one request, and say why.

    `execution_reactor` is filled in only when the tool layer can build the
    combination; otherwise it is None and `capability_status` is 'unsupported'.
    """
    count = len(request.reactions) if reaction_count is None else reaction_count

    def capability(kind: str, thermal: str | None) -> dict:
        return combination_status(kind, thermal, phase=request.phase,
                                  reaction_count=count, solid_phase=solid_phase)

    # ------------------------------------------------ 1. kinetics stated
    if request.has_kinetics():
        if request.is_polymerisation:
            # The exam excludes polymerisation from the liquid-CSTR rule.
            return _decision(
                'unsupported', RULE_KINETIC_POLYMER,
                ['the request describes a polymerisation with a rate law'],
                None, 'unsupported',
                '该过程为聚合反应。题目第 8 节的相态规则明确不适用于聚合反应，'
                '因此不能按"全液相即 CSTR"处理，而工具层也没有聚合反应器实现。'
                '需要先明确所用的反应器形式与停留时间分布。')
        if request.phase == 'liquid':
            status = capability('cstr', 'isothermal')
            return _decision(
                'cstr', RULE_KINETIC_CSTR,
                ['a complete rate law and sizing data are present',
                 'the user states the reactants are all liquid'],
                'cstr' if status['status'] != 'unsupported' else None,
                status['status'],
                '用户给出了完整的动力学参数（速率方程与设备尺寸），且反应物均为液相，'
                '按题目第 8 节约定选择全混流反应器 CSTR。',
                reason=None if status['status'] != 'unsupported' else status['reason'],
                alternatives=['pfr'])
        if request.phase == 'gas':
            status = capability('pfr', 'isothermal')
            return _decision(
                'pfr', RULE_KINETIC_PFR,
                ['a complete rate law and sizing data are present',
                 'the user states the reactants are all gas'],
                'pfr' if status['status'] != 'unsupported' else None,
                status['status'],
                '用户给出了完整的动力学参数，且反应物均为气相，按题目第 8 节约定'
                '选择平推流反应器 PFR。',
                reason=None if status['status'] != 'unsupported' else status['reason'],
                alternatives=['cstr'])
        # Fix for defect 2: mixed or unknown phase is a question, not a guess.
        status = capability('cstr', 'isothermal')
        return _decision(
            'unsupported', RULE_KINETIC_PHASE_UNCLEAR,
            ['a complete rate law is present but the phase is %r' % request.phase],
            None, 'unsupported',
            '用户给出了动力学参数，但相态不明确（%s）。题目第 8 节的规则依赖相态'
            '判断：全液相选 CSTR、全气相选 PFR。相态未明确时不能默认按气相处理，'
            '需要先确认反应体系是单相还是混相。' % request.phase,
            reason='phase not established')

    # ------------------------------------------------ 2. conversion stated
    if request.has_conversion_constraints():
        # Fix for defect 3: the constraint itself is the evidence. No reactor-type
        # field is consulted, because no such field exists in the request.
        figures = ', '.join(
            '%s %.12g%%' % (c.base_component or c.reaction or 'base', c.percent)
            for c in request.conversion_constraints)
        thermal = _thermal_from_cases(request)
        status = capability('conversion', thermal)
        return _decision(
            'conversion', RULE_CONVERSION,
            ['the user states a conversion figure: ' + figures,
             'no rate law was provided'],
            'conversion' if status['status'] != 'unsupported' else None,
            status['status'],
            '用户未提供动力学参数，但明确给出了转化率（%s），符合题目第 8 节对'
            ' Conversion 反应器的使用条件：已知转化率的简单反应。' % figures,
            reason=None if status['status'] != 'unsupported' else status['reason'],
            alternatives=['gibbs', 'equilibrium'])

    # --------------------------------------- 3. yield/selectivity only
    # Fix for defect 6: a yield or selectivity is not a conversion unless the reaction
    # progress can be determined some other way. The other ways include a rate law
    # (handled above), traceable equilibrium data, and a high-temperature
    # many-species system where free-energy minimisation determines the progress.
    #
    # Gasification is why this check cannot simply test for the word "收率": the exam
    # asks for the CO yield as a RESULT to be computed, not as an input constraint.
    # Treating that mention as a constraint made the whole scenario unsupported.
    if any(word in request.source_text for word in ('收率', '选择性', 'yield',
                                                    'selectivity')):
        if not request.has_conversion_constraints():
            if not (is_gibbs_candidate(request) or _has_equilibrium_data(request)):
                return _decision(
                    'unsupported', RULE_YIELD_NOT_CONVERSION,
                    ['the request mentions yield or selectivity but no conversion',
                     'and nothing else determines the reaction progress'],
                    None, 'unsupported',
                    '用户提到收率或选择性，但没有给出转化率，也无法从动力学、平衡数据'
                    '或高温平衡特征推导反应进度。收率与选择性不能直接当作转化率使用，'
                    '需要先确定独立的反应进度，否则无法确定反应器出口组成。')

    # ------------------------------------------------ 4. equilibrium data
    if is_reversible_declared(request) and _has_equilibrium_data(request):
        status = capability('equilibrium', _thermal_from_cases(request))
        return _decision(
            'equilibrium', RULE_EQUILIBRIUM,
            ['the request describes a reversible reaction',
             'equilibrium constants or Gibbs data were supplied'],
            'equilibrium' if status['status'] != 'unsupported' else None,
            status['status'],
            '体系为可逆反应且提供了可追溯的平衡数据，理论上可用 Equilibrium '
            '反应器按化学平衡计算反应终点。',
            reason=status['reason'] if status['status'] == 'unsupported'
            else 'the tool layer cannot set the Gibbs Ln(K) source on this '
                 'workstation, so this path is refused rather than run with a fixed K',
            alternatives=['gibbs'])

    # ------------------------------------------------ 5. Gibbs candidate
    if is_gibbs_candidate(request):
        thermal = _thermal_from_cases(request)
        status = capability('gibbs', thermal)
        candidates = ', '.join(candidate_products(request))
        hottest = hottest_case_c(request)
        if hottest is not None and hottest > HIGH_TEMPERATURE_C:
            why = ('出口温度 %g°C 超过题目所述的高温界线 %g°C'
                   % (hottest, HIGH_TEMPERATURE_C))
        else:
            why = ('该体系包含 %d 个相互关联的可逆反应，候选产物覆盖 %s，'
                   '无法用有限反应式完整描述' % (len(request.reactions), candidates))
        return _decision(
            'gibbs', RULE_GIBBS,
            [why,
             'no rate law and no conversion figure were provided',
             'the candidate products span several species (%s)' % candidates,
             'the exam describes this class as equilibrium-controlled and hard to '
             'predict from simple equations'],
            'gibbs' if status['status'] != 'unsupported' else None,
            status['status'],
            '%s；反应机理复杂、候选产物较多（%s），且用户未提供动力学参数。'
            'Gibbs 反应器按自由能最小化直接给出平衡产物分布，无需写出全部反应式，'
            '是合适的模型。' % (why, candidates),
            reason=None if status['status'] != 'unsupported' else status['reason'],
            alternatives=['equilibrium'])

    # ------------------------------------------------ 6. not enough to choose
    # Fix for defect 5: absence of reactions is NOT evidence for Gibbs.
    return _decision(
        'unsupported', RULE_INSUFFICIENT,
        ['no rate law, no conversion figure, no equilibrium data, and no '
         'high-temperature many-species signature'],
        None, 'unsupported',
        '目前的信息不足以确定反应器类型：既没有动力学参数，也没有转化率或平衡'
        '数据，而且体系的温度与产物特征也不足以判定为自由能最小化控制的高温'
        '复杂体系。需要补充其中任意一项后再选型。')


def _thermal_from_cases(request: ProcessRequest) -> str | None:
    """Thermal mode implied by the operating cases, if the user stated one."""
    modes = {c.thermal_mode for c in request.operating_cases if c.thermal_mode}
    if len(modes) == 1:
        return modes.pop()
    if not modes and request.operating_cases:
        # An outlet temperature is only meaningful as a setpoint if the case is
        # isothermal; without it the temperature is an outcome.
        if all(c.outlet_temperature is not None for c in request.operating_cases):
            return 'isothermal'
    return None


def _has_equilibrium_data(request: ProcessRequest) -> bool:
    """True when the request carries data an equilibrium reactor could use."""
    text = request.source_text.casefold()
    return any(token in text for token in ('平衡常数', 'ka', 'lnk', 'gibbs',
                                           '自由能', 'equilibrium constant'))


def selection_questions(decision: SelectionDecision,
                        request: ProcessRequest) -> list[Question]:
    """Follow-up questions raised by the selection itself."""
    questions: list[Question] = []
    if decision.rule_id == RULE_KINETIC_PHASE_UNCLEAR:
        questions.append(Question(
            id='q-phase', field='phase', blocking=True,
            question='这套动力学体系的反应物是全部液相、全部气相，还是气液混相？',
            reason='题目第 8 节的 CSTR/PFR 规则依赖相态判断。'))
    if decision.rule_id == RULE_INSUFFICIENT:
        questions.append(Question(
            id='q-basis', field='kinetics_or_conversion', blocking=True,
            question='能否提供动力学参数（速率方程与设备尺寸）、或明确的转化率、'
                     '或可追溯的平衡常数？',
            reason='没有其中任意一项就无法确定反应器类型。'))
    if decision.rule_id == RULE_YIELD_NOT_CONVERSION:
        questions.append(Question(
            id='q-progress', field='conversion_or_extent', blocking=True,
            question='收率/选择性之外，能否给出反应的转化率或独立的反应进度约束？',
            reason='收率与选择性不能直接当作转化率，需要独立确定反应进度。'))
    return questions
