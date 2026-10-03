"""reactor.solid_carbon = "saturation": route two inside the real build_case.

The end-to-end tests go through `main.build_case` itself, on fakes that run the
conversion reactor and a real gas-phase equilibrium (fake_gasifier). The reference
numbers are those probe five measured on the workstation (route2-20261003-084249):
X = 0.41158; Carbon 1490.93, CO 1017.18, H2 979.77, CH4 22.19 kmol/h; 84.66 MW. The
fake is ideal-gas, so it is compared to those within a few tenths of a percent.
"""
import json
import tempfile
import unittest
from pathlib import Path

from . import fake_gasifier, saturation, selfcheck as sc
from .core import canonical
from .examples import EXAMPLES, gasification_native_spec, gasification_saturation_spec
from .main import build_case, list_capabilities
from .precheck import validate_spec
from .reactor import CaseBuilder
from .validate import ResultCheckError, check_gibbs_equilibrium

MEASURED = {'carbon': 1490.93, 'carbon monoxide': 1017.18, 'hydrogen': 979.77,
            'methane': 22.19}


def run(spec, scenario='direct'):
    with fake_gasifier.environment(scenario), tempfile.TemporaryDirectory() as tmp:
        result = build_case(spec, Path(tmp) / 'out', sc.PythonComStub, sc.Win32ComStub,
                            sc.FakeApplication())
        saved = (Path(tmp) / 'out' / (result.get('case_file') or 'none')).is_file()
    return json.loads(json.dumps(result, default=str)), saved


class TheSaturationPathPasses(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.result, cls.saved = run(gasification_saturation_spec())
        cls.flows = {canonical(k): v for k, v in
                     (cls.result.get('component_flows_kmol_h') or {}).items()}
        cls.block = cls.result.get('solid_carbon_saturation') or {}

    def test_status(self):
        self.assertEqual(self.result['status'], 'PASS', self.result.get('error'))
        self.assertTrue(self.saved)

    def test_lands_on_the_measured_answer(self):
        self.assertAlmostEqual(self.block['carbon_conversion_x'], 0.41158, delta=2e-4)
        for key, value in MEASURED.items():
            self.assertLess(abs(self.flows[key] / value - 1), 0.004, key)

    def test_all_three_gate_checks(self):
        checks = self.result['checks']
        self.assertEqual(checks['condensed_phase_location']['verdict'],
                         'CONDENSED_PHASE_ONLY')
        self.assertEqual(checks['gibbs_equilibrium']['verdict'], 'CONSISTENT')
        self.assertEqual(checks['independent_duty']['verdict'], 'CONSISTENT')

    def test_three_activities_agree(self):
        self.assertLess(self.block['carbon_activity']['spread_decades'], 1e-3)

    def test_library_carbon_gibbs_never_used_and_the_skip_is_logged(self):
        self.assertFalse(self.block['library_carbon_gibbs_used'])
        skipped = [s for s in self.result['steps']
                   if s['step'] == 'solid_carbon_reference']
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]['status'], 'SKIPPED')

    def test_duty_is_the_sum_of_both_reactors(self):
        parts = self.block['duty_by_reactor_kW']
        self.assertAlmostEqual(self.result['heat_duty_kW'], sum(parts.values()), places=6)
        self.assertEqual(set(parts), {'conversion', 'gibbs'})

    def test_report_quantities(self):
        co = self.result['checks']['co_yield']
        self.assertAlmostEqual(co['co_yield_percent'], 40.14, delta=0.1)
        self.assertAlmostEqual(co['carbon_conversion_percent'], 41.16, delta=0.1)
        self.assertAlmostEqual(co['co_mole_fraction_dry'], 0.503, delta=0.002)

    def test_edit_mode_recorded(self):
        self.assertEqual(self.block['edit_mode'], 'direct')


