"""Independent enthalpy data and energy balance, used to check the duty HYSYS reports.

Why this exists
---------------
The equilibrium gate checks the outlet COMPOSITION. It cannot see an enthalpy error:
a carbon component with the right Gibbs energy but the wrong heat of formation or heat
capacity would give the right outlet and a wrong duty. A hypothetical graphite component
built by hand is exactly where that can happen, because HYSYS does not carry the library
component's enthalpy data over to it.

So the duty is recomputed here from first principles, using HYSYS's OWN outlet
composition. Using HYSYS's composition (not an independently computed equilibrium)
isolates the enthalpy data: a composition error is the equilibrium gate's business, an
enthalpy error is this module's.

Measured agreement against HYSYS on this workstation, before any of this was relied on:

    coal-slurry gasification, failed run (library Carbon)   +0.03 %
    steam reforming 600 C (accepted baseline)               -0.11 %
    steam reforming 710 C (accepted baseline)               -0.11 %

so a 1 % acceptance band leaves an order of magnitude of margin.

Data sources - VERIFY BEFORE RELYING ON THEM
-------------------------------------------
* Gas species: NIST Chemistry WebBook Shomate coefficients (from the JANAF tables).
  Checked here for self-consistency: H - H298 is zero at 298.15 K, Cp(298.15) matches
  the tabulated value, and both enthalpy and Cp are continuous at every range boundary
  (see test_phase1_gate). That catches most transcription errors but is not a
  substitute for comparing against the WebBook pages.
* Graphite: Kelley-type fit Cp = 16.86 + 4.77e-3 T - 8.54e5 / T^2 J/(mol K), valid
  298-2300 K. Integrates to 27.3 kJ/mol at 1673 K; JANAF tabulates about 27.4.
* Liquid water: dHf = -285.83 kJ/mol, Cp = 75.3 J/(mol K). Adequate to 150 C.
* Water saturation, for deciding the feed water phase: NIST Antoine parameters.

Ideal-gas enthalpies are used for the gas phase. At 1400 C and 40 bar, or 600 C and
13.5 bar, the residual enthalpy is far below the 1 % band (the agreement above already
includes it, because HYSYS uses Peng-Robinson).
"""
from __future__ import annotations

import math
from typing import Any

T_REF_K = 298.15

# NIST WebBook Shomate coefficients, kJ/mol and J/(mol K):
#   Cp = A + B t + C t^2 + D t^3 + E / t^2
#   H - H298 = A t + B t^2/2 + C t^3/3 + D t^4/4 - E/t + F - H      (t = T / 1000)
# Each row: (Tmin, Tmax, A, B, C, D, E, F, H). H is dHf(298.15 K).
SHOMATE = {
    'carbon monoxide': (
        (298.0, 1300.0, 25.56759, 6.096130, 4.054656, -2.671301, 0.131021,
         -118.0089, -110.5271),
        (1300.0, 6000.0, 35.15070, 1.300095, -0.205921, 0.013550, -3.282780,
         -127.8375, -110.5271),
    ),
    'carbon dioxide': (
        (298.0, 1200.0, 24.99735, 55.18696, -33.69137, 7.948387, -0.136638,
         -403.6075, -393.5224),
        (1200.0, 6000.0, 58.16639, 2.720074, -0.492289, 0.038844, -6.447293,
         -425.9186, -393.5224),
    ),
    'hydrogen': (
        (298.0, 1000.0, 33.066178, -11.363417, 11.432816, -2.772874, -0.158558,
         -9.980797, 0.0),
        (1000.0, 2500.0, 18.563083, 12.257357, -2.859786, 0.268238, 1.977990,
         -1.147438, 0.0),
    ),
    # Tabulated from 500 K; below that the low-range fit is still used for water
    # VAPOUR, where its error is a few J/mol (H - H298 = 0.001 kJ/mol at 298.15 K).
    'water': (
        (298.0, 1700.0, 30.09200, 6.832514, 6.793435, -2.534480, 0.082139,
         -250.8810, -241.8264),
        (1700.0, 6000.0, 41.96426, 8.622053, -1.499780, 0.098119, -11.15764,
         -272.1797, -241.8264),
    ),
    'methane': (
        (298.0, 1300.0, -0.703029, 108.4773, -42.52157, 5.862788, 0.678565,
         -76.84376, -74.87310),
        (1300.0, 6000.0, 85.81217, 11.26467, -2.114146, 0.138190, -26.42221,
         -153.5327, -74.87310),
    ),
}

# Graphite (the standard state of carbon, dHf = 0): Cp = a + b T - c / T^2.
GRAPHITE_CP = (16.86, 4.77e-3, 8.54e5)
GRAPHITE_RANGE_K = (298.0, 2300.0)

# Liquid water.
WATER_LIQUID_DHF_KJ = -285.83
WATER_LIQUID_CP_KJ = 75.3e-3
WATER_LIQUID_MAX_K = 423.15

# NIST Antoine for water: log10(P / bar) = A - B / (T + C), T in K.
WATER_ANTOINE = (
    (255.9, 373.0, 4.6543, 1435.264, -64.848),
    (379.0, 573.0, 3.55959, 643.748, -198.043),
)
# Within this many kelvin of saturation the phase is not decided offline.
PHASE_MARGIN_K = 5.0

# Species that are condensed by definition in this module.
SOLID_SPECIES = ('carbon',)

# Flows below this are ignored, so a 1e-20 kmol/h trace never makes a balance
# "unavailable" because of a species with no data.
TRACE_KMOL_H = 1e-9


class ReferenceUnavailable(Exception):
    """The independent value cannot be computed for this case (not an error)."""


