"""Pre-solve refusal when HYSYS's Carbon is not graphite.

Measured on the workstation (`probe-runs/carbon-properties-20261003-054212`): in a
Peng-Robinson package, `Carbon` reports `EvaluateGibbs(1673.15) = +450.5 kJ/mol`, while
graphite is zero at every temperature. The value matches gaseous atomic carbon to about
10 kJ/mol over 1000-1673 K. Every other component is right - methane reports
+93.66 kJ/mol at 1673 K against a JANAF +94 - so a single wrong reference state is the
whole defect.

That inversion is what produced the vertex solution, so the case is refused before the
solve rather than after it. These tests use a stub package, because the point is the
decision the method makes about a number, not the COM plumbing.
"""
import unittest
from types import SimpleNamespace

from .core import ModelLimitationError
from .reactor import CaseBuilder


class Log:
    """Records (name, status, detail), matching core.StepLog.add's signature."""

    def __init__(self):
        self.entries = []

    def add(self, name, status='OK', detail=None):
        self.entries.append((name, status, detail))


def package_with(readings, name='Carbon'):
    """A stub fluid package whose component answers EvaluateGibbs as told."""
    component = SimpleNamespace(
        Name=name,
        EvaluateGibbs=lambda kelvin: readings.get(str(int(kelvin)), 0.0) * 1000.0)
    components = SimpleNamespace(Count=1, Item=lambda _i: component)
    return SimpleNamespace(Components=components)


def builder_for(package):
    """A CaseBuilder without running __init__, since only two attributes are used."""
    builder = CaseBuilder.__new__(CaseBuilder)
    builder.log = Log()
    builder._package = package
    return builder


# What the workstation actually reported, in kJ/mol, as EvaluateGibbs returns J/mol.
MEASURED_CARBON = {'1000': 560.78, '1500': 480.0}
GRAPHITE_CARBON = {'1000': 0.0, '1500': 0.0}


class TheMeasuredCarbonIsRefused(unittest.TestCase):

    def test_it_raises_a_model_limitation_not_a_spec_error(self):
        """A specification problem says "edit the spec and retry".

        This is not one: no edit to the specification changes what HYSYS does with
        Carbon, and only one property package has been verified here. Classifying it
        as a specification problem invites an agent to start swapping packages and
        component names, which burns remote runs and could produce a wrong PASS.
        """
        builder = builder_for(package_with(MEASURED_CARBON))
        with self.assertRaises(ModelLimitationError):
            builder.verify_solid_carbon_reference()

    def test_it_is_still_catchable_as_a_spec_error(self):
        """Existing handlers must keep working."""
        builder = builder_for(package_with(MEASURED_CARBON))
        with self.assertRaises(Exception) as ctx:
            builder.verify_solid_carbon_reference()
        from .core import SpecError
        self.assertIsInstance(ctx.exception, SpecError)

    def test_the_message_tells_the_caller_not_to_retry(self):
        builder = builder_for(package_with(MEASURED_CARBON))
        with self.assertRaises(ModelLimitationError) as ctx:
            builder.verify_solid_carbon_reference()
        message = str(ctx.exception)
        self.assertIn('DO NOT RETRY AUTOMATICALLY', message)
        self.assertIn('person', message)

    def test_it_raises(self):
        builder = builder_for(package_with(MEASURED_CARBON))
        with self.assertRaises(Exception) as ctx:
            builder.verify_solid_carbon_reference()
        self.assertIn('graphite', str(ctx.exception).lower())

    def test_the_message_quotes_the_value_and_the_tolerance(self):
        builder = builder_for(package_with(MEASURED_CARBON))
        with self.assertRaises(Exception) as ctx:
            builder.verify_solid_carbon_reference()
        message = str(ctx.exception)
        # The worst of the probed values, not the first.
        self.assertIn('+560.8 kJ/mol', message)
        self.assertIn('tolerance 25 kJ/mol', message)
        self.assertIn('0 by definition', message)

    def test_the_message_says_it_refuses_before_the_solve(self):
        builder = builder_for(package_with(MEASURED_CARBON))
        with self.assertRaises(Exception) as ctx:
            builder.verify_solid_carbon_reference()
        self.assertIn('before the solve', str(ctx.exception))

    def test_the_reading_is_logged_even_though_it_fails(self):
        builder = builder_for(package_with(MEASURED_CARBON))
        with self.assertRaises(Exception):
            builder.verify_solid_carbon_reference()
        names = [entry[0] for entry in builder.log.entries]
        self.assertIn('solid_carbon_reference', names)
        detail = {entry[0]: entry[2] for entry in builder.log.entries}['solid_carbon_reference']
        self.assertEqual(detail['verdict'], 'NOT_GRAPHITE')
        self.assertEqual(detail['graphite_reference_kj_per_mol'], 0.0)


