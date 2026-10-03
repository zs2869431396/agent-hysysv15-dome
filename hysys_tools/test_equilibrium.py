"""The thermodynamic gate on a Gibbs outlet.

This exists because the first real gasification run produced an outlet that balanced
its atoms perfectly and was still impossible: at 1400 C and 40 bar, HYSYS returned
every oxygen atom as CO, every hydrogen atom as CH4, hydrogen at 3.8e-4 and water and
CO2 at zero. Atom and mass conservation were satisfied - they cannot see this class of
error - so the result would have been reported as a PASS.

The numbers below are that run's actual output, taken from
`tool-layer-runs/native-flow-20261003-044213-6ddd6d33/gasification/result.json`.

Two defects in the first version of this gate are pinned by tests here, because both
were found only by review and both mattered:

  * mole fractions were normalised over every species including solid carbon, which
    multiplied K_p by 1.73 and made the check disagree with the JANAF reference;
  * the two-sided equality was applied even when no condensed phase was present, where
    only an upper bound holds - which rejected correct carbon-free reformer results.
"""
import math
import unittest

from .examples import reforming_spec
from .validate import (
    ABSENT_MOLE_FRACTION,
    EQUILIBRIUM_TOLERANCE_ORDERS,
    ResultCheckError,
    check_gibbs_equilibrium,
    gas_phase_fractions,
    is_condensed_species,
    methanation_kp,
)

# What HYSYS actually produced at 1400 C / 40 bar.
MEASURED_OUTLET = {
    'Carbon': 980.6967864262893,
    'H2O': 5.0410966717477984e-20,
    'CO': 1035.4024305629503,
    'Hydrogen': 3.8327569e-04,
    'CO2': 4.0690299e-14,
    'Methane': 5.177010e+02,
}

# An independently computed equilibrium at the same conditions: carbon left over,
# H2 and CO dominant, methane almost absent.
EQUILIBRIUM_1400C = {
    'Carbon': 1491.5, 'CO': 1017.0, 'Hydrogen': 981.0, 'Methane': 22.0,
    'H2O': 11.0, 'CO2': 3.5,
}

# A carbon-free reformer outlet, which no condensed-phase equality applies to.
SMR_OUTLET = {'Methane': 40.0, 'H2O': 300.0, 'CO': 320.0, 'CO2': 180.0,
              'Hydrogen': 1300.0}

TEMPERATURE_K = 1673.15
PRESSURE_BAR = 40.0


class TheEquilibriumConstant(unittest.TestCase):

    def test_it_reproduces_the_298_k_textbook_anchor(self):
        """dGf(CH4) = -50.5 kJ/mol gives K = 7.03e8 bar^-1."""
        self.assertAlmostEqual(methanation_kp(298.15), 7.03e8, delta=1e7)

    def test_it_reproduces_the_janaf_anchor(self):
        """dGf(CH4, 1673 K) = +94 kJ/mol gives K = 1.1626e-3 bar^-1.

        The previous single-anchor Van't Hoff fit gave 1.28e-2 here, eleven times too
        high, because dHf(CH4) moves from -74.6 to about -92 kJ/mol over the range.
        """
        self.assertAlmostEqual(methanation_kp(1673.15), 1.1626e-3, delta=1e-5)

    def test_the_effective_enthalpy_is_physically_reasonable(self):
        """It must sit between the 298 K value and the high-temperature value."""
        from .validate import EFFECTIVE_REACTION_ENTHALPY_J_PER_MOL
        self.assertLess(EFFECTIVE_REACTION_ENTHALPY_J_PER_MOL, -74600.0)
        self.assertGreater(EFFECTIVE_REACTION_ENTHALPY_J_PER_MOL, -92000.0)

    def test_it_falls_with_temperature(self):
        values = [methanation_kp(t) for t in (298.15, 500, 700, 900, 1200, 1673.15)]
        self.assertEqual(values, sorted(values, reverse=True))

    def test_it_crosses_one_where_methanation_stops_being_favoured(self):
        self.assertGreater(methanation_kp(700.0), 1.0)
        self.assertLess(methanation_kp(900.0), 1.0)

    def test_a_bad_temperature_is_rejected(self):
        for bad in (0.0, -1.0, float('nan'), float('inf')):
            with self.subTest(bad=bad), self.assertRaises(ResultCheckError):
                methanation_kp(bad)


