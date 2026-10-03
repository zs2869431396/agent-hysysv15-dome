"""Independent checks on a finished case.

Nothing here trusts HYSYS. Every quantity is recomputed from the reported
component flows, so a solver that quietly returned a pass-through or a
non-converged result is rejected rather than reported as a success.

Which checks apply depends on the reactor kind:
  * element and mass conservation always apply;
  * a specified conversion is only meaningful for a conversion reactor;
  * CO yield is reported whenever the feed contains carbon, as the exam defines
    it (net CO produced over carbon fed), not as a mole fraction.
"""
from __future__ import annotations

import math
from typing import Any

from .core import (
    SpecError,
    atoms_of,
    canonical,
    element_relative_errors,
    stoichiometry_balance,
)

ELEMENT_TOLERANCE = 1e-5
MASS_TOLERANCE = 1e-4


class ResultCheckError(Exception):
    """The simulation ran, but its reported output failed a check.

    Deliberately NOT a subclass of SpecError, even though both surface as a failed
    run. A caller that writes ``except SpecError`` to mean "the input was wrong"
    would otherwise swallow result failures too, and send an agent off to edit a
    specification that was perfectly fine. The two mean different things:
    SpecError means the input was unusable, this means the simulator's output
    disagreed. Recorded cause: a reactor that solved without its reaction set taking
    effect, so the outlet came back identical to the inlet.
    """


def inlet_molar_flows(feed_molar_flows: dict[str, float]) -> dict[str, float]:
    return {canonical(k): float(v) for k, v in feed_molar_flows.items()}


def check_conservation(inlet: dict[str, float], outlet: dict[str, float],
                       molar_mass: dict[str, float] | None = None) -> dict:
    """Element and mass conservation between a known inlet and a measured outlet.

    Both sides are reduced to internal component keys first. The inlet arrives
    keyed by internal name while the outlet arrives keyed by the name HYSYS
    reports, so comparing them without normalising would silently compare nothing.
    """
    in_flows = inlet_molar_flows(inlet)
    out_flows = {canonical(k): float(v) for k, v in outlet.items()}
    for label, flows in (('inlet', in_flows), ('outlet', out_flows)):
        if not flows or any(not math.isfinite(v) or v < -1e-8 for v in flows.values()):
            raise ResultCheckError('%s contains invalid component flows' % label)
        if sum(flows.values()) <= 0:
            raise ResultCheckError('%s total flow must be positive' % label)
    errors = element_relative_errors(in_flows, out_flows)
    worst_element = max((abs(v) for v in errors.values()), default=0.0)

    def mass_of(flows: dict[str, float]) -> float:
        total = 0.0
        for name, amount in flows.items():
            if molar_mass and name in molar_mass:
                total += amount * float(molar_mass[name])
            else:
                total += amount * _molar_mass(name)
        return total

    inlet_mass = mass_of(in_flows)
    outlet_mass = mass_of(out_flows)
    mass_error = ((outlet_mass - inlet_mass) / inlet_mass) if inlet_mass else 0.0

    if not all(math.isfinite(v) for v in (worst_element, inlet_mass, outlet_mass, mass_error)):
        raise ResultCheckError('non-finite balance result')
    if worst_element > ELEMENT_TOLERANCE:
        raise ResultCheckError('element balance failed: relative errors %s'
                               % {k: round(v, 12) for k, v in errors.items()})
    if abs(mass_error) > MASS_TOLERANCE:
        raise ResultCheckError(
            'mass balance failed: inlet %.6f kg/h, outlet %.6f kg/h, relative '
            'error %.3e' % (inlet_mass, outlet_mass, mass_error))
    return {
        'element_relative_error': {k: round(v, 15) for k, v in errors.items()},
        'worst_element_relative_error': worst_element,
        'inlet_mass_kg_h': inlet_mass,
        'outlet_mass_kg_h': outlet_mass,
        'mass_relative_error': mass_error,
    }


def _molar_mass(name: str) -> float:
    """kg/kmol from the elemental composition (adequate for a conservation check)."""
    atomic = {'C': 12.011, 'H': 1.008, 'O': 15.999, 'N': 14.007}
    composition = atoms_of(name)
    return sum(atomic[element] * count for element, count in composition.items())


