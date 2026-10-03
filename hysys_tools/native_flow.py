r"""Feed total flow: delegate to HYSYS, or convert on the requester's stated basis.

Two ways a spec may give the absolute feed flow, and the difference matters.

``flow_input: 'hysys'``
    Hand the number and the unit straight to a HYSYS variable. Faithful when the unit
    is one HYSYS accepts for that property.

``flow_input: 'normal_volume'``
    The requester stated the flow as a normal (standard) gas volume - ``Nm3/h`` at a
    stated temperature and pressure - and the spec declares those conditions. The
    ideal-gas molar volume follows from them, the molar flow is computed here, and
    HYSYS is given a molar unit it accepts.

Why the second mode exists, in measurements
-------------------------------------------

A probe on this workstation (``scripts/probe_native_flow_units.py``, evidence in
``probe-runs/native-flow-units-*``) tried 11 properties x 23 units x 2 stream types:

* **No property accepts ``Nm3/h``.** ``MolarFlow`` takes ``gmole/h``, ``kgmole/h``,
  ``lbmole/h`` only, on a pure gas stream and on the coal-water stream alike, and the
  COM failure is identical on both - so it is the unit dimension, not the stream
  state. ``StdGasFlow`` exists but rejects every unit with ``E_ACCESSDENIED``: it is a
  calculated property, not a settable one.
* **HYSYS's own standard gas basis is 15 C, not 0 C.** ``StdLiqVolFlow`` read back at
  23.586 m3/kmol, which is 15 C with a real-gas correction, and its ``MMSCFD`` unit
  uses 60 F.

That last point is why this module converts rather than searching harder for a volume
unit. Had one been found, HYSYS would have applied **its** standard conditions while
the exam states 0 C - a 5.2% difference on the feed, invisible in the result, and
carried straight into the carbon balance and the CO yield. Converting against the
stated basis is not a workaround; it is the only way to use the basis that was
actually specified.
"""
import math

from .core import SpecError, feed_molar_flows, normal_molar_volume

# The conditions the requester stated for the normal volume, used when the spec does
# not override them.
DEFAULT_STANDARD_TEMPERATURE_C = 0.0
DEFAULT_STANDARD_PRESSURE_KPA = 101.325

# Units HYSYS accepts for the properties this module sets. Anything else is rejected
# offline rather than sent to a COM call that cannot succeed.
HYSYS_MOLAR_UNITS = frozenset({'gmole/h', 'kgmole/h', 'lbmole/h'})
HYSYS_MASS_UNITS = frozenset({'kg/h', 'lb/h'})

# The molar unit used to hand a computed flow to HYSYS. Measured working.
MOLAR_UNIT_FOR_HYSYS = 'kgmole/h'


def mode_of(feed: dict) -> str:
    """The declared flow mode, normalised the same way `core` normalises it.

    The comparison is stripped because `core.feed_molar_flows` strips: without that,
    `'normal_volume '` with a trailing space would be a valid mode in one module and
    an error in the other, and the two would disagree about the same spec.
    """
    mode = str(feed.get('flow_input', 'local') or 'local').strip()
    if mode not in ('local', 'hysys', 'normal_volume'):
        raise SpecError(
            "flow_input must be 'local', 'hysys' or 'normal_volume', not %r"
            % (feed.get('flow_input'),))
    return mode


def is_native(feed: dict) -> bool:
    """True when the spec hands the total to HYSYS verbatim."""
    return mode_of(feed) == 'hysys'


def is_normal_volume(feed: dict) -> bool:
    """True when the total is a stated standard gas volume, converted here."""
    return mode_of(feed) == 'normal_volume'


def delegates_total(feed: dict) -> bool:
    """True when the total flow is delegated rather than converted locally."""
    return mode_of(feed) != 'local'


def check_native_unit(feed: dict) -> None:
    """Reject, offline, a native unit HYSYS cannot accept.

    This is the rule that would have caught the original failure before it cost a
    remote run: ``MolarFlow`` + ``Nm3/h`` cannot succeed, and the probe proved that on
    both a gas stream and the slurry. A volumetric unit is only meaningful through
    ``flow_input: 'normal_volume'``, where its standard conditions are declared.
    """
    prop = feed.get('flow_property')
    unit = str(feed.get('total_flow_unit') or '').strip()
    if prop == 'MolarFlow' and unit not in HYSYS_MOLAR_UNITS:
        raise SpecError(
            'flow_property MolarFlow accepts only %s; %r is not one of them. A '
            'standard gas volume such as Nm3/h must go through '
            "flow_input='normal_volume' with its standard conditions stated, because "
            "HYSYS's own normal basis is 15 C while the exam states 0 C."
            % (sorted(HYSYS_MOLAR_UNITS), unit))
    if prop == 'MassFlow' and unit not in HYSYS_MASS_UNITS:
        raise SpecError('flow_property MassFlow accepts only %s; %r is not one of them'
                        % (sorted(HYSYS_MASS_UNITS), unit))


def stated_molar_volume(feed: dict) -> tuple:
    """Ideal-gas molar volume implied by the spec's stated standard conditions.

    Delegates to `core.normal_molar_volume` so the conversion exists in one place.
    A second copy would be a second thing to keep in step, and the two are used by
    different code paths for the same number - the setter here and the offline
    precheck and conservation checks there.

    Returns (m3/kmol, human-readable conditions).
    """
    return normal_molar_volume(
        feed.get('standard_temperature_C', DEFAULT_STANDARD_TEMPERATURE_C),
        feed.get('standard_pressure_kPa', DEFAULT_STANDARD_PRESSURE_KPA))