class MoleFractionsAreGasPhaseOnly(unittest.TestCase):
    """Solid carbon is not in the gas, so it must not dilute the mole fractions."""

    def test_the_condensed_phase_is_excluded_from_the_total(self):
        y, condensed = gas_phase_fractions(EQUILIBRIUM_1400C)
        self.assertTrue(condensed)
        self.assertNotIn('carbon', y)
        self.assertAlmostEqual(sum(y.values()), 1.0, places=12)

    def test_the_ratio_matches_the_janaf_reference(self):
        """Including carbon multiplied this by 1.73 and broke the comparison."""
        y, _ = gas_phase_fractions(EQUILIBRIUM_1400C)
        kp = y['methane'] / (y['hydrogen'] ** 2 * PRESSURE_BAR)
        self.assertAlmostEqual(kp / 1.14e-3, 1.0, delta=0.05)

    def test_including_carbon_would_have_inflated_it(self):
        total_all = sum(EQUILIBRIUM_1400C.values())
        gas = {k: v for k, v in EQUILIBRIUM_1400C.items() if k != 'Carbon'}
        factor = total_all / sum(gas.values())
        self.assertAlmostEqual(factor, 1.73, delta=0.02)

    def test_a_gas_without_carbon_reports_no_condensed_phase(self):
        _, condensed = gas_phase_fractions(SMR_OUTLET)
        self.assertFalse(condensed)

    def test_an_outlet_with_no_gas_at_all_is_rejected(self):
        with self.assertRaises(ResultCheckError):
            gas_phase_fractions({'Carbon': 100.0})
        with self.assertRaises(ResultCheckError):
            gas_phase_fractions({})


class TheCondensedPhaseMustBeRecognised(unittest.TestCase):
    """A name-match miss silently disables the strict half of this check.

    HYSYS appends an asterisk to hypothetical component names. Matching the raw string
    missed `Carbon*`, so the outlet looked carbon-free, the two-sided test was skipped
    for the one-sided one, and the impossible gasification outlet was ACCEPTED as
    `NO_HYDROGEN`. A check that fails open is worse than no check, because the run
    looks guarded.
    """

    def test_the_hypothetical_marker_is_stripped(self):
        for name in ('Carbon', 'Carbon*', '*Carbon', 'Graphite', 'graphite*', 'C'):
            with self.subTest(name=name):
                self.assertTrue(is_condensed_species(name))

    def test_unrelated_names_are_not_condensed(self):
        for name in ('CO', 'CO2', 'Methane', 'Hydrogen', 'Water', 'AGENT-C'):
            with self.subTest(name=name):
                self.assertFalse(is_condensed_species(name))

    def test_a_marked_carbon_is_still_excluded_from_the_gas(self):
        marked = {'Carbon*': 980.6967864262893, 'CO': 1035.4024305629503,
                  'Methane': 517.701, 'Hydrogen': 3.8327569e-04}
        y, condensed = gas_phase_fractions(marked)
        self.assertTrue(condensed)
        self.assertNotIn('carbon', y)
        self.assertAlmostEqual(sum(y.values()), 1.0, places=12)

    def test_the_measured_outlet_is_rejected_even_when_carbon_is_marked(self):
        marked = dict(MEASURED_OUTLET)
        marked['Carbon*'] = marked.pop('Carbon')
        with self.assertRaises(ResultCheckError) as ctx:
            check_gibbs_equilibrium(marked, TEMPERATURE_K, PRESSURE_BAR)
        self.assertIn('wrong carbon reference state', str(ctx.exception).lower())

    def test_an_unrecognised_carbon_name_is_an_error_not_a_downgrade(self):
        """When the spec declares solid carbon, its absence from the outlet cannot be
        read as "thermodynamics say there is none"."""
        unrecognised = {'AGENT-C': 980.7, 'CO': 1035.4, 'Hydrogen': 3.8e-04,
                        'Methane': 517.7}
        with self.assertRaises(ResultCheckError) as ctx:
            check_gibbs_equilibrium(unrecognised, TEMPERATURE_K, PRESSURE_BAR,
                                    expect_condensed=True)
        self.assertIn('no condensed phase', str(ctx.exception).lower())

    def test_the_expectation_is_opt_in(self):
        """A carbon-free reformer must keep working without the flag set."""
        unrecognised = {'AGENT-C': 980.7, 'CO': 1035.4, 'Hydrogen': 3.8e-04,
                        'Methane': 517.7}
        block = check_gibbs_equilibrium(unrecognised, TEMPERATURE_K, PRESSURE_BAR)
        self.assertIn('one-sided', block['comparison'])


