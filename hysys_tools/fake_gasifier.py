"""Test support: a fake HYSYS that can run the graphite-saturation path end to end.

Extends the `selfcheck` fakes with
  * a conversion reactor that applies the bookkeeping reaction at the reaction's current
    conversion, unconverted carbon to its LIQUID product;
  * a Gibbs reactor that solves the real gas-phase C/H/O equilibrium (CO, CO2, H2, H2O,
    CH4) with HYSYS's measured Gibbs data at 1673.15 K - pure Python, no scipy;
  * duties from the independent enthalpy data.

Everything is patched through `unittest.mock.patch.object` inside `environment()`, so
the selfcheck fakes are restored on exit and nothing leaks into other test modules.
"""
from __future__ import annotations

import contextlib
import math
from unittest import mock

from . import core, saturation, selfcheck as sc
from .thermo_reference import stream_enthalpy_kW

UNKNOWN = -32767.0
T_K = 1673.15
R = 8.314462618
# HYSYS EvaluateGibbs at 1673.15 K, J/mol (probe-carbon-properties, measured).
HYSYS_GIBBS = {'carbon monoxide': -258442.3617224739, 'carbon dioxide': -396366.98012628464,
               'water': -154706.26552152785, 'methane': 93662.48412873709,
               'hydrogen': 0.0, 'carbon': 450525.3452596415}

SCENARIOS = ('direct', 'basis_edit', 'ineffective', 'carbon_in_conv_vapour', 'no_solve')


def gas_equilibrium(bC, bH, bO, T, P_atm, G):
    """CO/CO2/H2/H2O/CH4 equilibrium for given element totals; nested bisection."""
    RT = R * T
    lnK_smr = -(G['carbon monoxide'] - G['methane'] - G['water']) / RT
    lnK_wgs = -(G['carbon dioxide'] - G['carbon monoxide'] - G['water']) / RT
    tiny = 1e-300

    def moles(u, v):
        co = bC - u - v
        h2o = bO - bC + u - v
        return {'methane': max(u, tiny), 'carbon dioxide': max(v, tiny),
                'carbon monoxide': max(co, tiny), 'water': max(h2o, tiny),
                'hydrogen': max(bH / 2 - 2 * u - h2o, tiny)}

    def solve_v(u):
        lo, hi = max(0.0, bO - bC + 3 * u - bH / 2), min(bC - u, bO - bC + u)
        if not hi > lo:
            return None

        def f(v):
            n = moles(u, v)
            return (math.log(n['carbon dioxide']) + math.log(n['hydrogen'])
                    - math.log(n['carbon monoxide']) - math.log(n['water']) - lnK_wgs)
        a, b = lo, hi
        if f(a + (b - a) * 1e-15) > 0:
            return a
        if f(b - (b - a) * 1e-15) < 0:
            return b
        for _ in range(200):
            m = 0.5 * (a + b)
            a, b = (a, m) if f(m) > 0 else (m, b)
            if b - a < 1e-14 * max(1.0, b):
                break
        return 0.5 * (a + b)

    def g(u):
        v = solve_v(u)
        if v is None:
            return None, None
        n = moles(u, v)
        ln_n = math.log(sum(n.values()))
        L = {k: math.log(x) - ln_n for k, x in n.items()}
        return (L['carbon monoxide'] + 3 * L['hydrogen'] + 2 * math.log(P_atm)
                - L['methane'] - L['water'] - lnK_smr), n

    u_lo, u_hi = max(0.0, bC - bO), min(bC, bH / 4)
    span = (u_hi - u_lo) * (1 - 1e-9)
    prev = None
    for i in range(601):
        u = u_lo + 10 ** (-14 + 14 * i / 600) * span
        val, _ = g(u)
        if val is None:
            continue
        if prev is not None and (prev[1] > 0) != (val > 0):
            a, b = prev[0], u
            break
        prev = (u, val)
    else:
        raise ValueError('no equilibrium bracket')
    for _ in range(300):
        m = math.sqrt(a * b) if a > 0 else 0.5 * (a + b)
        a, b = (m, b) if (g(a)[0] > 0) == (g(m)[0] > 0) else (a, m)
        if b - a < 1e-13 * max(1e-30, b):
            break
    return g(0.5 * (a + b))[1]


class _State:
    scenario = 'direct'
    editing = False


def _stream_flows(stream, names):
    total = float(stream.MolarFlow.value)
    if abs(total - UNKNOWN) < 0.5:
        return None
    values = list(getattr(stream.ComponentMolarFraction, 'Values', []) or [])
    return {core.canonical(n): total * float(y) for n, y in zip(names, values)}


def _fill(stream, flows, names, pressure_kPa):
    total = sum(flows.get(core.canonical(n), 0.0) for n in names)
    stream.ComponentMolarFraction = type('C', (), {'Values': [
        (flows.get(core.canonical(n), 0.0) / total if total else 0.0) for n in names]})()
    stream.MolarFlow.value = total
    stream.Temperature.value = T_K - 273.15
    stream.Pressure.value = pressure_kPa
    stream.MassFlow.value = sum(flows.get(core.canonical(n), 0.0) * core.molar_mass_of(n)
                                for n in names)