def _shomate_row(key: str, temperature_K: float):
    rows = SHOMATE[key]
    if temperature_K < rows[0][0] - 1e-6 or temperature_K > rows[-1][1] + 1e-6:
        raise ReferenceUnavailable('%s outside %g-%g K: %.2f K'
                                   % (key, rows[0][0], rows[-1][1], temperature_K))
    for row in rows:
        if row[0] - 1e-6 <= temperature_K <= row[1] + 1e-6:
            return row
    return rows[-1]


def gas_enthalpy(key: str, temperature_K: float) -> float:
    """Ideal-gas enthalpy on the formation basis, kJ/mol."""
    if key not in SHOMATE:
        raise ReferenceUnavailable('no gas-phase data for %r' % key)
    _lo, _hi, a, b, c, d, e, f, h = _shomate_row(key, temperature_K)
    t = temperature_K / 1000.0
    return h + (a * t + b * t ** 2 / 2 + c * t ** 3 / 3 + d * t ** 4 / 4 - e / t + f - h)


def gas_cp(key: str, temperature_K: float) -> float:
    """Ideal-gas heat capacity, J/(mol K). Used by the self-consistency tests."""
    _lo, _hi, a, b, c, d, e, _f, _h = _shomate_row(key, temperature_K)
    t = temperature_K / 1000.0
    return a + b * t + c * t ** 2 + d * t ** 3 + e / t ** 2


def graphite_enthalpy(temperature_K: float) -> float:
    """Graphite enthalpy on the formation basis (dHf = 0), kJ/mol."""
    low, high = GRAPHITE_RANGE_K
    if not low - 1e-6 <= temperature_K <= high + 1e-6:
        raise ReferenceUnavailable('graphite outside %g-%g K: %.2f K'
                                   % (low, high, temperature_K))
    a, b, c = GRAPHITE_CP
    t0 = T_REF_K
    t = temperature_K
    return (a * (t - t0) + b / 2 * (t * t - t0 * t0) + c * (1 / t - 1 / t0)) / 1000.0


def water_saturation_K(pressure_bar: float) -> float:
    """Saturation temperature of water at a pressure, K (NIST Antoine)."""
    if not pressure_bar > 0:
        raise ReferenceUnavailable('pressure must be positive: %r' % pressure_bar)
    log_p = math.log10(pressure_bar)
    for t_min, t_max, a, b, c in WATER_ANTOINE:
        if a - log_p <= 0:
            continue
        t_sat = b / (a - log_p) - c
        if t_min - 10 <= t_sat <= t_max + 10:
            return t_sat
    raise ReferenceUnavailable('water saturation not covered at %.3g bar' % pressure_bar)


def water_phase(temperature_K: float, pressure_bar: float) -> str:
    """'liquid' or 'vapour'; ReferenceUnavailable when too close to call."""
    try:
        t_sat = water_saturation_K(pressure_bar)
    except ReferenceUnavailable:
        # Above the range of the fit: only a clearly superheated state is decidable.
        if temperature_K > 700.0:
            return 'vapour'
        raise
    if temperature_K < t_sat - PHASE_MARGIN_K:
        return 'liquid'
    if temperature_K > t_sat + PHASE_MARGIN_K:
        return 'vapour'
    raise ReferenceUnavailable('water at %.1f K and %.3g bar is within %g K of '
                               'saturation (%.1f K); its phase is not decided offline'
                               % (temperature_K, pressure_bar, PHASE_MARGIN_K, t_sat))


def species_enthalpy(key: str, temperature_K: float, pressure_bar: float) -> float:
    """Enthalpy of one species in the phase it occupies, formation basis, kJ/mol."""
    if key in SOLID_SPECIES:
        return graphite_enthalpy(temperature_K)
    if key == 'water' and water_phase(temperature_K, pressure_bar) == 'liquid':
        if temperature_K > WATER_LIQUID_MAX_K:
            raise ReferenceUnavailable('liquid water above %.0f K is not covered'
                                       % WATER_LIQUID_MAX_K)
        return WATER_LIQUID_DHF_KJ + WATER_LIQUID_CP_KJ * (temperature_K - T_REF_K)
    return gas_enthalpy(key, temperature_K)


def stream_enthalpy_kW(flows_kmol_h: dict[str, float], temperature_K: float,
                       pressure_bar: float) -> float:
    """Total enthalpy flow of a stream, kW, keyed by internal component name."""
    total_kj_per_h = 0.0
    for key, amount in flows_kmol_h.items():
        amount = float(amount)
        if abs(amount) < TRACE_KMOL_H:
            continue
        # kmol/h * kJ/mol = 1000 kJ/h
        total_kj_per_h += amount * 1000.0 * species_enthalpy(key, temperature_K,
                                                              pressure_bar)
    return total_kj_per_h / 3600.0


def independent_duty_kW(inlet: dict[str, float], inlet_K: float, inlet_bar: float,
                        outlet: dict[str, float], outlet_K: float,
                        outlet_bar: float) -> float:
    """H_out - H_in, kW. Raises ReferenceUnavailable when it cannot be computed."""
    return (stream_enthalpy_kW(outlet, outlet_K, outlet_bar)
            - stream_enthalpy_kW(inlet, inlet_K, inlet_bar))


def describe_sources() -> dict[str, Any]:
    """What the independent value rests on, recorded in the result."""
    return {
        'gas': 'NIST WebBook Shomate (JANAF), ideal gas',
        'graphite': 'Cp = 16.86 + 4.77e-3 T - 8.54e5/T^2 J/(mol K), dHf = 0',
        'liquid_water': 'dHf = -285.83 kJ/mol, Cp = 75.3 J/(mol K)',
        'basis': 'HYSYS outlet composition; enthalpy data only is being checked',
    }