def check_specified_conversion(reactions: list[dict], outlet: dict[str, float],
                               reactor_kind: str, inlet: dict[str, float]) -> list[dict]:
    """Recompute a specified conversion from the outlet and compare.

    Only applied to a conversion reactor: for Gibbs and Equilibrium the outlet
    composition is a thermodynamic result, so a specified conversion is not a
    target and must not be enforced.

    The comparison denominator is the base component's own inlet flow, taken from
    the feed, not the total feed: a 50% toluene conversion means half of the
    toluene fed, whatever else is present.
    """
    if reactor_kind != 'conversion':
        return []
    results: list[dict] = []
    for entry in reactions:
        percent = entry.get('conversion_percent')
        if percent is None:
            continue
        base = entry.get('base_component')
        name = entry.get('name')
        if not base:
            raise SpecError('a conversion reaction needs base_component for the '
                            'conversion to be checked: %r' % name)
        key = canonical(base)
        base_inlet = float(inlet.get(key, 0.0))
        if base_inlet <= 0:
            results.append({
                'reaction': name, 'base_component': base, 'checked': False,
                'reason': 'the feed contains no %s, so the specified conversion '
                          'cannot be verified independently' % base})
            continue
        base_outlet = float(outlet.get(key, 0.0))
        measured = (base_inlet - base_outlet) / base_inlet * 100.0
        if abs(measured - float(percent)) > 0.05:
            hint = ''
            if abs(measured) < 0.05 and float(percent) > 0.05:
                # An outlet identical to the inlet means the operation solved as a
                # pass-through: the reaction never took effect. Saying so saves the
                # reader from suspecting the specification.
                hint = (' The outlet equals the inlet, so the reactor behaved as a '
                        'pass-through and the reaction never took effect. Check that '
                        'the reaction set is attached to the operation and has active '
                        'reactions; the specification itself was accepted.')
            raise ResultCheckError(
                'conversion check failed for %s: requested %g%%, measured %g%% '
                '(base component %s: %.6f kmol/h in, %.6f kmol/h out).%s'
                % (name, float(percent), measured, base, base_inlet, base_outlet,
                   hint))
        results.append({'reaction': name, 'base_component': base, 'checked': True,
                        'requested_percent': float(percent),
                        'measured_percent': measured,
                        'base_component_inlet_kmol_h': base_inlet,
                        'base_component_outlet_kmol_h': base_outlet})
    return results


def reactant_conversions(inlet: dict[str, float],
                         outlet: dict[str, float]) -> dict[str, float]:
    """Conversion of every component that enters, as a percentage.

    Reported for every feed component because the key figure differs by scenario and
    a reader should not have to infer it: for reforming it is the methane
    conversion, for toluene disproportionation the toluene conversion. Components
    that only appear as products are not listed, so nothing here can be mistaken for
    a feed conversion.
    """
    in_flows = inlet_molar_flows(inlet)
    out_flows = {canonical(k): float(v) for k, v in outlet.items()}
    conversions: dict[str, float] = {}
    for name, amount in in_flows.items():
        if amount <= 0:
            continue
        remaining = out_flows.get(name, 0.0)
        conversions[name] = (amount - remaining) / amount * 100.0
    return conversions


def co_yield(spec: dict, inlet: dict[str, float],
             outlet: dict[str, float]) -> dict[str, Any] | None:
    """CO yield on a carbon fed basis, exactly as the exam defines it.

        Y_CO = (n_CO,out - n_CO,in) / n_C,feed * 100%

    Deliberately NOT the CO mole fraction in the product gas and NOT the overall
    carbon conversion; the exam states these are different quantities and asks for
    all three to be distinguishable.

    Carbon conversion is only defined when solid carbon enters. For a system without
    solid carbon it is always exactly 100%, which reads like the conversion of the
    key reactant, so it is reported as null together with a note instead.
    """
    in_flows = inlet_molar_flows(inlet)
    out_flows = {canonical(k): float(v) for k, v in outlet.items()}
    carbon_fed = sum(amount * atoms_of(name).get('C', 0)
                     for name, amount in in_flows.items())
    if carbon_fed <= 0:
        return None
    co_in = in_flows.get('carbon monoxide', 0.0)
    co_out = out_flows.get('carbon monoxide', 0.0)
    produced = co_out - co_in
    total_carbon_out = sum(
        amount * atoms_of(name).get('C', 0) for name, amount in out_flows.items())
    solid_carbon_fed = in_flows.get('carbon', 0.0)
    # Dry basis: exclude everything that would be a separate condensed phase, i.e.
    # water AND unreacted solid carbon. Excluding only water would understate the CO
    # fraction in a gasification case that still has carbon leaving the reactor.
    out_water = out_flows.get('water', 0.0)
    out_carbon = out_flows.get('carbon', 0.0)
    total_out = sum(out_flows.values())
    dry_out = total_out - out_water - out_carbon

    block: dict[str, Any] = {
        'definition': '(n_CO_out - n_CO_in) / n_C_feed * 100%',
        'co_produced_kmol_h': produced,
        'carbon_fed_kmol_h': carbon_fed,
        'co_yield_percent': produced / carbon_fed * 100.0,
        'total_carbon_out_kmol_h': total_carbon_out,
        'total_outlet_kmol_h': total_out,
        'water_outlet_kmol_h': out_water,
        'dry_outlet_kmol_h': dry_out,
        'co_mole_fraction_dry': (co_out / dry_out) if dry_out > 0 else None,
        'note': 'CO yield, carbon conversion and CO mole fraction are three '
                'different quantities and are reported separately. The mole fraction '
                'is on a dry basis: water and any unreacted solid carbon are '
                'excluded from the denominator.',
    }
    if solid_carbon_fed > 0:
        block.update({
            'solid_carbon_fed_kmol_h': solid_carbon_fed,
            'carbon_remaining_solid_kmol_h': out_flows.get('carbon', 0.0),
            'carbon_conversion_percent':
                (1 - out_flows.get('carbon', 0.0) / solid_carbon_fed) * 100.0,
        })
    else:
        block.update({
            'solid_carbon_fed_kmol_h': 0.0,
            'carbon_conversion_percent': None,
            'carbon_conversion_note':
                'No solid carbon enters this case, so a carbon conversion is not '
                'defined here. Use reactant_conversion_percent for the key '
                'conversion; a carbon conversion of 100% would be meaningless.',
        })
    return block


