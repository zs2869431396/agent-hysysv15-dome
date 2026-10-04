"""Local web interface: `python -m reactor_agent.web`.

    python -m reactor_agent.web [--port 8765]

A page for the whole flow - fill in the model connection, paste a request, answer the
follow-up questions, read the result - without a command line. It is the same graph,
the same run folders and the same report as the CLI; only the input method differs.

Three rules shape this module, and none of them is incidental:

  * **Standard library only.** `http.server.ThreadingHTTPServer` and one HTML file with
    inline CSS and JavaScript for the legacy form interface. The default UI is now
    Streamlit; it reuses WebApp below without running this HTTP server.

  * **The key never lands anywhere.** It is read from the page (or `TR_BASE`/`TR_MODEL`
    and `TR_KEY` in the environment, or a `.env` file in the project root) and kept in
    this process's memory. It is never written into a checkpoint, `state.json`,
    `explanation.txt`, a log, an error message, or the browser's `localStorage`, and no
    response body contains it - `GET /api/settings` reports only whether it is set.

  * **`127.0.0.1` only.** The server binds the loopback address, so the page cannot be
    reached from another machine, and the only thing that leaves this machine is the
    request to the model endpoint the operator configured.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .graph import build_graph, dump_state, initial_state, open_checkpointer
from .llm import DEFAULT_BASE, DEFAULT_MODEL, ChatClient, LlmConfig
from .pipeline import write_run_artifacts
from .report import view_from_state

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / 'web_static'
INDEX_FILE = STATIC_DIR / 'index.html'

DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 8765

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

_RUN_ID = re.compile(r'^[A-Za-z0-9._-]{1,80}$')


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
    The key itself is never returned by any endpoint, only `key_set`.
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


class WebApp:
    """The server's state: settings, open runs, and the one execution slot."""

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
        self._execute_lock = threading.Lock()
        self._busy = False
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


def render_report(view: dict[str, Any]) -> str:
    from .report import render_report as render

    return render(view)


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


def run_dry_pipeline_in_memory(text: str, scenario: str, kind: str, phase: str,
                               feed_basis: str, client: ChatClient) -> dict[str, Any]:
    """Preview a request without creating anything on disk.

    Used by `POST /api/preview`: the page wants to show what the system understood
    before spending a HYSYS run, and "asking a question has no side effects" is a
    project rule, so this path writes no file at all. It costs one model call.

    `client` is the app's own client factory result, not a fresh one, so the model
    client is configured in exactly one place.
    """
    import tempfile

    from .pipeline import run_pipeline

    with tempfile.TemporaryDirectory(prefix='agent-preview-') as tmp:
        # The pipeline insists on a run root; a temporary directory keeps the rule
        # that a preview leaves the project's own folders untouched.
        run = run_pipeline(text, scenario_label=scenario or 'custom', kind=kind,
                           phase=phase, feed_basis=feed_basis, client=client,
                           adapter=None, run_root=Path(tmp), dry_run=True,
                           allowed_ungrounded=allowed_ungrounded(scenario))
    return {'status': run.status, 'report': run.explanation,
            'blocking': [{'id': q.id, 'field': q.field, 'question': q.question,
                          'default': q.default} for q in run.blocking_question_objects()],
            'problems': run.problems}


# --------------------------------------------------------------------- handler

