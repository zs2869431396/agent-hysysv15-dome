"""Phase 1 of the gasification plan: harden the result gate before route one runs.

Three ways a solid-carbon Gibbs result can be wrong while atoms and mass balance:

  1. wrong carbon reference state (the measured vertex)   -> equilibrium gate (existing)
  2. carbon handled as a fluid, dissolved in the gas      -> phase location + tighter band
  3. right composition, wrong enthalpy data               -> independent duty

and one requirement: specs without a condensed species (toluene, steam reforming) must
produce exactly the block they produced before. The golden file holds the PRE-CHANGE
output for the three accepted remote runs.

Reference compositions below come from an ideal-gas Gibbs minimisation at 1673.15 K and
40 bar using HYSYS's own (probe-verified) Gibbs data for CO, CO2, H2O and CH4:
  * GRAPHITE    - carbon as a pure solid at unit activity (the correct answer);
  * GAS_CARBON  - carbon as a GAS species with dGf = 0, i.e. a hypothetical whose solid
                  flag did not take. Its methane is six times too high.
"""
import json
import math
import unittest
from pathlib import Path

from .core import atoms_of, canonical, molar_mass_of, resolve_in_readback
from .examples import gasification_native_spec
from .thermo_reference import (
    SHOMATE,
    ReferenceUnavailable,
    gas_cp,
    gas_enthalpy,
    graphite_enthalpy,
    independent_duty_kW,
    water_phase,
)
from .validate import (
    CONDENSED_IN_GAS_LIMIT,
    CONDENSED_TOLERANCE_ORDERS,
    DUTY_RELATIVE_TOLERANCE,
    EQUILIBRIUM_TOLERANCE_ORDERS,
    ResultCheckError,
    check_condensed_phase_location,
    check_gibbs_equilibrium,
    check_independent_duty,
    verify_case,
)

T_K = 1673.15
P_BAR = 40.0
FEED = {'carbon': 2533.8002406328606, 'water': 1035.4024318705458}

GRAPHITE = {'Carbon': 1490.8434079847957, 'H2O': 11.170927535980029,
            'CO': 1017.2891397794393, 'Hydrogen': 979.8384831524398,
            'CO2': 3.4711822775628285, 'Methane': 22.196510591062776}
GAS_CARBON = {'Carbon': 1363.1698196509533, 'H2O': 0.33473544891884865,
              'CO': 1034.7961909154135, 'Hydrogen': 763.6707417948509,
              'CO2': 0.13575275310704915, 'Methane': 135.69847731338825}
# Measured on the workstation (native-flow-20261003-044213, probe-carbon-properties).
MEASURED = {'Carbon': 980.6967864262893, 'H2O': 5.0410966717477984e-20,
            'CO': 1035.4024305629503, 'Hydrogen': 0.0003832757063456391,
            'CO2': 4.069030387495692e-14, 'Methane': 517.701023643622}
MEASURED_DUTY_KW = 73019.77127946336


def products(outlet: dict, carbon_in_gas: bool) -> dict:
    """Split an outlet into VAPOUR / LIQUID the way solve_and_read records it."""
    carbon_names = [n for n in outlet if canonical(n) == 'carbon']
    gas = {n: v for n, v in outlet.items() if n not in carbon_names or carbon_in_gas}
    solid = {n: v for n, v in outlet.items() if n in carbon_names and not carbon_in_gas}

    def entry(flows):
        total = sum(flows.values())
        fractions = {n: (flows.get(n, 0.0) / total if total else 0.0) for n in outlet}
        return {'molar_flow_kmol_h': total, 'temperature_C': T_K - 273.15,
                'pressure_kPa': P_BAR * 100, 'mole_fractions': fractions}

    return {'VAPOUR': entry(gas), 'LIQUID': entry(solid)}


def run(outlet, duty_kW, carbon_in_gas=False, with_products=True):
    spec = gasification_native_spec()
    masses = {canonical(k): molar_mass_of(k) for k in FEED}
    return verify_case(spec, FEED, outlet, 'gibbs', duty_kW, 'isothermal', masses,
                       products=(products(outlet, carbon_in_gas) if with_products
                                 else None))