# ---------------------------------------------------------------- thermochemistry
#
# The reaction that decides whether a gasification outlet is chemically possible at
# all:  C(s) + 2 H2(g) <-> CH4(g).  Solid carbon has unit activity, so
#
#     K_p  =  p_CH4 / p_H2^2        [bar^-1],  p in bar
#          =  y_CH4 / (y_H2^2 * P)              <- GAS-PHASE mole fractions only
#
# Two things about this that were wrong first time and are worth stating.
#
# **Mole fractions are gas-phase only.** Solid carbon is not in the gas, so including
# it in the normalising total dilutes y_CH4 and y_H2 by the same factor and multiplies
# K_p by it - on the measured case by 1.73. That inflated the check's output and made
# it disagree with the JANAF reference by exactly that factor.
#
# **A single Van't Hoff fit from 298 K is not good enough.** dHf(CH4) moves from
# -74.6 kJ/mol at 298 K to about -92 at high temperature, so a constant-dH form is off
# by a factor of 11 at 1673 K - an entire order of magnitude, half of what the
# tolerance was meant to cover. The constant below is instead fitted through TWO
# anchors, which makes it right at both ends of the range that matters:
#
#   anchor 1  298.15 K   dGf(CH4) = -50.5 kJ/mol (textbook)      -> K = 7.03e8
#   anchor 2  1673.15 K  dGf(CH4) = +94 kJ/mol   (JANAF)          -> K = 1.1626e-3
#
# The effective dH that satisfies both is -81.8 kJ/mol, which sits between the 298 K
# value and the high-temperature value - where an average belongs. The resulting K(T)
# is monotonic, crosses K = 1 near 800 K (matching where methanation stops being
# favoured), and its 1673 K value agrees to 0.0% with the gas-phase normalisation of an
# independently computed equilibrium composition.
#
# An earlier attempt used NASA polynomials instead of the two anchors. Their 298 K
# output did not match the textbook entropies (156 vs 186 J/mol/K for methane, 109 vs
# 5.7 for graphite, the latter being impossible for a solid). That says the coefficients
# as transcribed, or the implementation around them, were wrong - **not** that the NASA
# data is unreliable. The polynomials themselves are a standard, well-validated source
# and remain the right thing to use; anyone tightening this tolerance should go back to
# them (or to a tabulated dfG(CH4, T)) rather than distrusting the source. What mattered
# was checking the result against known values before letting it gate anything.
ANCHOR_TEMPERATURE_K = 298.15
ANCHOR_DG_J_PER_MOL = -50500.0
REFERENCE_TEMPERATURE_K = 1673.15
REFERENCE_DG_J_PER_MOL = 94000.0

GAS_CONSTANT_J_PER_MOL_K = 8.314462618

# One order of magnitude, for the ONE-SIDED test (no condensed phase): a carbon-free gas
# only has to stay below the methanation equilibrium, and a steam reformer sits well
# below it by design, so this band only has to catch gross supersaturation.
EQUILIBRIUM_TOLERANCE_ORDERS = 1.0

# Half an order, for the TWO-SIDED test (condensed carbon present). One order was too
# loose. If a hand-built carbon component ends up in the gas phase instead of as a
# separate solid, its activity is y_C * P rather than 1 - about 10 at 40 bar - and the
# outlet lands log10(10) = 0.99 orders off: just inside a 1.0 band, so a result with
# six times the true methane would have passed. The K(T) fit is exact at 1673 K and
# within 0.25 orders anywhere in 873-1800 K, so 0.5 still leaves margin for real results
# while rejecting that failure. (The phase-location check catches it first; this is the
# second line.)
CONDENSED_TOLERANCE_ORDERS = 0.5

# A declared condensed species may carry at most this share of its total flow in the
# vapour product. More than that means HYSYS is not treating it as a separate solid.
CONDENSED_IN_GAS_LIMIT = 1e-4

# Allowed relative deviation between HYSYS's duty and the independent energy balance.
# Measured agreement on this workstation was 0.03-0.11 %, so 1 % has a tenfold margin.
DUTY_RELATIVE_TOLERANCE = 0.01

# Product streams that are a gas phase.
GAS_PRODUCT_NAMES = ('VAPOUR', 'VAPOR')

# Species whose activity is 1 because they are a pure condensed phase present in the
# reactor. They are excluded from gas-phase mole fractions, and their presence is what
# makes the equilibrium an equality rather than an upper bound.
CONDENSED_SPECIES = ('carbon', 'graphite')


