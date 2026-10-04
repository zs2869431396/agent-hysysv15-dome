"""Graph state: what travels between nodes, and how it is summarised.

The state must stay JSON-serialisable, because a SQLite checkpoint writes it to disk
and reads it back. That rules out carrying an `AgentRun`, a `Path`, or a model client
- all of which appear in the single-pass `pipeline`. This is the price of being able
to pause a run and continue it in another process, and it is worth paying: a HYSYS
run is an expensive side effect that must not be repeated.

Every list-valued field replaces rather than accumulates. See `_replace`.
"""
from __future__ import annotations

from typing import Annotated, Any, TypedDict


def _replace(_old, new):
    """Reducer that replaces rather than accumulates.

    GWOA needed this for its accumulated fields: with `operator.add` a second turn
    appended to the first turn's list, and a summary computed over it was wrong. The
    same trap applies to questions and problems here - a resumed run would otherwise
    report the questions it has already had answered - so every list replaces.
    """
    return new


class AgentState(TypedDict, total=False):
    """Everything the graph carries between nodes. Must stay JSON-serialisable."""
    text: str
    scenario_label: str
    kind: str
    phase: str
    feed_basis: str
    allowed_ungrounded: list[str]

    facts: dict[str, Any]
    extraction_error: str | None
    extraction_attempts: int
    review: dict[str, Any]
    ungrounded: Annotated[list[str], _replace]

    answers: dict[str, Any]
    pending_questions: Annotated[list[dict[str, Any]], _replace]
    problems: Annotated[list[str], _replace]
    applied: Annotated[list[str], _replace]
    notes: Annotated[list[str], _replace]
    assumptions: Annotated[list[dict[str, Any]], _replace]

    components: list[str]
    thermal_mode: str
    decision: dict[str, Any]
    cases: Annotated[list[dict[str, Any]], _replace]
    blocking: Annotated[list[str], _replace]
    open_questions: Annotated[list[str], _replace]

    status: str
    explanation: str
    executions: Annotated[list[dict[str, Any]], _replace]

    # How many times the run has paused for clarification, and which ungrounded
    # fields the user has actually confirmed. Together these bound the pause loop
    # without ever silently dropping a question that is still open.
    clarification_rounds: int
    resolved_fields: Annotated[list[str], _replace]


def initial_state(text: str, *, scenario_label: str = '', kind: str = '',
                  phase: str = 'mixed', feed_basis: str = 'molar_fraction',
                  allowed_ungrounded: set[str] | None = None) -> AgentState:
    """A fully-populated starting state.

    Every list is present rather than absent. A missing key would take the plain
    (non-reducer) update path in some LangGraph versions and silently drop a later
    write, and the failure would look like a node doing nothing.
    """
    return AgentState(text=text, scenario_label=scenario_label, kind=kind,
                      phase=phase, feed_basis=feed_basis,
                      allowed_ungrounded=sorted(allowed_ungrounded or set()),
                      answers={}, pending_questions=[], problems=[], cases=[],
                      blocking=[], open_questions=[], assumptions=[],
                      ungrounded=[], executions=[], status='',
                      clarification_rounds=0, resolved_fields=[])


def state_summary(state: AgentState) -> dict[str, Any]:
    """The JSON-safe view of a final state, for artifacts and tests."""
    return {
        'status': state.get('status'),
        'reactor': state.get('decision', {}).get('executed'),
        'capability': state.get('decision', {}).get('capability'),
        'rule': state.get('decision', {}).get('rule'),
        'cases': [c['case_id'] for c in (state.get('cases') or [])],
        'blocking_questions': [q['question'] for q in (state.get('blocking') or [])],
        'assumptions_we_made': [
            {'field': a['field'], 'value': a['value'], 'scope': a['scope']}
            for a in (state.get('assumptions') or [])
            if a.get('source') == 'agent_default'],
        'executions': state.get('executions') or [],
        'problems': state.get('problems') or [],
        'input_review': state.get('review') or {},
        'applied': state.get('applied') or [],
    }
