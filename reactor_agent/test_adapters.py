"""Tests for the execution adapter and the ledger.

Everything runs with an injected runner, so no subprocess is started and no HYSYS is
needed. That matters because the interesting cases are the failure modes - a worker
that times out, one that dies without writing a result, a spec that changed under a
resumed run - and those are exactly the ones that are hard to stage for real.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from reactor_agent.adapters.hysys_cli import (
    STATUS_CANNOT_CONNECT,
    STATUS_NO_RESULT,
    STATUS_PASS,
    STATUS_TIMEOUT,
    ExecutionResult,
    HysysCliAdapter,
    safe_segment,
)
from reactor_agent.adapters.run_store import LedgerEntry, RunStore, hash_spec

SPEC = {'schema': 'hysys-agent/spec/1', 'case_name': 'demo',
        'reactor': {'kind': 'conversion'}}


def fake_runner(returncode=0, stdout='ok', stderr='', writes_result=None,
                delay_raises=False, capture=None):
    """A stand-in for subprocess.run."""
    def run(command, **kwargs):
        if capture is not None:
            capture.append({'command': command, 'kwargs': kwargs})
        if delay_raises:
            raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs.get('timeout'),
                                            output=b'partial stdout')
        if writes_result is not None:
            # The worker would have written result.json into --folder.
            folder = Path(command[command.index('--folder') + 1])
            (folder / 'result.json').write_text(
                json.dumps(writes_result, ensure_ascii=False), encoding='utf-8')
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)
    return run


def adapter(runner, **kwargs):
    return HysysCliAdapter(Path(tempfile.gettempdir()), python='python',
                           timeout=1.0, use_lock=False, runner=runner, **kwargs)


class SafeSegments(unittest.TestCase):

    def test_a_path_traversal_cannot_escape(self):
        for hostile in ('../../etc/passwd', '..\\..\\windows', 'a/b/c'):
            with self.subTest(hostile=hostile):
                segment = safe_segment(hostile)
                self.assertNotIn('/', segment)
                self.assertNotIn('\\', segment)
                self.assertNotIn('..', segment)

    def test_reserved_names_are_replaced(self):
        self.assertEqual(safe_segment('CON'), 'item')
        self.assertEqual(safe_segment('nul.txt'), 'item')

    def test_an_empty_name_falls_back(self):
        self.assertEqual(safe_segment(''), 'item')
        self.assertEqual(safe_segment(None), 'item')


class AttemptDirectories(unittest.TestCase):

    def test_every_attempt_uses_a_fresh_directory(self):
        """Reusing a path makes HYSYS return a non-blank case."""
        a = adapter(fake_runner()).attempt_dir(Path('runs'), 'case-1', 1)
        b = adapter(fake_runner()).attempt_dir(Path('runs'), 'case-1', 1)
        self.assertNotEqual(a, b)
        self.assertIn('attempt-1-', a.name)

    def test_the_attempt_number_is_visible_in_the_path(self):
        path = adapter(fake_runner()).attempt_dir(Path('runs'), 'case-1', 3)
        self.assertIn('attempt-3-', path.name)

    def test_a_hostile_case_id_cannot_escape_the_run_root(self):
        path = adapter(fake_runner()).attempt_dir(Path('runs'), '../../evil', 1)
        self.assertEqual(path.parent.parent.name, 'runs')


class CommandConstruction(unittest.TestCase):

    def test_the_command_is_built_by_the_adapter_not_the_model(self):
        capture: list = []
        with tempfile.TemporaryDirectory() as tmp:
            a = adapter(fake_runner(writes_result={'status': 'PASS', 'case_file': 'x.hsc'}, capture=capture))
            a.run_case(SPEC, 'case-1', Path(tmp))
        command = capture[0]['command']
        self.assertEqual(command[0], 'python')
        self.assertEqual(command[1:3], ['-m', 'hysys_tools'])
        self.assertIn('--spec', command)
        self.assertIn('--folder', command)

    def test_the_shell_is_never_used(self):
        capture: list = []
        with tempfile.TemporaryDirectory() as tmp:
            a = adapter(fake_runner(writes_result={'status': 'PASS', 'case_file': 'x.hsc'}, capture=capture))
            a.run_case(SPEC, 'case-1', Path(tmp))
        self.assertIs(capture[0]['kwargs']['shell'], False)

    def test_the_spec_is_written_as_utf8_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = adapter(fake_runner(returncode=1))
            result = a.run_case({**SPEC, 'note': '甲苯歧化'}, 'case-1', Path(tmp))
            written = json.loads((result.run_dir / 'spec.json').read_text('utf-8'))
            self.assertEqual(written['note'], '甲苯歧化')


class SuccessAndFailure(unittest.TestCase):

    def test_a_passing_run_is_reported_as_passed(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = adapter(fake_runner(writes_result={'status': 'PASS',
                                                   'case_file': 'demo.hsc'}))
            result = a.run_case(SPEC, 'case-1', Path(tmp))
        self.assertEqual(result.status, STATUS_PASS)
        self.assertTrue(result.passed)
        self.assertEqual(result.case_file, 'demo.hsc')

    def test_a_failed_run_keeps_the_tool_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = adapter(fake_runner(returncode=1, writes_result={
                'status': 'FAILED', 'error_type': 'result_check',
                'error': 'conversion check failed'}))
            result = a.run_case(SPEC, 'case-1', Path(tmp))
        self.assertEqual(result.status, 'FAILED')
        self.assertEqual(result.error_type, 'result_check')
        self.assertFalse(result.passed)

    def test_a_missing_result_file_is_a_recorded_failure_not_an_exception(self):
        """The worker can die before writing anything; that must be reported."""
        with tempfile.TemporaryDirectory() as tmp:
            a = adapter(fake_runner(returncode=1, stderr='ImportError: no pywin32'))
            result = a.run_case(SPEC, 'case-1', Path(tmp))
        self.assertEqual(result.status, STATUS_NO_RESULT)
        self.assertIn('no result.json', result.error)
        self.assertIn('ImportError', result.stderr)

    def test_a_connection_failure_is_recognised(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = adapter(fake_runner(returncode=1, stdout='CANNOT_CONNECT_TO_HYSYS'))
            result = a.run_case(SPEC, 'case-1', Path(tmp))
        self.assertEqual(result.status, STATUS_CANNOT_CONNECT)

    def test_unreadable_json_is_reported(self):
        def runner(command, **kwargs):
            folder = Path(command[command.index('--folder') + 1])
            (folder / 'result.json').write_text('{not json', encoding='utf-8')
            return subprocess.CompletedProcess(command, 1, '', '')
        with tempfile.TemporaryDirectory() as tmp:
            result = adapter(runner).run_case(SPEC, 'case-1', Path(tmp))
        self.assertEqual(result.status, STATUS_NO_RESULT)
        self.assertIn('could not be read', result.error)


class TimeoutHandling(unittest.TestCase):

    def test_a_timeout_is_reported_without_touching_hysys(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = adapter(fake_runner(delay_raises=True)).run_case(
                SPEC, 'case-1', Path(tmp))
        self.assertEqual(result.status, STATUS_TIMEOUT)
        self.assertIn('HYSYS itself was not touched', result.error)

    def test_partial_output_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = adapter(fake_runner(delay_raises=True)).run_case(
                SPEC, 'case-1', Path(tmp))
        self.assertIn('partial stdout', result.stdout)


class AdapterWritesItsOwnRecord(unittest.TestCase):

    def test_an_adapter_summary_is_written_beside_the_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = adapter(fake_runner(writes_result={'status': 'PASS', 'case_file': 'x.hsc'}))
            result = a.run_case(SPEC, 'case-1', Path(tmp))
            written = json.loads((result.run_dir / 'adapter.json').read_text('utf-8'))
        self.assertEqual(written['status'], STATUS_PASS)
        self.assertEqual(written['case_id'], 'case-1')

    def test_the_summary_contains_no_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = adapter(fake_runner(writes_result={'status': 'PASS', 'case_file': 'x.hsc'}))
            result = a.run_case(SPEC, 'case-1', Path(tmp))
            blob = json.dumps(result.summary())
        for forbidden in ('sk-', 'TR_KEY', 'Authorization', 'Bearer'):
            self.assertNotIn(forbidden, blob)


class ComputedResultsAreKept(unittest.TestCase):
    """The exam asks for the simulation results, not for a status line.

    `summary()` is a log record - status, elapsed time, error. An earlier version
    returned only that, so a successful run reported "case-1: PASS, 4.2s" and none of
    the composition, temperature, conversion or duty that had actually been computed.
    """

    RESULT = {
        'status': 'PASS',
        'reactor_kind': 'gibbs',
        'heat_duty_kW': 39988.61475671525,
        'case_file': 'agent-smr-710C.hsc',
        'solver_is_solving': False,
        'outlet': {
            'VAPOUR': {'temperature_C': 710.0, 'pressure_kPa': 1350.0,
                       'molar_flow_kmol_h': 4780.716185884519,
                       'mass_flow_kg_h': 64684.06114187387,
                       'mole_fractions': {'Methane': 0.09614498940867292,
                                          'Hydrogen': 0.406385}},
            'LIQUID': {'temperature_C': None, 'pressure_kPa': None,
                       'molar_flow_kmol_h': 0.0, 'mass_flow_kg_h': None,
                       'mole_fractions': {}},
        },
        'checks': {'reactant_conversion_percent': {'Methane': 54.03580929422617},
                   'worst_element_relative_error': 7.74e-16,
                   'mass_relative_error': -4.5e-16,
                   'co_yield': {'definition': '(n_CO_out - n_CO_in) / n_C_feed * 100%',
                                'co_yield_percent': 21.862005121604053,
                                'dry_outlet_kmol_h': 2942.812320553002}},
        'warnings': ['a warning'], 'open_questions': ['a question'],
        'assumptions': ['an assumption'],
    }

    def _result(self, payload=None):
        return ExecutionResult(case_id='case-1', attempt=1, run_dir=Path('runs/x'),
                               status=STATUS_PASS, exit_code=0,
                               result=self.RESULT if payload is None else payload)

    def test_the_numbers_survive(self):
        results = self._result().results()
        self.assertEqual(results['heat_duty_kW'], 39988.61475671525)
        self.assertEqual(results['reactant_conversion_percent']['Methane'],
                         54.03580929422617)
        self.assertEqual(results['streams']['VAPOUR']['temperature_C'], 710.0)
        self.assertEqual(results['co_yield']['co_yield_percent'],
                         21.862005121604053)

    def test_the_co_yield_keeps_its_definition(self):
        """A yield without its basis is not a result, it is a number."""
        self.assertIn('n_C_feed', self._result().results()['co_yield']['definition'])

    def test_the_summary_stays_small(self):
        """`summary` is a log line and must not start carrying payloads."""
        summary = self._result().summary()
        self.assertNotIn('streams', summary)
        self.assertNotIn('co_yield', summary)
        json.dumps(summary)

    def test_a_missing_section_degrades_instead_of_raising(self):
        for payload in ({'status': 'PASS', 'case_file': 'x.hsc'}, {'status': 'PASS', 'checks': {}},
                        {'status': 'PASS', 'outlet': None}, {}):
            with self.subTest(payload=payload):
                results = self._result(payload).results()
                self.assertEqual(results['streams'], {})
                self.assertEqual(results['reactant_conversion_percent'], {})

    def test_the_results_are_json_safe(self):
        json.dumps(self._result().results(), ensure_ascii=False)

    def test_a_stream_keeps_its_name(self):
        """The explanation has to label the stream it is describing."""
        self.assertEqual(self._result().results()['streams']['VAPOUR']['name'],
                         'VAPOUR')


class SuccessNeedsEvidence(unittest.TestCase):
    """A PASS is not a success unless the process agreed and evidence exists.

    Three ways this was wrong before: a worker that wrote `PASS` and then exited
    non-zero was accepted; a result that said `PASS` with no case file, no outlet and
    no checks was accepted; and nothing of the worker's output was kept, so a remote
    failure could not be diagnosed afterwards.
    """

    def test_a_non_zero_exit_is_not_a_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = adapter(fake_runner(
                returncode=1,
                writes_result={'status': 'PASS', 'case_file': 'x.hsc'})
            ).run_case(SPEC, 'case-1', Path(tmp))
        self.assertNotEqual(result.status, STATUS_PASS)
        self.assertFalse(result.passed)
        self.assertIn('exited with code 1', result.error)

    def test_a_pass_with_no_evidence_is_not_a_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = adapter(fake_runner(
                returncode=0, writes_result={'status': 'PASS'})
            ).run_case(SPEC, 'case-1', Path(tmp))
        self.assertNotEqual(result.status, STATUS_PASS)
        self.assertIn('no simulation is evidenced', result.error)

    def test_each_kind_of_evidence_is_accepted(self):
        for payload in ({'status': 'PASS', 'case_file': 'x.hsc'},
                        {'status': 'PASS', 'outlet': {'VAPOUR': {}}},
                        {'status': 'PASS', 'checks': {'worst_element_relative_error': 0}},
                        {'status': 'PASS', 'readback_component_names': ['Methane']}):
            with self.subTest(payload=sorted(payload)):
                with tempfile.TemporaryDirectory() as tmp:
                    result = adapter(fake_runner(returncode=0,
                                                 writes_result=payload)
                                     ).run_case(SPEC, 'case-1', Path(tmp))
                self.assertEqual(result.status, STATUS_PASS)
                self.assertTrue(result.passed)

    def test_the_worker_output_is_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = adapter(fake_runner(
                returncode=1, stdout='building case', stderr='Traceback: boom',
                writes_result={'status': 'FAILED'})
            ).run_case(SPEC, 'case-1', Path(tmp))
            stdout = (result.run_dir / 'worker-stdout.txt').read_text('utf-8')
            stderr = (result.run_dir / 'worker-stderr.txt').read_text('utf-8')
        self.assertIn('building case', stdout)
        self.assertIn('Traceback: boom', stderr)

    def test_nothing_is_written_when_there_is_no_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = adapter(fake_runner(returncode=0, stdout='', stderr='',
                                         writes_result={'status': 'PASS',
                                                        'case_file': 'x.hsc'})
                             ).run_case(SPEC, 'case-1', Path(tmp))
            self.assertFalse((result.run_dir / 'worker-stdout.txt').exists())

    def test_the_summary_flags_a_pass_without_evidence(self):
        entry = ExecutionResult(case_id='c', attempt=1, run_dir=Path('r'),
                                status=STATUS_PASS, exit_code=0,
                                result={'status': 'PASS'})
        self.assertTrue(entry.summary()['error'])

    def test_a_non_zero_exit_alone_does_not_break_a_failed_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = adapter(fake_runner(returncode=1, writes_result={
                'status': 'FAILED', 'error_type': 'result_check'})
            ).run_case(SPEC, 'case-1', Path(tmp))
        self.assertEqual(result.status, 'FAILED')
        self.assertEqual(result.error_type, 'result_check')


class TimeoutStopsTheRun(unittest.TestCase):

    def test_a_timeout_is_its_own_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = adapter(fake_runner(delay_raises=True)).run_case(
                SPEC, 'case-1', Path(tmp))
        self.assertEqual(result.status, STATUS_TIMEOUT)

    def test_the_run_stops_after_a_timeout(self):
        """The workstation is in an unknown state; the next case must wait."""
        from reactor_agent.pipeline import _status_from_executions

        class Fake:
            status = STATUS_TIMEOUT
            passed = False

        self.assertEqual(_status_from_executions([Fake()], 2), 'FAILED')


class Ledger(unittest.TestCase):

    def test_an_entry_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp), 'run-1')
            store.record_result('case-1', SPEC, 1, STATUS_PASS, seconds=12.5,
                                tool_status='PASS')
            reread = RunStore(Path(tmp), 'run-1').entries()
        self.assertEqual(len(reread), 1)
        self.assertEqual(reread[0].case_id, 'case-1')
        self.assertEqual(reread[0].seconds, 12.5)

    def test_a_completed_case_is_not_run_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp))
            store.record_result('case-1', SPEC, 1, STATUS_PASS)
            self.assertFalse(store.needs_running('case-1', SPEC))

    def test_a_changed_spec_makes_the_case_pending_again(self):
        """Identity is case + spec hash, not the case id alone."""
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp))
            store.record_result('case-1', SPEC, 1, STATUS_PASS)
            changed = {**SPEC, 'reactor': {'kind': 'gibbs'}}
            self.assertTrue(store.needs_running('case-1', changed))
            self.assertNotEqual(hash_spec(SPEC), hash_spec(changed))

    def test_a_failed_attempt_does_not_count_as_completed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp))
            store.record_result('case-1', SPEC, 1, 'FAILED')
            self.assertTrue(store.needs_running('case-1', SPEC))

    def test_attempts_are_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp))
            for attempt in (1, 2, 3):
                store.record_result('case-1', SPEC, attempt, 'FAILED')
            self.assertEqual(store.attempts('case-1'), 3)

    def test_a_truncated_final_line_does_not_break_resume(self):
        """A hard kill can leave half a line; refusing to resume would be worse."""
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp))
            store.record_result('case-1', SPEC, 1, STATUS_PASS)
            with store.path.open('a', encoding='utf-8') as handle:
                handle.write('{"case_id": "case-2", "spec_h')
            entries = RunStore(Path(tmp)).entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].case_id, 'case-1')

    def test_appends_never_overwrite_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp))
            store.record_result('case-1', SPEC, 1, 'FAILED')
            store.record_result('case-1', SPEC, 2, STATUS_PASS)
            raw = store.path.read_text('utf-8').strip().splitlines()
        self.assertEqual(len(raw), 2)

    def test_the_summary_reports_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp), 'run-1')
            store.record_result('case-1', SPEC, 1, STATUS_PASS)
            store.record_result('case-2', SPEC, 1, 'FAILED')
            summary = store.summary()
        self.assertEqual(summary['run_id'], 'run-1')
        self.assertEqual(summary['succeeded'], ['case-1'])
        self.assertEqual(summary['by_status'][STATUS_PASS], 1)

    def test_the_ledger_holds_no_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp))
            store.record_result('case-1', SPEC, 1, STATUS_PASS, notes=['ok'])
            blob = store.path.read_text('utf-8')
        for forbidden in ('sk-', 'TR_KEY', 'Bearer'):
            self.assertNotIn(forbidden, blob)

    def test_the_hash_ignores_key_order(self):
        self.assertEqual(hash_spec({'a': 1, 'b': 2}), hash_spec({'b': 2, 'a': 1}))


class ResultView(unittest.TestCase):

    def test_summary_fields_are_json_safe(self):
        entry = ExecutionResult(case_id='c', attempt=1, run_dir=Path('runs/x'),
                                status=STATUS_PASS, seconds=1.23456)
        json.dumps(entry.summary())

    def test_error_type_is_read_from_the_tool_result(self):
        entry = ExecutionResult(case_id='c', attempt=1, run_dir=Path('r'),
                                status='FAILED',
                                result={'status': 'FAILED',
                                        'error_type': 'result_check'})
        self.assertEqual(entry.error_type, 'result_check')


if __name__ == '__main__':
    unittest.main(verbosity=2)