def strip_hypothetical_marker(name: str) -> str:
    """Remove HYSYS's marker for a hypothetical component.

    HYSYS appends an asterisk to hypothetical component names. Matching on the raw
    string therefore misses them, which matters more than it looks: a carbon that is
    not recognised as condensed makes `check_gibbs_equilibrium` fall back to a
    one-sided test and ACCEPT an outlet it should reject. Measured - a carbon named
    `Carbon*` turned the impossible gasification outlet into `NO_HYDROGEN`, passing.
    """
    return str(name).strip().strip('*').strip()


def is_condensed_species(name: str) -> bool:
    """Whether a component name denotes a pure condensed phase."""
    return canonical(strip_hypothetical_marker(name)) in CONDENSED_SPECIES

# A mole fraction below this counts as absent. HYSYS reports values like 3.8e-4 for
# species that are thermodynamically impossible at 1400 C, so the test is "is there
# any real amount", not "is it exactly zero".
ABSENT_MOLE_FRACTION = 1e-6
# Below this, a methane fraction is unremarkable at high temperature.
NEGLIGIBLE_METHANE = 1e-3


def _effective_reaction_enthalpy() -> float:
    """dH that makes a constant-dH fit pass through both anchors, J/mol."""
    ln_ratio = (math.log(math.exp(-REFERENCE_DG_J_PER_MOL
                                  / (GAS_CONSTANT_J_PER_MOL_K * REFERENCE_TEMPERATURE_K)))
                - math.log(math.exp(-ANCHOR_DG_J_PER_MOL
                                    / (GAS_CONSTANT_J_PER_MOL_K * ANCHOR_TEMPERATURE_K))))
    inverse_difference = 1.0 / REFERENCE_TEMPERATURE_K - 1.0 / ANCHOR_TEMPERATURE_K
    return -ln_ratio / inverse_difference * GAS_CONSTANT_J_PER_MOL_K


EFFECTIVE_REACTION_ENTHALPY_J_PER_MOL = _effective_reaction_enthalpy()


def methanation_kp(temperature_K: float) -> float:
    """Equilibrium constant for C(s) + 2 H2 <-> CH4 at a temperature, in bar^-1.

    Anchored at 298.15 K (textbook) and 1673.15 K (JANAF); see the block comment above.
    """
    if not math.isfinite(temperature_K) or temperature_K <= 0:
        raise ResultCheckError('temperature for the equilibrium check must be a '
                               'positive finite number in K, got %r' % temperature_K)
    ln_at_anchor = -ANCHOR_DG_J_PER_MOL / (GAS_CONSTANT_J_PER_MOL_K
                                           * ANCHOR_TEMPERATURE_K)
    ln_k = ln_at_anchor - (EFFECTIVE_REACTION_ENTHALPY_J_PER_MOL
                           / GAS_CONSTANT_J_PER_MOL_K) * (
        1.0 / temperature_K - 1.0 / ANCHOR_TEMPERATURE_K)
    return math.exp(ln_k)


def gas_phase_fractions(outlet_flows: dict[str, float]) -> tuple[dict[str, float],
                                                                bool]:
    """Mole fractions over the GAS species, and whether a condensed phase is present.

    Solid carbon is excluded from the normalising total: it is not in the gas, so
    counting it would dilute every mole fraction and scale K_p by the same factor.
    Returns the fractions and whether any condensed species is present, because that
    decides whether the equilibrium is an equality or an upper bound.
    """
    normalised = {canonical(name): float(value)
                  for name, value in (outlet_flows or {}).items()}
    condensed_keys = {canonical(name) for name in (outlet_flows or {})
                      if is_condensed_species(name)}
    # Compare flow against flow. An earlier version tested a kmol/h flow against a
    # mole-fraction threshold, which is a unit mismatch that happened to work only
    # because the two numbers were far apart.
    gas_total = sum(value for name, value in normalised.items()
                    if name not in condensed_keys and value > 0)
    condensed_total = sum(value for name, value in normalised.items()
                          if name in condensed_keys and value > 0)
    # A condensed species counts as present when it is a real share of the outlet, not
    # a trace. The threshold is a fraction of the whole, applied to a flow by scaling.
    condensed_present = (condensed_total > ABSENT_MOLE_FRACTION
                         * (gas_total + condensed_total))
    gas = {name: value for name, value in normalised.items()
           if name not in condensed_keys and value > 0}
    if not gas:
        raise ResultCheckError('outlet has no gas-phase species; nothing to check')
    total = sum(gas.values())
    return {name: value / total for name, value in gas.items()}, condensed_present


