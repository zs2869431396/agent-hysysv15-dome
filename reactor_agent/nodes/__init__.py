"""One module per stage of the graph, plus the state they share.

    intake   natural language -> checked facts (the only node that calls a model)
    plan     facts -> normalised request, selection, compiled and pre-checked spec
    ask      the pause: hands questions to a human, and does nothing else
    execute  the ONLY node with side effects, guarded by the status plan produced
    explain  the run's own record -> something a person can read

Splitting the stages out of `graph.py` keeps that module to one job - wiring - and
makes the rule that matters easy to see: exactly one node writes to the outside
world, and it cannot run while a question is outstanding.

`pipeline.py` remains the single-pass version of the same work, used by the CLI's
default path and by callers that do not need to pause.
"""

from .answers import apply_answers, confirmation_text, parse_answer_value, resolve_ungrounded
from .ask import ask_node, gate, route_after_plan
from .execute import make_execute_node
from .explain import explain_node
from .intake import make_intake
from .plan import make_plan_node, question_to_dict, still_missing
from .state import AgentState, initial_state, state_summary

__all__ = [
    'AgentState', 'initial_state', 'state_summary',
    'apply_answers', 'parse_answer_value', 'resolve_ungrounded', 'confirmation_text',
    'make_intake',
    'make_plan_node', 'question_to_dict', 'still_missing',
    'ask_node', 'gate', 'route_after_plan',
    'make_execute_node',
    'explain_node',
]
