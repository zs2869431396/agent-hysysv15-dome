"""Session backend shared by the Streamlit UI and chat service.

Contains model settings, run metadata and access to the existing business graph.
It does not start an HTTP server or provide a second frontend.
"""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .graph import build_graph, dump_state, initial_state, open_checkpointer
from .llm import DEFAULT_BASE, DEFAULT_MODEL, ChatClient, LlmConfig
from .report import render_report, view_from_state

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCENARIO_LABELS = {
    'toluene': '甲苯歧化',
    'smr': '甲烷蒸汽重整',
    'gasification': '水煤浆气化',
}

# The scenarios the page offers, reused from the CLI so the two cannot drift.
def scenarios() -> dict[str, dict[str, str]]:
    from .__main__ import SCENARIOS

    return SCENARIOS


def allowed_ungrounded(scenario: str) -> set[str]:
    from .__main__ import ALLOWED_UNGROUNDED

    return ALLOWED_UNGROUNDED.get(scenario, set())


# Files a run folder may offer for download. Anything else is refused, so a crafted
# name cannot walk out of the run directory.
DOWNLOADABLE = (
    re.compile(r'^spec-[A-Za-z0-9._-]+\.json$'),
    re.compile(r'^state\.json$'),
    re.compile(r'^request\.json$'),
    re.compile(r'^run\.json$'),
    re.compile(r'^normalization\.json$'),
    re.compile(r'^explanation\.txt$'),
    re.compile(r'^paused\.json$'),
    re.compile(r'^process\.json$'),
)

def is_downloadable(name: str) -> bool:
    """True when a file name is inside the whitelist and outside any path."""
    if not name or '/' in name or '\\' in name or name.startswith('.'):
        return False
    if '..' in name:
        return False
    return any(pattern.match(name) for pattern in DOWNLOADABLE)


def parse_env_file(text: str) -> dict[str, str]:
    """`KEY=VALUE` lines from a `.env` file.

    Blank lines and `#` comments are ignored, and quotes around a value are stripped -
    `TR_KEY="abc"` and `TR_KEY=abc` mean the same thing. The program only ever reads
    this file.
    """
    values: dict[str, str] = {}
    for raw in (text or '').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.lower().startswith('export '):
            line = line[len('export '):].strip()
        key, sep, value = line.partition('=')
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def read_dotenv(root: Path | None = None) -> dict[str, str]:
    """The project-root `.env`, if it exists. Never written to."""
    path = (root or PROJECT_ROOT) / '.env'
    try:
        return parse_env_file(path.read_text(encoding='utf-8'))
    except OSError:
        return {}


@dataclass
class Settings:
    """The model connection, in memory only.

    Precedence: what the page sent > the process environment > the project `.env`.
    The public settings view contains only `key_set`, never the key.
    """
    base: str = ''
    model: str = ''
    key: str = ''
    env: dict[str, str] = field(default_factory=dict)

    def resolve(self) -> 'Settings':
        return Settings(base=self.base or self.env.get('TR_BASE') or DEFAULT_BASE,
                        model=self.model or self.env.get('TR_MODEL') or DEFAULT_MODEL,
                        key=self.key or self.env.get('TR_KEY') or '',
                        env=self.env)

    def public(self) -> dict[str, Any]:
        resolved = self.resolve()
        return {'base': resolved.base, 'model': resolved.model,
                'key_set': bool(resolved.key)}

    def config(self) -> LlmConfig:
        resolved = self.resolve()
        return LlmConfig(base=resolved.base, model=resolved.model, key=resolved.key)


@dataclass
class Run:
    """One run of the graph, as the page sees it."""
    run_id: str
    thread_id: str
    folder: Path
    label: str
    execute: bool
    created_at: str
    status: str = 'RUNNING'
    remaining: int = 0