def check_gibbs_equilibrium(outlet_flows: dict[str, float], temperature_K: float,
                            pressure_bar: float,
                            expect_condensed: bool = False) -> dict[str, Any]:
    """Reject an outlet that no equilibrium could produce.

    Atom and mass conservation cannot see this class of error: an outlet where every
    oxygen atom becomes CO and every hydrogen atom becomes CH4 balances perfectly and
    is still impossible. Measured on the workstation at 1400 C and 40 bar, HYSYS
    returned exactly that - CO 1035.402, CH4 517.701, Carbon 980.697, H2 3.8e-4.

    **It is not a solver failure.** Back-calculating the chemical potential of the
    carbon phase from that outlet, using HYSYS's own (verified correct) data for the
    gas species, gives the same answer from three independent reactions:

        via C + 2H2 <-> CH4      mu_C = +450.38 kJ/mol
        via C + H2O <-> CO + H2  mu_C = +450.64
        via C + CO2 <-> 2CO      mu_C = +450.66
        HYSYS EvaluateGibbs(Carbon)       +450.53

    agreeing to 0.15 kJ/mol. So the minimiser converged correctly and the composition
    is self-consistent - it solved the wrong problem exactly. Two things follow:

      * The carbon phase is handled with **unit activity** (mu_C equals dGf(Carbon)
        exactly), which is the correct treatment for a pure condensed phase. HYSYS's
        method is right; only the data is wrong.
      * Giving the component graphite's data (dGf(T) = 0, dHf = 0) is therefore a
        plausible fix rather than a dead end.

    Three tests:

      * **wrong-reference signature** - carbon left over while H2, H2O and CO2 have all
        vanished and CH4 has not. Needs no thermochemistry and names the cause.
      * **two-sided K_p** - when a condensed phase is present the activity is 1, so
        the outlet must sit ON the equilibrium: |log10 Q/K| <= tolerance.
      * **one-sided K_p** - when no condensed phase is present the equality does not
        hold, because carbon cannot be consumed if there is none. Only an upper bound
        applies: Q must not exceed K, or carbon would precipitate. Applying the
        two-sided test here rejected correct carbon-free results - a high-temperature
        steam reformer sits far below the methanation equilibrium by design.
    """
    y, condensed_present = gas_phase_fractions(outlet_flows)

    # Totals in the flows' own units, so the carbon share below is a ratio of like with
    # like. An earlier version divided a kmol/h flow by (that flow + 1.0), because the
    # gas side was already a normalised fraction sum: 1491.5 kmol/h of carbon was
    # reported as a carbon fraction of 0.9993 instead of 0.4230.
    #
    # Matching goes through `is_condensed_species`, which strips HYSYS's hypothetical
    # marker. Matching the raw string missed `Carbon*` and silently downgraded this
    # check to the one-sided form, which accepted the very outlet it exists to reject.
    raw = {canonical(name): float(value)
           for name, value in (outlet_flows or {}).items()}
    condensed_keys = {canonical(name) for name in (outlet_flows or {})
                      if is_condensed_species(name)}
    gas_total_flow = sum(value for name, value in raw.items()
                         if name not in condensed_keys and value > 0)
    carbon = sum(value for name, value in raw.items()
                 if name in condensed_keys and value > 0)

    h2 = y.get('hydrogen', 0.0)
    ch4 = y.get('methane', 0.0)
    h2o = y.get('water', 0.0)
    co2 = y.get('carbon dioxide', 0.0)
    total_flow = gas_total_flow + carbon
    carbon_fraction = carbon / total_flow if total_flow > 0 else 0.0

    # The caller knows whether the feed contained a solid carbon component; this
    # function only sees the outlet. When it did, an outlet with no condensed phase
    # means the component was not recognised - not that the thermodynamics say carbon
    # is absent. Silently continuing would apply the weaker one-sided test and pass a
    # wrong answer without a trace, so it is an error instead.
    if expect_condensed and not condensed_present:
        raise ResultCheckError(
            'the feed declares a solid carbon component but no condensed phase is '
            'present in the outlet, and no component named like carbon or graphite was '
            'found among %s. The condensed-phase equilibrium cannot be applied, and '
            'falling back to the one-sided test would accept an outlet this check '
            'exists to reject. Check that the carbon component name maps onto a '
            'condensed species (HYSYS marks hypotheticals with an asterisk, which is '
            'stripped before matching).' % sorted(outlet_flows or {}))

    block: dict[str, Any] = {
        'temperature_K': temperature_K,
        'pressure_bar': pressure_bar,
        'condensed_phase_present': condensed_present,
        'y_CH4': ch4, 'y_H2': h2, 'y_H2O': h2o, 'y_CO2': co2,
        'carbon_fraction': carbon_fraction,
        'normalisation': 'gas-phase species only',
    }

    # ------------------------------------------------------ vertex signature
    if condensed_present and h2 < ABSENT_MOLE_FRACTION and \
            h2o < ABSENT_MOLE_FRACTION and co2 < ABSENT_MOLE_FRACTION and \
            ch4 > NEGLIGIBLE_METHANE:
        block['verdict'] = 'WRONG_CARBON_REFERENCE'
        raise ResultCheckError(
            'Gibbs outlet is an equilibrium under a wrong carbon reference state, not '
            'a usable result. Carbon is left unconverted while H2, H2O and CO2 have all '
            'vanished and CH4 has not (y=%.4g in the gas phase): every oxygen atom '
            'became CO and every hydrogen atom became CH4. Back-calculating the '
            'chemical potential of the carbon phase from the outlet gives the same '
            'value from three independent reactions, and it equals what HYSYS reports '
            'for Carbon - so the minimiser converged correctly and the composition is '
            'self-consistent. The defect is the data it was given: HYSYS carries '
            'Carbon with the properties of gaseous atomic carbon rather than graphite. '
            'The result cannot be reported, but the solver is not at fault.'
            % ch4)

    # ------------------------------------------------------------ K_p comparison
    expected = methanation_kp(temperature_K)
    tolerance = (CONDENSED_TOLERANCE_ORDERS if condensed_present
                 else EQUILIBRIUM_TOLERANCE_ORDERS)
    block['expected_kp_bar_inverse'] = expected
    block['tolerance_orders'] = tolerance
    block['comparison'] = 'two-sided (a condensed phase is present)' \
        if condensed_present else 'one-sided upper bound (no condensed phase)'

    if h2 < ABSENT_MOLE_FRACTION:
        # Solid carbon and water side by side with no hydrogen at all is not a state
        # any equilibrium at gasification temperature can produce: C + H2O -> CO + H2
        # is strongly favoured. It is what a reactor that never reacted looks like -
        # measured: the route-one probe case came back as its own feed, unreacted.
        # Reporting NO_HYDROGEN and returning let such an outlet through the tool
        # (only the validation script, which demands CONSISTENT, would have stopped it).
        if condensed_present and h2o > ABSENT_MOLE_FRACTION:
            block['verdict'] = 'NO_HYDROGEN_WITH_CARBON_AND_WATER'
            raise ResultCheckError(
                'solid carbon and water leave together with no hydrogen (y_H2O=%.4g in '
                'the gas): C + H2O -> CO + H2 cannot have reached equilibrium. This is '
                'the signature of a reactor that did not react (outlet equal to the '
                'feed), not a result.' % h2o)
        # No hydrogen in the system at all (for example a dry carbon / CO2 case): the
        # methanation ratio is undefined, and the vertex test above already covers the
        # case where that matters.
        block['verdict'] = 'NO_HYDROGEN'
        return block

    outlet_kp = ch4 / (h2 * h2 * pressure_bar) if pressure_bar > 0 else float('inf')
    block['outlet_kp_bar_inverse'] = outlet_kp
    if outlet_kp <= 0 or not math.isfinite(outlet_kp):
        block['verdict'] = 'UNDEFINED'
        return block

    orders = math.log10(outlet_kp / expected)
    block['orders_from_equilibrium'] = orders

    if condensed_present:
        if abs(orders) > tolerance:
            block['verdict'] = 'OFF_EQUILIBRIUM'
            raise ResultCheckError(
                'Gibbs outlet is %.2f orders of magnitude off the equilibrium for '
                'C(s) + 2 H2 <-> CH4 at %.1f K with solid carbon present: the outlet '
                'implies K_p = %.3e bar^-1 while the thermodynamic value is %.3e '
                'bar^-1 (tolerance %.1f orders). An offset near +1 order at 40 bar is '
                'the signature of carbon dissolved in the gas (activity y_C * P instead '
                'of 1). Atom and mass conservation cannot detect this, so the result '
                'must not be reported as a simulation result.'
                % (orders, temperature_K, outlet_kp, expected, tolerance))
    elif orders > tolerance:
        # No condensed phase, so only the upper bound applies: exceeding K means
        # carbon would have precipitated and the outlet is not a stable state.
        block['verdict'] = 'CARBON_SHOULD_PRECIPITATE'
        raise ResultCheckError(
            'Gibbs outlet has no solid carbon yet is %.1f orders of magnitude ABOVE '
            'the equilibrium for C(s) + 2 H2 <-> CH4 at %.1f K: the outlet implies '
            'K_p = %.3e bar^-1 against %.3e bar^-1. Carbon would have to precipitate '
            'for this gas to be stable, so the outlet is not a valid carbon-free '
            'equilibrium.' % (orders, temperature_K, outlet_kp, expected))

    # Below the equilibrium with no condensed phase is entirely normal: a
    # high-temperature reformer sits there deliberately, to avoid coking.
    block['verdict'] = 'CONSISTENT'
    return block


