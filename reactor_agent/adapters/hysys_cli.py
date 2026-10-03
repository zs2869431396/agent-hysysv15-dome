"""Run the HYSYS tool layer as a subprocess: one call, one self-contained case.

Several constraints here are not style choices, they are consequences of how HYSYS
behaves, and each one was paid for during this project:

**One case per call, never "open and continue".** HYSYS is a COM server, and COM
objects cannot cross a process boundary. A process either builds a complete case in
one go or it builds nothing.

**Every attempt gets a brand-new directory.** Reusing a path makes HYSYS resolve the
duplicate case file against a case that is still open, and the "new" case comes back
non-blank. That failure cost five remote runs to find.

**A timeout kills the Python worker, never HYSYS.** The solver subprocess is
expendable; the HYSYS session is not. Killing HYSYS would destroy the operator's own
unsaved work.

**A missing `result.json` is a normal outcome, not an exception.** The process can
die, the import can fail, the disk write can fail. When there is no result the
adapter writes its own failure record and keeps whatever stdout/stderr arrived -
it never fabricates a tool PASS.

**Calls are serialised across processes.** Two copies of this runner, launched by a
double-click and by a web request, must not drive the same HYSYS instance at once.

**The model never supplies any of this.** Command lines, paths and arguments are
built here from a case id and a spec. `shell=False` throughout.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

# Statuses this adapter can produce. `TOOL_*` values mirror the tool layer's own
# status so a caller can compare them directly.
STATUS_PASS = 'PASS'
STATUS_FAILED = 'FAILED'
STATUS_CANNOT_CONNECT = 'CANNOT_CONNECT_TO_HYSYS'
STATUS_TIMEOUT = 'TIMEOUT'
STATUS_NO_RESULT = 'NO_RESULT'
STATUS_REFUSED = 'REFUSED_BEFORE_EXECUTION'


@dataclass
class ExecutionResult:
    """What came back from one attempt to run a spec."""
    case_id: str
    attempt: int
    run_dir: Path
    status: str
    exit_code: int | None = None
    result: dict[str, Any] | None = None
    stdout: str = ''
    stderr: str = ''
    error: str | None = None
    seconds: float = 0.0
    case_file: str | None = None

    @property
    def passed(self) -> bool:
        """Success needs all three: the adapter's verdict, the tool's verdict, and
        evidence that a case was actually built.

        Checking only the result file was a real hole. A worker that wrote
        `status: PASS` and then exited non-zero - because it failed while flushing, or
        because the file was left over from an earlier attempt - was reported as a
        success. Equally, a PASS with no case file, no outlet stream and no checks is
        not evidence of a simulation; it is an empty document that says PASS.
        """
        if self.status != STATUS_PASS:
            return False
        if (self.result or {}).get('status') != 'PASS':
            return False
        # None means the runner was injected and there is no process to judge.
        if self.exit_code not in (0, None):
            return False
        return self.has_evidence()

    def has_evidence(self) -> bool:
        """True when the result file shows a case was actually constructed.

        Any one of these is enough: the tool layer saves the case before reading
        results back, so a saved case file, an outlet stream, an independent check, or
        the list of components it read back all prove the run got that far.
        """
        payload = self.result or {}
        return bool(payload.get('case_file') or payload.get('outlet')
                    or payload.get('checks')
                    or payload.get('readback_component_names'))

    @property
    def error_type(self) -> str | None:
        return (self.result or {}).get('error_type')

    def summary(self) -> dict[str, Any]:
        """Small, JSON-safe view for logs and manifests."""
        summary = {'case_id': self.case_id, 'attempt': self.attempt,
                   'status': self.status, 'exit_code': self.exit_code,
                   'seconds': round(self.seconds, 2), 'error': self.error,
                   'tool_status': (self.result or {}).get('status'),
                   'error_type': self.error_type, 'case_file': self.case_file,
                   'run_dir': str(self.run_dir)}
        if self.status == STATUS_PASS and not self.has_evidence():
            summary['error'] = (summary['error']
                                or 'the tool reported PASS but the result carries no '
                                   'case file, outlet or checks')
        return summary

    def results(self) -> dict[str, Any]:
        """The engineering numbers the tool layer computed, for the user.

        Kept separate from `summary`, which is a log line. The exam asks for the
        simulation results, and a summary of statuses and elapsed times is not that:
        an earlier version returned only `summary`, so a successful run reported
        "case-1: PASS, 4.2s" and none of the composition, temperature, conversion or
        duty that were actually computed.

        Every field is fetched defensively. A result file that is missing a section
        must degrade to "not reported", never to a KeyError in the middle of
        explaining a run that already succeeded.
        """
        payload = self.result or {}
        checks = payload.get('checks') or {}
        outlet = payload.get('outlet') or {}
        streams = {}
        for name, stream in outlet.items():
            if not isinstance(stream, dict):
                continue
            streams[name] = {
                'name': name,
                'temperature_C': stream.get('temperature_C'),
                'pressure_kPa': stream.get('pressure_kPa'),
                'molar_flow_kmol_h': stream.get('molar_flow_kmol_h'),
                'mass_flow_kg_h': stream.get('mass_flow_kg_h'),
                'mole_fractions': stream.get('mole_fractions') or {},
            }

        # Fields the tool layer computes and the earlier report threw away. Each is
        # fetched with `.get` so an older result file - which has none of them -
        # degrades to None or an empty list instead of raising in the middle of
        # explaining a run that already succeeded.
        saturation = payload.get('solid_carbon_saturation') or {}
        activity = saturation.get('carbon_activity') or {}
        equilibrium_evidence = [
            {key: item.get(key) for key in
             ('reaction', 'fit_max_residual', 'lnK_exact_bar', 'basis_units')}
            for item in (payload.get('equilibrium_evidence') or [])
            if isinstance(item, dict)]

        return {
            'status': payload.get('status'),
            'reactor_kind': payload.get('reactor_kind'),
            'heat_duty_kW': payload.get('heat_duty_kW'),
            'heat_duty_scope': checks.get('heat_duty_scope'),
            'streams': streams,
            'reactant_conversion_percent':
                checks.get('reactant_conversion_percent') or {},
            'specified_conversion': checks.get('specified_conversion') or [],
            'co_yield': checks.get('co_yield'),
            'worst_element_relative_error': checks.get('worst_element_relative_error'),
            'mass_relative_error': checks.get('mass_relative_error'),
            'equilibrium_QK': checks.get('equilibrium_QK'),
            'equilibrium_fit': equilibrium_evidence,
            'solid_carbon_saturation': ({
                'carbon_conversion_x': saturation.get('carbon_conversion_x'),
                'water_limited_x_max': saturation.get('water_limited_x_max'),
                'carbon_activity': {
                    'via_methanation': activity.get('via_methanation'),
                    'via_water_gas': activity.get('via_water_gas'),
                    'via_boudouard': activity.get('via_boudouard'),
                    'spread_decades': activity.get('spread_decades')},
                'duty_by_reactor_kW': saturation.get('duty_by_reactor_kW'),
                'library_carbon_gibbs_used':
                    saturation.get('library_carbon_gibbs_used'),
            } if saturation else None),
            'gibbs_equilibrium': ({
                'verdict': (checks.get('gibbs_equilibrium') or {}).get('verdict'),
                'orders_from_equilibrium':
                    (checks.get('gibbs_equilibrium') or {}
                     ).get('orders_from_equilibrium'),
            } if checks.get('gibbs_equilibrium') else None),
            'independent_duty': ({
                'verdict': (checks.get('independent_duty') or {}).get('verdict'),
                'relative_deviation': (checks.get('independent_duty') or {}
                                       ).get('relative_deviation'),
            } if checks.get('independent_duty') else None),
            'condensed_phase_location':
                (checks.get('condensed_phase_location') or {}).get('verdict'),
            'feed_molar_flows_kmol_h': payload.get('feed_molar_flows_kmol_h'),
            'component_flows_kmol_h': payload.get('component_flows_kmol_h'),
            'normal_volume_conversion':
                (payload.get('native_flow_readback') or {}).get('conversion'),
            'solver_is_solving': payload.get('solver_is_solving'),
            'case_file': payload.get('case_file'),
            'warnings': payload.get('warnings') or [],
            'open_questions': payload.get('open_questions') or [],
            'assumptions': payload.get('assumptions') or [],
        }


class WorkstationLock:
    """A cross-process lock so only one runner drives HYSYS at a time.

    Advisory and per-user, which is the right scope: HYSYS runs in one interactive
    session. The OS releases it if the process dies, so a crashed run cannot wedge
    the workstation. A small lock file is deliberately left behind.
    """

    def __init__(self, name: str = 'hysys-agent-execution.lock',
                 timeout: float = 0.0, poll: float = 0.5) -> None:
        self.path = Path(tempfile.gettempdir()) / name
        self.timeout = timeout
        self.poll = poll
        self._handle = None

    def _try_acquire(self) -> bool:
        import msvcrt
        self._handle = open(self.path, 'a+b')
        try:
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            self._handle.close()
            self._handle = None
            return False

    def acquire(self) -> bool:
        deadline = time.monotonic() + max(0.0, self.timeout)
        while True:
            if self._try_acquire():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(self.poll)

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            import msvcrt
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:                               # noqa: BLE001
            pass
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> 'WorkstationLock':
        if not self.acquire():
            raise TimeoutError('another run holds the HYSYS workstation lock')
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()


def safe_segment(text: str, fallback: str = 'item') -> str:
    """A path segment that cannot escape its parent or hit a reserved name."""
    if text is None:
        return fallback
    cleaned = ''.join(ch if ch.isalnum() or ch in '-_.' else '-' for ch in str(text))
    cleaned = cleaned.strip('-._')
    if not cleaned:
        return fallback
    stem = cleaned.split('.')[0].upper()
    reserved = {'CON', 'PRN', 'AUX', 'NUL',
                *(f'COM{i}' for i in range(1, 10)),
                *(f'LPT{i}' for i in range(1, 10))}
    if stem in reserved:
        return fallback
    return cleaned[:60]


class HysysCliAdapter:
    """Drive `python -m hysys_tools --spec ... --folder ...` for each case."""

    def __init__(self, project_root: Path, python: str | None = None,
                 timeout: float = 180.0, lock_timeout: float = 600.0,
                 runner: Callable[..., subprocess.CompletedProcess] | None = None,
                 use_lock: bool = True) -> None:
        self.project_root = Path(project_root)
        self.python = python or sys.executable
        self.timeout = float(timeout)
        self.lock_timeout = float(lock_timeout)
        # Injectable so the whole control flow can be tested without HYSYS.
        self._run = runner or subprocess.run
        self._use_lock = use_lock

    # ------------------------------------------------------------------ paths
    def attempt_dir(self, run_root: Path, case_id: str, attempt: int) -> Path:
        """`<run_root>/<case_id>/attempt-<n>-<uuid>/` - never reused."""
        return (Path(run_root) / safe_segment(case_id)
                / ('attempt-%d-%s' % (attempt, uuid4().hex[:8])))

    # -------------------------------------------------------------------- run
    def run_case(self, spec: dict[str, Any], case_id: str, run_root: Path,
                 attempt: int = 1) -> ExecutionResult:
        """Execute one spec. Never raises for an expected failure mode."""
        workdir = self.attempt_dir(run_root, case_id, attempt)
        workdir.mkdir(parents=True, exist_ok=True)
        spec_path = workdir / 'spec.json'
        spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2),
                             encoding='utf-8')

        # Arguments are built here, from fixed pieces and validated segments. The
        # model's contribution is the spec file's *contents*, never a path or a flag.
        command = [self.python, '-m', 'hysys_tools',
                   '--spec', str(spec_path), '--folder', str(workdir)]

        result = ExecutionResult(case_id=case_id, attempt=attempt, run_dir=workdir,
                                 status=STATUS_FAILED)
        started = time.monotonic()
        try:
            completed = self._invoke(command)
        except subprocess.TimeoutExpired as exc:
            result.status = STATUS_TIMEOUT
            result.seconds = time.monotonic() - started
            result.stdout = _decode(exc.stdout)
            result.stderr = _decode(exc.stderr)
            result.error = ('the worker exceeded %.0fs and was terminated; HYSYS '
                            'itself was not touched' % self.timeout)
            _write_json(workdir / 'adapter.json', result.summary())
            return result
        except FileNotFoundError as exc:
            result.seconds = time.monotonic() - started
            result.error = 'could not start the worker: %s' % exc
            _write_json(workdir / 'adapter.json', result.summary())
            return result

        result.seconds = time.monotonic() - started
        result.exit_code = completed.returncode
        result.stdout = _decode(completed.stdout)
        result.stderr = _decode(completed.stderr)
        # Persisted so a failure on a remote workstation can be diagnosed without
        # having to reproduce it. The summary that goes into adapter.json is a log
        # line and would lose the traceback.
        self._write_output(workdir, result)

        # A missing result file is expected in some failure modes; the adapter must
        # record its own failure rather than pretend the tool passed.
        payload, note = self._load_result(workdir)
        if payload is None:
            result.status = self._status_without_result(result)
            result.error = note
        else:
            result.result = payload
            result.status = str(payload.get('status') or STATUS_FAILED)
            result.case_file = payload.get('case_file') or payload.get(
                'failed_case_file')
            if result.status != STATUS_PASS:
                result.error = result.error or str(payload.get('error') or '')[:400]
            elif completed.returncode not in (0, None):
                # The tool said PASS but the process exited non-zero. Something failed
                # after the result was written, so the result cannot be trusted.
                result.status = STATUS_FAILED
                result.error = ('the tool reported PASS but the worker exited with '
                                'code %d; see worker-stderr.txt' % completed.returncode)
            elif not result.has_evidence():
                result.status = STATUS_FAILED
                result.error = ('the tool reported PASS but the result carries no case '
                                'file, outlet or checks, so no simulation is evidenced')

        _write_json(workdir / 'adapter.json', result.summary())
        return result

    # --------------------------------------------------------------- internals
    def _invoke(self, command: list[str]):
        """Run the worker, holding the workstation lock when enabled."""
        def call():
            return self._run(command, cwd=str(self.project_root), shell=False,
                             capture_output=True, timeout=self.timeout)

        if not self._use_lock:
            return call()
        with WorkstationLock(timeout=self.lock_timeout):
            return call()

    @staticmethod
    def _write_output(workdir: Path, result: ExecutionResult) -> None:
        """Save the worker's streams, so a remote failure can be read afterwards."""
        for name, text in (('worker-stdout.txt', result.stdout),
                           ('worker-stderr.txt', result.stderr)):
            if not text:
                continue
            try:
                (workdir / name).write_text(text, encoding='utf-8', errors='replace')
            except Exception:                           # noqa: BLE001
                pass

    @staticmethod
    def _load_result(workdir: Path) -> tuple[dict[str, Any] | None, str | None]:
        path = workdir / 'result.json'
        if not path.is_file():
            return None, ('the worker left no result.json; it probably failed before '
                          'or during the imports (see stderr)')
        try:
            return json.loads(path.read_text(encoding='utf-8')), None
        except Exception as exc:                        # noqa: BLE001
            return None, 'result.json could not be read: %s' % exc

    @staticmethod
    def _status_without_result(result: ExecutionResult) -> str:
        """Classify a run that produced no result file at all."""
        blob = (result.stderr or '') + (result.stdout or '')
        if 'CANNOT_CONNECT' in blob or 'GetActiveObject' in blob:
            return STATUS_CANNOT_CONNECT
        return STATUS_NO_RESULT


def _decode(raw: Any) -> str:
    if raw is None:
        return ''
    if isinstance(raw, bytes):
        return raw.decode('utf-8', 'replace')
    return str(raw)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding='utf-8')
    except Exception:                                   # noqa: BLE001
        pass


def find_python() -> str:
    """The interpreter to use for workers: this one, unless overridden."""
    return os.environ.get('HYSYS_AGENT_PYTHON') or sys.executable


def describe_environment(project_root: Path) -> dict[str, Any]:
    """Non-sensitive facts about the execution environment, for the manifest."""
    return {
        'project_root': str(project_root),
        'python': sys.version.split()[0],
        'python_executable': sys.executable,
        'platform': sys.platform,
        'tool_layer_present': (Path(project_root) / 'hysys_tools' / 'main.py').is_file(),
    }