class SessionApp:
    """Per-session model settings, graph runs and checkpoint access."""

    def __init__(self, root: Path | None = None,
                 settings: Settings | None = None) -> None:
        self.root = Path(root or (PROJECT_ROOT / 'agent-runs'))
        self.root.mkdir(parents=True, exist_ok=True)
        base_settings = settings or Settings()
        if not base_settings.env:
            merged = dict(read_dotenv())
            merged.update({k: v for k, v in os.environ.items()
                           if k in ('TR_BASE', 'TR_MODEL', 'TR_KEY')})
            base_settings.env = merged
        self.settings = base_settings
        self.runs: dict[str, Run] = {}
        self._runs_file = self.root / 'web-runs.json'
        self._lock = threading.Lock()
        self._load_runs()

    # ------------------------------------------------------------ persistence
    def _load_runs(self) -> None:
        """Remember run ids across restarts; never remember the key."""
        try:
            payload = json.loads(self._runs_file.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return
        for item in payload if isinstance(payload, list) else []:
            try:
                run = Run(run_id=item['run_id'], thread_id=item['thread_id'],
                          folder=self.root / item['folder'], label=item['label'],
                          execute=bool(item.get('execute')),
                          created_at=item.get('created_at', ''),
                          status=item.get('status', 'PAUSED'))
                if run.folder.is_dir():
                    self.runs[run.run_id] = run
            except (KeyError, TypeError):
                continue

    def _save_runs(self) -> None:
        payload = [{'run_id': run.run_id, 'thread_id': run.thread_id,
                    'folder': run.folder.name, 'label': run.label,
                    'execute': run.execute, 'created_at': run.created_at,
                    'status': run.status} for run in self.runs.values()]
        self._runs_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                   encoding='utf-8')

    # ------------------------------------------------------------------- runs
    def new_run(self, label: str, execute: bool) -> Run:
        run_id = uuid.uuid4().hex[:12]
        folder = self.root / ('%s-web-%s-%s' % (label or 'run',
                                                datetime.now().strftime('%Y%m%d-%H%M%S'),
                                                run_id))
        folder.mkdir(parents=True, exist_ok=True)
        run = Run(run_id=run_id, thread_id='web-%s' % run_id, folder=folder,
                  label=label or 'run', execute=execute,
                  created_at=datetime.now().isoformat(timespec='seconds'))
        with self._lock:
            self.runs[run_id] = run
            self._save_runs()
        return run

    def get_run(self, run_id: str) -> Run | None:
        return self.runs.get(run_id)

    # ------------------------------------------------------------------ graph
    def _open(self, run: Run):
        """The checkpointer for one run; `open_checkpointer` is a context manager."""
        return open_checkpointer(run.folder / 'checkpoints.sqlite')

    def client(self) -> ChatClient:
        """The model client for the current settings. A seam for the tests."""
        return ChatClient(self.settings.config(), logger=lambda _m: None)

    def _build(self, run: Run, saver, progress: bool = False):
        """The graph for one run. `saver` is the value yielded by `_open`."""
        adapter = None
        if run.execute:
            from .adapters.hysys_cli import HysysCliAdapter

            adapter = HysysCliAdapter(PROJECT_ROOT,
                                      python=os.environ.get('HYSYS_AGENT_PYTHON'))
            if progress:
                from .process_trace import ProgressAdapter
                adapter = ProgressAdapter(adapter)
        return build_graph(self.client(), adapter=adapter, run_root=run.folder,
                           dry_run=not run.execute, checkpointer=saver)

    def start(self, run: Run, text: str, scenario: str, kind: str, phase: str,
              feed_basis: str, on_event=None) -> dict[str, Any]:
        with self._open(run) as saver:
            graph = self._build(run, saver, progress=True) if on_event else self._build(run, saver)
            inputs = initial_state(text, scenario_label=run.label, kind=kind, phase=phase,
                                   feed_basis=feed_basis,
                                   allowed_ungrounded=allowed_ungrounded(scenario))
            config = {'configurable': {'thread_id': run.thread_id}}
            if on_event:
                from .process_trace import stream_graph
                return stream_graph(graph, inputs, config, on_event)
            return graph.invoke(inputs, config)

    def answer(self, run: Run, answers: dict[str, Any], on_event=None) -> dict[str, Any]:
        from langgraph.types import Command

        with self._open(run) as saver:
            graph = self._build(run, saver, progress=True) if on_event else self._build(run, saver)
            if on_event:
                from .process_trace import stream_graph
                return stream_graph(graph, Command(resume=answers),
                                    {'configurable': {'thread_id': run.thread_id}}, on_event)
            return graph.invoke(Command(resume=answers),
                                {'configurable': {'thread_id': run.thread_id}})

    def current_state(self, run: Run) -> dict[str, Any]:
        """The checkpointed state, read back without running anything."""
        with self._open(run) as saver:
            graph = self._build(run, saver)
            snapshot = graph.get_state({'configurable': {'thread_id': run.thread_id}})
            return dict(snapshot.values or {})

    # --------------------------------------------------------------- payloads
    def run_payload(self, run: Run, state: dict[str, Any] | None = None) -> dict:
        """Everything the page needs to draw one run, including the questions."""
        questions = questions_of(state) if state else self._pending_questions(run)
        report = ''
        if state:
            report = state.get('explanation') or ''
            if not report and not questions:
                report = render_state(state)
        files = []
        if run.folder.is_dir():
            files = sorted(child.name for child in run.folder.iterdir()
                           if child.is_file() and is_downloadable(child.name))
        summary = state_summary(state) if state else {}
        return {
            'run_id': run.run_id,
            'status': run.status,
            'execute': run.execute,
            'folder': run.folder.name,
            'questions': questions,
            'report': report,
            'decision': summary.get('decision') or {},
            'assumptions': summary.get('assumptions') or [],
            'open_questions': summary.get('open_questions') or [],
            'cases': summary.get('cases') or [],
            'problems': summary.get('problems') or [],
            'files': files,
        }

    def _pending_questions(self, run: Run) -> list[dict[str, Any]]:
        """Questions from the marker written when the run paused."""
        try:
            payload = json.loads((run.folder / 'paused.json').read_text(
                encoding='utf-8'))
        except (OSError, ValueError):
            return []
        return payload.get('questions') or []