def reference_duty(outlet) -> float:
    return independent_duty_kW(FEED, 313.15, P_BAR,
                               {canonical(k): v for k, v in outlet.items()}, T_K, P_BAR)


class TheShomateDataIsSelfConsistent(unittest.TestCase):
    """Catches transcription errors; not a substitute for checking the WebBook."""

    CP_298 = {'carbon monoxide': 29.14, 'carbon dioxide': 37.13, 'hydrogen': 28.84,
              'water': 33.58, 'methane': 35.69}

    def test_enthalpy_is_dhf_at_298(self):
        for key, rows in SHOMATE.items():
            self.assertAlmostEqual(gas_enthalpy(key, 298.15), rows[0][8], places=2, msg=key)

    def test_heat_capacity_at_298_matches_the_tables(self):
        for key, value in self.CP_298.items():
            self.assertAlmostEqual(gas_cp(key, 298.15), value, delta=0.1, msg=key)

    def test_enthalpy_and_cp_are_continuous_at_every_boundary(self):
        for key, rows in SHOMATE.items():
            for low, high in zip(rows, rows[1:]):
                boundary = low[1]
                t = boundary / 1000.0

                def h(row):
                    _a, _b, a, b, c, d, e, f, hh = row
                    return a * t + b * t ** 2 / 2 + c * t ** 3 / 3 + d * t ** 4 / 4 - e / t + f

                def cp(row):
                    _a, _b, a, b, c, d, e, _f, _h = row
                    return a + b * t + c * t ** 2 + d * t ** 3 + e / t ** 2

                self.assertAlmostEqual(h(low), h(high), delta=0.02, msg=key)
                self.assertAlmostEqual(cp(low), cp(high), delta=0.25, msg=key)

    def test_graphite_matches_janaf_at_the_reactor_temperature(self):
        # JANAF H - H298 for graphite at 1673 K is about 27.4 kJ/mol.
        self.assertAlmostEqual(graphite_enthalpy(T_K), 27.4, delta=0.3)
        self.assertAlmostEqual(graphite_enthalpy(298.15), 0.0, places=6)


class TheIndependentDutyAgreesWithHysys(unittest.TestCase):
    """Measured agreement that justifies a 1 % band."""

    def test_the_measured_gasification_run(self):
        # Library Carbon's enthalpy data is graphite's; only its Gibbs data is wrong.
        reference = reference_duty(MEASURED)
        self.assertLess(abs(reference / MEASURED_DUTY_KW - 1), 0.001)

    def test_the_accepted_reforming_runs(self):
        golden = json.loads((Path(__file__).parent / 'golden'
                             / 'verify_case_baselines.json').read_text(encoding='utf-8'))
        for label in ('smr-600C', 'smr-710C'):
            case = golden['cases'][label]
            block = check_independent_duty(case['spec'], case['inlet'], case['outlet'],
                                           case['heat_duty_kW'], case['products'])
            self.assertEqual(block['verdict'], 'CONSISTENT', msg=label)
            self.assertLess(abs(block['relative_deviation']), 0.002, msg=label)

    def test_the_band_is_one_percent(self):
        self.assertEqual(DUTY_RELATIVE_TOLERANCE, 0.01)

    def test_the_correct_answer_needs_about_85_mw(self):
        self.assertAlmostEqual(reference_duty(GRAPHITE) / 1000, 85.2, delta=0.5)

    def test_feed_water_at_40_c_and_40_bar_is_liquid(self):
        self.assertEqual(water_phase(313.15, 40.0), 'liquid')
        self.assertEqual(water_phase(793.15, 13.5), 'vapour')

    def test_water_near_saturation_is_not_guessed(self):
        with self.assertRaises(ReferenceUnavailable):
            water_phase(523.0, 40.0)          # saturation is about 523-527 K


