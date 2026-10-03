"""Command-line entry point: `python -m reactor_agent`.

    python -m reactor_agent --scenario toluene              # dry run (default)
    python -m reactor_agent --scenario gasification         # asks two questions
    python -m reactor_agent --scenario gasification --accept-defaults
    python -m reactor_agent --scenario gasification --no-input
    python -m reactor_agent --scenario gasification --answer q-volumetric-flow=默认
    python -m reactor_agent --scenario toluene --execute    # really run HYSYS
    python -m reactor_agent --text "甲苯进料10000kg/h..."    # your own wording
    python -m reactor_agent --file request.txt --execute
    python -m reactor_agent --list-scenarios

Safety default: **without `--execute` nothing is started.** A dry run performs the
real extraction, normalisation, selection, compilation and pre-check, and prints the
spec it would have run. That is also how the front half is verified on a machine with
no HYSYS.

The state graph is the default path, because a request that is missing a fact has to
be able to ask. `--single-pass` runs the older one-shot pipeline instead; `--graph` is
kept as a no-op for old scripts. A run that pauses writes `paused.json` and can be
resumed later - `--answer` finds the most recent paused run by itself when `--out` is
not given, which is what makes the pause and the answer two separate commands.

Console output is kept ASCII because the Windows console mangles UTF-8; the Chinese
explanation is written to `explanation.txt` in the run folder and also printed at the
end. Credentials are read from the environment only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .adapters.hysys_cli import HysysCliAdapter, describe_environment
from .graph import build_graph, dump_state, initial_state, state_summary
from .llm import ChatClient, LlmConfig, LlmError, describe_config
from .pipeline import (
    FAILED,
    PARTIAL,
    PASS,
    READY,
    UNSUPPORTED,
    WAITING_INPUT,
    run_pipeline,
    write_run_artifacts,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The exam's three requests, copied verbatim, so the same input can be replayed.
SCENARIOS: dict[str, dict[str, str]] = {
    'toluene': {
        # The phase is 'unknown' rather than 'liquid'. It only affects the CSTR/PFR
        # rule for kinetic systems, and this request states no rate law, so pinning
        # it to liquid claimed something the request does not say. The reaction's own
        # phase is fixed to `combined` by the compiler.
        'kind': 'conversion', 'phase': 'unknown', 'feed_basis': 'mass_fraction',
        'label': 'toluene',
        'text': '请帮我完成甲苯歧化反应的模拟，甲苯原料进入转化率反应器，发生歧化反应：'
                '2C₇H₈ → C₆H₆ + C₈H₁₀。甲苯进料流量10000kg/h，进料温度为380℃，'
                '操作压力2.5MPa，甲苯转化率为50%，反应产物为苯和二甲苯'
                '（邻、间、对三种异构体），请配置反应并模拟产物分布和流股组成',
    },
    'smr': {
        'kind': 'gibbs', 'phase': 'gas', 'feed_basis': 'molar_fraction',
        'label': 'smr',
        'text': '我需要模拟甲烷蒸汽重整。进料是甲烷和水蒸气（摩尔比 1:2.7）有两个反应，'
                '主反应甲烷和水反应生成一氧化碳和氢气；副反应一氧化碳和水蒸汽反应生成'
                '二氧化碳和氢气，请分析以下两种情况下反应炉的组分分布：'
                '1、重整炉出口气温度为 710°C，压力 13.5 bar，进料温度520℃；'
                '2、重整炉出口气温度为600℃，压力13.5bar，进料温度520℃。'
                '进料流量可以自定，要求符合一个工厂一年正常的处理量',
    },
    'gasification': {
        'kind': 'gibbs', 'phase': 'gas', 'feed_basis': 'mass_fraction',
        'label': 'gasification',
        'text': '我要模拟水煤浆的气化过程。进料为煤炭和水，流量80000Nm3/h，压力40bar，'
                '水煤浆进料浓度62wt%，进料温度40摄氏度，主要反应：C+H2O → CO+H2。'
                '请帮我计算一下气化炉出口温度为1400度时出口组成及CO的收率，'
                '反应器灰分不做考虑',
    },
}

# Fields the exam explicitly leaves to us, so a value there is chosen, not quoted.
ALLOWED_UNGROUNDED = {'smr': {'feed_total'}}

EXIT_BY_STATUS = {PASS: 0, READY: 0, WAITING_INPUT: 3, PARTIAL: 4, FAILED: 5,
                  UNSUPPORTED: 6}

# Where paused runs are remembered inside their own folder.
PAUSED_MARKER = 'paused.json'


def run_folder(label: str, base: Path | None = None) -> Path:
    """A fresh folder per invocation, like the tool layer's own runner."""
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    safe = ''.join(ch if ch.isalnum() or ch in '-_' else '-' for ch in label)[:40]
    return (base or (PROJECT_ROOT / 'agent-runs')) / ('%s-%s' % (safe or 'run', stamp))