class Handler(BaseHTTPRequestHandler):
    server_version = 'reactor-agent-web/1'
    app: WebApp

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        # Keep the console readable; nothing here carries a credential anyway.
        sys.stderr.write('%s - %s\n' % (self.address_string(), fmt % args))

    # ------------------------------------------------------------- responses
    def _send(self, status: int, body: bytes, content_type: str,
              extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self._send(status, body, 'application/json; charset=utf-8')

    def _error(self, status: int, message: str) -> None:
        self._json(status, {'error': message})

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    # ------------------------------------------------------------------ verbs
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ('/', '/index.html'):
            try:
                body = INDEX_FILE.read_bytes()
            except OSError:
                self._error(500, 'the page file is missing')
                return
            self._send(200, body, 'text/html; charset=utf-8')
            return
        if path == '/api/settings':
            self._json(200, self.app.settings.public())
            return
        if path == '/api/scenarios':
            self._json(200, {'scenarios': [
                {'name': name, 'label': SCENARIO_LABELS.get(name, scenario['label']),
                 'text': scenario['text']}
                for name, scenario in sorted(scenarios().items())]})
            return
        if path.startswith('/api/run/'):
            parts = [unquote(part) for part in path.split('/') if part]
            if len(parts) == 3 and parts[1] == 'run':
                self._get_run(parts[2])
                return
            if len(parts) == 5 and parts[3] == 'files':
                self._get_file(parts[2], parts[4])
                return
        self._error(404, 'no such path')

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == '/api/settings':
            payload = self._body()
            with self.app._lock:
                if isinstance(payload.get('base'), str):
                    self.app.settings.base = payload['base'].strip()
                if isinstance(payload.get('model'), str):
                    self.app.settings.model = payload['model'].strip()
                if isinstance(payload.get('key'), str):
                    self.app.settings.key = payload['key'].strip()
            self._json(200, self.app.settings.public())
            return
        if path == '/api/run':
            self._post_run()
            return
        if path == '/api/preview':
            self._post_preview()
            return
        if path == '/api/answer':
            self._post_answer()
            return
        self._error(404, 'no such path')

    # ------------------------------------------------------------------- runs
    def _post_run(self) -> None:
        payload = self._body()
        scenario = str(payload.get('scenario') or '').strip()
        text = str(payload.get('text') or '').strip()
        execute = bool(payload.get('execute'))
        if not text:
            self._error(400, 'the request text is empty')
            return
        if not self.app.settings.public()['key_set']:
            self._error(400, 'the model key is not set: fill it in the Model section')
            return
        known = scenarios()
        if scenario in known:
            source = known[scenario]
            label, kind = scenario, source['kind']
            phase, feed_basis = source['phase'], source['feed_basis']
        else:
            label, kind, phase = 'custom', '', 'mixed'
            feed_basis = str(payload.get('basis') or 'mass_fraction')

        if execute:
            # One real run at a time: two adapters driving one HYSYS session is the
            # failure the workstation lock exists to prevent.
            if not self.app._execute_lock.acquire(blocking=False):
                self._error(409, 'the workstation is busy with another real run')
                return
        run = self.app.new_run(label, execute)
        try:
            state = self.app.start(run, text, scenario, kind, phase, feed_basis)
        except Exception as exc:                      # noqa: BLE001
            run.status = 'FAILED'
            self._json(200, {'run_id': run.run_id, 'status': run.status,
                             'report': '', 'questions': [],
                             'problems': ['the model call or the graph failed: %s'
                                          % exc]})
            if execute:
                self.app._execute_lock.release()
            return

        questions = questions_of(state)
        if questions:
            run.status = 'WAITING_INPUT'
            self._write_paused(run, questions)
        else:
            run.status = state.get('status') or 'FAILED'
            try:
                persist_run(run, state)
            except Exception:                         # noqa: BLE001
                pass
            if execute:
                self.app._execute_lock.release()
        with self.app._lock:
            self.app._save_runs()
        self._json(200, self.app.run_payload(run, state))

    def _post_answer(self) -> None:
        payload = self._body()
        run = self.app.get_run(str(payload.get('run_id') or ''))
        if run is None:
            self._error(404, 'no such run')
            return
        answers = payload.get('answers')
        if not isinstance(answers, dict) or not answers:
            self._error(400, 'answers must be a non-empty object')
            return
        try:
            state = self.app.answer(run, answers)
        except Exception as exc:                      # noqa: BLE001
            self._error(500, 'resuming the run failed: %s' % exc)
            return
        finally:
            if run.execute and self.app._execute_lock.locked():
                self.app._execute_lock.release()
        questions = questions_of(state)
        if questions:
            run.status = 'WAITING_INPUT'
            self._write_paused(run, questions)
        else:
            run.status = state.get('status') or 'FAILED'
            try:
                persist_run(run, state)
            except Exception:                         # noqa: BLE001
                pass
        with self.app._lock:
            self.app._save_runs()
        self._json(200, self.app.run_payload(run, state))

    def _post_preview(self) -> None:
        payload = self._body()
        text = str(payload.get('text') or '').strip()
        if not text:
            self._error(400, 'the request text is empty')
            return
        if not self.app.settings.public()['key_set']:
            self._error(400, 'the model key is not set: fill it in the Model section')
            return
        scenario = str(payload.get('scenario') or '').strip()
        known = scenarios()
        if scenario in known:
            source = known[scenario]
            kind, phase = source['kind'], source['phase']
            feed_basis = source['feed_basis']
        else:
            kind, phase = '', 'mixed'
            feed_basis = str(payload.get('basis') or 'mass_fraction')
        try:
            result = run_dry_pipeline_in_memory(text, scenario, kind, phase,
                                                feed_basis, self.app.client())
        except Exception as exc:                      # noqa: BLE001
            self._error(500, 'the preview failed: %s' % exc)
            return
        result['note'] = ('This is a read-only preview: it creates no run folder and '
                          'no case. Start a run to get the artifacts.')
        self._json(200, result)

    def _get_run(self, run_id: str) -> None:
        run = self.app.get_run(run_id)
        if run is None:
            self._error(404, 'no such run')
            return
        # Reading is idempotent: this never invokes the graph, so refreshing the page
        # cannot cause another model call.
        try:
            state = self.app.current_state(run)
        except Exception:                             # noqa: BLE001
            state = {}
        self._json(200, self.app.run_payload(run, state))

    def _get_file(self, run_id: str, name: str) -> None:
        run = self.app.get_run(run_id)
        if run is None:
            self._error(404, 'no such run')
            return
        if not is_downloadable(name):
            self._error(404, 'no such file')
            return
        path = (run.folder / name).resolve()
        try:
            path.relative_to(run.folder.resolve())
        except ValueError:
            self._error(404, 'no such file')
            return
        try:
            body = path.read_bytes()
        except OSError:
            self._error(404, 'no such file')
            return
        content_type = ('application/json; charset=utf-8'
                        if name.endswith('.json') else 'text/plain; charset=utf-8')
        self._send(200, body, content_type,
                   {'Content-Disposition': 'attachment; filename="%s"' % name})

    def _write_paused(self, run: Run, questions: list[dict[str, Any]]) -> None:
        (run.folder / 'paused.json').write_text(json.dumps({
            'label': run.label, 'thread_id': run.thread_id,
            'questions': questions,
            'paused_at': datetime.now().isoformat(timespec='seconds'),
        }, ensure_ascii=False, indent=2), encoding='utf-8')


def make_server(port: int = DEFAULT_PORT, host: str = DEFAULT_HOST,
                root: Path | None = None, settings: Settings | None = None,
                app: WebApp | None = None) -> ThreadingHTTPServer:
    """A server bound to `host` (loopback by default). `port=0` picks a free one."""
    application = app or WebApp(root=root, settings=settings)

    class BoundHandler(Handler):
        pass

    BoundHandler.app = application
    server = ThreadingHTTPServer((host, port), BoundHandler)
    server.app = application        # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='python -m reactor_agent.web',
                                     description='Local web interface for the agent.')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT,
                        help='port on 127.0.0.1 (default %d)' % DEFAULT_PORT)
    parser.add_argument('--host', default=DEFAULT_HOST,
                        help='bind address; the default is loopback only')
    parser.add_argument('--open', action='store_true',
                        help='print the URL to open (nothing is launched)')
    args = parser.parse_args(argv)

    server = make_server(port=args.port, host=args.host)
    host, port = server.server_address[0], server.server_address[1]
    print('reactor_agent web interface')
    print('  url    : http://%s:%d' % (host, port))
    print('  model  : %s' % server.app.settings.public()['base'])  # type: ignore
    print('  key    : %s'
          % ('set (from the environment or .env; type one in the page to replace it)'
             if server.app.settings.public()['key_set'] else  # type: ignore
             'not set - fill it in on the page, or put TR_KEY in .env'))
    print('  runs   : %s' % server.app.root)                        # type: ignore
    print('Press Ctrl+C to stop.')
    if args.open:
        print('  open   : http://%s:%d' % (host, port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nstopped')
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