class HypotheticalNamesResolveEverywhere(unittest.TestCase):

    def test_the_marker_is_stripped_in_canonical(self):
        self.assertEqual(canonical('Graphite*'), 'carbon')
        self.assertEqual(canonical('Carbon*'), 'carbon')
        self.assertEqual(canonical(' Graphite * '), 'carbon')

    def test_library_names_are_unchanged(self):
        for name, key in (('H2O', 'water'), ('Methane', 'methane'), ('CO', 'carbon monoxide'),
                          ('Hydrogen', 'hydrogen'), ('Toluene', 'toluene')):
            self.assertEqual(canonical(name), key)

    def test_atoms_and_molar_mass_resolve(self):
        self.assertEqual(atoms_of('Graphite*'), {'C': 1})
        self.assertAlmostEqual(molar_mass_of('Graphite*'), 12.011)

    def test_readback_resolution_finds_the_hypothetical(self):
        self.assertEqual(resolve_in_readback('Carbon', ['Graphite*', 'H2O']), 'Graphite*')

    def test_a_full_case_keyed_by_the_hypothetical_name_passes(self):
        outlet = dict(GRAPHITE)
        outlet['Graphite*'] = outlet.pop('Carbon')
        checks = run(outlet, reference_duty(GRAPHITE))
        self.assertEqual(checks['gibbs_equilibrium']['verdict'], 'CONSISTENT')
        self.assertEqual(checks['condensed_phase_location']['verdict'],
                         'CONDENSED_PHASE_ONLY')


class TheBandsAreSplit(unittest.TestCase):

    def test_values(self):
        self.assertEqual(CONDENSED_TOLERANCE_ORDERS, 0.5)
        self.assertEqual(EQUILIBRIUM_TOLERANCE_ORDERS, 1.0)
        self.assertEqual(CONDENSED_IN_GAS_LIMIT, 1e-4)

    def test_gas_dissolved_carbon_was_inside_the_old_band(self):
        """The reason for tightening: 0.99 orders passed a 1.0 band."""
        gas = {k: v for k, v in GAS_CARBON.items() if k != 'Carbon'}
        total = sum(gas.values())
        orders = math.log10((gas['Methane'] / total)
                            / ((gas['Hydrogen'] / total) ** 2 * P_BAR)
                            / check_gibbs_equilibrium(GRAPHITE, T_K, P_BAR)
                            ['expected_kp_bar_inverse'])
        self.assertGreater(orders, CONDENSED_TOLERANCE_ORDERS)
        self.assertLess(orders, EQUILIBRIUM_TOLERANCE_ORDERS)

    def test_the_gate_now_rejects_it_on_its_own(self):
        with self.assertRaises(ResultCheckError) as ctx:
            check_gibbs_equilibrium(GAS_CARBON, T_K, P_BAR, expect_condensed=True)
        self.assertIn('dissolved in the gas', str(ctx.exception))

    def test_the_correct_answer_is_still_well_inside(self):
        block = check_gibbs_equilibrium(GRAPHITE, T_K, P_BAR, expect_condensed=True)
        self.assertEqual(block['verdict'], 'CONSISTENT')
        self.assertLess(abs(block['orders_from_equilibrium']), 0.1)
        self.assertEqual(block['tolerance_orders'], CONDENSED_TOLERANCE_ORDERS)

    def test_reforming_still_uses_the_one_sided_band(self):
        golden = json.loads((Path(__file__).parent / 'golden'
                             / 'verify_case_baselines.json').read_text(encoding='utf-8'))
        case = golden['cases']['smr-710C']
        block = check_gibbs_equilibrium(case['outlet'], 983.15, 13.5)
        self.assertEqual(block['tolerance_orders'], EQUILIBRIUM_TOLERANCE_ORDERS)


