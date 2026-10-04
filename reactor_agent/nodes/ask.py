"""The `ask` node: pause for a human, and do nothing else.

**This node has no side effects, and that is the point.** Nothing is simulated, no
file is written, no case is opened while a question is outstanding. Mixing the
interrupt into the node that runs the simulation is how a paused run ends up having
already done half its work - so the pause is its own node, and `execute` is guarded
by the status that `plan` produced.

The rule this node follows when a reply arrives:

    release only what the user actually answered, and only if the answer parsed.

It used to release everything. That produced a hole worse than no check at all: a
fabricated flow was correctly blocked, the user replied "not a number", and the
fabricated value was then handed to the adapter - having *appeared* to be confirmed.
`resolve_ungrounded` in `answers.py` is the fix; this node just applies it.
"""
from __future__ import annotations

from typing import Any

from langgraph.types import Command, interrupt

from .answers import resolve_ungrounded
from .state import AgentState

# How many times a run will pause for the same unanswered question before it gives up
# and reports instead. A human answering would never reach this; an automated caller
# that keeps replying with something unreadable would otherwise loop forever.
MAX_CLARIFICATION_ROUNDS = 5


def ask_node(state: AgentState) -> Command:
    """Pause for the user, then release only the fields they actually answered."""
    answer = interrupt({
        'kind': 'clarification',
        'scenario': state.get('scenario_label', ''),
        'round': int(state.get('clarification_rounds') or 0) + 1,
        'questions': state.get('pending_questions') or [],
    })
    answers = dict(state.get('answers') or {})
    if isinstance(answer, dict):
        answers.update(answer)

    resolved, unresolved, notes = resolve_ungrounded(
        list(state.get('ungrounded') or []), answer if isinstance(answer, dict) else {})
    from .answers import apply_answers
    merged = apply_answers(state.get('facts') or {}, answers)
    reviewed_before = {q['field'] for q in (state.get('facts') or {}).get('_review_questions', [])}
    reviewed_after = {q['field'] for q in merged.get('_review_questions', [])}
    confirmed_review = reviewed_before - reviewed_after
    newly_resolved = [f for f in unresolved if any(
        f == name or f.startswith(name + '[') for name in confirmed_review)]
    resolved.extend(newly_resolved)
    unresolved = [f for f in unresolved if f not in newly_resolved]

    return Command(goto='plan', update={
        'answers': answers,
        'pending_questions': [],
        # Only the confirmed fields stop blocking. Anything unanswered, or answered
        # with something unreadable, keeps blocking and will be asked again.
        'ungrounded': unresolved,
        'clarification_rounds': int(state.get('clarification_rounds') or 0) + 1,
        'problems': list(state.get('problems') or []) + notes,
        'resolved_fields': sorted(set(state.get('resolved_fields') or []) | set(resolved)),
    })


def route_after_plan(state: AgentState) -> str:
    """Ask when something is still unresolved, run when ready, otherwise explain.

    Routing happens *after* `plan`, never before: questions are produced by
    normalisation and by the compiler, and asking earlier would mean asking about
    something `plan` was about to resolve on its own - a per-case pressure, for
    instance, never becomes a question.

    There is deliberately no "already answered once, so stop asking" rule. That rule
    meant a second, still-unresolved question was silently dropped and the run ended
    with an empty question list. Progress is bounded by `clarification_rounds`
    instead, so a person can keep answering while a stuck automated caller cannot
    loop forever.
    """
    if state.get('blocking'):
        rounds = int(state.get('clarification_rounds') or 0)
        if rounds >= MAX_CLARIFICATION_ROUNDS:
            return 'explain'
        return 'ask'
    return 'execute' if state.get('status') == 'READY' else 'explain'


def gate(state: AgentState) -> str:
    """Unused by the compiled graph; kept because the name documents the decision.

    The real routing is `route_after_plan`. This function exists so that a reader
    looking for "where does it decide to ask" finds an answer explaining why the
    decision cannot be made at the entry point.
    """
    return 'ask' if state.get('pending_questions') else 'plan'
