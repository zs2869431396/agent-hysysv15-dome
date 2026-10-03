r"""Solid-carbon Gibbs by imposed graphite saturation ("route two").

Selected by ``reactor.solid_carbon = "saturation"`` on an isothermal Gibbs reactor whose
feed is carbon and water. Every other spec is untouched.

Why
---
HYSYS V15's library ``Carbon`` is a true solid (Class 600, IsSolid True) with correct
ENTHALPY data, but its Gibbs data is that of gaseous atomic carbon (+671 kJ/mol at
298 K, +450 at 1673 K; graphite is 0). A Gibbs reactor given it consumes as much carbon
as the oxygen and hydrogen allow - a perfectly balanced, impossible outlet. The library
component cannot be edited, and a hypothetical one created over COM is a fluid with no
writable enthalpy data (probe four). So the carbon's Gibbs energy is never used:

    FEED -> CONV (conversion reactor) -VAPOUR-> GIBBS (gas only) -> G-VAP
               \-> LIQUID (unconverted solid carbon)

* CONV converts a fraction X of the carbon by the BOOKKEEPING reaction
  3 C + 2 H2O -> 2 CO + CH4. A conversion reactor does no equilibrium, so carbon's
  Gibbs data is unused; its enthalpy data (correct) enters the duty. The reaction is
  bookkeeping only: GIBBS re-equilibrates the gas, so the choice does not affect the
  result. It is chosen because it can deliver enough carbon (C + H2O -> CO + H2 runs out
  of water below the amount equilibrium needs).
* GIBBS receives no carbon and cannot form any (carbon costs it +450 kJ/mol). It
  equilibrates CO / CO2 / H2 / H2O / CH4 with HYSYS's own gas data (verified vs JANAF).
* X is solved so that the gas is exactly saturated with graphite:
      a_C = p_CH4 / (K p_H2^2) = 1,    K = exp(-dGf(CH4)/RT)
  from HYSYS's own dGf(CH4) and graphite = 0 by definition. The activity is also
  computed from C + H2O <-> CO + H2 and C + CO2 <-> 2 CO; all three must agree.
* The combined outlet and the summed duty then pass the normal result gate (phase
  location, two-sided equilibrium against the INDEPENDENT JANAF-anchored K, and the
  independent energy balance) like any other result.

Measured on the workstation (probe-runs/route2-20261003-084249): converged in 7
evaluations to X = 0.41158; activities 1.0000 / 0.9936 / 0.9944; outlet within 0.1 % of
an independent ideal-gas equilibrium; duty 84.66 MW, 0.65 % below the independent
balance (HYSYS's graphite sensible heat at 1400 C is about 1.5 kJ/mol lower than the
reference correlation, times 1491 kmol/h of carbon); the conversion could be rewritten
in place ("direct").
"""
from __future__ import annotations

import math
import time
from typing import Any

from .core import SpecError, canonical, to_celsius, to_kpa
from .validate import ResultCheckError

UNKNOWN = -32767.0
GAS_CONSTANT = 8.314462618
REACTION_SET = 'AGENT-SAT'
REACTION_NAME = 'C-BOOKKEEPING'
BOOKKEEPING_STOICHIOMETRY = {'Carbon': -3.0, 'Water': -2.0, 'CO': 2.0, 'Methane': 1.0}
GAS_SPECIES = ('carbon monoxide', 'carbon dioxide', 'hydrogen', 'water', 'methane')
REQUIRED_COMPONENTS = ('carbon', 'water', 'carbon monoxide', 'methane', 'hydrogen',
                       'carbon dioxide')
ALLOWED_FEED = ('carbon', 'water')

# Search, as fractions of the largest conversion the water allows.
LOW_FRACTIONS = (0.49, 0.25, 0.08, 0.03)
HIGH_FRACTIONS = (0.816, 0.92, 0.98)
LOG_TOLERANCE = 2e-3            # |log10 a_C|: 0.5 % in activity
X_TOLERANCE = 1e-6
MAX_EVALUATIONS = 30
CROSS_CHECK_TOLERANCE = 0.05    # decades between the three activities
CARBON_IN_CONV_VAPOUR_LIMIT = 1e-4

