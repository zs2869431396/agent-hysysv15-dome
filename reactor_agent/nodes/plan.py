"""The `plan` node: facts in, a compiled and pre-checked plan out.

Deterministic throughout - no model, no file writes, no HYSYS. Given the same facts
it produces the same plan, which is what makes the same request reproducible.

Order matters and is not arbitrary:

    merge answers -> re-derive complaints -> normalise -> select -> compile
        -> collect ALL open questions -> precheck

Two things here were wrong once and are worth keeping in mind.

**Questions are collected after compiling, not before.** The compiler adds its own
blocking questions (`feed_questions`, `coal_questions`) and returns WAITING_INPUT. The
node used to build its question list from normalisation alone, so those extra
questions never reached the graph: the run stopped with an empty question list and
asked the user nothing. Reading `plan.questions` afterwards - which is where
normalisation's questions also live - is what makes the two sources agree.

**Complaints are re-derived, never inherited.** After the user answers, "required
field came back empty" and "this may be invented" are no longer true for the fields
they answered, and repeating them would say their answer was ignored. So those lines
are dropped and rebuilt from the current facts and the current ungrounded set.
"""
from __future__ import annotations

from typing import Any

from ..compiler import CompileError, compile_plan
from ..extraction import required_kind_for
from ..normalize import normalize
from ..pipeline import FAILED, READY, build_plan, precheck_spec
from ..schemas import Question
from ..selection import select_reactor
from .answers import apply_answers, confirmation_text
from .state import AgentState

# Complaints that must be re-derived after answers are merged rather than inherited.
_REDERIVED_PREFIXES = ('required field', 'model call failed', 'extraction failed')
_REDERIVED_SUBSTRING = 'cannot be found in the request'


def question_to_dict(question: Question) -> dict[str, Any]:
    """A question in the JSON-safe form the state carries."""
    return {'id': question.id, 'field': question.field,
            'question': question.question, 'reason': question.reason,
            'blocking': question.blocking}


def still_missing(facts: dict[str, Any], kind: str) -> list[str]:
    """Required fields that are still absent, judged on the merged facts.

    `feed_pressure` counts as present when it arrived per operating case, matching
    `Extraction.gaps` - otherwise a reformer run would be told it is missing a
    pressure it has already given.
    """
    from ..extraction import REQUIRED_BY_KIND
    missing: list[str] = []
    for name in REQUIRED_BY_KIND.get(kind, ()):
        if facts.get(name) is not None:
            continue
        if name == 'feed_pressure' and any(
                value is not None for value in (facts.get('case_pressures') or [])):
            continue
        missing.append(name)
    return missing


def collect_blocking(questions: list[Question]) -> list[dict[str, Any]]:
    """Every open blocking question, from every source, in one list."""
    return [question_to_dict(q) for q in questions if q.blocking and q.is_open()]


def make_plan_node():
    """Build the plan node. It closes over nothing mutable, so one instance per
    graph is fine and several graphs can share it."""

    def plan_node(state: AgentState) -> dict[str, Any]:
        answer_notes: list[str] = []
        facts = apply_answers(state.get('facts') or {}, state.get('answers') or {},
                              note=answer_notes)
        kind = state.get('kind', 'conversion')
        ungrounded = list(state.get('ungrounded') or [])

        problems = [p for p in (state.get('problems') or [])
                    if not p.startswith(_REDERIVED_PREFIXES)
                    and _REDERIVED_SUBSTRING not in p
                    and 'could not be read as a number' not in p]
        for field in ungrounded:
            problems.append('%r is still unconfirmed in this request, so it may be '
                            'invented' % field)
        problems.extend(answer_notes)

        request, report = normalize(facts, confirmation_text(state.get('answers') or {},
                                                           state['text']),
                                    scenario_label=state.get('scenario_label', ''),
                                    phase=state.get('phase', 'mixed'),
                                    feed_basis=state.get('feed_basis',
                                                         'molar_fraction'),
                                    ungrounded=ungrounded)
        decision = select_reactor(request,
                                 solid_phase=request.has_solid_reactant)

        # The required set belongs to the reactor the facts selected, not to whatever
        # the caller guessed. A Gibbs request has no conversion figure by definition,
        # so asking for one before selecting reported a gap that could never be
        # filled - which is what a custom `--text` request used to hit.
        effective_kind = kind or required_kind_for(decision.execution_reactor
                                                   or decision.preferred_reactor)
        for name in still_missing(facts, effective_kind):
            problems.append('required field %r is still missing for a %s case'
                            % (name, effective_kind))

        plan = build_plan(request, decision, report)

        decision_payload = {
            'preferred': decision.preferred_reactor,
            'executed': decision.execution_reactor,
            'capability': decision.capability_status,
            'rule': decision.rule_id,
            'explanation': decision.explanation,
            'fallback_reason': decision.fallback_reason,
            'substituted': decision.was_substituted(),
        }

        try:
            compiled = compile_plan(plan)
        except CompileError as exc:
            problems.append('compilation failed: %s' % exc)
            compiled = plan

        # Collected AFTER compiling: the compiler contributes its own blocking
        # questions, and they live in `plan.questions` alongside normalisation's.
        blocking = collect_blocking(plan.questions)
        open_questions = [q.question for q in plan.questions if not q.blocking]

        cases = [{'case_id': c.case_id, 'spec': c.spec}
                 for c in getattr(compiled, 'cases', []) if c.spec]
        status = getattr(compiled, 'status', FAILED) or FAILED

        if status == READY:
            for case in compiled.cases:
                check = precheck_spec(case.spec)
                if not check['ok']:
                    status = FAILED
                    problems.append('case %s failed the pre-check: %s'
                                    % (case.case_id, '; '.join(check['errors'])))

        return {
            'blocking': blocking,
            'open_questions': open_questions,
            'assumptions': [{'field': a.field, 'value': a.value, 'scope': a.scope,
                             'source': a.source} for a in plan.assumptions],
            'applied': list(report.applied),
            'notes': list(report.notes),
            'components': list(request.components),
            'decision': decision_payload,
            'cases': cases,
            'thermal_mode': plan.thermal_mode,
            'status': status,
            # `ask` reads this, so the interrupt carries the actual questions.
            'pending_questions': blocking,
            'problems': problems,
        }

    return plan_node
