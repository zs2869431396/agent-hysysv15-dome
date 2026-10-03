"""The `execute` node: the only node that changes the outside world.

Everything expensive and irreversible happens here, so it is guarded three ways:

  * it does nothing unless the status is READY - a plan waiting on input, or one the
    tool layer refuses, never reaches this node;
  * it does nothing unless an adapter was supplied and a dry run was not requested;
  * it asks the ledger before every case, so a resumed run does not repeat work that
    already succeeded.

Cases that already succeeded count towards completion. Without that, a run in which
every case was skipped would report READY even though the work is demonstrably done -
the status would go backwards, which is a bug this node had.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..adapters.hysys_cli import HysysCliAdapter
from ..adapters.run_store import RunStore
from ..pipeline import FAILED, PARTIAL, PASS, READY
from ..report import results_view
from .state import AgentState


def make_execute_node(adapter: HysysCliAdapter | None, run_root: Path,
                      enabled: bool):
    """Build the execution node.

    `enabled` is false for a dry run. It is separate from `adapter is None` so that a
    caller can hold an adapter and still ask for nothing to be started.
    """

    def execute_node(state: AgentState) -> dict[str, Any]:
        if not enabled or adapter is None or state.get('status') != READY:
            return {'executions': []}

        store = RunStore(run_root, run_id=state.get('scenario_label', 'run'))
        executions: list[dict[str, Any]] = []
        problems = list(state.get('problems') or [])
        expected = len(state.get('cases') or [])
        skipped: list[str] = []
        succeeded: list[str] = []

        for case in state.get('cases') or []:
            case_id, spec = case['case_id'], case['spec']
            if store.completed(case_id, spec) is not None:
                skipped.append(case_id)
                succeeded.append(case_id)
                problems.append('case %s already has a result for this spec; not '
                                'running it again' % case_id)
                continue

            attempt = store.attempts(case_id) + 1
            outcome = adapter.run_case(spec, case_id, store.root, attempt)
            store.record_result(case_id, spec, attempt, outcome.status,
                                run_dir=str(outcome.run_dir),
                                seconds=round(outcome.seconds, 2),
                                exit_code=outcome.exit_code,
                                tool_status=(outcome.result or {}).get('status'),
                                error_type=outcome.error_type, error=outcome.error,
                                case_file=outcome.case_file)
            executions.append(results_view(outcome))
            if outcome.passed:
                succeeded.append(case_id)

            if outcome.status in ('CANNOT_CONNECT_TO_HYSYS', 'TIMEOUT'):
                # No point trying the rest: the workstation is unavailable.
                problems.append(
                    'the workstation did not answer (%s); the remaining cases '
                    'were not attempted, and the workstation state should be checked '
                    'before resuming' % outcome.status)
                break
            if not outcome.passed:
                problems.append('case %s did not pass: %s'
                                % (case_id, outcome.error or outcome.status))

        attempted = len(skipped) + len(executions)
        if attempted == 0:
            status = state.get('status', READY)
        elif len(succeeded) == expected and attempted == expected:
            status = PASS
        elif succeeded:
            status = PARTIAL
        else:
            status = FAILED
        return {'executions': executions, 'status': status, 'problems': problems}

    return execute_node
