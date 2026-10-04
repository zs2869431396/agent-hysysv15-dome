"""The pipeline: one natural-language request, all the way to results.

Written as plain functions rather than as graph nodes on purpose. The interesting
behaviour - deciding, refusing, compiling, executing, summarising - should be
testable without installing or understanding an orchestration framework, and the
framework should only be responsible for pausing and resuming. `graph.py` wraps this.

Stage order, and why it is this order:

    extract -> normalise -> select -> compile -> precheck -> execute -> summarise

Selection happens after normalisation because the rules read facts, not prose.
Compilation happens after selection because the reactor kind decides what the spec
contains. Pre-checking happens before execution because it costs milliseconds and a
HYSYS run costs a subprocess, a case and a minute.

Two hard rules run through the whole thing:

  * **Nothing is invented.** A missing value becomes a question, and the run stops as
    WAITING_INPUT with zero executions.
  * **A dry run touches nothing.** With no adapter, or with `dry_run=True`, the
    pipeline stops after the pre-check and reports the spec it would have run. This
    is how the whole front half is tested on a machine without HYSYS.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from hysys_tools import precheck
from hysys_tools.core import canonical

from .adapters.hysys_cli import ExecutionResult, HysysCliAdapter
from .adapters.run_store import RunStore
from .capabilities import combination_status
from .compiler import CompileError, coal_assumption, compile_plan
from .extraction import extract_verified, required_kind_for
from .llm import ChatClient, LlmError
from .normalize import NormalizationReport, normalize
from .report import render_report, view_from_run
from .schemas import (
    Assumption,
    ModelingPlan,
    OperatingCase,
    ProcessRequest,
    Question,
    SelectionDecision,
)
from .selection import complete_gibbs_candidates, planned_thermal_mode, select_reactor

# Overall task status. Mirrors what the plan promises to report.
WAITING_INPUT = 'WAITING_INPUT'
READY = 'READY'
PASS = 'PASS'
PARTIAL = 'PARTIAL'
FAILED = 'FAILED'
UNSUPPORTED = 'UNSUPPORTED'


@dataclass
class AgentRun:
    """The complete outcome of one request."""
    text: str
    scenario_label: str = ''
    status: str = WAITING_INPUT
    request: ProcessRequest | None = None
    report: NormalizationReport | None = None
    decision: SelectionDecision | None = None
    plan: ModelingPlan | None = None
    executions: list[ExecutionResult] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    input_review: dict[str, Any] = field(default_factory=dict)
    explanation: str = ''
    run_root: Path | None = None
    started_at: str = ''
    finished_at: str = ''

    # ------------------------------------------------------------- reporting
    @property
    def blocking_questions(self) -> list[str]:
        return [q.question for q in self.blocking_question_objects()]

    def blocking_question_objects(self) -> list[Question]:
        """The open blocking questions themselves, defaults included.

        The report needs the suggested answers as well as the text, and re-deriving
        them from the plan here keeps one source of truth for what was asked.
        """
        if self.plan is not None:
            return self.plan.blocking_questions()
        if self.report is not None:
            return self.report.blocking
        return []

    @property
    def assumptions_we_made(self) -> list[dict[str, Any]]:
        """Values this agent chose, which must be stated in any report."""
        if self.plan is None:
            return []
        return [{'id': a.id, 'field': a.field, 'value': a.value, 'scope': a.scope,
                 'accepted': a.accepted}
                for a in self.plan.assumptions if a.source == 'agent_default']

    def spec(self, case_id: str | None = None) -> dict[str, Any] | None:
        if self.plan is None or not self.plan.cases:
            return None
        if case_id is None:
            return self.plan.cases[0].spec
        for case in self.plan.cases:
            if case.case_id == case_id:
                return case.spec
        return None

    def summary(self) -> dict[str, Any]:
        """JSON-safe overview, suitable for a manifest or a UI."""
        return {
            'scenario': self.scenario_label,
            'status': self.status,
            'reactor': {
                'preferred': self.decision.preferred_reactor if self.decision else None,
                'executed': self.decision.execution_reactor if self.decision else None,
                'capability': self.decision.capability_status if self.decision else None,
                'rule': self.decision.rule_id if self.decision else None,
            },
            'cases': [c.case_id for c in (self.plan.cases if self.plan else [])],
            'blocking_questions': self.blocking_questions,
            'assumptions_we_made': self.assumptions_we_made,
            'executions': [e.summary() for e in self.executions],
            'problems': self.problems,
            'input_review': self.input_review,
            'started_at': self.started_at,
            'finished_at': self.finished_at,
        }


def build_plan(request: ProcessRequest, decision: SelectionDecision,
               report: NormalizationReport, heat_mode: str | None = None
               ) -> ModelingPlan:
    """Assemble the plan, keeping questions and assumptions attached to it.

    Thermal mode: when the caller states one, that is used. Otherwise it is the mode
    selection already decided on (`selection.planned_thermal_mode`), so the lookup
    performed during selection and the spec that is actually compiled can never
    disagree about whether the case is adiabatic - which is exactly how toluene came
    to be reported experimental while being run adiabatically.
    """
    defaulted = heat_mode is None
    if heat_mode is None:
        heat_mode = planned_thermal_mode(request)
    cases = [OperatingCase(case_id=c.case_id, label=c.label)
             for c in request.operating_cases] or [OperatingCase(case_id='single')]

    # A Gibbs reactor can only distribute among the candidates it is given. When the
    # process is a black box the reaction list does not name them all, so the candidate
    # set is completed from the elements already present, and the completion is
    # declared - it is our choice, not something the user wrote.
    components = list(request.components)
    if decision.execution_reactor == 'gibbs':
        components, added = complete_gibbs_candidates(components)
        if added:
            report.assumptions.append(Assumption(
                id='a-gibbs-candidates', field='fluid_package.components',
                value=added, source='agent_default', accepted=False,
                scope=('Gibbs 只在给定组分中按自由能最小分配产物；题目未逐一列出，'
                       '按进料元素补齐候选产物 %s' % '、'.join(added))))

    plan = ModelingPlan(request=request, decision=decision,
                        components=components,
                        thermal_mode=heat_mode, cases=cases)
    plan.questions.extend(report.questions)
    plan.assumptions.extend(report.assumptions)

    solid_carbon = _feed_has_carbon(request)
    if solid_carbon and decision.execution_reactor == 'gibbs':
        # The tool layer refuses a plain Gibbs reactor with library Carbon before it
        # solves, so the saturated-carbon route is what actually runs. It changes what
        # the outlet means, so it is declared rather than left implicit.
        plan.assumptions.append(Assumption(
            id='a-solid-carbon-route', field='reactor.solid_carbon',
            value='saturation', source='derived', accepted=True,
            scope=('转化率反应器加仅含气相的 Gibbs 反应器，外层求解使气相碳活度为 1；'
                   '不使用 HYSYS 库 Carbon 的 Gibbs 数据；未反应碳会出现在'
                   '名为 LIQUID 的物流中，实为固相')))
        if not any(canonical(name) == 'oxygen' for name in components):
            # Not blocking: the temperature is still achievable with external heat,
            # but the duty then means something different from autothermal gasification.
            plan.questions.append(Question(
                id='q-no-oxygen', field='feeds[0].oxygen', blocking=False,
                question='题目没有给出氧气进料，维持出口温度需要外部供热；'
                         '报告的热负荷是外供热，不是自热气化。'))

    coal = coal_assumption(request)
    if coal is not None:
        plan.assumptions.append(coal)

    if decision.execution_reactor == 'equilibrium':
        plan.assumptions.append(Assumption(
            id='a-equilibrium-k', field='reactions.equilibrium_constant',
            value='由 HYSYS 组分 Gibbs 数据拟合', source='derived', accepted=True,
            scope=('平衡常数由 HYSYS 组分 Gibbs 数据在出口温度上下 150 K 内拟合 '
                   'ln K = A + B/T + C·ln T；工具层校验拟合残差与出口 Q/K；'
                   'Q/K 接近 1 不能证明高温区 Gibbs 数据本身准确')))

    if defaulted:
        # Choosing the thermal boundary is a modelling decision, and "adiabatic
        # because nobody said otherwise" has a real effect on the duty. A reader who
        # cannot tell it apart from a stated condition cannot judge the result.
        word = '等温' if heat_mode == 'isothermal' else '绝热'
        english = 'isothermal' if heat_mode == 'isothermal' else 'adiabatic'
        stated = bool(re.search(word + '|' + english, request.source_text, re.I))
        if re.search(r'(?:不|非|未|没有|无|not\s+|non[- ])[^。；;，,\n]{0,6}(?:' + word + '|' + english + ')',
                     request.source_text, re.I):
            stated = False
        plan.assumptions.append(Assumption(
            id='a-thermal-mode', field='thermal_mode', value=heat_mode,
            source='user_text' if stated else 'agent_default', accepted=stated,
            scope=('题目明确指定%s工况' % word if stated else
                   '题面给出了出口温度，按等温处理（出口温度由外部热流维持）'
                   if request.operating_cases
                   else '题面未说明热边界，按绝热处理')))
    return plan


def _feed_has_carbon(request: ProcessRequest) -> bool:
    """True when a feed actually contains carbon, which selects the saturation route.

    Judged from the composition, not from the component list: a candidate set may name
    Carbon as a possible product while the feed is pure gas.
    """
    for feed in request.feeds:
        for source in (feed.fractions, feed.flows):
            for name, value in source.items():
                if canonical(name) == 'carbon' and float(value) > 0:
                    return True
    return False


def precheck_spec(spec: dict[str, Any] | None) -> dict[str, Any]:
    """Run the tool layer's own pre-check; never raises."""
    if spec is None:
        return {'ok': False, 'errors': ['no spec was produced'], 'warnings': []}
    return precheck.validate_spec(spec)