class TheMeasuredOutletIsRejected(unittest.TestCase):

    def test_it_raises(self):
        with self.assertRaises(ResultCheckError):
            check_gibbs_equilibrium(MEASURED_OUTLET, TEMPERATURE_K, PRESSURE_BAR)

    def test_the_message_blames_the_data_not_the_solver(self):
        """The measured outlet is an exact equilibrium under HYSYS's carbon value.

        Back-calculating the carbon phase's chemical potential from the outlet gives
        +450.38 / +450.64 / +450.66 kJ/mol from three independent reactions, against
        EvaluateGibbs(Carbon) = +450.53. So the minimiser converged correctly and only
        its input data was wrong. The message must say so - it is both the accurate
        diagnosis and the more useful one for a report.
        """
        with self.assertRaises(ResultCheckError) as ctx:
            check_gibbs_equilibrium(MEASURED_OUTLET, TEMPERATURE_K, PRESSURE_BAR)
        message = str(ctx.exception).lower()
        self.assertIn('wrong carbon reference state', message)
        self.assertIn('solver is not at fault', message)
        self.assertIn('gaseous atomic carbon', message)

    def test_it_still_names_the_cause(self):
        with self.assertRaises(ResultCheckError) as ctx:
            check_gibbs_equilibrium(MEASURED_OUTLET, TEMPERATURE_K, PRESSURE_BAR)
        message = str(ctx.exception).lower()
        self.assertIn('carbon', message)
        self.assertIn('unconverted', message)

    def test_it_is_far_from_equilibrium(self):
        y, _ = gas_phase_fractions(MEASURED_OUTLET)
        orders = math.log10(y['methane'] / (y['hydrogen'] ** 2 * PRESSURE_BAR)
                            / methanation_kp(TEMPERATURE_K))
        self.assertGreater(orders, 10)

    def test_the_atoms_do_balance(self):
        """The point: conservation passes and the answer is still wrong."""
        feed_c, feed_o, feed_h = 2533.800240632860, 1035.402431870547, 2070.804863741094
        outlet_c = MEASURED_OUTLET['Carbon'] + MEASURED_OUTLET['CO'] \
            + MEASURED_OUTLET['CO2'] + MEASURED_OUTLET['Methane']
        outlet_o = MEASURED_OUTLET['CO'] + 2 * MEASURED_OUTLET['CO2'] \
            + MEASURED_OUTLET['H2O']
        outlet_h = 2 * MEASURED_OUTLET['H2O'] + 4 * MEASURED_OUTLET['Methane'] \
            + 2 * MEASURED_OUTLET['Hydrogen']
        self.assertAlmostEqual(outlet_c, feed_c, places=3)
        self.assertAlmostEqual(outlet_o, feed_o, places=3)
        self.assertAlmostEqual(outlet_h, feed_h, places=3)


