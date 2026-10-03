"""Render a saved execution checkpoint without a model call or HYSYS execution."""
from __future__ import annotations

import argparse
from contextlib import closing
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from langgraph.checkpoint.sqlite import SqliteSaver
from reactor_agent.graph import dump_state
from reactor_agent.report import render_report, view_from_state


def read_saved_state(folder: Path) -> dict:
    checkpoint = folder / 'checkpoints.sqlite'
    if not checkpoint.is_file():
        raise ValueError('Saved checkpoints.sqlite not found')
    # Read the original database only; operate on an in-memory snapshot.
    with closing(sqlite3.connect(checkpoint.resolve().as_uri() + '?mode=ro', uri=True)) as source:
        with closing(sqlite3.connect(':memory:', check_same_thread=False)) as snapshot:
            source.backup(snapshot)
            saver = SqliteSaver(snapshot)
            latest = next((item for item in saver.list(None)
                           if not item.config['configurable'].get('checkpoint_ns')), None)
            if latest is None:
                raise ValueError('No top-level checkpoint found')
            state = dict(latest.checkpoint.get('channel_values') or {})
    if not state.get('executions') or state.get('status') not in ('PASS', 'PARTIAL', 'FAILED'):
        raise ValueError('No completed execution state was saved; no simulation was attempted')
    return state


def recover(folder: Path) -> tuple[dict, Path]:
    state = read_saved_state(folder)
    state['explanation'] = render_report(view_from_state(state))
    target = folder / 'recovered-report'
    dump_state(state, target)
    return state, target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, help='original Agent run folder')
    args = parser.parse_args()
    if args.run is None:
        candidates = list((ROOT / 'agent-runs').glob('gasification-*/checkpoints.sqlite'))
        if not candidates:
            print('No saved gasification checkpoint found. Nothing was executed.')
            return 1
        folder = max(candidates, key=lambda path: path.stat().st_mtime).parent
    else:
        folder = args.run.resolve()
    print('Original run:', folder)
    try:
        state, target = recover(folder)
    except (ValueError, sqlite3.Error) as error:
        print('Recovery stopped:', error)
        return 1
    print('Saved execution status:', state['status'])
    print('Recovered report:', target / 'explanation.txt')
    print(state['explanation'])
    return 0


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    raise SystemExit(main())