# Readback.
READ_TIMEOUT_S = 60.0
POLL_S = 0.4
STABLE_READS = 3
EXPECT_REL = 1e-3

EDIT_MODES = ('direct', 'basis_edit')


class NotReady(Exception):
    """No valid, stable outlet yet."""


class ConversionNotApplied(Exception):
    """A stable outlet that does not reflect the conversion just written."""


# ----------------------------------------------------------------- spec handling
def is_requested(reactor_spec: dict) -> bool:
    return str(reactor_spec.get('solid_carbon') or '').strip().casefold() == 'saturation'


def check_spec(spec: dict, resolved_components: list[str], errors: list[str],
               warnings: list[str]) -> None:
    """Offline rules for reactor.solid_carbon. Only called when the field is present."""
    reactor = spec.get('reactor') or {}
    value = reactor.get('solid_carbon')
    if str(value or '').strip().casefold() != 'saturation':
        errors.append('reactor.solid_carbon %r is not supported; the only value is '
                      '"saturation"' % (value,))
        return
    if reactor.get('kind') != 'gibbs':
        errors.append('reactor.solid_carbon "saturation" needs reactor.kind "gibbs"')
    if reactor.get('thermal_mode') != 'isothermal':
        errors.append('reactor.solid_carbon "saturation" needs thermal_mode "isothermal" '
                      'with an outlet_temperature')
    present = {canonical(c) for c in resolved_components}
    missing = [c for c in REQUIRED_COMPONENTS if c not in present]
    if missing:
        errors.append('reactor.solid_carbon "saturation" needs these components in '
                      'fluid_package.components: %s (missing %s)'
                      % (list(REQUIRED_COMPONENTS), missing))
    feeds = spec.get('feeds') or []
    feed = feeds[0] if feeds and isinstance(feeds[0], dict) else {}
    named = list((feed.get('fractions') or {}).keys()) + list((feed.get('flows') or {}).keys())
    in_feed = {canonical(n) for n in named
               if float((feed.get('fractions') or feed.get('flows') or {}).get(n) or 0) > 0}
    if 'carbon' not in in_feed or 'water' not in in_feed:
        errors.append('reactor.solid_carbon "saturation" needs carbon and water in the '
                      'feed')
    extra = sorted(in_feed - set(ALLOWED_FEED))
    if extra:
        errors.append('reactor.solid_carbon "saturation" is verified for a carbon + water '
                      'feed only; the feed also contains %s' % extra)
    if spec.get('reactions'):
        warnings.append('reactor.solid_carbon "saturation" builds its own bookkeeping '
                        'reaction; the supplied reactions are not used')


def configure_reactions(builder, x: float):
    """Inside the open basis edit: the bookkeeping conversion reaction."""
    reaction_set = builder._reaction_set(REACTION_SET)
    builder.configure_conversion_reactions(reaction_set, [{
        'name': REACTION_NAME, 'stoichiometry': BOOKKEEPING_STOICHIOMETRY,
        'base_component': 'Carbon', 'conversion_percent': round(100.0 * x, 10)}])
    reaction_set.AssociateFluidPackage(builder._package)
    return reaction_set


def initial_conversion(feed_spec: dict) -> float:
    """A starting X inside the water-limited range, from the feed composition only."""
    from .native_flow import composition_weights

    weights = composition_weights(feed_spec)
    carbon = sum(v for k, v in weights.items() if canonical(k) == 'carbon')
    water = sum(v for k, v in weights.items() if canonical(k) == 'water')
    return LOW_FRACTIONS[0] * water_limited_x(carbon, water)


def water_limited_x(carbon: float, water: float) -> float:
    if carbon <= 0:
        raise SpecError('no carbon in the feed')
    return min(1.0, 1.5 * water / carbon) * 0.999