def _still_missing(facts: dict[str, Any], kind: str) -> list[str]:
    """Required fields still absent for a scenario, judged on the extracted facts.

    `feed_pressure` counts as present when it arrived per operating case, matching
    `Extraction.gaps` - otherwise a reformer run would be told it is missing a
    pressure it has already given.
    """
    if not kind:
        return []
    from .extraction import REQUIRED_BY_KIND
    missing: list[str] = []
    for name in REQUIRED_BY_KIND.get(kind, ()):
        if facts.get(name) is not None:
            continue
        if name == 'feed_pressure' and any(
                value is not None for value in (facts.get('case_pressures') or [])):
            continue
        missing.append(name)
    return missing


def run_pipeline(text: str, *, scenario_label: str = '', kind: str = '',
                 phase: str = 'mixed', feed_basis: str = 'molar_fraction',
                 client: ChatClient | None = None,
                 adapter: HysysCliAdapter | None = None,
                 run_root: Path | None = None,
                 dry_run: bool = True,
                 allowed_ungrounded: set[str] | None = None) -> AgentRun:
    """Take one request as far as the arguments allow.

    Stops at the first stage that cannot proceed, and says why. Execution only
    happens when the plan is READY, an adapter was supplied and `dry_run` is false.

    `run_root` is THIS RUN's directory, used exactly as given - the caller is
    responsible for making it unique (a timestamped folder, like the tool layer's
    own validation runner). The ledger lives there, and every case attempt creates a
    fresh child directory inside it. Reusing a `run_root` is supported and means
    "continue this run": cases that already succeeded with the same spec are skipped.
    """
    run = AgentRun(text=text, scenario_label=scenario_label,
                   started_at=datetime.now().isoformat(timespec='seconds'))

    # ------------------------------------------------------------ ② extract
    if client is None:
        run.status = FAILED
        run.problems.append('no model client was supplied')
        return _finish(run)

    try:
        extraction, problems = extract_verified(
            client, text, kind, allowed=allowed_ungrounded or set())
    except LlmError as exc:
        run.status = FAILED
        run.problems.append('model call failed: %s' % exc)
        return _finish(run)

    run.problems.extend(problems)
    run.input_review = extraction.facts.get('_review_record') or {}
    if extraction.error:
        run.status = FAILED
        run.problems.append('extraction failed: %s' % extraction.error)
        return _finish(run)

    # --------------------------------------------------------- ③ normalise
    # Ungrounded fields become blocking questions here, so a fabricated value can
    # never reach the compiler. Reporting it without blocking was a real defect: the
    # value stayed in `facts` and would have been used.
    request, report = normalize(extraction.facts, text,
                                scenario_label=scenario_label, phase=phase,
                                feed_basis=feed_basis,
                                ungrounded=extraction.ungrounded)
    run.request = request
    run.report = report

    # ------------------------------------------------------------ ④ select
    decision = select_reactor(request,
                             solid_phase=request.has_solid_reactant)
    run.decision = decision

    # Required fields are judged against the reactor the facts selected, not against a
    # guess made before extraction. See `extraction.required_kind_for`.
    effective_kind = kind or required_kind_for(decision.execution_reactor
                                               or decision.preferred_reactor)
    for name in _still_missing(extraction.facts, effective_kind):
        run.problems.append('required field %r is still missing for a %s case'
                            % (name, effective_kind))

    # ----------------------------------------------------------- ⑤ compile
    plan = build_plan(request, decision, report)
    run.plan = plan
    try:
        compiled = compile_plan(plan)
    except CompileError as exc:
        run.status = FAILED
        run.problems.append('compilation failed: %s' % exc)
        return _finish(run)

    if compiled.status == WAITING_INPUT:
        run.status = WAITING_INPUT
        run.explanation = render_report(view_from_run(run))
        return _finish(run)
    if compiled.status == 'UNSUPPORTED' or not decision.is_executable():
        run.status = UNSUPPORTED
        run.explanation = render_report(view_from_run(run))
        return _finish(run)

    # ---------------------------------------------------------- ⑥ precheck
    for case in compiled.cases:
        check = precheck_spec(case.spec)
        if not check['ok']:
            run.status = FAILED
            run.problems.append('case %s failed the pre-check: %s'
                                % (case.case_id, '; '.join(check['errors'])))
            return _finish(run)

    run.status = READY

    # ----------------------------------------------------------- ⑦ execute
    if dry_run or adapter is None:
        run.explanation = render_report(view_from_run(run))
        return _finish(run)

    store = RunStore(run_root or Path('runs'), run_id=scenario_label or 'run')
    run.run_root = store.root
    skipped: list[str] = []
    for case in compiled.cases:
        # Never redo a job that already succeeded with this exact spec.
        if not store.needs_running(case.case_id, case.spec):
            skipped.append(case.case_id)
            run.problems.append('case %s already has a result for this spec; not '
                                'running it again' % case.case_id)
            continue
        attempt = store.attempts(case.case_id) + 1
        outcome = adapter.run_case(case.spec, case.case_id, store.root, attempt)
        store.record_result(case.case_id, case.spec, attempt, outcome.status,
                            run_dir=str(outcome.run_dir),
                            seconds=round(outcome.seconds, 2),
                            exit_code=outcome.exit_code,
                            tool_status=(outcome.result or {}).get('status'),
                            error_type=outcome.error_type, error=outcome.error,
                            case_file=outcome.case_file)
        run.executions.append(outcome)
        if outcome.status in ('CANNOT_CONNECT_TO_HYSYS', 'TIMEOUT'):
            # No point trying the remaining cases: the workstation is either
            # unavailable or was left in an unknown state by the timeout.
            run.problems.append(
                'the workstation did not answer (%s); the remaining cases were not '
                'attempted, and the workstation state should be checked before '
                'resuming' % outcome.status)
            break
        if not outcome.passed:
            run.problems.append('case %s did not pass: %s'
                                % (case.case_id, outcome.error or outcome.status))

    run.status = _status_from_executions(run.executions, len(compiled.cases),
                                         already_done=len(skipped))
    run.explanation = render_report(view_from_run(run))
    return _finish(run)