def check_condensed_phase_location(products: dict[str, Any] | None) -> dict[str, Any]:
    """Every declared condensed species must leave in a condensed product, not the gas.

    The equilibrium gate recognises carbon by NAME and excludes it from the gas-phase
    mole fractions. That is right only if HYSYS really treats it as a separate solid. A
    hand-built hypothetical carbon whose solid flag cannot be set may instead be handled
    as a fluid and dissolve in the gas, and the name-based gate cannot see that: its
    outlet lands about one order off the methanation equilibrium, which a loose band
    accepts. This check looks at WHERE the carbon actually left, which is the direct
    test of whether the solid treatment took effect.

    ``products`` is the per-product block `solve_and_read` records:
    ``{'VAPOUR': {'molar_flow_kmol_h': .., 'mole_fractions': {name: y}}, 'LIQUID': ..}``.
    """
    if not products:
        raise ResultCheckError(
            'a condensed species is declared but no per-product composition was '
            'supplied, so it cannot be confirmed that it left as a condensed phase. '
            'Refusing rather than assuming.')
    in_gas: dict[str, float] = {}
    total: dict[str, float] = {}
    for product, entry in products.items():
        entry = entry or {}
        flow = float(entry.get('molar_flow_kmol_h') or 0.0)
        fractions = entry.get('mole_fractions') or {}
        is_gas = str(product).strip().upper() in GAS_PRODUCT_NAMES
        for name, fraction in fractions.items():
            if not is_condensed_species(name):
                continue
            amount = flow * float(fraction)
            key = canonical(name)
            total[key] = total.get(key, 0.0) + amount
            if is_gas:
                in_gas[key] = in_gas.get(key, 0.0) + amount
    shares = {key: (in_gas.get(key, 0.0) / amount if amount > 0 else 0.0)
              for key, amount in total.items()}
    block: dict[str, Any] = {
        'condensed_total_kmol_h': total,
        'condensed_in_gas_kmol_h': in_gas,
        'share_in_gas': shares,
        'limit': CONDENSED_IN_GAS_LIMIT,
    }
    worst = max(shares.values(), default=0.0)
    if worst > CONDENSED_IN_GAS_LIMIT:
        block['verdict'] = 'CONDENSED_SPECIES_IN_GAS'
        raise ResultCheckError(
            'a declared condensed species left in the gas product: %s of its flow is '
            'in the vapour (limit %.0e). HYSYS is treating it as a fluid, not as a '
            'separate solid, so its activity is not 1 and the Gibbs result does not '
            'describe solid carbon. Detail: %s'
            % ('%.3g' % worst, CONDENSED_IN_GAS_LIMIT,
               {k: round(v, 6) for k, v in shares.items()}))
    block['verdict'] = 'CONDENSED_PHASE_ONLY'
    return block