# ------------------------------------------------------------------- readback
def _unknown(value: float) -> bool:
    return (not math.isfinite(value)) or abs(value - UNKNOWN) < 0.5


def read_stream(stream, names: list[str]) -> dict:
    flow = float(stream.MolarFlow.GetValue('kgmole/h'))
    if _unknown(flow):
        raise NotReady('flow is the unknown marker')
    if flow < -1e-6:
        raise NotReady('negative flow %.6g' % flow)
    entry: dict[str, Any] = {'molar_flow_kmol_h': max(flow, 0.0)}
    if flow > 1e-8:
        t = float(stream.Temperature.GetValue('C'))
        p = float(stream.Pressure.GetValue('kPa'))
        fractions = [float(v) for v in stream.ComponentMolarFraction.Values]
        if _unknown(t) or _unknown(p):
            raise NotReady('temperature/pressure unknown')
        if len(fractions) != len(names):
            raise NotReady('component vector length mismatch')
        if any(not math.isfinite(v) or v < -1e-8 for v in fractions):
            raise NotReady('invalid composition')
        if abs(sum(fractions) - 1.0) > 1e-6:
            raise NotReady('mole fractions do not sum to 1')
        entry.update({'temperature_C': t, 'pressure_kPa': p,
                      'mole_fractions': dict(zip(names, fractions))})
    return entry


def flows_of(entry: dict) -> dict[str, float]:
    total = float(entry.get('molar_flow_kmol_h') or 0.0)
    return {n: total * float(y) for n, y in (entry.get('mole_fractions') or {}).items()}


def merge(entries: list[dict], names: list[str]) -> dict:
    flows = {n: 0.0 for n in names}
    temperature = pressure = None
    for entry in entries:
        for n, v in flows_of(entry).items():
            flows[n] = flows.get(n, 0.0) + v
        if temperature is None and entry.get('temperature_C') is not None:
            temperature, pressure = entry['temperature_C'], entry.get('pressure_kPa')
    total = sum(flows.values())
    merged: dict[str, Any] = {'molar_flow_kmol_h': total}
    if total > 0:
        merged.update({'mole_fractions': {n: v / total for n, v in flows.items()},
                       'temperature_C': temperature, 'pressure_kPa': pressure})
    return merged


def carbon_activities(gas: dict, gibbs_J: dict[str, float], temperature_K: float,
                      pressure_kPa: float) -> dict:
    """Carbon activity of the gas from three independent reactions (1 atm basis)."""
    y: dict[str, float] = {}
    for name, value in (gas.get('mole_fractions') or {}).items():
        key = canonical(name)
        if key in GAS_SPECIES:
            y[key] = y.get(key, 0.0) + float(value)
    total = sum(y.values())
    if total <= 0:
        return {'error': 'no gas'}
    y = {k: v / total for k, v in y.items()}
    rt = GAS_CONSTANT * temperature_K
    p = pressure_kPa / 101.325
    out: dict[str, Any] = {'p_atm': p, 'y_gas': y}

    def attempt(label, function):
        try:
            out[label] = function()
        except (KeyError, ZeroDivisionError, ValueError, OverflowError):
            out[label] = None

    attempt('via_methanation', lambda: y['methane'] / (
        math.exp(-gibbs_J['methane'] / rt) * y['hydrogen'] ** 2 * p))
    attempt('via_water_gas', lambda: y['carbon monoxide'] * y['hydrogen'] * p / (
        math.exp(-(gibbs_J['carbon monoxide'] - gibbs_J['water']) / rt) * y['water']))
    attempt('via_boudouard', lambda: y['carbon monoxide'] ** 2 * p / (
        math.exp(-(2 * gibbs_J['carbon monoxide'] - gibbs_J['carbon dioxide']) / rt)
        * y['carbon dioxide']))
    logs = [math.log10(v) for v in (out['via_methanation'], out['via_water_gas'],
                                    out['via_boudouard']) if v and v > 0]
    out['spread_decades'] = (max(logs) - min(logs)) if len(logs) == 3 else None
    return out