def latest_paused_run(label: str, base: Path | None = None) -> Path | None:
    """The most recently modified run folder for a label that is still paused.

    `--answer` without `--out` needs this: the process that asked the question has
    exited, and the answer arrives in a new one. The folder name is the run's identity,
    so the label is matched against it rather than against anything inside.
    """
    root = base or (PROJECT_ROOT / 'agent-runs')
    if not root.is_dir():
        return None
    prefix = '%s-' % (''.join(ch if ch.isalnum() or ch in '-_' else '-'
                              for ch in label)[:40] or 'run')
    candidates = [child for child in root.iterdir()
                  if child.is_dir() and child.name.startswith(prefix)
                  and (child / PAUSED_MARKER).is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda child: child.stat().st_mtime)


def _jsonable(value: Any) -> Any:
    """State values are plain JSON, but a resume payload can carry a dataclass."""
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


# ------------------------------------------------------------- paused markers

def _read_paused(folder: Path) -> dict[str, Any] | None:
    marker = Path(folder) / PAUSED_MARKER
    if not marker.is_file():
        return None
    try:
        return json.loads(marker.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def _write_paused(folder: Path, label: str, thread_id: str,
                  questions: list[dict[str, Any]]) -> None:
    """Record that this run is waiting, so a later process can find and resume it.

    The thread id is the important part: the checkpointer holds the state under it, and
    a second process has no way to guess it.
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / PAUSED_MARKER).write_text(json.dumps({
        'label': label,
        'thread_id': thread_id,
        'questions': _jsonable(questions),
        'paused_at': datetime.now().isoformat(timespec='seconds'),
    }, ensure_ascii=False, indent=2), encoding='utf-8')


def _clear_paused(folder: Path) -> None:
    """The run is no longer waiting, so the marker must not survive it."""
    marker = Path(folder) / PAUSED_MARKER
    try:
        marker.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


# ------------------------------------------------------------------ answering

def defaults_answerer(questions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Take the suggested answer for every question, or give up as a whole.

    All or nothing on purpose: answering some questions and stopping would leave the
    run paused with a mixture of confirmed and unconfirmed values, which is harder to
    reason about than either finishing or not starting.
    """
    for question in questions:
        if question.get('default') is None:
            _print_ascii('question %s has no default answer, so --accept-defaults '
                         'cannot continue:' % question.get('id'))
            _print_ascii('  ? %s' % question.get('question'))
            return None
    return {question['id']: question['default'] for question in questions}


def terminal_answerer(questions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Ask each question in the terminal; Enter accepts the suggested answer."""
    answers: dict[str, Any] = {}
    for question in questions:
        _print_ascii('')
        _print_ascii('? %s' % question.get('question'))
        if question.get('reason'):
            _print_ascii('  why: %s' % question['reason'])
        default = question.get('default')
        if default:
            _print_ascii('  [Enter = default: %s]' % default)
        for _attempt in range(3):
            try:
                reply = input('> ').strip()
            except EOFError:
                return None
            if reply:
                answers[question['id']] = reply
                break
            if default:
                answers[question['id']] = default
                _print_ascii('  using the default: %s' % default)
                break
            _print_ascii('  this question has no default; please type an answer.')
        else:
            _print_ascii('  no answer given; stopping and keeping the run paused.')
            return None
    return answers


def no_input_answerer(questions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Never answer. The run stays paused and the questions are printed."""
    return None


def drive_graph(graph, first_input, config: dict,
                answer_fn: Callable[[list[dict[str, Any]]], dict[str, Any] | None]
                ) -> dict:
    """Run the graph, pausing as often as the questions require.

    `answer_fn` returns a mapping of question id to answer, or None to stop and leave
    the run paused. The graph's own `MAX_CLARIFICATION_ROUNDS` bounds the loop, so this
    does not count rounds itself.
    """
    from langgraph.types import Command

    state = graph.invoke(first_input, config)
    while state.get('__interrupt__'):
        questions = state['__interrupt__'][0].value.get('questions') or []
        answers = answer_fn(questions)
        if answers is None:
            return state
        state = graph.invoke(Command(resume=answers), config)
    return state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='python -m reactor_agent',
        description='Turn a natural-language request into a HYSYS reactor case.')
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--scenario', choices=sorted(SCENARIOS),
                        help='replay one of the exam scenarios')
    source.add_argument('--text', help='the request, inline')
    source.add_argument('--file', help='read the request from a UTF-8 file')
    parser.add_argument('--execute', action='store_true',
                        help='really run HYSYS (default is a dry run that starts nothing)')
    parser.add_argument('--timeout', type=float, default=180.0,
                        help='per-case worker timeout in seconds (default 180)')
    parser.add_argument('--out', help='run folder (default agent-runs/<label>-<stamp>)')
    parser.add_argument('--list-scenarios', action='store_true',
                        help='show the built-in scenarios and exit')
    parser.add_argument('--basis', choices=('mass_fraction', 'molar_fraction'),
                        default=None,
                        help='how the feed composition is expressed. The built-in '
                             'scenarios know this; for --text/--file the default is '
                             'mass_fraction, which must agree with the flow unit')
    parser.add_argument('--graph', action='store_true',
                        help='the default behaviour, kept so that old scripts and '
                             'command lines still work')
    parser.add_argument('--single-pass', action='store_true',
                        help='use the older one-shot pipeline, which cannot pause to '
                             'ask a question')
    parser.add_argument('--accept-defaults', action='store_true',
                        help='answer every question that has a suggested answer with '
                             'that answer, so a run finishes unattended')
    parser.add_argument('--no-input', action='store_true',
                        help='never prompt; print the questions and the resume command, '
                             'then exit with code 3')
    parser.add_argument('--answer', action='append', default=[],
                        metavar='ID=VALUE',
                        help='answer a clarification question, e.g. '
                             '--answer q-feed-flow=10000 (can repeat). Without --out, '
                             'the most recent paused run for this scenario is resumed')
    parser.add_argument('--show-config', action='store_true',
                        help='print the non-sensitive model configuration and exit')
    return parser