def _unknown(op):
    for stream in (op._vapour, op._liquid):
        if stream is not None:
            stream.MolarFlow.value = UNKNOWN
    op.HeatFlow.value = UNKNOWN


def _compute(op, names):
    if op._ignored or not op._feeds or op._vapour is None or op._liquid is None \
            or _State.scenario == 'no_solve':
        _unknown(op)
        return
    feed = op._feeds[0]
    inlet = _stream_flows(feed, names)
    if inlet is None:
        _unknown(op)
        return
    p_kpa = float(feed.Pressure.value)
    if op.factory == 'conversionreactorop':
        x = float(op.ReactionSet.ActiveReactions._items[0].ConversionCoefficients_[0]) / 100
        if _State.scenario == 'ineffective':
            x = op.__dict__.setdefault('_x_first', x)
        carbon = inlet.get('carbon', 0.0)
        converted = x * carbon
        gas = {'water': inlet.get('water', 0.0) - 2.0 / 3.0 * converted,
               'carbon monoxide': 2.0 / 3.0 * converted, 'methane': converted / 3.0}
        solid = {'carbon': carbon - converted}
        if _State.scenario == 'carbon_in_conv_vapour':
            gas['carbon'] = 0.01 * solid['carbon']
            solid['carbon'] *= 0.99
        h_in = stream_enthalpy_kW(inlet, float(feed.Temperature.value) + 273.15,
                                  p_kpa / 100.0)
    else:
        totals = {e: sum(v * core.atoms_of(k).get(e, 0) for k, v in inlet.items())
                  for e in 'CHO'}
        gas = gas_equilibrium(totals['C'], totals['H'], totals['O'], T_K,
                              p_kpa / 101.325, HYSYS_GIBBS)
        solid = {}
        h_in = stream_enthalpy_kW(inlet, T_K, p_kpa / 100.0)
    _fill(op._vapour, gas, names, p_kpa)
    _fill(op._liquid, solid, names, p_kpa)
    out = dict(gas)
    for k, v in solid.items():
        out[k] = out.get(k, 0.0) + v
    op.HeatFlow.value = stream_enthalpy_kW(out, T_K, p_kpa / 100.0) - h_in


class _DynamicSolver:
    """Reading IsSolving re-solves the fake flowsheet, operations in creation order."""

    def __init__(self, case):
        self._case = case
        self.CanSolve = True

    @property
    def IsSolving(self):
        names = list(self._case.BasisManager.components._items)
        for op in self._case.Flowsheet.Operations._items.values():
            _compute(op, names)
        return False


@contextlib.contextmanager
def environment(scenario: str = 'direct'):
    """Patch the selfcheck fakes for one run; everything is restored on exit."""
    if scenario not in SCENARIOS:
        raise ValueError(scenario)
    _State.scenario = scenario
    _State.editing = False
    original_case_init = sc.FakeCase.__init__
    original_stream_init = sc.FakeStream.__init__

    def case_init(self, converged=True, registry=None):
        original_case_init(self, converged, registry)
        self.Solver = _DynamicSolver(self)

    def stream_init(self, name, basis):
        original_stream_init(self, name, basis)

        def derive_mass():
            values = list(getattr(self.ComponentMolarFraction, 'Values', []) or [])
            names = list(self._basis.components._items)
            if values and len(values) == len(names):
                self.MassFlow.value = self.MolarFlow.value * sum(
                    f * core.molar_mass_of(n) for f, n in zip(values, names))
        self.MolarFlow.on_set = derive_mass

    def start(self):
        self.editing = True
        _State.editing = True

    def end(self):
        self.editing = False
        _State.editing = False

    def set_coefficients(self, values):
        if _State.scenario == 'basis_edit' and not _State.editing:
            raise sc.FakeComError('reaction is read-only outside a basis edit')
        self.ConversionCoefficients_ = list(values)

    with contextlib.ExitStack() as stack:
        patch = stack.enter_context
        patch(mock.patch.object(sc.FakeOperation, '_fill',
                                lambda self, stream, vapour: setattr(
                                    stream.MolarFlow, 'value', UNKNOWN)))
        patch(mock.patch.object(sc.FakeCase, '__init__', case_init))
        patch(mock.patch.object(sc.FakeStream, '__init__', stream_init))
        patch(mock.patch.object(sc._FakeComponent, 'EvaluateGibbs',
                                lambda self, kelvin: HYSYS_GIBBS.get(
                                    core.canonical(self.Name), -1000.0 + kelvin)))
        patch(mock.patch.object(sc.FakeBasis, 'StartBasisChange', start))
        patch(mock.patch.object(sc.FakeBasis, 'EndBasisChange', end))
        patch(mock.patch.object(sc.FakeReaction, 'ConversionCoefficientsValue', property(
            lambda self: tuple(self.ConversionCoefficients_), set_coefficients)))
        patch(mock.patch.object(saturation, 'POLL_S', 0.0))
        patch(mock.patch.object(saturation, 'READ_TIMEOUT_S', 1.0))
        yield