def check_independent_duty(spec: dict, inlet: dict[str, float],
                           outlet_flows: dict[str, float], heat_duty_kW: float,
                           products: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compare HYSYS's duty with an energy balance built from independent data.

    The equilibrium gate checks composition only. A carbon component with correct
    Gibbs data but wrong enthalpy data (heat of formation, heat capacity) gives the
    right outlet and a wrong duty, and nothing else would notice. HYSYS's own outlet
    composition is used, so this tests the enthalpy data and nothing else.

    Returns ``verdict = 'UNAVAILABLE'`` (no exception) when the reference does not cover
    the case - a species without data, a temperature out of range, or feed water too
    close to saturation to place. Raises when it IS available and disagrees.
    """
    from .thermo_reference import (
        ReferenceUnavailable,
        describe_sources,
        independent_duty_kW,
    )
    from .core import to_celsius, to_kpa

    block: dict[str, Any] = {'hysys_duty_kW': heat_duty_kW,
                             'tolerance_relative': DUTY_RELATIVE_TOLERANCE,
                             'sources': describe_sources()}
    try:
        feed = (spec.get('feeds') or [{}])[0]
        reactor = spec.get('reactor') or {}
        inlet_K = to_celsius(feed['temperature'], feed.get('temperature_unit', 'C')) \
            + 273.15
        inlet_bar = to_kpa(feed['pressure'], feed.get('pressure_unit', 'kPa')) / 100.0
        outlet_C = None
        for entry in (products or {}).values():
            if entry and entry.get('temperature_C') is not None:
                outlet_C = float(entry['temperature_C'])
                break
        if outlet_C is None:
            outlet_C = to_celsius(reactor['outlet_temperature'],
                                  reactor.get('outlet_temperature_unit', 'C'))
        outlet_bar = max(inlet_bar - float(reactor.get('pressure_drop_kPa') or 0.0)
                         / 100.0, 1e-6)
        inlet_internal = {canonical(k): float(v) for k, v in inlet.items()}
        outlet_internal = {canonical(k): float(v) for k, v in outlet_flows.items()}
        reference = independent_duty_kW(inlet_internal, inlet_K, inlet_bar,
                                        outlet_internal, outlet_C + 273.15, outlet_bar)
    except (ReferenceUnavailable, KeyError, TypeError, ValueError, SpecError) as exc:
        block['verdict'] = 'UNAVAILABLE'
        block['reason'] = str(exc)
        return block

    block.update({'independent_duty_kW': reference,
                  'inlet_K': inlet_K, 'inlet_bar': inlet_bar,
                  'outlet_K': outlet_C + 273.15, 'outlet_bar': outlet_bar})
    scale = max(abs(reference), abs(heat_duty_kW))
    if scale <= 0:
        block['verdict'] = 'UNAVAILABLE'
        block['reason'] = 'both duties are zero; a relative comparison is undefined'
        return block
    deviation = (heat_duty_kW - reference) / scale
    block['relative_deviation'] = deviation
    if abs(deviation) > DUTY_RELATIVE_TOLERANCE:
        block['verdict'] = 'DUTY_MISMATCH'
        raise ResultCheckError(
            'HYSYS duty %.1f kW differs from the independent energy balance %.1f kW by '
            '%.2f %% (tolerance %.1f %%), computed on HYSYS\'s own outlet composition. '
            'The composition may be right while the enthalpy data is not - for a '
            'hand-built carbon component, check its heat of formation (graphite: 0) '
            'and heat capacity.'
            % (heat_duty_kW, reference, deviation * 100, DUTY_RELATIVE_TOLERANCE * 100))
    block['verdict'] = 'CONSISTENT'
    return block


def verify_case(spec: dict, feed_molar_flows: dict[str, float],
                outlet_flows: dict[str, float], reactor_kind: str,
                heat_duty_kW: float, thermal_mode: str = 'adiabatic',
                molar_mass: dict[str, float] | None = None,
                products: dict[str, Any] | None = None,
                equilibrium_evidence: list[dict] | None = None) -> dict[str, Any]:
    """Run every applicable check and return the quality block.

    ``feed_molar_flows`` must be keyed by internal component name; the outlet may
    be keyed by whatever HYSYS reports, because it is normalised inside.

    ``products`` is the per-product block from `solve_and_read`. It is only REQUIRED
    when the spec declares a condensed species (solid carbon): then the phase location
    and the independent duty are checked as well. Specs without one - toluene, steam
    reforming - run exactly the checks they ran before, and produce the same block.
    """
    inlet = inlet_molar_flows(feed_molar_flows)
    if not math.isfinite(heat_duty_kW) or abs(heat_duty_kW + 32767.0) < 0.5:
        raise ResultCheckError('heat duty is unknown or non-finite')
    checks: dict[str, Any] = {}
    if str(reactor_kind).casefold() == 'equilibrium':
        from .equilibrium import check_outlet
        checks['equilibrium_QK'] = check_outlet(products, equilibrium_evidence)
    checks.update(check_conservation(inlet, outlet_flows, molar_mass))
    checks['specified_conversion'] = check_specified_conversion(
        spec.get('reactions', []), {canonical(k): float(v)
                                    for k, v in outlet_flows.items()},
        reactor_kind, inlet)
    # Conversion of every component that enters. This is the figure a reader
    # actually needs (methane conversion for reforming, toluene conversion for
    # disproportionation); for a Gibbs reactor it is the only conversion reported,
    # because there is no specified one to check.
    checks['reactant_conversion_percent'] = reactant_conversions(inlet, outlet_flows)
    yield_block = co_yield(spec, inlet, outlet_flows)
    if yield_block:
        checks['co_yield'] = yield_block
    checks['heat_duty_kW'] = heat_duty_kW
    checks['heat_duty_scope'] = (
        'External heat needed to hold the stated outlet temperature. For an '
        'isothermal case this includes sensible heating of the feed from its inlet '
        'temperature to the outlet temperature as well as the heat of reaction, so '
        'it is the reactor duty, not the heat of reaction alone.'
        if thermal_mode == 'isothermal' else
        'Adiabatic case: the duty is zero by construction and the outlet '
        'temperature is the computed result.')

    declared = ((spec.get('fluid_package') or {}).get('components') or [])
    condensed_declared = any(is_condensed_species(name) for name in declared)

    # Where the carbon actually left. Checked before the equilibrium gate because it is
    # the more specific diagnosis: "carbon is in the gas" names the cause, whereas an
    # off-equilibrium verdict only reports the symptom.
    if condensed_declared:
        checks['condensed_phase_location'] = check_condensed_phase_location(products)

    # A Gibbs outlet can balance perfectly and still be impossible. This raises when
    # it is, so a chemically wrong answer cannot reach a report as a PASS.
    if str(reactor_kind).casefold() == 'gibbs':
        reactor = spec.get('reactor') or {}
        outlet_c = reactor.get('outlet_temperature')
        if outlet_c is not None:
            temperature_K = float(outlet_c) + 273.15
            pressure_bar = _outlet_pressure_bar(spec, reactor)
            if pressure_bar:
                # The spec knows whether a solid carbon component was declared, so it
                # can insist that the outlet actually show one. Without this, a carbon
                # whose name does not map onto a condensed species downgrades the check
                # to its one-sided form and passes in silence.
                declared = ((spec.get('fluid_package') or {}).get('components') or [])
                expect_condensed = any(is_condensed_species(name)
                                       for name in declared)
                checks['gibbs_equilibrium'] = check_gibbs_equilibrium(
                    outlet_flows, temperature_K, pressure_bar,
                    expect_condensed=expect_condensed)

    # Enthalpy data, checked independently of composition. Only for cases that declare
    # a condensed species and hold the outlet temperature: that is where a hand-built
    # component can carry wrong enthalpy data, and an isothermal duty is a quantity the
    # report states. Other specs are untouched.
    if condensed_declared and str(thermal_mode).casefold() == 'isothermal':
        checks['independent_duty'] = check_independent_duty(
            spec, inlet, outlet_flows, heat_duty_kW, products)
    return checks


def _outlet_pressure_bar(spec: dict, reactor: dict) -> float | None:
    """The pressure the reactor holds, in bar, for the equilibrium comparison."""
    value = reactor.get('pressure')
    unit = str(reactor.get('pressure_unit') or 'bar').strip().casefold()
    if value is None:
        feeds = spec.get('feeds') or []
        if feeds:
            value = feeds[0].get('pressure')
            unit = str(feeds[0].get('pressure_unit') or 'bar').strip().casefold()
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    factors = {'bar': 1.0, 'kpa': 0.01, 'mpa': 10.0, 'pa': 1e-5, 'atm': 1.01325,
               'psi': 0.0689476}
    return number * factors.get(unit, 1.0)