def _parse_answers(pairs: list[str]) -> dict[str, Any]:
    """`--answer id=value`.

    The value is tried as JSON first, so `q-feed-flow={"value": 10000, "unit":
    "kg/h"}` arrives as the structured object `_apply_answers` expects. Falling back
    to a bare number and then to text keeps the simple cases (`q-feed-flow=10000`)
    working without ceremony.
    """
    answers: dict[str, Any] = {}
    for pair in pairs:
        if '=' not in pair:
            continue
        key, _, raw = pair.partition('=')
        key, raw = key.strip(), raw.strip()
        try:
            answers[key] = json.loads(raw)
            continue
        except (ValueError, TypeError):
            pass
        try:
            answers[key] = float(raw) if '.' in raw else int(raw)
        except ValueError:
            answers[key] = raw
    return answers


def _print_graph_state(state: dict) -> None:
    for question in state.get('blocking') or []:
        _print_ascii('  ? %s  [%s]' % (question['question'], question['id']))
        _print_ascii('    field: %s' % question['field'])
    summary = state_summary(state)
    _print_ascii('status  : %s' % summary['status'])
    _print_ascii('reactor : %s (%s, rule=%s)'
                 % (summary['reactor'], summary['capability'], summary['rule']))
    _print_ascii('cases   : %s' % ', '.join(summary['cases']))
    if summary['assumptions_we_made']:
        _print_ascii('')
        _print_ascii('ASSUMPTIONS WE MADE (must be stated in the report):')
        for item in summary['assumptions_we_made']:
            _print_ascii('  * %s = %s' % (item['field'], item['value']))
    if summary['problems']:
        _print_ascii('')
        _print_ascii('problems:')
        for problem in summary['problems']:
            _print_ascii('  - %s' % problem.encode('ascii', 'replace').decode('ascii'))
    for execution in summary['executions']:
        _print_ascii('  case %-10s %-24s %6.1fs'
                     % (execution['case_id'], execution['status'],
                        execution.get('seconds') or 0.0))


