"""Offline contract tests. Not proof of remote COM unit support.

The unit lists asserted here are not guesses: they come from a probe that ran on the
workstation (``scripts/probe_native_flow_units.py``, evidence in
``probe-runs/native-flow-units-*``) and tried 11 properties x 23 units x 2 streams.
That probe is the reason ``MolarFlow`` + ``Nm3/h`` is rejected offline rather than by
a failing COM call after a remote run.
"""
import unittest
from types import SimpleNamespace
from .core import SpecError
from .examples import gasification_native_spec, gasification_spec
from .native_flow import (
    HYSYS_MOLAR_UNITS,
    check_native_unit,
    composition_weights,
    delegates_total,
    is_native,
    is_normal_volume,
    set_delegated_total,
    set_native_total,
    set_normal_volume_total,
    stated_molar_volume,
)
from .precheck import validate_spec


class Variable:
    """A stand-in for a HYSYS variable.

    It echoes what it was given, because that is what HYSYS does and because the
    setter checks the readback: a fake that returned a fixed number would fail the
    setter for the wrong reason. `echo=False` seeds the readback so a bad readback can
    be tested deliberately.
    """

    def __init__(self, values=None, reject=False, echo=True):
        self.values = dict(values or {})
        self.calls = []
        self.reject = reject
        self.echo = echo

    def SetValue(self, value, unit):
        self.calls.append((value, unit))
        if self.reject:
            raise RuntimeError('unit not supported')
        if self.echo:
            self.values[unit] = value

    def GetValue(self, unit):
        if unit not in self.values:
            raise RuntimeError('no value for %s' % unit)
        return self.values[unit]


def make_stream(molar=None, molar_echo=True, mass=50000.0, reject=False):
    return SimpleNamespace(
        MolarFlow=Variable(molar, reject=reject, echo=molar_echo),
        MassFlow=Variable({'kg/h': mass}))


def hysys_feed(unit='kgmole/h'):
    """A feed that hands its total to HYSYS verbatim, with a unit HYSYS accepts."""
    spec = gasification_native_spec()
    feed = spec['feeds'][0]
    feed.update(flow_input='hysys', flow_property='MolarFlow', total_flow=3569.2027,
                total_flow_unit=unit)
    return feed


