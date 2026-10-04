"""The main graph: a request that can pause to ask, then resume and run.

`pipeline.run_pipeline` already does the work in one pass. What this module adds is
the ability to stop in the middle, hand questions to a human, and continue later
without redoing anything - which is what the exam's scenarios need, because one of
them is genuinely under-specified and waiting for an answer is the correct behaviour,
not a failure.

    START -> intake -> review -> plan -+-> ask (interrupt) -> plan -> execute -> explain -> END
                             +-> execute -> explain -> END
                             +-> explain -> END

The stages live in `nodes/`, one module each; this module only wires them together
and owns the two things that are not a stage:

  * **checkpointing** - SQLite by default, so a paused run survives the process
    exiting. State is plain JSON for that reason.
  * **artifact output** - the final state and every compiled spec, written at the end.

The wiring encodes the rule the design rests on: `execute` is the only node with side
effects, and `plan` decides whether it may run. A question that is outstanding means
zero cases opened, and that is enforced by routing rather than by remembering to
check.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from .adapters.hysys_cli import HysysCliAdapter
from .llm import ChatClient
from .review import make_review
from .nodes import (
    AgentState,
    apply_answers,
    ask_node,
    explain_node,
    initial_state,
    make_execute_node,
    make_intake,
    make_plan_node,
    parse_answer_value,
    question_to_dict,
    route_after_plan,
    state_summary,
)

# Private aliases kept so existing callers and tests keep working after the split of
# the nodes into their own package. New code should import from `nodes`.
_apply_answers = apply_answers
_question_to_dict = question_to_dict
_route_after_plan = route_after_plan

__all__ = [
    'AgentState', 'build_graph', 'dump_state', 'initial_state',
    'open_checkpointer', 'parse_answer_value', 'state_summary',
]


def build_graph(client: ChatClient, *, adapter: HysysCliAdapter | None = None,
                run_root: Path | None = None, dry_run: bool = True,
                checkpointer: Any = None):
    """Assemble the graph. `checkpointer=None` keeps state in memory."""
    graph = StateGraph(AgentState)
    graph.add_node('intake', make_intake(client))
    graph.add_node('review', make_review(client))
    graph.add_node('plan', make_plan_node())
    graph.add_node('ask', ask_node)
    graph.add_node('execute', make_execute_node(adapter,
                                                Path(run_root or 'agent-runs'),
                                                enabled=not dry_run))
    graph.add_node('explain', explain_node)

    graph.add_edge(START, 'intake')
    # `review` first runs the same deterministic pre-check as `plan`, then calls
    # the recovery model only for data gaps. The final plan owns every question;
    # the review model never routes the graph or executes HYSYS.
    graph.add_edge('intake', 'review')
    graph.add_edge('review', 'plan')
    graph.add_conditional_edges(
        'plan', route_after_plan,
        {'ask': 'ask', 'execute': 'execute', 'explain': 'explain'})
    graph.add_edge('ask', 'plan')
    graph.add_edge('execute', 'explain')
    graph.add_edge('explain', END)

    return graph.compile(checkpointer=checkpointer or InMemorySaver())


def open_checkpointer(path: Path):
    """A SQLite checkpointer, so a paused run survives the process exiting."""
    from langgraph.checkpoint.sqlite import SqliteSaver
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return SqliteSaver.from_conn_string(str(path))


def dump_state(state: AgentState, folder: Path) -> list[str]:
    """Write the final state, the explanation and every compiled spec."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    (folder / 'state.json').write_text(
        json.dumps(state_summary(state), ensure_ascii=False, indent=2),
        encoding='utf-8')
    written.append('state.json')
    (folder / 'explanation.txt').write_text(state.get('explanation') or '',
                                            encoding='utf-8')
    written.append('explanation.txt')
    for case in state.get('cases') or []:
        name = 'spec-%s.json' % case['case_id']
        (folder / name).write_text(json.dumps(case['spec'], ensure_ascii=False,
                                              indent=2), encoding='utf-8')
        written.append(name)
    return written