def _use_utf8_console() -> None:
    """Try to make the console speak UTF-8 so Chinese survives.

    The default Windows code page mangles it. `reconfigure` works on Windows 10+
    consoles; if it does not, `_print_ascii` still keeps output readable by
    substituting, so nothing crashes either way.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:                               # noqa: BLE001
            pass


def _print_ascii(text: str) -> None:
    """Print without tripping the console code page."""
    try:
        print(text)
    except UnicodeEncodeError:
        sys.stdout.write(text.encode('ascii', 'replace').decode('ascii') + '\n')


def main(argv: list[str] | None = None) -> int:
    _use_utf8_console()
    args = build_parser().parse_args(argv)

    if args.list_scenarios:
        for name, scenario in sorted(SCENARIOS.items()):
            _print_ascii('  %-14s %s' % (name, scenario['label']))
        return 0

    config = LlmConfig.from_env()
    if args.show_config:
        _print_ascii(json.dumps(describe_config(config), ensure_ascii=False))
        _print_ascii(json.dumps(describe_environment(PROJECT_ROOT), ensure_ascii=False))
        return 0

    # ------------------------------------------------------------ the request
    if args.scenario:
        scenario = SCENARIOS[args.scenario]
        text = scenario['text']
        kind, phase = scenario['kind'], scenario['phase']
        feed_basis, label = scenario['feed_basis'], scenario['label']
        allowed = ALLOWED_UNGROUNDED.get(args.scenario, set())
    elif args.text:
        text = args.text
        # Which reactor this is has not been decided yet, so no required-field set
        # is assumed here: selection decides, and plan then checks what that
        # reactor needs. Defaulting to Conversion asked Gibbs requests for a
        # conversion figure they could never have.
        kind, phase, label = '', 'mixed', 'custom'
        feed_basis = args.basis or 'mass_fraction'
        allowed = set()
    elif args.file:
        text = Path(args.file).read_text(encoding='utf-8')
        # Which reactor this is has not been decided yet, so no required-field set
        # is assumed here: selection decides, and plan then checks what that
        # reactor needs. Defaulting to Conversion asked Gibbs requests for a
        # conversion figure they could never have.
        kind, phase, label = '', 'mixed', 'custom'
        feed_basis = args.basis or 'mass_fraction'
        allowed = set()
    else:
        _print_ascii('give one of --scenario / --text / --file (see --help)')
        return 2

    _print_ascii('request : %s' % (args.scenario or label))
    _print_ascii('model   : %s at %s (thinking=%s)'
                 % (config.model, config.base, config.enable_thinking))
    _print_ascii('mode    : %s' % ('EXECUTE (HYSYS will be driven)'
                                   if args.execute else 'dry run (nothing is started)'))
    _print_ascii('')

    if not config.key:
        _print_ascii('TR_KEY is not set. Export it in this shell and try again:')
        _print_ascii('  PowerShell:  $env:TR_KEY = "<key>"')
        return 2

    client = ChatClient(config, logger=lambda m: _print_ascii('  [llm] ' + m))
    adapter = None
    if args.execute:
        adapter = HysysCliAdapter(PROJECT_ROOT,
                                  python=os.environ.get('HYSYS_AGENT_PYTHON'))
        adapter.timeout = args.timeout

    answers = _parse_answers(args.answer)

    # An `--answer` without `--out` is the second half of a paused run, and the run
    # folder it belongs to was chosen by the process that asked the question.
    if answers and not args.out:
        found = latest_paused_run(label)
        if found is None:
            _print_ascii('no run is waiting for an answer for %r; '
                         'use --out to name the run folder' % label)
            return 2
        folder = found
        resumed = _read_paused(folder)
        if resumed:
            label = resumed.get('label') or label
        _print_ascii('resuming paused run: %s' % folder)
    else:
        folder = Path(args.out) if args.out else run_folder(label)

    # ------------------------------------------------------- graph execution
    # The graph is the default: a request that is missing a fact has to be able to
    # ask, and the single-pass pipeline cannot.
    if not args.single_pass:
        from .graph import open_checkpointer
        if args.accept_defaults:
            answer_fn = defaults_answerer
        elif args.no_input or not sys.stdin.isatty():
            answer_fn = no_input_answerer
        else:
            answer_fn = terminal_answerer

        with open_checkpointer(folder / 'checkpoints.sqlite') as saver:
            graph = build_graph(client, adapter=adapter, run_root=folder,
                                dry_run=not args.execute, checkpointer=saver)
            thread_id = (_read_paused(folder) or {}).get('thread_id') \
                or label or 'run'
            config = {'configurable': {'thread_id': thread_id}}
            if answers:
                from langgraph.types import Command
                # Resume the paused run. Passing a fresh initial state here instead
                # would start a new run and silently discard the answer - which is
                # exactly what happened the first time this was written.
                state = graph.invoke(Command(resume=answers), config)
            else:
                state = graph.invoke(
                    initial_state(text, scenario_label=label, kind=kind, phase=phase,
                                  feed_basis=feed_basis, allowed_ungrounded=allowed),
                    config)
            if state.get('__interrupt__'):
                questions = state['__interrupt__'][0].value.get('questions') or []
                _print_ascii('PAUSED for clarification. Nothing was executed.')
                for question in questions:
                    _print_ascii('  ? %s' % question['question'])
                    _print_ascii('    id: %s  (field %s)'
                                 % (question['id'], question['field']))
                    _print_ascii('    why: %s' % question.get('reason', ''))
                    if question.get('default'):
                        _print_ascii('    default: %s' % question['default'])
                _write_paused(folder, label, thread_id, questions)
                _print_ascii('')
                _print_ascii('resume with one of:')
                _print_ascii('  --answer <id>=<value>        (or)')
                _print_ascii('  --accept-defaults            (or)')
                _print_ascii('  --scenario %s                to answer in the terminal'
                             % (args.scenario or '<name>'))
                _print_ascii('artifacts  : %s' % folder)
                return 3

        _clear_paused(folder)
        _print_graph_state(state)
        written = dump_state(state, folder)
        explanation = state.get('explanation') or ''
        if explanation:
            _print_ascii('')
            _print_ascii(explanation)
        _print_ascii('')
        _print_ascii('artifacts: %s' % folder)
        _print_ascii('  files: %s' % ', '.join(written))
        return EXIT_BY_STATUS.get(state.get('status'), 5)

    try:
        run = run_pipeline(text, scenario_label=label, kind=kind, phase=phase,
                           feed_basis=feed_basis, client=client, adapter=adapter,
                           run_root=folder, dry_run=not args.execute,
                           allowed_ungrounded=allowed)
    except LlmError as exc:
        _print_ascii('model error: %s' % exc)
        return 5

    # ---------------------------------------------------------------- report
    _print_ascii('status  : %s' % run.status)
    if run.decision is not None:
        decision = run.decision
        _print_ascii('reactor : %s (%s, rule=%s)'
                     % (decision.execution_reactor, decision.capability_status,
                        decision.rule_id))
        if decision.was_substituted():
            _print_ascii('substitution: preferred %s, executed %s'
                         % (decision.preferred_reactor, decision.execution_reactor))
    if run.plan is not None:
        _print_ascii('cases   : %s' % ', '.join(c.case_id for c in run.plan.cases))

    if run.assumptions_we_made:
        _print_ascii('')
        _print_ascii('ASSUMPTIONS WE MADE (must be stated in the report):')
        for item in run.assumptions_we_made:
            _print_ascii('  * %s = %s' % (item['field'], item['value']))

    if run.blocking_questions:
        _print_ascii('')
        _print_ascii('BLOCKING QUESTIONS (nothing was executed):')
        for question in run.blocking_questions:
            _print_ascii('  ? %s' % question.encode('ascii', 'replace').decode('ascii'))

    if run.problems:
        _print_ascii('')
        _print_ascii('problems:')
        for problem in run.problems:
            _print_ascii('  - %s' % problem.encode('ascii', 'replace').decode('ascii'))

    for execution in run.executions:
        summary = execution.summary()
        _print_ascii('  case %-10s %-24s %6.1fs  %s'
                     % (summary['case_id'], summary['status'],
                        summary['seconds'] or 0.0, summary['error'] or ''))

    written = write_run_artifacts(run, folder)
    # The report already carries the assumptions and the open questions, each in its
    # own section, so re-assembling them here produced a second, thinner copy of the
    # same document.
    (folder / 'explanation.txt').write_text((run.explanation or '(no explanation)')
                                            + '\n', encoding='utf-8')
    if run.explanation:
        _print_ascii('')
        _print_ascii(run.explanation)

    _print_ascii('')
    _print_ascii('artifacts: %s' % folder)
    _print_ascii('  files: %s' % ', '.join(written + ['explanation.txt']))
    return EXIT_BY_STATUS.get(run.status, 5)


if __name__ == '__main__':
    raise SystemExit(main())