class ThePhaseLocationIsChecked(unittest.TestCase):

    def test_carbon_in_the_gas_is_rejected(self):
        with self.assertRaises(ResultCheckError) as ctx:
            check_condensed_phase_location(products(GAS_CARBON, carbon_in_gas=True))
        self.assertIn('treating it as a fluid', str(ctx.exception))

    def test_carbon_in_the_solid_product_passes(self):
        block = check_condensed_phase_location(products(GRAPHITE, carbon_in_gas=False))
        self.assertEqual(block['verdict'], 'CONDENSED_PHASE_ONLY')
        self.assertEqual(block['share_in_gas']['carbon'], 0.0)

    def test_a_trace_below_the_limit_passes(self):
        split = products(GRAPHITE, carbon_in_gas=False)
        trace = 0.5 * CONDENSED_IN_GAS_LIMIT * GRAPHITE['Carbon']
        vapour = split['VAPOUR']
        flow = vapour['molar_flow_kmol_h']
        flows = {n: y * flow for n, y in vapour['mole_fractions'].items()}
        flows['Carbon'] = trace
        vapour['molar_flow_kmol_h'] = sum(flows.values())
        vapour['mole_fractions'] = {n: v / vapour['molar_flow_kmol_h']
                                    for n, v in flows.items()}
        self.assertEqual(check_condensed_phase_location(split)['verdict'],
                         'CONDENSED_PHASE_ONLY')

    def test_missing_products_are_refused_not_assumed(self):
        with self.assertRaises(ResultCheckError):
            check_condensed_phase_location(None)
        with self.assertRaises(ResultCheckError):
            run(GRAPHITE, reference_duty(GRAPHITE), with_products=False)


class TheWholeGateOnSolidCarbonCases(unittest.TestCase):

    def test_the_correct_answer_passes_every_check(self):
        checks = run(GRAPHITE, reference_duty(GRAPHITE))
        self.assertEqual(checks['condensed_phase_location']['verdict'],
                         'CONDENSED_PHASE_ONLY')
        self.assertEqual(checks['gibbs_equilibrium']['verdict'], 'CONSISTENT')
        self.assertEqual(checks['independent_duty']['verdict'], 'CONSISTENT')

    def test_gas_dissolved_carbon_is_caught_by_the_phase_check_first(self):
        with self.assertRaises(ResultCheckError) as ctx:
            run(GAS_CARBON, reference_duty(GAS_CARBON), carbon_in_gas=True)
        self.assertIn('left in the gas product', str(ctx.exception))

    def test_the_measured_run_is_still_rejected_for_its_carbon_reference(self):
        # Its carbon did leave as a solid, so the phase check passes and the existing
        # diagnosis is the one reported.
        with self.assertRaises(ResultCheckError) as ctx:
            run(MEASURED, MEASURED_DUTY_KW)
        self.assertIn('wrong carbon reference state', str(ctx.exception))

    def test_a_wrong_enthalpy_is_caught_even_with_the_right_composition(self):
        with self.assertRaises(ResultCheckError) as ctx:
            run(GRAPHITE, reference_duty(GRAPHITE) * 1.05)
        self.assertIn('enthalpy data', str(ctx.exception))

    def test_a_small_duty_difference_passes(self):
        checks = run(GRAPHITE, reference_duty(GRAPHITE) * 1.005)
        self.assertEqual(checks['independent_duty']['verdict'], 'CONSISTENT')

    def test_an_uncovered_species_makes_the_duty_unavailable_not_failed(self):
        spec = gasification_native_spec()
        inlet = dict(FEED, nitrogen=10.0)
        block = check_independent_duty(spec, inlet, GRAPHITE, 85000.0,
                                       products(GRAPHITE, False))
        self.assertEqual(block['verdict'], 'UNAVAILABLE')


class SpecsWithoutASolidAreUnchanged(unittest.TestCase):
    """Golden output of the PRE-CHANGE code for the three accepted remote runs."""

    def test_identical_block(self):
        golden = json.loads((Path(__file__).parent / 'golden'
                             / 'verify_case_baselines.json').read_text(encoding='utf-8'))
        for label, case in golden['cases'].items():
            actual = verify_case(case['spec'], case['inlet'], case['outlet'],
                                 case['reactor_kind'], case['heat_duty_kW'],
                                 case['thermal_mode'], case['molar_mass'],
                                 products=case['products'])
            self.assertEqual(json.loads(json.dumps(actual)), case['expected'], msg=label)
            self.assertNotIn('condensed_phase_location', actual, msg=label)
            self.assertNotIn('independent_duty', actual, msg=label)


if __name__ == '__main__':
    unittest.main()