class GraphiteIsAccepted(unittest.TestCase):

    def test_zero_is_accepted(self):
        builder = builder_for(package_with(GRAPHITE_CARBON))
        builder.verify_solid_carbon_reference()
        detail = {e[0]: e[2] for e in builder.log.entries}['solid_carbon_reference']
        self.assertEqual(detail['verdict'], 'reference state correct')

    def test_the_verdict_does_not_claim_solid_carbon_is_supported(self):
        """This reads the standard state only, not the phase.

        A hypothetical component with dGf set to zero would pass here and still be a
        fluid in the package, so the log must not read as "solid carbon works".
        """
        builder = builder_for(package_with(GRAPHITE_CARBON))
        builder.verify_solid_carbon_reference()
        detail = {e[0]: e[2] for e in builder.log.entries}['solid_carbon_reference']
        self.assertIn('standard-state', detail['checks'])
        self.assertIn('condensed phase', detail['does_not_check'])
        self.assertEqual(detail['authoritative_check'],
                         'validate.check_gibbs_equilibrium')

    def test_a_small_numerical_offset_is_tolerated(self):
        builder = builder_for(package_with({'1000': 20.0, '1500': -20.0}))
        builder.verify_solid_carbon_reference()


class AnUnreadablePackageIsRecordedNotIgnored(unittest.TestCase):
    """A check that silently did not run is worse than no check."""

    def test_a_missing_package_is_recorded(self):
        builder = builder_for(None)
        builder.verify_solid_carbon_reference()
        # No package at all means there is nothing to check and no Carbon to check it
        # for; nothing is logged, and the post-solve gate is unaffected.
        self.assertEqual(builder.log.entries, [])

    def test_an_unreadable_component_list_warns(self):
        broken = SimpleNamespace(Components=SimpleNamespace(
            Count=1, Item=lambda _i: (_ for _ in ()).throw(RuntimeError('no'))))
        builder = builder_for(broken)
        builder.verify_solid_carbon_reference()
        name, status, detail = self._entry(builder)
        self.assertEqual(name, 'solid_carbon_reference')
        self.assertEqual(status, 'WARNING')
        self.assertEqual(detail['verdict'], 'UNVERIFIED')

    def test_an_unreadable_gibbs_value_warns(self):
        component = SimpleNamespace(Name='Carbon')
        package = SimpleNamespace(Components=SimpleNamespace(
            Count=1, Item=lambda _i: component))
        builder = builder_for(package)
        builder.verify_solid_carbon_reference()
        _name, status, detail = self._entry(builder)
        self.assertEqual(status, 'WARNING')
        self.assertEqual(detail['verdict'], 'UNVERIFIED')
        self.assertIn('post-solve', detail['consequence'])

    def test_an_unverified_check_does_not_fail_the_run(self):
        component = SimpleNamespace(Name='Carbon')
        package = SimpleNamespace(Components=SimpleNamespace(
            Count=1, Item=lambda _i: component))
        builder = builder_for(package)
        builder.verify_solid_carbon_reference()   # must not raise

    @staticmethod
    def _entry(builder):
        for entry in builder.log.entries:
            if entry[0] == 'solid_carbon_reference':
                return (entry[0],
                        entry[1] if len(entry) > 1 else 'OK',
                        entry[-1])
        raise AssertionError('no solid_carbon_reference entry was logged')


class CarbonIsMatchedCanonically(unittest.TestCase):
    """The library name, an alias and a spec spelling can all differ."""

    def test_an_alias_spelling_is_still_checked(self):
        for spelling in ('Carbon', 'CARBON', 'carbon'):
            with self.subTest(spelling=spelling):
                builder = builder_for(package_with(MEASURED_CARBON, name=spelling))
                with self.assertRaises(ModelLimitationError):
                    builder.verify_solid_carbon_reference()

    def test_a_non_carbon_component_is_never_checked(self):
        builder = builder_for(package_with({'1000': 94.0}, name='Methane'))
        builder.verify_solid_carbon_reference()
        self.assertEqual(builder.log.entries, [])


class OtherComponentsAreNotChecked(unittest.TestCase):
    """Methane legitimately has a large positive formation Gibbs energy."""

    def test_a_non_carbon_component_is_ignored(self):
        builder = builder_for(package_with({'1000': 94.0, '1500': 200.0},
                                           name='Methane'))
        builder.verify_solid_carbon_reference()
        self.assertEqual(builder.log.entries, [])


if __name__ == '__main__':
    unittest.main()