class NativeFlowTests(unittest.TestCase):
    def setUp(self):
        self.spec = gasification_native_spec()
        self.feed = self.spec['feeds'][0]
        self.stream = make_stream()

    # ------------------------------------------------------- the delegated mode
    def test_the_example_spec_uses_the_stated_basis(self):
        """The examiner stated 0 C, so the spec converts on that basis."""
        self.assertTrue(is_normal_volume(self.feed))
        self.assertFalse(is_native(self.feed))
        self.assertTrue(delegates_total(self.feed))
        self.assertEqual(self.feed['standard_temperature_C'], 0.0)
        self.assertEqual(self.feed['standard_pressure_kPa'], 101.325)

    def test_the_stated_basis_gives_the_right_molar_volume(self):
        molar_volume, conditions = stated_molar_volume(self.feed)
        self.assertAlmostEqual(molar_volume, 22.41397, places=4)
        self.assertIn('0 C', conditions)

    def test_the_conversion_is_80000_over_22_414(self):
        result = set_normal_volume_total(self.stream, self.feed)
        self.assertAlmostEqual(result['molar_flow_kmol_h'], 3569.2027, places=3)
        self.assertAlmostEqual(result['molar_flow_kmol_h'], 80000.0 / 22.41397,
                               places=3)
        self.assertEqual(result['unit'], 'kgmole/h')
        self.assertIn('conversion', result)

    def test_the_conversion_is_arithmetic_on_the_spec_not_on_hysys(self):
        """HYSYS's own basis is 15 C; using it would be a silent 5.2% error."""
        fifteen, _ = stated_molar_volume(dict(self.feed, standard_temperature_C=15.0))
        zero, _ = stated_molar_volume(self.feed)
        self.assertGreater(fifteen / zero, 1.05)

    def test_it_sets_a_molar_unit_never_a_volume_unit(self):
        # Computed from the stated basis rather than hardcoded, so a change to the
        # basis shows up as a change in the expected call. Compared with a tolerance
        # because the total now comes from summing the component flows, which differs
        # from a single division in the last bit.
        expected = 80000.0 / stated_molar_volume(self.feed)[0]
        set_normal_volume_total(self.stream, self.feed)
        self.assertEqual(len(self.stream.MolarFlow.calls), 1)
        value, unit = self.stream.MolarFlow.calls[0]
        self.assertEqual(unit, 'kgmole/h')
        self.assertAlmostEqual(value, expected, places=9)
        self.assertNotIn('Nm3/h', [u for _v, u in self.stream.MolarFlow.calls])

    def test_precheck_converts_it_offline(self):
        report = validate_spec(self.spec)
        self.assertTrue(report['ok'], report)
        conversion = report['readback']['normal_volume_conversion']
        self.assertAlmostEqual(conversion['molar_volume_m3_per_kmol'], 22.41397,
                               places=4)
        self.assertAlmostEqual(conversion['molar_flow_kmol_h'], 3569.2027, places=3)
        self.assertNotIn('feed_molar_flows_kmol_h', report['readback'])
        self.assertAlmostEqual(
            sum(report['readback']['feed_mole_fractions'].values()), 1)

    def test_a_temperature_override_moves_the_conversion(self):
        warm = dict(self.feed, standard_temperature_C=15.0)
        self.assertGreater(stated_molar_volume(warm)[0],
                           stated_molar_volume(self.feed)[0])

    def test_a_bad_standard_pressure_is_rejected(self):
        for bad in (0.0, -1.0):
            with self.subTest(bad=bad):
                with self.assertRaises(SpecError):
                    stated_molar_volume(dict(self.feed, standard_pressure_kPa=bad))


class OneConversionNotTwo(unittest.TestCase):
    """The bug that failed a run *after* HYSYS had already solved.

    Adding the normal-volume mode meant updating every place that decided for itself
    whether a feed's total was delegated. Three existed; one was missed, and `main.py`
    sent the spec to `feed_molar_flows` for its post-solve conservation check, which
    rejected Nm3/h. The simulation had already produced results by then.

    The fix is structural rather than another call site: `core.feed_molar_flows` itself
    now understands the mode, so no caller can get it wrong. These tests pin the
    invariant that made that possible - the two paths must agree exactly.
    """

    def test_feed_molar_flows_handles_the_mode(self):
        from .core import feed_molar_flows
        flows = feed_molar_flows(gasification_native_spec()['feeds'][0])
        self.assertAlmostEqual(sum(flows.values()), 3569.2027, places=3)

    def test_it_matches_what_hysys_was_given(self):
        """Measured remotely: HYSYS reported Carbon 2533.800240632860 kmol/h."""
        from .core import feed_molar_flows
        flows = feed_molar_flows(gasification_native_spec()['feeds'][0])
        self.assertAlmostEqual(flows['Carbon'], 2533.800240632860, places=6)
        self.assertAlmostEqual(flows['Water'], 1035.402431870547, places=6)

    def test_the_two_paths_agree(self):
        """core's conversion and native_flow's conversion are the same number."""
        from .core import feed_molar_flows
        feed = gasification_native_spec()['feeds'][0]
        delegated = set_normal_volume_total(make_stream(), feed)['molar_flow_kmol_h']
        self.assertAlmostEqual(sum(feed_molar_flows(feed).values()), delegated,
                               places=9)

    def test_a_mass_fraction_basis_with_a_molar_total_works(self):
        """Composition basis and flow basis are independent, and both are exercised
        here: mass fractions carried by a normal-volume total."""
        from .core import feed_molar_flows
        flows = feed_molar_flows(gasification_native_spec()['feeds'][0])
        self.assertAlmostEqual(flows['Carbon'] / sum(flows.values()),
                               0.70991, places=5)

    def test_the_molar_volume_comes_from_one_place(self):
        from .core import normal_molar_volume
        here = normal_molar_volume(0.0, 101.325)[0]
        there = stated_molar_volume(gasification_native_spec()['feeds'][0])[0]
        self.assertEqual(here, there)

    def test_an_overridden_basis_flows_through_both_paths(self):
        from .core import feed_molar_flows
        feed = dict(gasification_native_spec()['feeds'][0],
                    standard_temperature_C=15.0)
        self.assertLess(sum(feed_molar_flows(feed).values()), 3569.2027)

    def test_the_composition_reference_does_not_carry_the_mode(self):
        """`composition_weights` builds a reference feed for the composition only.

        It must clear `flow_input`, or `feed_molar_flows` sees a normal-volume feed
        carrying a molar unit and refuses it. That is exactly what broke when the mode
        handling moved into core.
        """
        weights = composition_weights(gasification_native_spec()['feeds'][0])
        self.assertGreater(weights['Carbon'], weights['Water'])