class TheEditFallbackAndTheFailures(unittest.TestCase):

    def test_basis_edit_fallback(self):
        result, _ = run(gasification_saturation_spec(), 'basis_edit')
        self.assertEqual(result['status'], 'PASS', result.get('error'))
        self.assertEqual(result['solid_carbon_saturation']['edit_mode'], 'basis_edit')

    def test_an_edit_that_never_takes_effect_fails_clearly(self):
        result, _ = run(gasification_saturation_spec(), 'ineffective')
        self.assertEqual(result['status'], 'FAILED')
        self.assertIn('could not be changed in place', result['error'])

    def test_carbon_in_the_conversion_vapour_is_a_result_failure(self):
        result, _ = run(gasification_saturation_spec(), 'carbon_in_conv_vapour')
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(result['error_type'], 'result_check')
        self.assertIn('vapour product', result['error'])

    def test_nothing_solving_fails(self):
        result, _ = run(gasification_saturation_spec(), 'no_solve')
        self.assertEqual(result['status'], 'FAILED')
        self.assertIn('no valid outlet', result['error'])


class TheDefaultPathIsUnchanged(unittest.TestCase):

    def test_plain_gibbs_with_library_carbon_is_still_refused_before_the_solve(self):
        result, _ = run(gasification_native_spec())
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(result['error_type'], 'model_limitation')

    def test_the_flag_defaults_off(self):
        builder = CaseBuilder.__new__(CaseBuilder)
        CaseBuilder.__init__(builder, {}, Path('.'), None, None, None)
        self.assertFalse(builder.carbon_gibbs_unused)

    def test_specs_without_the_field_are_not_touched(self):
        for name, factory in EXAMPLES.items():
            spec = factory()
            if name in ('coal-slurry-gasification-saturation','coal-slurry-gasification'):
                continue
            self.assertFalse(saturation.is_requested(spec['reactor']), name)


class ThePrecheckRules(unittest.TestCase):

    def spec(self, **reactor):
        spec = gasification_saturation_spec()
        spec['reactor'].update(reactor)
        return spec

    def errors(self, spec):
        return ' '.join(validate_spec(spec)['errors'])

    def test_the_example_passes(self):
        report = validate_spec(gasification_saturation_spec())
        self.assertTrue(report['ok'], report['errors'])
        self.assertEqual(report['readback']['solid_carbon'], 'saturation')

    def test_an_unknown_value(self):
        self.assertIn('only value is "saturation"', self.errors(self.spec(solid_carbon='x')))

    def test_needs_gibbs(self):
        spec = self.spec(kind='conversion')
        self.assertIn('needs reactor.kind "gibbs"', self.errors(spec))

    def test_needs_the_candidate_products(self):
        spec = gasification_saturation_spec()
        spec['fluid_package']['components'] = ['Carbon', 'Water', 'CO', 'Hydrogen', 'CO2']
        self.assertIn('missing', self.errors(spec))

    def test_carbon_and_water_feed_only(self):
        spec = gasification_saturation_spec()
        spec['fluid_package']['components'].append('Nitrogen')
        spec['feeds'][0]['fractions'] = {'Carbon': 0.6, 'Water': 0.3, 'Nitrogen': 0.1}
        self.assertIn('carbon + water feed only', self.errors(spec))

    def test_every_example_still_passes_its_preflight_as_remote_check_expects(self):
        for name, factory in EXAMPLES.items():
            self.assertEqual(validate_spec(factory())['ok'],
                             name != 'coal-slurry-gasification-unclarified', name)


class TheGateNoLongerWavesThroughAReactorThatDidNotReact(unittest.TestCase):
    """Measured: the route-one probe case came back as its own feed."""

    def test_carbon_and_water_with_no_hydrogen_is_rejected(self):
        passthrough = {'Carbon': 2533.8, 'H2O': 1035.4, 'CO': 0.0, 'Hydrogen': 0.0,
                       'CO2': 0.0, 'Methane': 0.0}
        with self.assertRaises(ResultCheckError) as ctx:
            check_gibbs_equilibrium(passthrough, 1673.15, 40.0, expect_condensed=True)
        self.assertIn('did not react', str(ctx.exception))

    def test_a_system_with_no_hydrogen_at_all_still_reports(self):
        dry = {'Carbon': 10.0, 'CO': 5.0, 'CO2': 1.0}
        block = check_gibbs_equilibrium(dry, 1673.15, 40.0, expect_condensed=True)
        self.assertEqual(block['verdict'], 'NO_HYDROGEN')


class TheCapabilitiesDescribeIt(unittest.TestCase):

    def test_listed(self):
        capabilities = list_capabilities()
        self.assertIn('saturation', capabilities['solid_carbon'])
        self.assertIn('solid_carbon', json.dumps(capabilities['capability_combinations']))


if __name__ == '__main__':
    unittest.main()