def composition_weights(feed: dict) -> dict:
    """Relative mole amounts only, never an offline prediction of native flow."""
    basis = feed.get('basis', 'molar_fraction')
    if basis not in ('molar_fraction', 'mole_fraction', 'mass_fraction', 'weight_fraction'):
        raise SpecError('a delegated flow requires a fraction composition basis')
    unit = feed.get('total_flow_unit')
    if not isinstance(unit, str) or not unit.strip():
        raise SpecError('a delegated flow requires the exact unit string')
    value = feed.get('total_flow')
    if isinstance(value, bool) or not math.isfinite(float(value)) or float(value) <= 0:
        raise SpecError('total_flow must be positive and finite')
    if mode_of(feed) == 'hysys':
        if feed.get('flow_property') not in ('MolarFlow', 'MassFlow'):
            raise SpecError('flow_property must explicitly be MolarFlow or MassFlow; '
                            'actual/liquid volume is not inferred')
    reference = dict(
        feed, total_flow=1.0,
        # `flow_input='local'` is required, not cosmetic: this reference exists only to
        # get the COMPOSITION, and `feed_molar_flows` now interprets `flow_input`. Left
        # as 'normal_volume', the reference would demand a normal volume unit while
        # carrying a molar or mass one.
        flow_input='local',
        total_flow_unit=('kg/h' if basis in ('mass_fraction', 'weight_fraction')
                         else 'kmol/h'))
    return feed_molar_flows(reference)


def set_native_total(stream, feed: dict) -> dict:
    """Use exactly the requested property and unit. Rejection never falls back."""
    composition_weights(feed)
    check_native_unit(feed)
    prop = feed['flow_property']
    unit = feed['total_flow_unit']
    requested = float(feed['total_flow'])
    try:
        variable = getattr(stream, prop)
        variable.SetValue(requested, unit)
        echoed = float(variable.GetValue(unit))
        molar = float(stream.MolarFlow.GetValue('kgmole/h'))
        mass = float(stream.MassFlow.GetValue('kg/h'))
    except Exception as exc:
        raise SpecError('HYSYS native flow failed for %s (%s): %s; verify the unit '
                        'spelling on the remote workstation'
                        % (prop, unit, exc)) from exc
    if not all(math.isfinite(v) and v > 0 for v in (echoed, molar, mass)):
        raise SpecError('HYSYS native flow readback is unknown, nonfinite or nonpositive')
    if not math.isclose(echoed, requested, rel_tol=1e-6, abs_tol=1e-8):
        raise SpecError('HYSYS native flow readback does not match requested total')
    return {'property': prop, 'unit': unit, 'requested': requested,
            'readback': echoed, 'molar_flow_kmol_h': molar, 'mass_flow_kg_h': mass,
            'reference_conditions': ('HYSYS unit definition; no local standard-volume '
                                     'conversion')}


def set_normal_volume_total(stream, feed: dict) -> dict:
    """Convert the stated normal volume to a molar flow and set that.

    The conversion is `core.feed_molar_flows`, not a second copy of the arithmetic
    here. That matters: this function and the pre-check used to divide `total_flow` by
    the molar volume themselves, so **neither validated the unit** - `flow_input:
    'normal_volume'` with `kg/h`, `t/h`, `Nm3/d` or even a nonsense string was accepted
    and silently divided by 22.4. Routing both through core puts the unit check back
    on the execution path, which is the only place it can protect anything.
    """
    component_flows = feed_molar_flows(feed)
    molar_flow_kmol_h = sum(component_flows.values())
    molar_volume, conditions = stated_molar_volume(feed)
    try:
        variable = stream.MolarFlow
        variable.SetValue(molar_flow_kmol_h, MOLAR_UNIT_FOR_HYSYS)
        echoed = float(variable.GetValue(MOLAR_UNIT_FOR_HYSYS))
        mass = float(stream.MassFlow.GetValue('kg/h'))
    except Exception as exc:
        raise SpecError('HYSYS molar flow set failed (%s %s): %s'
                        % (molar_flow_kmol_h, MOLAR_UNIT_FOR_HYSYS, exc)) from exc
    if not all(math.isfinite(v) and v > 0 for v in (echoed, mass)):
        raise SpecError('molar flow readback is unknown, nonfinite or nonpositive')
    if not math.isclose(echoed, molar_flow_kmol_h, rel_tol=1e-6, abs_tol=1e-8):
        raise SpecError('molar flow readback %r does not match the computed %r'
                        % (echoed, molar_flow_kmol_h))
    return {'property': 'MolarFlow',
            'unit': MOLAR_UNIT_FOR_HYSYS,
            'requested_normal_flow': float(feed['total_flow']),
            'requested_normal_unit': feed.get('total_flow_unit'),
            'standard_conditions': conditions,
            'molar_volume_m3_per_kmol': molar_volume,
            'requested': molar_flow_kmol_h,
            'readback': echoed,
            'molar_flow_kmol_h': echoed,
            'component_flows_kmol_h': dict(component_flows),
            'mass_flow_kg_h': mass,
            'reference_conditions': conditions,
            'conversion': ('%g %s / %.6f m3/kmol at %s = %.6f kmol/h'
                           % (float(feed['total_flow']),
                              feed.get('total_flow_unit'), molar_volume, conditions,
                              molar_flow_kmol_h))}


def set_delegated_total(stream, feed: dict) -> dict:
    """Dispatch on the mode the spec declared."""
    if is_normal_volume(feed):
        return set_normal_volume_total(stream, feed)
    return set_native_total(stream, feed)