def _status_from_executions(executions: list[ExecutionResult],
                            expected: int, already_done: int = 0) -> str:
    """PASS only when every awaited case passed; PARTIAL when some did.

    `already_done` counts cases the ledger says have already succeeded. Without it a
    resumed run - where every case is skipped and `executions` is therefore empty -
    reported FAILED, which is the opposite of what happened. The graph node had the
    same bug and was fixed the same way; the two paths disagreeing is how it survived
    in one of them.
    """
    attempted = already_done + len(executions)
    if attempted == 0:
        return FAILED
    passed = len([e for e in executions if e.passed]) + already_done
    if passed == expected and attempted == expected:
        return PASS
    if passed:
        return PARTIAL
    return FAILED


def _finish(run: AgentRun) -> AgentRun:
    run.finished_at = datetime.now().isoformat(timespec='seconds')
    return run


def write_run_artifacts(run: AgentRun, folder: Path) -> list[str]:
    """Persist the run: summary, every spec, and the request as understood."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    def dump(name: str, payload: Any) -> None:
        path = folder / name
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding='utf-8')
        written.append(name)

    dump('run.json', run.summary())
    if run.plan is not None:
        for case in run.plan.cases:
            if case.spec:
                dump('spec-%s.json' % case.case_id, case.spec)
    if run.request is not None:
        dump('request.json', run.request.model_dump())
    if run.report is not None:
        dump('normalization.json', {'applied': run.report.applied,
                                    'notes': run.report.notes,
                                    'questions': [q.model_dump()
                                                  for q in run.report.questions]})
    return written