# ------------------------------------------------------------------ the flowsheet
class SaturationRun:
    """Builds CONV + GIBBS on an open case, solves X, returns a solve_and_read-like dict."""

    def __init__(self, case, builder, reactor_spec: dict, feed_spec: dict, log,
                 reaction_set, x0: float):
        self.case, self.builder, self.log = case, builder, log
        self.reactor_spec, self.feed_spec = reactor_spec, feed_spec
        self.reaction_set = reaction_set
        self.x0 = x0
        self.names = list(builder.readback_names)
        self.carbon_name = next((n for n in self.names if canonical(n) == 'carbon'), None)
        self.outlet_c = to_celsius(reactor_spec['outlet_temperature'],
                                   reactor_spec.get('outlet_temperature_unit', 'C'))
        self.pressure_kPa = to_kpa(feed_spec['pressure'],
                                   feed_spec.get('pressure_unit', 'kPa')) \
            - float(reactor_spec.get('pressure_drop_kPa', 0.0) or 0.0)
        self.mode_index = 0
        self.evaluations: list[dict] = []
        self.mode_attempts: list[dict] = []
        self.gibbs_J: dict[str, float] = {}
        self.conv = self.gibbs = self.streams = None
        self.feed_carbon = 0.0
        self.feed_water = 0.0
        self.feed_molar_total = 0.0

    # -- build ----------------------------------------------------------------------
    def build(self) -> None:
        name = str(self.reactor_spec.get('name', 'GASIFIER'))
        conv_spec = {'name': name + '-CONV', 'kind': 'conversion',
                     'thermal_mode': 'isothermal', 'outlet_temperature': self.outlet_c,
                     'outlet_temperature_unit': 'C',
                     'pressure_drop_kPa': float(self.reactor_spec.get('pressure_drop_kPa',
                                                                      0.0) or 0.0)}
        conv, streams, _outlet, total = self.builder.create_streams_and_reactor(
            self.case, conv_spec, self.feed_spec, self.reaction_set)
        self.conv, self.streams, self.feed_molar_total = conv, streams, total
        self.feed_carbon = float(self.builder.feed_values.get('carbon', 0.0))
        self.feed_water = float(self.builder.feed_values.get('water', 0.0))

        # Solve the conversion reactor FIRST so the stream handed to the Gibbs reactor
        # is defined when it is attached.
        conv.IsIgnored = False
        self.read(expect_x=self.x0, with_gibbs=False)
        self.log.add('saturation_conversion_reactor_solved', detail={'x': self.x0})

        flow = self.case.Flowsheet
        streams.Add('G-VAP')
        streams.Add('G-LIQ')
        flow.EnergyStreams.Add('DUTY-G')
        flow.Operations.Add(name, 'gibbsreactorop')
        gibbs = flow.Operations.Item(name)
        self.gibbs = gibbs
        trace = []
        for label, action in (
                ('hold_ignored', lambda: setattr(gibbs, 'IsIgnored', True)),
                ('feeds_add', lambda: gibbs.Feeds.Add(streams.Item('VAPOUR'))),
                ('set_vapour_product', lambda: setattr(gibbs, 'VapourProduct',
                                                       streams.Item('G-VAP'))),
                ('set_liquid_product', lambda: setattr(gibbs, 'LiquidProduct',
                                                       streams.Item('G-LIQ'))),
                ('set_energy_stream', lambda: setattr(gibbs, 'EnergyStream',
                                                      flow.EnergyStreams.Item('DUTY-G'))),
                ('set_pressure_drop', lambda: gibbs.PressureDrop.SetValue(0.0, 'kPa')),
                ('set_outlet_temperature', lambda: streams.Item('G-VAP')
                 .Temperature.SetValue(self.outlet_c, 'C')),
                ('release_ignored', lambda: setattr(gibbs, 'IsIgnored', False))):
            entry = {'call': label}
            try:
                action()
                entry['result'] = 'OK'
                trace.append(entry)
            except Exception as exc:
                entry.update(result='FAILED', error='%s: %s' % (type(exc).__name__, exc))
                trace.append(entry)
                self.log.add('saturation_gibbs_reactor', status='FAILED', detail=trace)
                raise
        package = self.builder._package
        for index in range(int(package.Components.Count)):
            component = package.Components.Item(index)
            key = canonical(str(component.Name))
            if key in GAS_SPECIES:
                self.gibbs_J[key] = float(component.EvaluateGibbs(self.outlet_c + 273.15))
        missing = [k for k in GAS_SPECIES if k not in self.gibbs_J]
        if missing:
            raise SpecError('no HYSYS Gibbs data for %s' % missing)
        self.log.add('saturation_gibbs_reactor', detail={
            'connections': trace, 'hysys_gibbs_J_per_mol': dict(self.gibbs_J)})

    # -- conversion edits -------------------------------------------------------------
    def set_conversion(self, x: float, mode: str) -> None:
        basis = self.builder._basis
        reaction = basis.ReactionPackageManager.Reactions.Item(REACTION_NAME)
        target = (round(100.0 * x, 10), 0.0, 0.0)
        payload = self.builder.win32com.client.VARIANT(
            self.builder.pythoncom.VT_ARRAY | self.builder.pythoncom.VT_R8, target)
        if mode == 'direct':
            reaction.ConversionCoefficientsValue = payload
        else:
            basis.StartBasisChange()
            try:
                reaction.ConversionCoefficientsValue = payload
            finally:
                basis.EndBasisChange()
        readback = float(list(reaction.ConversionCoefficientsValue)[0])
        if not math.isclose(readback, target[0], rel_tol=1e-9, abs_tol=1e-9):
            raise ConversionNotApplied('conversion readback %r, wanted %r'
                                       % (readback, target[0]))

    # -- readback -------------------------------------------------------------------------
    def read(self, expect_x: float | None, with_gibbs: bool = True) -> dict:
        names = ['VAPOUR', 'LIQUID'] + (['G-VAP', 'G-LIQ'] if with_gibbs else [])
        deadline = time.monotonic() + READ_TIMEOUT_S
        previous, stable, last, stale = None, 0, 'nothing read', False
        while time.monotonic() < deadline:
            try:
                if bool(self.case.Solver.IsSolving):
                    raise NotReady('solver busy')
                entries = {n: read_stream(self.streams.Item(n), self.names) for n in names}
                duties = {'conversion': float(self.conv.HeatFlow.GetValue('kW'))}
                if with_gibbs:
                    duties['gibbs'] = float(self.gibbs.HeatFlow.GetValue('kW'))
                for label, value in duties.items():
                    if _unknown(value):
                        raise NotReady('%s duty unknown' % label)
                for n, entry in entries.items():
                    if entry['molar_flow_kmol_h'] > 1e-8 and \
                            abs(entry['temperature_C'] - self.outlet_c) > 0.01:
                        raise NotReady('%s at %.3f C, not %.3f C'
                                       % (n, entry['temperature_C'], self.outlet_c))
                snapshot = []
                for n in names:
                    snapshot.append(entries[n]['molar_flow_kmol_h'])
                    snapshot.extend((entries[n].get('mole_fractions') or {}).values())
                snapshot.extend(duties.values())
                if previous is not None and len(previous) == len(snapshot) and all(
                        math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
                        for a, b in zip(previous, snapshot)):
                    stable += 1
                else:
                    stable = 1
                previous = snapshot
                if stable >= STABLE_READS:
                    if expect_x is not None and self.carbon_name:
                        left = sum(flows_of(entries[n]).get(self.carbon_name, 0.0)
                                   for n in ('VAPOUR', 'LIQUID'))
                        expected = (1.0 - expect_x) * self.feed_carbon
                        if abs(left - expected) > EXPECT_REL * max(expected, 1e-9) + 1e-6:
                            stale = True
                            last = ('stable outlet leaves %.4f kmol/h carbon; X=%.6f '
                                    'should leave %.4f' % (left, expect_x, expected))
                            time.sleep(POLL_S)
                            continue
                    return {'x': expect_x, 'streams': entries, 'duties_kW': duties,
                            'stable_reads': stable}
            except NotReady as exc:
                last, stable, previous = str(exc), 0, None
            except Exception as exc:
                last, stable, previous = '%s: %s' % (type(exc).__name__, exc), 0, None
            time.sleep(POLL_S)
        if stale:
            raise ConversionNotApplied(last)
        raise RuntimeError('no valid outlet within %.0fs: %s' % (READ_TIMEOUT_S, last))

    # -- search -----------------------------------------------------------------------------
    def evaluate(self, x: float, first: bool = False) -> dict:
        started = time.monotonic()
        if first:
            reading = self.read(expect_x=x)
        else:
            while True:
                mode = EDIT_MODES[self.mode_index]
                try:
                    self.set_conversion(x, mode)
                    reading = self.read(expect_x=x)
                    self.mode_attempts.append({'x': x, 'mode': mode, 'result': 'OK'})
                    break
                except Exception as exc:
                    self.mode_attempts.append({'x': x, 'mode': mode,
                                               'result': '%s: %s' % (type(exc).__name__,
                                                                     exc)})
                    if self.mode_index + 1 >= len(EDIT_MODES):
                        raise RuntimeError(
                            'the bookkeeping conversion could not be changed in place '
                            '(tried %s); last error: %s' % (list(EDIT_MODES), exc)) from exc
                    self.mode_index += 1
        activity = carbon_activities(reading['streams']['G-VAP'], self.gibbs_J,
                                     self.outlet_c + 273.15, self.pressure_kPa)
        a = activity.get('via_methanation')
        reading['carbon_activity'] = activity
        reading['log10_aC'] = math.log10(a) if a and a > 0 else None
        self.evaluations.append({'x': x, 'log10_aC': reading['log10_aC'],
                                 'via_water_gas': activity.get('via_water_gas'),
                                 'via_boudouard': activity.get('via_boudouard'),
                                 'mode': 'initial' if first else EDIT_MODES[self.mode_index],
                                 'seconds': round(time.monotonic() - started, 2)})
        if reading['log10_aC'] is None:
            raise ResultCheckError('carbon activity undefined at X=%.6f: %s' % (x, activity))
        return reading

    def solve(self) -> dict:
        x_max = water_limited_x(self.feed_carbon, self.feed_water)
        low = self.evaluate(self.x0, first=True)
        for fraction in LOW_FRACTIONS:
            if low['log10_aC'] < 0:
                break
            if not math.isclose(fraction * x_max, low['x']):
                low = self.evaluate(fraction * x_max)
        high = self.evaluate(HIGH_FRACTIONS[0] * x_max)
        for fraction in HIGH_FRACTIONS[1:]:
            if high['log10_aC'] > 0:
                break
            high = self.evaluate(fraction * x_max)
        if not (low['log10_aC'] < 0 < high['log10_aC']):
            raise ResultCheckError(
                'graphite saturation not bracketed: log10 a_C is %.3f at X=%.4f and %.3f '
                'at X=%.4f. If it stays below 0 up to the water limit, the feed has '
                'enough water to gasify all the carbon - a case this mode does not cover.'
                % (low['log10_aC'], low['x'], high['log10_aC'], high['x']))
        x_lo, g_lo, x_hi, g_hi = low['x'], low['log10_aC'], high['x'], high['log10_aC']
        side = 0
        while len(self.evaluations) < MAX_EVALUATIONS:
            x = x_hi - g_hi * (x_hi - x_lo) / (g_hi - g_lo)
            reading = self.evaluate(x)
            g = reading['log10_aC']
            if abs(g) < LOG_TOLERANCE or (x_hi - x_lo) < X_TOLERANCE:
                return reading
            if g < 0:
                x_lo, g_lo = x, g
                if side == -1:
                    g_hi /= 2
                side = -1
            else:
                x_hi, g_hi = x, g
                if side == 1:
                    g_lo /= 2
                side = 1
        raise ResultCheckError('graphite saturation not converged after %d evaluations'
                               % len(self.evaluations))

    # -- result -------------------------------------------------------------------------
    def result(self, reading: dict) -> dict:
        streams = reading['streams']
        gas = streams['G-VAP']
        condensed = merge([streams['LIQUID'], streams['G-LIQ']], self.names)
        flows: dict[str, float] = {}
        for entry in (gas, condensed):
            for n, v in flows_of(entry).items():
                flows[canonical(n)] = flows.get(canonical(n), 0.0) + v
        named = {n: flows.get(canonical(n), 0.0) for n in self.names}
        in_conv_vapour = flows_of(streams['VAPOUR']).get(self.carbon_name, 0.0)
        left = flows.get('carbon', 0.0)
        share = in_conv_vapour / (in_conv_vapour + left) if (in_conv_vapour + left) else 0.0
        activity = reading['carbon_activity']
        if share > CARBON_IN_CONV_VAPOUR_LIMIT:
            raise ResultCheckError(
                'unconverted carbon left the conversion reactor in its vapour product '
                '(%.3g of it) and so reached the Gibbs reactor; the bookkeeping that '
                'fixes the gasified carbon does not hold' % share)
        spread = activity.get('spread_decades')
        if spread is None or spread > CROSS_CHECK_TOLERANCE:
            raise ResultCheckError(
                'the gas is not internally equilibrated: carbon activities via '
                'methanation / water-gas / Boudouard are %s / %s / %s (spread %s '
                'decades, limit %.2f)' % (activity.get('via_methanation'),
                                          activity.get('via_water_gas'),
                                          activity.get('via_boudouard'), spread,
                                          CROSS_CHECK_TOLERANCE))
        duties = reading['duties_kW']
        block = {
            'method': ('graphite saturation imposed: conversion reactor (bookkeeping '
                       '3C + 2H2O -> 2CO + CH4) + gas-only Gibbs reactor; conversion '
                       'solved so the gas carbon activity is 1'),
            'carbon_conversion_x': reading['x'],
            'water_limited_x_max': water_limited_x(self.feed_carbon, self.feed_water),
            'carbon_activity': activity,
            'edit_mode': EDIT_MODES[self.mode_index],
            'evaluations': self.evaluations,
            'mode_attempts': self.mode_attempts,
            'duty_by_reactor_kW': duties,
            'carbon_in_conversion_vapour_share': share,
            'hysys_gibbs_J_per_mol': dict(self.gibbs_J),
            'library_carbon_gibbs_used': False,
        }
        return {
            'outlet': {'VAPOUR': gas, 'LIQUID': condensed},
            'component_flows_kmol_h': named,
            'component_flows_internal': flows,
            'heat_duty_kW': sum(duties.values()),
            'solver_is_solving': False,
            'solver_evidence': {
                'stable_reads': reading.get('stable_reads'),
                'required_stable_reads': STABLE_READS,
                'evaluations': len(self.evaluations),
                'checks': 'phase flows, composition, temperature, pressure, both duties, '
                          'and that each stable outlet reflects the conversion written',
                'note': 'Stable readback and idle solver; the equilibrium itself is '
                        'checked by the result gate.'},
            'saturation': block,
        }


def run(case, builder, reactor_spec: dict, feed_spec: dict, log, reaction_set,
        x0: float):
    """Build, solve and read. Returns (solved, feed_molar_total)."""
    job = SaturationRun(case, builder, reactor_spec, feed_spec, log, reaction_set, x0)
    job.build()
    reading = job.solve()
    solved = job.result(reading)
    log.add('saturation_solved', detail={
        'carbon_conversion_x': solved['saturation']['carbon_conversion_x'],
        'evaluations': len(job.evaluations),
        'edit_mode': solved['saturation']['edit_mode'],
        'carbon_activity': {k: solved['saturation']['carbon_activity'].get(k)
                            for k in ('via_methanation', 'via_water_gas',
                                      'via_boudouard', 'spread_decades')}})
    return solved, job.feed_molar_total