class TheUnitIsCheckedOnTheExecutionPath(unittest.TestCase):
    """A silent unit hole that the pre-check alone did not close.

    The setter and the pre-check each divided `total_flow` by the molar volume
    locally, so neither validated the unit: `flow_input='normal_volume'` with `kmol/h`,
    `kg/h`, `t/h`, `Nm3/d` or a nonsense string was accepted and silently divided by
    22.4. `80000 kmol/h` would have become 3569 kmol/h.

    Both now call `core.feed_molar_flows`, so the check runs where the value is used
    and not only where it is reviewed. These tests exercise the setter, not just the
    pre-check, because a rule that only guards the review step protects nothing.
    """

    BAD_UNITS = ('kmol/h', 'kg/h', 't/h', 'Nm3/d', 'Sm3/h', 'm3/h', 'nonsense', '')

    def _feed(self, unit):
        feed = gasification_native_spec()['feeds'][0]
        feed['total_flow_unit'] = unit
        return feed

    def test_the_setter_rejects_every_wrong_unit(self):
        for unit in self.BAD_UNITS:
            with self.subTest(unit=unit), self.assertRaises(SpecError):
                set_normal_volume_total(make_stream(), self._feed(unit))

    def test_the_precheck_rejects_every_wrong_unit(self):
        for unit in self.BAD_UNITS:
            with self.subTest(unit=unit):
                spec = gasification_native_spec()
                spec['feeds'][0]['total_flow_unit'] = unit
                self.assertFalse(validate_spec(spec)['ok'], unit)

    def test_core_rejects_them_too(self):
        from .core import feed_molar_flows
        for unit in self.BAD_UNITS:
            with self.subTest(unit=unit), self.assertRaises(SpecError):
                feed_molar_flows(self._feed(unit))

    def test_the_right_unit_still_works(self):
        for unit in ('Nm3/h', 'nm3/h', 'Nm3/hr'):
            with self.subTest(unit=unit):
                from .core import feed_molar_flows
                flows = feed_molar_flows(self._feed(unit))
                self.assertAlmostEqual(sum(flows.values()), 3569.2027, places=3)

    def test_a_negative_total_is_rejected_where_it_is_used(self):
        """The normal-volume branch returns early, so it must run the same validity
        check the rest of the function ends with."""
        from .core import feed_molar_flows
        feed = self._feed('Nm3/h')
        feed['total_flow'] = -80000
        with self.assertRaises(SpecError):
            feed_molar_flows(feed)

    def test_a_zero_total_is_rejected(self):
        from .core import feed_molar_flows
        feed = self._feed('Nm3/h')
        feed['total_flow'] = 0
        with self.assertRaises(SpecError):
            feed_molar_flows(feed)


