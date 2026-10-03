"""Serial acceptance runner with bounded workers and portable evidence.

python -m hysys_tools.remote_check           # HYSYS workstation
python -m hysys_tools.remote_check --offline # no COM or HYSYS
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .capabilities import TOOL_REVISION
from .core import write_json
from .examples import EXAMPLES, write_examples
from .main import list_capabilities
from .precheck import validate_spec

ROOT = Path(__file__).resolve().parent.parent


def say(message):
    print(str(message).encode('ascii', 'backslashreplace').decode('ascii'), flush=True)


@contextmanager
def workstation_lock():
    """Cross-process lock shared by copies of this runner for this Windows user.

    Released by the OS if Python exits. The small lock file is deliberately retained.
    Direct CLI calls outside this runner still must be serialized by the caller.
    """
    if os.name != 'nt':
        raise RuntimeError('Remote HYSYS tests require Windows.')
    import msvcrt
    path = Path(tempfile.gettempdir()) / 'hysys-tool-layer-acceptance.lock'
    with path.open('a+b') as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError('Another acceptance runner is active. Wait for it to finish.') from None
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def run_worker(arguments, output, label, timeout):
    """Kill only the owned Python worker on timeout; never terminate HYSYS."""
    output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    record = {'label': label, 'arguments': arguments, 'timeout_seconds': timeout}
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    with (output / 'stdout.txt').open('wb') as stdout, (output / 'stderr.txt').open('wb') as stderr:
        worker = subprocess.Popen([sys.executable, *arguments], cwd=ROOT,
                                  stdout=stdout, stderr=stderr, env=env, shell=False)
        try:
            record['exit_code'] = worker.wait(timeout=timeout)
            record['status'] = 'FINISHED'
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.wait()
            record.update(status='TIMEOUT', exit_code=worker.returncode,
                          note='Only this Python worker was stopped. HYSYS was left running. '
                               'Inspect its dialogs and this run before another simulation.')
    record['elapsed_seconds'] = round(time.monotonic() - started, 3)
    write_json(output / 'process.json', record)
    say('%s: %s (exit %s)' % (label, record['status'], record['exit_code']))
    return record


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def compare_baseline(result, expected):
    """Compare independent recorded values; does not replace fresh HYSYS results."""
    failures = []
    for key, target in expected.items():
        actual = result
        try:
            for part in key.split('/'):
                actual = actual[part]
            if isinstance(target, (int, float)):
                if not math.isfinite(float(actual)) or not math.isclose(
                        float(actual), target, rel_tol=1e-6, abs_tol=1e-5):
                    failures.append('%s: expected %r, got %r' % (key, target, actual))
            elif actual != target:
                failures.append('%s: expected %r, got %r' % (key, target, actual))
        except (KeyError, TypeError, ValueError):
            failures.append('%s: missing or invalid value' % key)
    return failures


def run_acceptance(args, output, summary):
    specs = output / 'specs'
    write_examples(specs)
    write_json(output / 'capabilities.json', list_capabilities())
    write_json(output / 'source_hashes.json', {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((ROOT / 'hysys_tools').glob('*.py'))})
    for module_args, label in [(['-m', 'hysys_tools.selfcheck'], 'selfcheck'),
                               (['-m', 'unittest', 'discover', '-s', 'hysys_tools', '-t', '.', '-v'], 'regression')]:
        record = run_worker(module_args, output / label, label, 90)
        summary['checks'].append(record)
        if record['exit_code'] != 0 or record['status'] == 'TIMEOUT':
            raise RuntimeError('Offline checks failed; no HYSYS cases were attempted.')

    for name, factory in EXAMPLES.items():
        report = validate_spec(factory())
        write_json(output / 'preflight' / (name + '.json'), report)
        expected_ok = name != 'coal-slurry-gasification-unclarified'
        if report['ok'] != expected_ok:
            raise RuntimeError('Unexpected preflight outcome for %s' % name)
    summary['gasification_exam_status'] = 'SATURATION_ROUTE_PENDING'
    if args.offline:
        summary['status'] = 'OFFLINE_PASS_REMOTE_NOT_RUN'
        return

    with workstation_lock():
        health = run_worker(['-m', 'hysys_tools', '--health-check'], output / 'health-before',
                            'health-before', 30)
        summary['checks'].append(health)
        if health['exit_code'] != 0 or health['status'] == 'TIMEOUT':
            raise RuntimeError('HYSYS health check failed. See health-before logs.')
        baselines = read_json(ROOT / 'baseline_expected.json')
        jobs = [('toluene', 'toluene-disproportionation'),
                ('smr-gibbs-710C', 'methane-steam-reforming-gibbs-710C'),
                ('smr-gibbs-600C', 'methane-steam-reforming-gibbs-600C'),
                ('smr-710C', 'methane-steam-reforming-710C'),
                ('smr-600C', 'methane-steam-reforming-600C'),
                ('gasification', 'coal-slurry-gasification')]
        completed_results = {}
        for label, spec_name in jobs:
            folder = output / label
            record = run_worker(['-m', 'hysys_tools', '--spec', str(specs / (spec_name + '.json')),
                                 '--folder', str(folder)], folder, label, args.timeout)
            summary['checks'].append(record)
            if record['status'] == 'TIMEOUT':
                raise RuntimeError('Worker timed out; stopped the remaining HYSYS tests.')
            if not (folder / 'result.json').is_file():
                raise RuntimeError('No result.json for %s; see stderr.txt.' % label)
            result = read_json(folder / 'result.json')
            record['tool_status'] = result.get('status')
            completed_results[label] = result
            if label == 'toluene' or label.startswith('smr-gibbs-'):
                failures = compare_baseline(result, baselines['cases'][label.replace('smr-gibbs-','smr-')])
            elif label.startswith('smr-'):
                failures = []
                if result.get('reactor_kind') != 'equilibrium' or result.get('checks',{}).get('equilibrium_QK',{}).get('verdict') != 'PASS':
                    failures.append('missing Equilibrium Q/K gate')
                reference = completed_results[label.replace('smr-','smr-gibbs-')]
                try:
                    delta = result['checks']['reactant_conversion_percent']['Methane']-reference['checks']['reactant_conversion_percent']['Methane']
                    duty_relative = abs(result['heat_duty_kW']/reference['heat_duty_kW']-1.)
                    record['gibbs_crosscheck'] = {'conversion_difference_percentage_points':delta,'duty_relative_difference':duty_relative}
                    if not math.isfinite(delta) or abs(delta) > .5 or not math.isfinite(duty_relative) or duty_relative > .01:
                        failures.append('Equilibrium/Gibbs cross-check exceeds 0.5 percentage points or 1% duty')
                except (KeyError,TypeError,ZeroDivisionError):
                    failures.append('missing Gibbs comparison metrics')
            else:
                failures = []
                if not result.get('solid_carbon_saturation') or not result.get('checks',{}).get('co_yield'):
                    failures.append('missing graphite saturation or CO yield evidence')
            evidence = result.get('solver_evidence', {})
            if evidence.get('stable_reads', 0) < 3 or result.get('solver_is_solving') is not False:
                failures.append('missing stable solver evidence')
            file_name = result.get('case_file')
            if not file_name or not (folder / file_name).is_file() or (folder / file_name).stat().st_size == 0:
                failures.append('no saved HSC file')
            if record['exit_code'] != 0 or result.get('status') != 'PASS':
                failures.append('tool did not PASS')
            record['baseline_failures'] = failures
            record['acceptance'] = 'PASS' if not failures else 'FAILED'
            if failures:
                raise RuntimeError('Acceptance failed for %s; inspect its result and baseline differences.' % label)
            if result.get('warnings'):
                record['warnings'] = result['warnings']
            close_steps = [s for s in result.get('steps', []) if s.get('step') == 'close_saved_case']
            if not close_steps or close_steps[-1].get('detail', {}).get('result') != 'OK':
                raise RuntimeError('Case cleanup was not confirmed; remaining simulations stopped.')
        health = run_worker(['-m', 'hysys_tools', '--health-check'], output / 'health-after',
                            'health-after', 30)
        summary['checks'].append(health)
        if health['exit_code'] != 0 or health['status'] == 'TIMEOUT':
            raise RuntimeError('Final HYSYS health check failed.')
        summary['status'] = 'ALL_SCENARIOS_PASS'
        summary['gasification_exam_status'] = 'PASS_WITH_DECLARED_ASSUMPTIONS'
        # Preserve historical references; export full-precision new results separately.
        candidate = {'source_run':summary['run_id'], 'tool_revision':TOOL_REVISION,
                     'status':'CANDIDATE_FROM_FULL_REMOTE_ACCEPTANCE', 'cases':{}}
        for label, result in completed_results.items():
            values = {'status':result['status'],'reactor_kind':result['reactor_kind'], 'heat_duty_kW':result['heat_duty_kW']}
            values.update({'component_flows_kmol_h/'+k:v for k,v in result['component_flows_kmol_h'].items()})
            values.update({'checks/reactant_conversion_percent/'+k:v for k,v in result['checks']['reactant_conversion_percent'].items()})
            candidate['cases'][label]=values
        write_json(output/'baseline_candidate.json',candidate)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--timeout', type=float, default=180.)
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error('--timeout must be a positive finite number')
    run_id = datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:8]
    output = ROOT / 'tool-layer-runs' / ('acceptance-' + run_id)
    output.mkdir(parents=True, exist_ok=False)
    summary = {'tool_revision': TOOL_REVISION, 'run_id': run_id, 'status': 'RUNNING',
               'offline': args.offline, 'python': sys.version, 'platform': platform.platform(),
               'started_local': datetime.now().astimezone().isoformat(), 'checks': []}
    say('Output folder: %s' % output)
    try:
        run_acceptance(args, output, summary)
    except Exception as exc:
        summary.update(status='FAILED', error='%s: %s' % (type(exc).__name__, exc),
                       traceback=traceback.format_exc())
        say(summary['error'])
    finally:
        summary['finished_local'] = datetime.now().astimezone().isoformat()
        write_json(output / 'summary.json', summary)
    archive = shutil.make_archive(str(output), 'zip', root_dir=output.parent, base_dir=output.name)
    say('Result: %s' % summary['status'])
    say('Copy back this ZIP: %s' % archive)
    return 1 if summary['status'] == 'FAILED' else 0


if __name__ == '__main__':
    sys.exit(main())
