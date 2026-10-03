"""Offline regression tests. No COM connection and no simulated thermodynamics claims."""
from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from . import core, precheck, reactor, validate
from .main import main as cli_main, list_capabilities
from .examples import gasification_spec, reforming_spec, toluene_spec
from .selfcheck import FakeApplication, FakeCase, FakeOperation, run_spec


class Variable:
    def __init__(self, values):
        self.values = iter(values if isinstance(values, list) else [values])
        self.last = None

    def GetValue(self, unit):
        self.last = next(self.values, self.last)
        return self.last


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def solve(self, *, amount=100., temperature=710., pressure=1350.,
              mass=1600., fractions=None, duty=100., solver=None):
        spec = reforming_spec(710)
        builder = reactor.CaseBuilder(spec, self.folder, None, None, core.StepLog(self.folder))
        builder.readback_names = ['Methane']
        vapour = SimpleNamespace(MolarFlow=Variable(amount), Temperature=Variable(temperature),
                                 Pressure=Variable(pressure), MassFlow=Variable(mass),
                                 ComponentMolarFraction=SimpleNamespace(
                                     Values=[1.] if fractions is None else fractions))
        liquid = SimpleNamespace(MolarFlow=Variable(0.))
        streams = SimpleNamespace(Item=lambda name: {'VAPOUR': vapour, 'LIQUID': liquid}[name])
        operation = SimpleNamespace(IsIgnored=True, HeatFlow=Variable(duty))
        case = SimpleNamespace(Solver=solver or SimpleNamespace(IsSolving=False))
        with patch.object(reactor, 'time', Clock()), patch.object(reactor, 'SOLVER_TIMEOUT_SECONDS', 2.):
            return builder.solve_and_read(case, operation, streams, 710.)

    def test_stable_result_has_evidence(self):
        result = self.solve()
        self.assertEqual(result['solver_evidence']['stable_reads'], 3)
        self.assertFalse(result['solver_is_solving'])

    def test_valid_but_oscillating_flows_fail(self):
        with self.assertRaises(validate.ResultCheckError):
            self.solve(amount=[100., 101.] * 10)

    def test_stable_composition_with_moving_temperature_fails(self):
        with self.assertRaises(validate.ResultCheckError):
            self.solve(temperature=[710., 711.] * 10)

    def test_stable_composition_with_moving_duty_fails(self):
        with self.assertRaises(validate.ResultCheckError):
            self.solve(duty=[100., 101.] * 10)

    def test_last_valid_sample_is_not_returned_after_unknowns(self):
        with self.assertRaises(validate.ResultCheckError):
            self.solve(amount=[100., -32767.])

    def test_invalid_readback_rejected(self):
        for params in ({'duty': float('nan')}, {'duty': -32767.}, {'mass': float('inf')},
                       {'mass': -32767.}, {'fractions': [1., 0.]}, {'fractions': [float('nan')]},
                       {'pressure': 0.}, {'temperature': -300.}):
            with self.subTest(params=params), self.assertRaises(RuntimeError):
                self.solve(**params)

    def test_wrong_target_temperature_is_result_error(self):
        with self.assertRaises(validate.ResultCheckError):
            self.solve(temperature=700.)

    def test_wrong_target_pressure_is_result_error(self):
        with self.assertRaises(validate.ResultCheckError):
            self.solve(pressure=1300.)

    def test_solver_restarts_after_stable_reads(self):
        class Restarting:
            calls = 0

            @property
            def IsSolving(self):
                self.calls += 1
                return self.calls >= 7

        with self.assertRaises(validate.ResultCheckError):
            self.solve(solver=Restarting())

    def test_missing_reactions_allowed_for_gibbs(self):
        spec = reforming_spec(710)
        del spec['reactions']
        self.assertTrue(precheck.validate_spec(spec)['ok'])

    def test_nonfinite_input_rejected(self):
        for key in ('temperature', 'pressure', 'total_flow'):
            for value in (float('nan'), float('inf'), 'NaN', '-Infinity', None, True):
                spec = toluene_spec()
                spec['feeds'][0][key] = value
                with self.subTest(key=key, value=value):
                    self.assertFalse(precheck.validate_spec(spec)['ok'])
        spec = toluene_spec()
        spec['reactor']['pressure_drop_kPa'] = 'NaN'
        self.assertFalse(precheck.validate_spec(spec)['ok'])

    def test_invalid_case_names_rejected_before_creating_case(self):
        for name in ('../escape', '..\\escape', 'C:\\outside', 'NUL', 'COM1.txt', 'x.', '', None):
            spec = toluene_spec()
            spec['case_name'] = name
            app = FakeApplication()
            with self.subTest(name=name):
                result = run_spec(spec, self.folder, app)
                self.assertEqual(result['status'], 'FAILED')
                self.assertEqual(app.Count, 0)

    def test_null_collections_rejected(self):
        for key in ('feeds', 'reactor', 'fluid_package', 'reactions', 'assumptions', 'open_questions'):
            spec = toluene_spec()
            spec[key] = None
            with self.subTest(key=key):
                self.assertFalse(precheck.validate_spec(spec)['ok'])

    def test_blocking_question_rejected_even_with_valid_numbers(self):
        spec = gasification_spec()
        spec['feeds'][0]['total_flow_unit'] = 'kg/h'
        report = precheck.validate_spec(spec)
        self.assertFalse(report['ok'])
        self.assertTrue(any('blocking_questions' in e for e in report['errors']))

    def test_informational_question_does_not_block(self):
        self.assertTrue(precheck.validate_spec(reforming_spec(710))['ok'])

    def test_pressure_drop_cannot_exceed_inlet_pressure(self):
        spec = toluene_spec()
        spec['reactor']['pressure_drop_kPa'] = 2500.
        self.assertFalse(precheck.validate_spec(spec)['ok'])

    def test_duplicate_feed_aliases_rejected(self):
        spec = reforming_spec(710)
        spec['feeds'][0]['flows']['CH4'] = 1.
        self.assertFalse(precheck.validate_spec(spec)['ok'])

    def test_unknown_base_and_conflicting_conversion_rejected(self):
        spec = toluene_spec()
        spec['reactions'][0]['base_component'] = 'Xenon'
        self.assertFalse(precheck.validate_spec(spec)['ok'])
        spec = toluene_spec()
        spec['reactions'][0]['conversion_coefficients'] = [20., 0., 0.]
        self.assertFalse(precheck.validate_spec(spec)['ok'])

    def test_multiple_conversion_reactions_rejected(self):
        spec = toluene_spec()
        spec['reactions'].append(copy.deepcopy(spec['reactions'][0]))
        self.assertFalse(precheck.validate_spec(spec)['ok'])

    def test_balance_rejects_nan(self):
        with self.assertRaises(validate.ResultCheckError):
            validate.check_conservation({'Methane': 1.}, {'Methane': float('nan')})

    def test_existing_case_file_not_overwritten(self):
        path = self.folder / 'agent-toluene.hsc'
        path.write_text('original')
        app = FakeApplication()
        result = run_spec(toluene_spec(), self.folder, app)
        self.assertEqual(result['error_type'], 'environment')
        self.assertEqual(path.read_text(), 'original')
        self.assertEqual(app.Count, 0)

    def test_foreign_stream_case_not_edited_saved_or_closed(self):
        case = FakeCase()
        case.Flowsheet.MaterialStreams.Add('EXISTING')
        app = SimpleNamespace(SimulationCases=SimpleNamespace(Add=lambda path: case))
        result = run_spec(toluene_spec(), self.folder, app)
        self.assertEqual(result['error_type'], 'environment')
        self.assertFalse(case.BasisManager.editing)
        self.assertEqual(case.BasisManager.FluidPackages.Count, 0)
        self.assertEqual(case.close_calls, [])
        self.assertFalse(list(self.folder.glob('*.hsc')))

    def test_output_mismatch_keeps_failure_case_and_classification(self):
        original = FakeOperation._fill

        def wrong_temperature(operation, stream, vapour):
            original(operation, stream, vapour)
            stream.Temperature.value = 700.

        with patch.object(FakeOperation, '_fill', wrong_temperature), patch.object(reactor, 'time', Clock()):
            result = run_spec(reforming_spec(710), self.folder, FakeApplication())
        self.assertEqual(result['error_type'], 'result_check')
        self.assertTrue((self.folder / result['failed_case_file']).is_file())

    def test_missing_com_dependency_produces_json(self):
        path = self.folder / 'spec.json'
        core.write_json(path, toluene_spec())
        with patch.dict('sys.modules', {'pythoncom': None}), redirect_stdout(io.StringIO()):
            code = cli_main(['--spec', str(path), '--folder', str(self.folder / 'run')])
        self.assertEqual(code, 1)
        result = json.loads((self.folder / 'run/result.json').read_text(encoding='utf-8'))
        self.assertEqual(result['status'], 'CANNOT_CONNECT_TO_HYSYS')
        self.assertEqual(result['error_type'], 'environment')

    def test_json_write_failure_not_swallowed(self):
        path = self.folder / 'result.json'
        core.write_json(path, {'status': 'original'})
        with patch.object(core.os, 'replace', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                core.write_json(path, {'status': 'replacement'})
        self.assertEqual(json.loads(path.read_text())['status'], 'original')
        self.assertFalse(list(self.folder.glob('*.tmp')))

    def test_advertised_aliases_are_supported(self):
        for spelling in list_capabilities()['components']['accepted_spellings']:
            core.library_name(spelling)

    def test_malformed_reactor_cli_returns_result(self):
        spec = toluene_spec()
        spec['reactor'] = None
        path = self.folder / 'spec.json'
        core.write_json(path, spec)
        with redirect_stdout(io.StringIO()):
            code = cli_main(['--spec', str(path), '--folder', str(self.folder / 'run')])
        self.assertEqual(code, 1)
        result = json.loads((self.folder / 'run/result.json').read_text(encoding='utf-8'))
        self.assertEqual(result['error_type'], 'specification')

    def test_health_check_without_com_is_structured(self):
        output = io.StringIO()
        with patch.dict('sys.modules', {'pythoncom': None}), redirect_stdout(output):
            code = cli_main(['--health-check'])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())['error_type'], 'environment')

    def test_remote_worker_timeout_keeps_logs(self):
        from .remote_check import run_worker
        with redirect_stdout(io.StringIO()):
            result = run_worker(['-c', 'import time; time.sleep(10)'],
                                self.folder, 'timeout-fixture', .1)
        self.assertEqual(result['status'], 'TIMEOUT')
        self.assertTrue((self.folder / 'process.json').is_file())
        self.assertTrue((self.folder / 'stderr.txt').is_file())

    def test_baseline_comparison_catches_changes_and_missing_values(self):
        from .remote_check import compare_baseline
        self.assertEqual(compare_baseline({'checks': {'x': 50.}}, {'checks/x': 50.}), [])
        self.assertTrue(compare_baseline({'checks': {'x': 30.}}, {'checks/x': 50.}))
        self.assertTrue(compare_baseline({}, {'checks/x': 50.}))

    def test_failed_basis_build_closes_owned_case(self):
        from .selfcheck import FakeComponents
        app = FakeApplication()
        with patch.object(FakeComponents, 'Add', side_effect=RuntimeError('component error')):
            result = run_spec(toluene_spec(), self.folder, app)
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(app.Count, 0)
        self.assertTrue(app.closed_cases)

    def test_conversion_without_base_inlet_is_rejected(self):
        spec = toluene_spec()
        spec['feeds'][0]['fractions'] = {'Benzene': 1.}
        self.assertFalse(precheck.validate_spec(spec)['ok'])

    def test_json_bom_supported_and_nonfinite_constants_rejected(self):
        path = self.folder / 'spec.json'
        path.write_text(json.dumps(toluene_spec()), encoding='utf-8-sig')
        self.assertEqual(core.load_spec(path)['case_name'], 'agent-toluene')
        path.write_text('{"value": NaN}', encoding='utf-8')
        with self.assertRaises(core.SpecError):
            core.load_spec(path)

    def test_cli_summary_is_ascii_for_unicode_path_and_error(self):
        from .main import _emit_summary
        output = io.StringIO()
        with redirect_stdout(output):
            _emit_summary({'status': 'FAILED', 'reactor_kind': '\u4e0d\u652f\u6301',
                           'error': '\u9519\u8bef'}, self.folder / '\u6d4b\u8bd5.json')
        output.getvalue().encode('ascii')


if __name__ == '__main__':
    unittest.main()