class NativeUnitAcceptance(unittest.TestCase):
    """The rule that would have caught the failure before it cost a remote run."""

    def test_molar_flow_rejects_a_volume_unit(self):
        """Measured: MolarFlow accepts gmole/h, kgmole/h, lbmole/h and nothing else,
        on a gas stream and on the coal slurry alike."""
        feed = hysys_feed('Nm3/h')
        with self.assertRaisesRegex(SpecError, 'normal_volume'):
            check_native_unit(feed)

    def test_every_measured_molar_unit_is_accepted(self):
        for unit in sorted(HYSYS_MOLAR_UNITS):
            with self.subTest(unit=unit):
                check_native_unit(hysys_feed(unit))

    def test_precheck_rejects_it_offline(self):
        report = validate_spec(gasification_native_spec())
        self.assertTrue(report['ok'])
        bad = gasification_native_spec()
        bad['feeds'][0].update(flow_input='hysys', flow_property='MolarFlow',
                               total_flow_unit='Nm3/h')
        report = validate_spec(bad)
        self.assertFalse(report['ok'])
        self.assertIn('normal_volume', ' '.join(report['errors']))

    def test_mass_flow_rejects_a_volume_unit_too(self):
        feed = hysys_feed()
        feed.update(flow_property='MassFlow', total_flow_unit='Nm3/h')
        with self.assertRaises(SpecError):
            check_native_unit(feed)


class NativeSetterContract(unittest.TestCase):
    def setUp(self):
        self.feed = hysys_feed()
        self.stream = make_stream()

    def test_the_exact_value_unit_and_readback_are_preserved(self):
        result = set_native_total(self.stream, self.feed)
        self.assertEqual(self.stream.MolarFlow.calls,
                         [(self.feed['total_flow'], 'kgmole/h')])
        self.assertEqual(self.stream.MassFlow.calls, [])
        self.assertAlmostEqual(result['molar_flow_kmol_h'],
                               self.feed['total_flow'], places=6)

    def test_a_rejected_unit_never_falls_back(self):
        stream = make_stream(reject=True)
        with self.assertRaisesRegex(SpecError, 'unit not supported'):
            set_native_total(stream, self.feed)
        self.assertEqual(stream.MassFlow.calls, [])

    def test_a_mismatch_is_rejected(self):
        stream = make_stream(molar={'kgmole/h': 8}, molar_echo=False)
        with self.assertRaisesRegex(SpecError, 'does not match'):
            set_native_total(stream, self.feed)

    def test_unknown_and_nonfinite_readbacks_are_rejected(self):
        for value in (-32767, 0, float('nan'), float('inf')):
            stream = make_stream(molar={'kgmole/h': value}, molar_echo=False)
            with self.subTest(value=value), self.assertRaises(SpecError):
                set_native_total(stream, self.feed)

    def test_the_property_must_be_explicit(self):
        feed = hysys_feed()
        feed.pop('flow_property')
        with self.assertRaises(SpecError):
            set_native_total(self.stream, feed)

    def test_the_dispatch_picks_the_declared_mode(self):
        self.assertEqual(set_delegated_total(self.stream, self.feed)['property'],
                         'MolarFlow')


class SpecContract(unittest.TestCase):
    def test_mass_composition_is_not_used_as_mole_composition(self):
        weights = composition_weights(gasification_native_spec()['feeds'][0])
        self.assertGreater(weights['Carbon'] / sum(weights.values()), .62)

    def test_a_typo_mode_is_rejected(self):
        spec = gasification_native_spec()
        spec['feeds'][0]['flow_input'] = 'hysis'
        self.assertFalse(validate_spec(spec)['ok'])

    def test_negative_composition_is_rejected_before_execution(self):
        spec = gasification_native_spec()
        spec['feeds'][0]['fractions'] = {'Carbon': 1.1, 'Water': -.1}
        self.assertFalse(validate_spec(spec)['ok'])

    def test_the_historical_unclear_spec_remains_blocked(self):
        self.assertFalse(validate_spec(gasification_spec())['ok'])


if __name__ == '__main__':
    unittest.main()