def questions_of(state: dict[str, Any]) -> list[dict[str, Any]]:
    """The open questions carried by an interrupted state."""
    interrupts = state.get('__interrupt__') or []
    if not interrupts:
        return []
    value = getattr(interrupts[0], 'value', None)
    if isinstance(value, dict):
        return value.get('questions') or []
    return []


def state_summary(state: dict[str, Any]) -> dict[str, Any]:
    """The subset of the state the page renders, plus a report when there is one."""
    if not state:
        return {}
    from .nodes.state import state_summary as node_summary

    summary = node_summary(state)
    summary['decision'] = state.get('decision') or {}
    summary['open_questions'] = state.get('open_questions') or []
    summary['assumptions'] = state.get('assumptions') or []
    return summary


def render_state(state: dict[str, Any]) -> str:
    """The report for a finished state, rendered but not written to disk."""
    return render_report(view_from_state(state))


def persist_run(run: Run, state: dict[str, Any]) -> None:
    """Write the same artifacts the CLI writes for a finished run."""
    dump_state(state, run.folder)
    for case in state.get('cases') or []:
        name = 'spec-%s.json' % case['case_id']
        (run.folder / name).write_text(
            json.dumps(case['spec'], ensure_ascii=False, indent=2), encoding='utf-8')
    (run.folder / 'explanation.txt').write_text(
        (state.get('explanation') or '') + '\n', encoding='utf-8')
    marker = run.folder / 'paused.json'
    if marker.is_file():
        marker.unlink()