class ARealEquilibriumPasses(unittest.TestCase):

    def test_it_is_accepted(self):
        block = check_gibbs_equilibrium(EQUILIBRIUM_1400C, TEMPERATURE_K,
                                        PRESSURE_BAR)
        self.assertEqual(block['verdict'], 'CONSISTENT')

    def test_it_sits_on_the_equilibrium(self):
        block = check_gibbs_equilibrium(EQUILIBRIUM_1400C, TEMPERATURE_K,
                                        PRESSURE_BAR)
        self.assertLess(abs(block['orders_from_equilibrium']), 0.1)
        self.assertIn('two-sided', block['comparison'])

    def test_the_tolerance_still_rejects_a_clear_error(self):
        wrong = dict(EQUILIBRIUM_1400C, Hydrogen=1.0, Methane=900.0)
        with self.assertRaises(ResultCheckError) as ctx:
            check_gibbs_equilibrium(wrong, TEMPERATURE_K, PRESSURE_BAR)
        self.assertIn('orders of magnitude off', str(ctx.exception))


class WithoutACondensedPhaseOnlyAnUpperBoundApplies(unittest.TestCase):
    """The equality a_C = 1 requires solid carbon to be there.

    With no carbon, carbon cannot be consumed, so a stable gas only has to avoid being
    supersaturated. The first version compared two-sided regardless, which rejected
    correct carbon-free reformer results - a high-temperature reformer deliberately
    sits far below the methanation equilibrium.
    """

    def test_a_correct_reformer_outlet_is_accepted(self):
        for celsius in (600.0, 710.0, 950.0):
            with self.subTest(celsius=celsius):
                block = check_gibbs_equilibrium(SMR_OUTLET, celsius + 273.15, 13.5)
                self.assertEqual(block['verdict'], 'CONSISTENT')

    def test_it_is_allowed_to_sit_below_the_equilibrium(self):
        block = check_gibbs_equilibrium(SMR_OUTLET, 600.0 + 273.15, 13.5)
        self.assertLess(block['orders_from_equilibrium'], -1.0)
        self.assertIn('one-sided', block['comparison'])

    def test_a_supersaturated_gas_is_rejected(self):
        """Carbon would have to precipitate for this gas to be stable."""
        supersaturated = {'Methane': 700.0, 'Hydrogen': 100.0, 'CO': 50.0,
                          'CO2': 20.0, 'H2O': 30.0}
        with self.assertRaises(ResultCheckError) as ctx:
            check_gibbs_equilibrium(supersaturated, 600.0 + 273.15, 13.5)
        self.assertIn('precipitate', str(ctx.exception).lower())

    def test_the_real_reforming_specs_have_no_carbon(self):
        for celsius in (600.0, 710.0):
            with self.subTest(celsius=celsius):
                spec = reforming_spec(celsius)
                self.assertNotIn('Carbon', spec['fluid_package']['components'])


class EdgeCases(unittest.TestCase):

    def test_no_hydrogen_reports_rather_than_raises(self):
        block = check_gibbs_equilibrium({'CO': 100.0, 'Methane': 1.0},
                                        TEMPERATURE_K, PRESSURE_BAR)
        self.assertEqual(block['verdict'], 'NO_HYDROGEN')

    def test_an_empty_outlet_is_rejected(self):
        with self.assertRaises(ResultCheckError):
            check_gibbs_equilibrium({}, TEMPERATURE_K, PRESSURE_BAR)

    def test_symbols_and_names_both_work(self):
        by_symbol = {'C': 1491.5, 'CO': 1017.0, 'H2': 981.0, 'CH4': 22.0,
                     'H2O': 11.0, 'CO2': 3.5}
        block = check_gibbs_equilibrium(by_symbol, TEMPERATURE_K, PRESSURE_BAR)
        self.assertEqual(block['verdict'], 'CONSISTENT')

    def test_the_absent_threshold_is_small(self):
        self.assertLess(ABSENT_MOLE_FRACTION, 1e-4)

    def test_the_tolerance_is_one_order(self):
        self.assertEqual(EQUILIBRIUM_TOLERANCE_ORDERS, 1.0)


if __name__ == '__main__':
    unittest.main()
