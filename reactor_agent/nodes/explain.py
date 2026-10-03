"""The `explain` node: turn the run's record into something a person can read.

The text itself lives in `reactor_agent/report.py`, because the single-pass pipeline
produced its own version of the same report and the two disagreed: the pipeline's dry
run said "已完成选型与规格编译" where the graph said "规格已编译并通过预检", and only the
graph listed the substitution and the assumptions. Two writers for one document is a
defect that shows up as soon as a reader compares a dry run with a real one, so there
is one writer and this node calls it.

The old private helpers are re-exported for callers that imported them; they are now
the report module's implementations, not a second copy.
"""
from __future__ import annotations

from typing import Any

from ..report import (  # noqa: F401  (re-exported for backwards compatibility)
    _case_block,
    _comparison_table,
    _fmt,
    _num,
    _outlet_of,
    _percent_map,
    render_report,
    view_from_state,
)
from .state import AgentState


def explain_node(state: AgentState) -> dict[str, Any]:
    """Compose the human-facing explanation from what the run actually established."""
    return {'explanation': render_report(view_from_state(state))}
