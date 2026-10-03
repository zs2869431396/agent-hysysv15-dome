"""Offline verification suite for the HYSYS tool layer.

Runs the whole build path against fake COM objects that deliberately reproduce
the real failures measured on the V15 workstation:

  * Water is read back as 'H2O', and Reactants.Add('Water') raises E_FAIL while
    Add('H2O') succeeds (size and spelling sensitive).
  * Writing a stream while the basis is still being edited raises access denied.
  * Gibbs Ln(K) cannot be set: LnKSource stays at FixedK = 2.
  * A non-converged reactor leaves a negative product flow.

Because the fakes reproduce those behaviours, a passing suite means something: the
tool layer resolves names, respects the basis-edit ordering, refuses the
equilibrium path it cannot actually configure, and rejects non-converged output.

These are not HYSYS results. Run:  python -m hysys_tools.selfcheck
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
from pathlib import Path

from . import core, validate
from .main import build_case

# --------------------------------------------------------------------- errors
class FakeComError(Exception):
    pass


E_FAIL = -2147467259
E_ACCESSDENIED = -2147024891

_READBACK = {'water': 'H2O', 'steam': 'H2O'}

# Feed molar flows for the current fake run, keyed by internal component name.
# Set by the suite; deliberately not stored on any fake HYSYS object.
_FEED_VALUES: dict = {}


def _readback_name(name: str) -> str:
    return _READBACK.get(str(name).strip().casefold(), str(name))


class FakeComponents:
    def __init__(self, editing_flag=None):
        self._items = []
        self._editing_flag = editing_flag

    def Add(self, name):
        self._items.append(_readback_name(name))

    @property
    def Count(self):
        return len(self._items)

    def Item(self, key):
        if isinstance(key, int):
            return _FakeComponent(self._items[key])
        for name in self._items:
            if name.casefold() == str(key).casefold():
                return _FakeComponent(name)
        raise FakeComError('component not found: %r (have %s)'
                           % (key, self._items))


class _FakeComponent:
    def __init__(self, name):
        self.Name = name
        self.GibbsTminValue = 25.0
        self.GibbsTmaxValue = 1500.0

    def EvaluateGibbs(self, kelvin):
        return -1000.0 + kelvin


class FakeReactants:
    def __init__(self, reaction):
        self._reaction = reaction
        self._items = {}

    def Add(self, key):
        available = self._reaction.available_components
        match = None
        for name in available:
            if name == str(key):
                match = name
                break
        if match is None:
            raise FakeComError('Reactants.Add: component %r not recognised; '
                               'HYSYS expects the exact readback name' % key)
        self._items[match] = _FakeReactant(match)

    def Item(self, key):
        if isinstance(key, int):
            return list(self._items.values())[key]
        if str(key) not in self._items:
            raise FakeComError('reactant not present: %r' % key)
        return self._items[str(key)]

    @property
    def Count(self):
        return len(self._items)


class _FakeReactant:
    def __init__(self, name):
        self.ReactantStoichCoefValue = 0.0
        self.Component = _FakeComponent(name)


class FakeReaction:
    TYPE_NAME = 'ConversionReaction'

    def __init__(self, name, available_components, kind):
        self.Name = name
        self.TypeName = kind
        self.available_components = available_components
        self.Reactants = FakeReactants(self)
        self.ConversionCoefficients_ = [0.0, 0.0, 0.0]
        self.ReactionPhase_ = 0
        self.BalanceErrorValue = 0.0
        self.AutoDetect = True
        self.Basis = 1
        self.BasisUnits2 = 'bar'
        self.TemperatureApproachValue = 0.0
        self.LnKSource_ = 2                        # FixedK by default

    # measured behaviour: writes are accepted but never take effect
    @property
    def LnKSource(self):
        return self.LnKSource_

    @LnKSource.setter
    def LnKSource(self, value):
        if int(value) in (1,2,3,4):
            self.LnKSource_ = int(value)

    @property
    def ConversionCoefficientsValue(self):
        return tuple(self.ConversionCoefficients_)

    @ConversionCoefficientsValue.setter
    def ConversionCoefficientsValue(self, values):
        self.ConversionCoefficients_ = list(values)

    @property
    def ReactionPhase(self):
        return self.ReactionPhase_

    @ReactionPhase.setter
    def ReactionPhase(self, value):
        self.ReactionPhase_ = int(value)

    @property
    def _oleobj_(self):
        raise FakeComError('type information is not available in the fake')


class FakeReactions:
    def __init__(self, available_components):
        self._items = {}
        self._available = available_components

    def Add(self, name, kind):
        if kind == 'conversionrxn':
            type_name = 'ConversionReaction'
        elif kind == 'equilibriumrxn':
            type_name = 'EquilibriumReaction'
        else:
            raise FakeComError('unknown reaction factory: %r' % kind)
        self._items[name] = FakeReaction(name, self._available, type_name)

    def Item(self, key):
        return self._items[key]

    @property
    def Count(self):
        return len(self._items)


class FakeActiveReactions:
    def __init__(self):
        self._items = []

    def Add(self, reaction):
        self._items.append(reaction)

    @property
    def Count(self):
        return len(self._items)


class FakeReactionSet:
    def __init__(self, name):
        self.Name = name
        self.ActiveReactions = FakeActiveReactions()

    def AssociateFluidPackage(self, package):
        return None


class FakeReactionSets:
    def __init__(self):
        self._items = {}

    def Add(self, name):
        self._items[name] = FakeReactionSet(name)

    def Item(self, key):
        return self._items[key]


class FakeReactionPackageManager:
    def __init__(self, available_components):
        self.Reactions = FakeReactions(available_components)
        self.ReactionSets = FakeReactionSets()


class FakeVariable:
    def __init__(self, value=0.0):
        self.value = float(value)
        self.IsKnown = True
        self.on_set = None

    def SetValue(self, value, unit):
        self.value = float(value)
        if self.on_set is not None:
            self.on_set()

    def GetValue(self, unit):
        return self.value


class FakeStream:
    def __init__(self, name, basis):
        self.Name = name
        self._basis = basis
        self.ComponentMolarFraction = type('C', (), {'Values': []})()
        self.Temperature = FakeVariable(25.0)
        self.Pressure = FakeVariable(101.325)
        self.MolarFlow = FakeVariable(0.0)
        self.MassFlow = FakeVariable(0.0)
        # Measured behaviour: setting a mass flow makes HYSYS derive the molar flow
        # from its own molecular weights. The fake mirrors that so a driver that
        # only sets MolarFlow would diverge from the verified script.
        self.MassFlow.on_set = self._derive_molar_flow

    def _derive_molar_flow(self):
        values = list(getattr(self.ComponentMolarFraction, 'Values', []) or [])
        names = list(self._basis.components._items)
        if not values or len(values) != len(names):
            return
        average = sum(f * core.molar_mass_of(n) for f, n in zip(values, names))
        if average > 0:
            self.MolarFlow.value = self.MassFlow.value / average

    def write(self):
        # measured behaviour: writing a stream while the basis is being edited
        # raises access denied
        if self._basis.editing:
            raise FakeComError('access denied (0x%08X): cannot write a stream while '
                               'the basis is being edited' % (E_ACCESSDENIED & 0xFFFFFFFF))


class FakeStreams:
    def __init__(self, basis):
        self._basis = basis
        self._items = {}

    def Add(self, name):
        self._items[name] = FakeStream(name, self._basis)

    def Item(self, key):
        return self._items[key]

    @property
    def Count(self):
        return len(self._items)


class FakeOperation:
    # When True every operation returns its feed unchanged. Reproduces the failure
    # measured on the workstation: the reactor solved, but the reaction never took
    # effect, so the outlet equalled the inlet and the conversion came out as 0%.
    passthrough = False

    def __init__(self, name, factory, streams, converged=True):
        self.Name = name
        self.factory = factory
        self.TypeName = {'conversionreactorop': 'ConversionReactor',
                         'equilibriumreactorop': 'EquilibriumReactor',
                         'gibbsreactorop': 'GibbsReactor'}.get(factory, factory)
        self._streams = streams
        self._ignored = True
        self.converged = converged
        self.HeatFlow = FakeVariable(0.0)
        self.PressureDrop = FakeVariable(0.0)
        self._feeds = []
        self.Feeds = type('F', (), {
            'Add': lambda s, x: self._feeds.append(x),
            'Count': property(lambda s: len(self._feeds)),
        })()
        self.ReactionSet = None
        self._vapour = None
        self._liquid = None
        self._composition = None
        self.EnergyStream = None

    @property
    def IsIgnored(self):
        return self._ignored

    @IsIgnored.setter
    def IsIgnored(self, value):
        # Measured behaviour: an ignored operation leaves its product streams at the
        # HYSYS "no value" sentinel; only clearing IsIgnored makes it compute. The
        # fake mirrors that so forgetting to clear it fails here, not on the remote.
        object.__setattr__(self, '_ignored', bool(value))
        self._composition = None
        if self._vapour is not None:
            self._fill(self._vapour, vapour=True)

    @property
    def VapourProduct(self):
        return self._vapour

    @VapourProduct.setter
    def VapourProduct(self, stream):
        self._vapour = stream
        self._fill(stream, vapour=True)

    @property
    def LiquidProduct(self):
        return self._liquid

    @LiquidProduct.setter
    def LiquidProduct(self, stream):
        self._liquid = stream

    def _fill(self, stream, vapour):
        if not vapour:
            stream.MolarFlow.value = 0.0
            return
        if self._ignored:
            # HYSYS unknown sentinel: the operation has not been solved.
            stream.MolarFlow.value = -32767.0
            return
        if not self.converged:
            stream.MolarFlow.value = -1.0        # non-converged marker
            return
        composition = self._cached_composition()
        names = list(self._streams._basis.components._items)
        total = sum(composition.get(core.canonical(n), 0.0) for n in names)
        if __import__('os').environ.get('SELFCHECK_TRACE'):
            print('[TRACE-FILL] names=%s composition=%s total=%s'
                  % (names, composition, total))
        stream.ComponentMolarFraction = type('C', (), {
            'Values': [composition.get(core.canonical(n), 0.0) / total
                       for n in names]})()
        stream.MolarFlow.value = total
        if self._feeds:
            stream.Pressure.value = self._feeds[0].Pressure.value - self.PressureDrop.value
        # Mass flow from library molar masses, so the mass balance in the tool
        # layer checks real numbers rather than a fabricated constant.
        stream.MassFlow.value = sum(
            composition.get(core.canonical(n), 0.0) * core.molar_mass_of(n)
            for n in names)

    def _cached_composition(self):
        if self._composition is None:
            self._composition = self._outlet_composition()
        return self._composition

    def _outlet_composition(self):
        """Stoichiometric outlet, keyed by INTERNAL name.

        Keyed internally because the reader looks values up with
        ``core.canonical``; returning HYSYS spellings here would never match and
        every lookup would silently yield zero.

        The feed values come from ``_FEED_VALUES``, set by the suite before each
        run, rather than from a HYSYS object: attaching a Python attribute to a real
        COM object is what made Feeds.Add fail on the workstation, so the fakes must
        not depend on that mechanism either.
        """
        global _FEED_VALUES
        names = list(self._streams._basis.components._items)
        feed = _FEED_VALUES or {}
        if self.passthrough:
            # Outlet identical to the inlet: the reaction never took effect.
            return {core.canonical(n): float(feed.get(core.canonical(n), 0.0))
                    for n in names}

        def get(wanted):
            total = 0.0
            for name, amount in feed.items():
                if core.canonical(name) == wanted:
                    total += float(amount)
            return total

        methane = get('methane')
        steam = get('water')
        toluene = get('toluene')
        carbon = get('carbon')
        moles = {core.canonical(n): 0.0 for n in names}
        if methane > 0:
            converted = methane * 0.54
            z = converted * 0.35
            moles.update({'methane': methane - converted,
                          'water': steam - converted - z,
                          'carbon monoxide': converted - z,
                          'hydrogen': 3 * converted + z,
                          'carbon dioxide': z})
        elif toluene > 0:
            converted = toluene * 0.5
            moles['toluene'] = toluene - converted
            moles['benzene'] = converted / 2
            for xylene in ('o-xylene', 'm-xylene', 'p-xylene'):
                moles[xylene] = converted / 6
        elif carbon > 0:
            converted = carbon * 0.9
            z = converted * 0.2
            moles.update({'carbon': carbon - converted,
                          'water': steam - converted - z,
                          'carbon monoxide': converted - z,
                          'hydrogen': converted + z,
                          'carbon dioxide': z})
        return {k: v for k, v in moles.items() if k in
                {core.canonical(n) for n in names}}


class FakeOperations:
    def __init__(self, streams, converged=True):
        self._items = {}
        self._streams = streams
        self._converged = converged

    def Add(self, name, factory):
        self._items[name] = FakeOperation(name, factory, self._streams,
                                          converged=self._converged)

    def Item(self, key):
        return self._items[key]

    @property
    def Count(self):
        return len(self._items)


class FakeEnergyStreams:
    def Add(self, name):
        pass

    def Item(self, key):
        return type('E', (), {'Name': key})()


class FakeFlowsheet:
    def __init__(self, basis, converged=True):
        self._basis = basis
        self.MaterialStreams = FakeStreams(basis)
        self.EnergyStreams = FakeEnergyStreams()
        self.Operations = FakeOperations(self.MaterialStreams, converged=converged)
        self.FluidPackage = type('P', (), {'Components': basis.components})()


class FakeSolver:
    def __init__(self):
        self.IsSolving = False
        self.CanSolve = True


class FakeBasis:
    def __init__(self, components):
        self.editing = False
        self.components = components
        self.CanEndBasisChange = True
        self.FluidPackages = FakeFluidPackages(components)
        self.ReactionPackageManager = FakeReactionPackageManager(components._items)
        self.feed_values = None

    def StartBasisChange(self):
        self.editing = True

    def EndBasisChange(self):
        self.editing = False


class FakeFluidPackages:
    def __init__(self, components):
        self._components = components
        self._items = []

    def Add(self, name):
        self._items.append(FakePackage(self._components))

    @property
    def Count(self):
        return len(self._items)

    def Item(self, index):
        return self._items[index]


class FakePackage:
    def __init__(self, components):
        self.PropertyPackageName = 'PengRob'
        self.Components = components


class FakeCase:
    def __init__(self, converged=True, registry=None):
        self._registry = registry
        self.Name = '<unnamed>'
        self.close_calls = []
        components = FakeComponents()
        basis = FakeBasis(components)
        # the flowsheet and the basis share one component container, as in HYSYS
        self.Flowsheet = FakeFlowsheet(basis, converged=converged)
        basis.flowsheet = self.Flowsheet
        self.BasisManager = basis
        self.Solver = FakeSolver()

    def SaveAs(self, path):
        Path(path).write_text('fake case', encoding='utf-8')

    def Close(self, save_changes=True, file_name=None):
        """Mirror SimulationCase.Close(SaveChanges, FileName).

        Both arguments are optional VARIANTs in the real interface. The tool layer
        passes SaveChanges=False so HYSYS does not put up a save prompt, which would
        block the COM call.
        """
        self.close_calls.append({'save_changes': save_changes,
                                 'file_name': file_name})
        if self._registry is not None:
            self._registry._unregister(self, 'Close')


class _RemovableCases:
    """Case registry shared by every fake HYSYS application.

    Mirrors the real SimulationCases collection: Count, Add and Remove, plus the
    per-case Close(SaveChanges, FileName) that the tool layer prefers. The fakes
    record what was closed so the suite can assert the right case was closed rather
    than merely that no exception was raised.
    """

    def _init_case_registry(self):
        self._cases = []
        self.closed_cases = []

    def Add(self, path):
        case = FakeCase(converged=getattr(self, '_converged', True), registry=self)
        # Measured on the real workstation: case.Name reads back as "Case" for every
        # open case, whatever the file is called. A fake that returned the file stem
        # would hide exactly the failure that matters - matching a case by a name we
        # construct does not work - so the fake reports "Case" too.
        case.Name = 'Case'
        self._cases.append(case)
        return case

    @property
    def Count(self):
        return len(self._cases)

    def Remove(self, name):
        for case in list(self._cases):
            if str(getattr(case, 'Name', '')) == str(name):
                self._cases.remove(case)
                self.closed_cases.append(str(name))
                return
        raise FakeComError('Remove: no open case named %r' % (name,))

    def _unregister(self, case, how):
        if case in self._cases:
            self._cases.remove(case)
            self.closed_cases.append(str(getattr(case, 'Name', '?')))
            return True
        return False


class FakeApplication(_RemovableCases):
    Name = 'Fake HYSYS (offline suite)'

    def __init__(self, converged=True):
        self.SimulationCases = self
        self._converged = converged
        self._init_case_registry()


class ForeignApplicationForStale(_RemovableCases):
    """Application whose Add hands back an already-populated (stale) case.

    Models the real behaviour when an output folder is reused: HYSYS resolves the
    duplicate case file path against a case that is still open.
    """

    Name = 'Fake HYSYS (stale case)'

    def __init__(self):
        self.SimulationCases = self
        self._init_case_registry()

    def Add(self, path):
        case = FakeCase(registry=self)
        case.Name = 'Case'
        case.BasisManager.FluidPackages.Add('PRE-EXISTING-PACKAGE')
        self._cases.append(case)
        return case


class PythonComStub:
    VT_ARRAY = 0x2000
    VT_R8 = 5
    VT_PTR = 26
    VT_I4 = 3
    VT_VOID = 24
    DISPATCH_PROPERTYPUT = 8

    @staticmethod
    def CoInitialize():
        pass

    @staticmethod
    def CoUninitialize():
        pass


class Win32ComStub:
    class client:
        @staticmethod
        def VARIANT(kind, values):
            return list(values)

        @staticmethod
        def GetActiveObject(progid):
            return FakeApplication()


# ------------------------------------------------------------------- fixtures
SPEC_TOULENE = {
    'schema': core.SPEC_SCHEMA,
    'case_name': 'agent-toluene',
    'fluid_package': {
        'name': 'TOL-PR', 'property_package': 'PengRob',
        'components': ['Toluene', 'Benzene', 'o-Xylene', 'm-Xylene', 'p-Xylene'],
    },
    'feeds': [{
        'name': 'FEED', 'basis': 'molar_fraction',
        'fractions': {'Toluene': 1.0},
        'total_flow': 10000.0, 'total_flow_unit': 'kg/h',
        'temperature': 380.0, 'temperature_unit': 'C',
        'pressure': 2.5, 'pressure_unit': 'MPa',
    }],
    'reactions': [{
        'name': 'TOL-DISPROP',
        'stoichiometry': {'Toluene': -2.0, 'Benzene': 1.0, 'o-Xylene': 1 / 3,
                          'm-Xylene': 1 / 3, 'p-Xylene': 1 / 3},
        'conversion_percent': 50.0, 'base_component': 'Toluene', 'phase': 'combined',
    }],
    'reactor': {'name': 'TOL-RX', 'kind': 'conversion', 'thermal_mode': 'adiabatic',
                'pressure_drop_kPa': 0.0},
}

SPEC_SMR = {
    'schema': core.SPEC_SCHEMA,
    'case_name': 'agent-smr-710',
    'fluid_package': {
        'name': 'SMR-PR', 'property_package': 'PengRob',
        'components': ['Methane', 'Water', 'CO', 'Hydrogen', 'CO2'],
    },
    'feeds': [{
        'name': 'FEED', 'basis': 'molar_flow',
        'flows': {'Methane': 1000.0, 'Water': 2700.0},
        'temperature': 520.0, 'temperature_unit': 'C',
        'pressure': 13.5, 'pressure_unit': 'bar',
    }],
    'reactions': [{
        'name': 'SMR', 'stoichiometry': {'Methane': -1.0, 'Water': -1.0, 'CO': 1.0,
                                         'Hydrogen': 3.0},
        'conversion_percent': 54.0, 'base_component': 'Methane', 'phase': 'vapour',
    }],
    'reactor': {'name': 'SMR-RX', 'kind': 'gibbs', 'thermal_mode': 'isothermal',
                'outlet_temperature': 710.0, 'outlet_temperature_unit': 'C',
                'pressure_drop_kPa': 0.0},
}

SPEC_EQUILIBRIUM = json.loads(json.dumps(SPEC_SMR))
SPEC_EQUILIBRIUM['reactor']['kind'] = 'equilibrium'
SPEC_EQUILIBRIUM['case_name'] = 'agent-equilibrium'


# ---------------------------------------------------------------------- checks
class Results:
    def __init__(self):
        self.passed = 0
        self.failed = []

    def check(self, label, condition, detail=''):
        if condition:
            self.passed += 1
            print('  PASS  %s' % label)
        else:
            self.failed.append(label)
            print('  FAIL  %s %s' % (label, detail))

    def report(self):
        print('')
        print('%d passed, %d failed' % (self.passed, len(self.failed)))
        for label in self.failed:
            print('  failed: %s' % label)
        return 0 if not self.failed else 1


def run_spec(spec, folder, app):
    """Run a spec against the fakes, feeding them the spec's molar flows.

    The feed is injected here rather than read from a HYSYS object, mirroring the
    production driver, which no longer writes any Python attribute onto COM objects.
    """
    global _FEED_VALUES
    _FEED_VALUES = {core.canonical(k): float(v)
                    for k, v in core.feed_molar_flows(spec['feeds'][0]).items()}
    return build_case(spec, folder, PythonComStub, Win32ComStub, app)


def run_offline_checks() -> int:
    # Keep the suite quick. The non-converged fake would otherwise wait out the full
    # solver timeout on every run that uses it, which is what pushed this suite to
    # about three minutes. Only the offline suite is affected; a real run keeps the
    # real timeout.
    from . import reactor as reactor_module
    reactor_module.SOLVER_TIMEOUT_SECONDS = 2.0

    results = Results()
    pythoncom = PythonComStub
    win32com = Win32ComStub

    print('== pure logic ==')
    # name resolution must go through the readback names
    results.check('Water resolves to the H2O readback name',
                  core.resolve_in_readback('Water', ['Methane', 'H2O']) == 'H2O')
    results.check('an unknown component resolves to None',
                  core.resolve_in_readback('Xenon', ['Methane']) is None)
    results.check('unit conversion MPa -> kPa',
                  abs(core.to_kpa(2.5, 'MPa') - 2500.0) < 1e-9)
    results.check('unit conversion bar -> kPa',
                  abs(core.to_kpa(13.5, 'bar') - 1350.0) < 1e-9)
    results.check('SMR equation balances',
                  core.stoichiometry_balance({'Methane': -1, 'Water': -1, 'CO': 1,
                                              'Hydrogen': 3})['balanced'])
    results.check('an unbalanced equation is detected',
                  not core.stoichiometry_balance({'Methane': -1, 'CO': 1})['balanced'])
    results.check('mass_fraction with Nm3/h is rejected',
                  _raises(lambda: core.feed_molar_flows(
                      {'basis': 'mass_fraction', 'fractions': {'Coal': 0.62},
                       'total_flow': 80000, 'total_flow_unit': 'Nm3/h'})))
    results.check('molar_flow basis converts directly',
                  abs(core.feed_molar_flows(
                      {'basis': 'molar_flow',
                       'flows': {'Methane': 1000.0}})['Methane'] - 1000.0) < 1e-12)
    results.check('molar_fraction + a kg/h total converts through molar mass',
                  abs(core.feed_molar_flows(
                      {'basis': 'molar_fraction', 'fractions': {'Toluene': 1.0},
                       'total_flow': 10000.0, 'total_flow_unit': 'kg/h'}
                  )['Toluene'] - 10000.0 / core.molar_mass_of('Toluene')) < 1e-9)
    # A MIXTURE with molar fractions and a mass total: the fractions are molar, so the
    # mass total must be turned into a total molar flow through the MIXTURE molar mass.
    # Dividing each fraction by that component's own molar mass instead treats molar
    # fractions as mass fractions, which gave 31.17 / 27.75 kmol/h (and a 0.529/0.471
    # composition) for an equimolar CH4/H2O feed of 1000 kg/h, silently.
    mixture = core.feed_molar_flows({'basis': 'molar_fraction',
                                     'fractions': {'Methane': 0.5, 'Water': 0.5},
                                     'total_flow': 1000.0,
                                     'total_flow_unit': 'kg/h'})
    average = (0.5 * core.molar_mass_of('Methane')
               + 0.5 * core.molar_mass_of('Water'))
    expected_each = 1000.0 / average * 0.5
    results.check('an equimolar mixture with a mass total splits evenly',
                  abs(mixture['Methane'] - expected_each) < 1e-9
                  and abs(mixture['Water'] - expected_each) < 1e-9,
                  str({k: round(v, 4) for k, v in mixture.items()}))
    mixture_total = sum(mixture.values())
    results.check('the composition handed to HYSYS is the stated 50/50',
                  abs(mixture['Methane'] / mixture_total - 0.5) < 1e-9,
                  str({k: round(v / mixture_total, 6)
                       for k, v in mixture.items()}))
    results.check('an equimolar mixture does not favour the lighter component',
                  abs(mixture['Methane'] - mixture['Water']) < 1e-9,
                  str({k: round(v, 4) for k, v in mixture.items()}))
    # The same mixture expressed as MASS fractions must still use each component's own
    # molar mass - that path was always correct and must not regress.
    by_mass = core.feed_molar_flows({'basis': 'mass_fraction',
                                     'fractions': {'Methane': 0.5, 'Water': 0.5},
                                     'total_flow': 1000.0,
                                     'total_flow_unit': 'kg/h'})
    results.check('mass fractions still convert per component',
                  abs(by_mass['Methane']
                      - 500.0 / core.molar_mass_of('Methane')) < 1e-9
                  and by_mass['Methane'] > by_mass['Water'],
                  str({k: round(v, 4) for k, v in by_mass.items()}))
    results.check('mass_fraction -> molar flow for toluene',
                  abs(core.feed_molar_flows(
                      {'basis': 'mass_fraction', 'fractions': {'Toluene': 1.0},
                       'total_flow': 10000.0, 'total_flow_unit': 'kg/h'}
                  )['Toluene'] - 10000.0 / core.molar_mass_of('Toluene')) < 1e-9)
    # Remote benchmark: HYSYS read a 10000 kg/h toluene feed back as
    # 108.52955420760267 kmol/h. Reproducing that matters because every downstream
    # flow is scaled from it.
    measured = core.feed_molar_flows(
        {'basis': 'molar_fraction', 'fractions': {'Toluene': 1.0},
         'total_flow': 10000.0, 'total_flow_unit': 'kg/h'})['Toluene']
    results.check('toluene feed matches the measured HYSYS molar flow',
                  abs(measured - 108.52955420760267) / 108.52955420760267 < 1e-6,
                  '%.10f vs 108.52955420760267' % measured)

    print('')
    print('== conversion reactor (toluene) ==')
    folder = Path(tempfile.mkdtemp(prefix='agent-toluene-'))
    result = run_spec(SPEC_TOULENE, folder, FakeApplication())
    results.check('status PASS', result.get('status') == 'PASS',
                  str(result.get('error'))[:200])
    results.check('case file written', (folder / 'agent-toluene.hsc').is_file())
    results.check('result.json written', (folder / 'result.json').is_file())
    checks = result.get('checks') or {}
    errors = checks.get('element_relative_error') or {}
    results.check('C/H element balance closed',
                  all(abs(v) < 1e-12 for v in errors.values()), str(errors))
    conversions = checks.get('specified_conversion') or []
    checked = [c for c in conversions if c.get('checked')]
    results.check('specified conversion was verified independently',
                  bool(checked), str(conversions))
    if checked:
        results.check('measured conversion matches 50%',
                      abs(checked[0]['measured_percent'] - 50.0) < 0.05,
                      str(checked[0]))

    print('')
    print('== gibbs reactor (reforming, isothermal 710 C) ==')
    folder = Path(tempfile.mkdtemp(prefix='agent-smr-'))
    smr_fake = FakeApplication()
    result = run_spec(SPEC_SMR, folder, smr_fake)
    results.check('status PASS', result.get('status') == 'PASS',
                  str(result.get('error'))[:300])
    checks = result.get('checks') or {}
    errors = checks.get('element_relative_error') or {}
    results.check('C/H/O element balance closed',
                  all(abs(v) < 1e-12 for v in errors.values()), str(errors))
    co = checks.get('co_yield')
    results.check('CO yield block present when carbon is fed', co is not None)
    if co:
        results.check('CO yield uses carbon fed as the denominator',
                      co['carbon_fed_kmol_h'] > 0 and
                      abs(co['co_yield_percent'] -
                          co['co_produced_kmol_h'] / co['carbon_fed_kmol_h'] * 100)
                      < 1e-9)

    print('')
    print('== equilibrium with inconsistent fake Gibbs data is rejected by Q/K ==')
    folder = Path(tempfile.mkdtemp(prefix='agent-eq-'))
    result = run_spec(SPEC_EQUILIBRIUM, folder, FakeApplication())
    results.check('status FAILED', result.get('status') == 'FAILED',
                  str(result.get('status')))
    message = str(result.get('error', ''))
    results.check('error explains the failed Q/K check',
                  'Q/K' in message, message[:200])
    results.check('wrong equilibrium result is classified as a result failure',
                  result.get('error_type') == 'result_check', message[:200])

    print('')
    print('== non-converged output is rejected ==')
    folder = Path(tempfile.mkdtemp(prefix='agent-nonconv-'))
    result = run_spec(SPEC_TOULENE, folder, FakeApplication(converged=False))
    results.check('status FAILED', result.get('status') == 'FAILED',
                  str(result.get('status')))
    results.check('a non-converged case is not reported as PASS',
                  result.get('checks') is None)

    print('')
    print('== basis-edit ordering is enforced ==')
    # If the driver wrote streams before EndBasisChange, the fake raises access
    # denied, so a PASS above already proves the ordering. Assert it positively:
    folder = Path(tempfile.mkdtemp(prefix='agent-order-'))
    result = run_spec(SPEC_TOULENE, folder, FakeApplication())
    steps = [s['step'] for s in result.get('steps', [])]
    results.check('configure_basis precedes stream creation',
                  steps.index('configure_basis') < steps.index('create_streams_and_reactor'),
                  str(steps))

    print('')
    print('== the reactor is reactivated before solving ==')
    # Regression guard: an operation left in the ignored state returns the HYSYS
    # unknown sentinel (-32767) on its product streams, which reads like a negative
    # flow. Forgetting to clear IsIgnored cost one whole remote run.
    ignored_at_fill = []

    class WatchingOperation(FakeOperation):
        def __setattr__(self, key, value):
            if key == 'IsIgnored' and getattr(self, '_ignored', True) is not False:
                # remember what the flag was when the product was attached
                pass
            object.__setattr__(self, key, value)

    class WatchingOperations(FakeOperations):
        def Add(self, name, factory):
            operation = WatchingOperation(name, factory, self._streams,
                                          converged=self._converged)
            original_fill = operation._fill

            def recording_fill(stream, vapour):
                if vapour:
                    ignored_at_fill.append(operation._ignored)
                return original_fill(stream, vapour)

            operation._fill = recording_fill
            self._items[name] = operation

    class WatchingFlowsheet(FakeFlowsheet):
        def __init__(self, basis, converged=True):
            self._basis = basis
            self.MaterialStreams = FakeStreams(basis)
            self.EnergyStreams = FakeEnergyStreams()
            self.Operations = WatchingOperations(self.MaterialStreams,
                                                 converged=converged)
            self.FluidPackage = type('P', (), {'Components': basis.components})()

    class WatchingCase(FakeCase):
        def __init__(self, converged=True):
            components = FakeComponents()
            basis = FakeBasis(components)
            self.Flowsheet = WatchingFlowsheet(basis, converged=converged)
            basis.flowsheet = self.Flowsheet
            self.BasisManager = basis
            self.Solver = FakeSolver()

    class WatchingApplication:
        Name = 'Fake HYSYS (watching IsIgnored)'

        def __init__(self):
            self.SimulationCases = self

        def Add(self, path):
            return WatchingCase()

    folder = Path(tempfile.mkdtemp(prefix='agent-ignored-'))
    result = run_spec(SPEC_TOULENE, folder, WatchingApplication())
    results.check('build still passes with the watching fake',
                  result.get('status') == 'PASS', str(result.get('error'))[:200])
    results.check('the first product fill happens while still ignored (as intended)',
                  bool(ignored_at_fill) and ignored_at_fill[0] is True,
                  str(ignored_at_fill))
    results.check('clearing IsIgnored triggers a recompute (not left at the sentinel)',
                  False in ignored_at_fill, str(ignored_at_fill))
    steps = [s['step'] for s in result.get('steps', [])]
    results.check('solve step ran, so IsIgnored was cleared before it',
                  'solve_and_read_outputs' in steps, str(steps))

    print('')
    print('== a non-blank case is refused, not silently reused ==')
    # Regression guard for the defect that cost five remote runs: reusing an output
    # folder makes HYSYS resolve the duplicate case path against an already-open
    # case, so the "new" case comes back dirty and Feeds.Add fails with a bare
    # E_FAIL. Detecting it must produce a clear error instead.
    class DirtyCase(FakeCase):
        def __init__(self, converged=True):
            super().__init__(converged=converged)
            # simulate a case that already holds content
            self.Flowsheet.MaterialStreams.Add('LEFTOVER-STREAM')
            self.Flowsheet.Operations.Add('LEFTOVER-RX', 'conversionreactorop')

    class DirtyApplication:
        Name = 'Fake HYSYS (dirty case)'

        def __init__(self):
            self.SimulationCases = self

        def Add(self, path):
            return DirtyCase()

    folder = Path(tempfile.mkdtemp(prefix='agent-dirty-'))
    result = run_spec(SPEC_TOULENE, folder, DirtyApplication())
    results.check('a dirty case is rejected', result.get('status') == 'FAILED',
                  str(result.get('status')))
    message = str(result.get('error', ''))
    results.check('the error names the real cause (not blank / already open)',
                  'not blank' in message and 'open' in message, message[:200])
    results.check('the error suggests the fix (unique folder or close it)',
                  'unique' in message.casefold() or 'close' in message.casefold(),
                  message[:200])

    print('')
    print('== a non-blank basis is refused too ==')
    # The basis check catches the same situation one step earlier.
    class DirtyBasis(FakeBasis):
        def __init__(self, components):
            super().__init__(components)
            self.FluidPackages.Add('PRE-EXISTING-PACKAGE')

    class DirtyBasisCase(FakeCase):
        def __init__(self, converged=True):
            components = FakeComponents()
            basis = DirtyBasis(components)
            self.Flowsheet = FakeFlowsheet(basis, converged=converged)
            basis.flowsheet = self.Flowsheet
            self.BasisManager = basis
            self.Solver = FakeSolver()

    class DirtyBasisApplication:
        Name = 'Fake HYSYS (pre-existing package)'

        def __init__(self):
            self.SimulationCases = self

        def Add(self, path):
            return DirtyBasisCase()

    folder = Path(tempfile.mkdtemp(prefix='agent-dirtybasis-'))
    result = run_spec(SPEC_TOULENE, folder, DirtyBasisApplication())
    results.check('a case with an existing fluid package is rejected',
                  result.get('status') == 'FAILED', str(result.get('status')))
    results.check('the basis error also names the cause',
                  'not blank' in str(result.get('error', '')),
                  str(result.get('error'))[:200])

    print('')
    print('== reported figures cannot be misread ==')
    # A carbon conversion of 100% is meaningless when no solid carbon enters, and it
    # reads like the conversion of the key reactant. The key conversion for each
    # scenario must be present explicitly instead, under the same component names
    # that component_flows_kmol_h uses (HYSYS spelling), not internal keys.
    folder = Path(tempfile.mkdtemp(prefix='agent-figures-'))
    smr = run_spec(SPEC_SMR, folder, FakeApplication())
    smr_checks = smr.get('checks') or {}
    conversions = smr_checks.get('reactant_conversion_percent') or {}
    results.check('reforming reports the methane conversion',
                  'Methane' in conversions and conversions['Methane'] > 0,
                  str(conversions))
    results.check('reforming also reports the steam conversion under the HYSYS name',
                  'H2O' in conversions, str(conversions))
    results.check('conversion keys use the readback spelling, not internal keys',
                  'methane' not in conversions and 'water' not in conversions,
                  str(conversions))
    carbon = (smr_checks.get('co_yield') or {}).get('carbon_conversion_percent',
                                                    'missing')
    results.check('carbon conversion is null when no solid carbon enters',
                  carbon is None, 'got %r' % (carbon,))
    results.check('the null carbon conversion carries an explanation',
                  'carbon_conversion_note' in (smr_checks.get('co_yield') or {}),
                  str(list((smr_checks.get('co_yield') or {}).keys())))
    results.check('an isothermal case states the duty scope',
                  'sensible' in str(smr_checks.get('heat_duty_scope', '')).casefold(),
                  str(smr_checks.get('heat_duty_scope'))[:120])

    folder = Path(tempfile.mkdtemp(prefix='agent-figures-tol-'))
    tol = run_spec(SPEC_TOULENE, folder, FakeApplication())
    tol_checks = tol.get('checks') or {}
    tol_conversions = tol_checks.get('reactant_conversion_percent') or {}
    results.check('toluene reports the toluene conversion',
                  abs(tol_conversions.get('Toluene', 0.0) - 50.0) < 0.05,
                  str(tol_conversions))
    results.check('an adiabatic case states a different duty scope',
                  'adiabatic' in str(tol_checks.get('heat_duty_scope', '')).casefold(),
                  str(tol_checks.get('heat_duty_scope'))[:120])

    print('')
    print('== the case is closed through the case object, not by name ==')
    # Closing by name is not viable: HYSYS reports 'agent-smr-710' for a file called
    # 'agent-smr-710C.hsc' (extension dropped AND the trailing letter dropped), so any
    # name built from the file name fails to match. The object's own Close is used.
    steps = [s['step'] for s in smr.get('steps', [])]
    results.check('the saved case is closed before the run returns',
                  'close_saved_case' in steps, str(steps))
    close_detail = next((s.get('detail') or {} for s in smr.get('steps', [])
                         if s['step'] == 'close_saved_case'), {})
    results.check('closing goes through case.Close',
                  'case.Close' in str(close_detail.get('method', '')),
                  str(close_detail)[:250])
    results.check('SaveChanges=False is passed so no save prompt can block the call',
                  'SaveChanges=False' in str(close_detail.get('method', '')),
                  str(close_detail.get('method')))
    results.check('the case count actually dropped',
                  int(close_detail.get('case_count_change') or 0) >= 1,
                  str(close_detail)[:250])
    # On the workstation case.Name is "Case" for every case, whatever the file is
    # called, so a name built from the file name can never match and closing by name
    # is unworkable. The fake reports the same deliberately.
    readback = close_detail.get('case_name_readback')
    results.check('the readback name is the unhelpful "Case", as on the workstation',
                  readback == 'Case', 'readback=%r' % (readback,))
    results.check('closing works anyway, because it uses the object not the name',
                  'case.Close' in str(close_detail.get('method', '')),
                  str(close_detail.get('method')))

    print('')
    print('== a failed Close never falls back to removing by name ==')
    # Regression guard for a real hazard: case.Name is "Case" for everything, so a
    # fallback of SimulationCases.Remove(case.Name) would call Remove("Case") and
    # could close an unrelated case - possibly the operator's own - while the case
    # count still dropped by one and the log reported success.
    class CloseFailsCase(FakeCase):
        def Close(self, save_changes=True, file_name=None):
            raise FakeComError('simulated Close failure')

    class CloseFailsApplication(_RemovableCases):
        Name = 'Fake HYSYS (Close fails)'

        def __init__(self, converged=True):
            self.SimulationCases = self
            self._converged = converged
            self._init_case_registry()

        def Add(self, path):
            case = CloseFailsCase(converged=self._converged, registry=self)
            case.Name = 'Case'
            self._cases.append(case)
            return case

    folder = Path(tempfile.mkdtemp(prefix='agent-closefail-'))
    failing_app = CloseFailsApplication()
    result = run_spec(SPEC_TOULENE, folder, failing_app)
    results.check('a failed Close does not fail the whole run',
                  result.get('status') == 'PASS', str(result.get('error'))[:200])
    close_detail = next((s.get('detail') or {} for s in result.get('steps', [])
                         if s['step'] == 'close_saved_case'), {})
    results.check('the failed Close is reported as FAILED',
                  close_detail.get('result') == 'FAILED', str(close_detail)[:200])
    results.check('no case was removed by name as a fallback',
                  failing_app.closed_cases == [],
                  'closed_cases=%r' % (failing_app.closed_cases,))
    results.check('the operator is warned that a case may still be open',
                  any('could not be closed' in w for w in result.get('warnings', [])),
                  str(result.get('warnings'))[:200])

    print('')
    print('== a failed build still tidies up its case ==')
    folder = Path(tempfile.mkdtemp(prefix='agent-failcleanup-'))
    failed_app = FakeApplication(converged=False)   # makes solve_and_read fail
    result = run_spec(SPEC_TOULENE, folder, failed_app)
    results.check('the run failed as intended', result.get('status') == 'FAILED',
                  str(result.get('status')))
    steps = [s['step'] for s in result.get('steps', [])]
    results.check('the failed case is saved for inspection',
                  'close_failed_case' in steps
                  and str(result.get('failed_case_file', '')).endswith('-FAILED.hsc'),
                  'failed_case_file=%r steps=%s'
                  % (result.get('failed_case_file'), steps))
    results.check('the failed case is closed, so nothing accumulates',
                  failed_app.closed_cases == ['Case'],
                  'closed_cases=%r' % (failed_app.closed_cases,))

    print('')
    print('== input problems and result problems are distinguishable ==')
    # An agent seeing a result-check failure must not go and edit the spec.
    folder = Path(tempfile.mkdtemp(prefix='agent-errortype-'))
    bad_result = run_spec(SPEC_TOULENE, folder, FakeApplication(converged=False))
    results.check('a solver timeout is not classified as a spec problem',
                  bad_result.get('error_type') in ('result_check', 'runtime'),
                  'error_type=%r' % (bad_result.get('error_type'),))
    invalid_eq = json.loads(json.dumps(SPEC_EQUILIBRIUM))
    invalid_eq['reactions'] = []
    bad_spec = run_spec(invalid_eq, folder, FakeApplication())
    results.check('an unusable specification is classified separately',
                  bad_spec.get('error_type') == 'specification',
                  'error_type=%r' % (bad_spec.get('error_type'),))

    print('')
    print('== a pass-through outlet is caught as a RESULT problem ==')
    # This is the failure that actually happened on the workstation (09:55): the
    # reactor solved, but the reaction never took effect, so the outlet equalled the
    # inlet. The earlier suite only ever exercised a solver timeout, so neither
    # ResultCheckError nor the pass-through hint in the message was ever executed.
    FakeOperation.passthrough = True
    try:
        folder = Path(tempfile.mkdtemp(prefix='agent-passthrough-'))
        passed_through = run_spec(SPEC_TOULENE, folder, FakeApplication())
    finally:
        FakeOperation.passthrough = False
    results.check('the run is rejected', passed_through.get('status') == 'FAILED',
                  str(passed_through.get('status')))
    results.check('it is classified as a result problem, not a spec problem',
                  passed_through.get('error_type') == 'result_check',
                  'error_type=%r, error=%s'
                  % (passed_through.get('error_type'),
                     str(passed_through.get('error'))[:160]))
    message = str(passed_through.get('error', ''))
    results.check('the message says the outlet equalled the inlet',
                  'outlet equals the inlet' in message
                  or 'pass-through' in message.casefold(),
                  message[:240])
    results.check('the message tells the reader the spec was accepted',
                  'specification itself was accepted' in message, message[:240])
    results.check('ResultCheckError is not a SpecError, so except SpecError will not '
                  'swallow it',
                  not issubclass(validate.ResultCheckError, core.SpecError),
                  'MRO: %s' % [c.__name__ for c in validate.ResultCheckError.__mro__])
    # The failed pass-through run should still tidy up after itself.
    results.check('the pass-through case was saved for inspection',
                  str(passed_through.get('failed_case_file', '')).endswith('-FAILED.hsc'),
                  'failed_case_file=%r' % (passed_through.get('failed_case_file'),))

    print('')
    print('== a foreign (non-blank) case is never modified or closed ==')
    # Hazard: when the output folder is reused, SimulationCases.Add returns an
    # already-open case belonging to somebody else. The cleanup path used to save it
    # as -FAILED.hsc and close it, which rewrites its file path and destroys the
    # operator's work - in direct contradiction of "never touch an existing case".
    class ForeignCase(FakeCase):
        def __init__(self, converged=True, registry=None):
            super().__init__(converged=converged, registry=registry)
            # A case somebody else already has open: it has a fluid package.
            self.BasisManager.FluidPackages.Add('SOMEONE-ELSES-PACKAGE')

    class ForeignApplication(_RemovableCases):
        Name = 'Fake HYSYS (foreign case)'

        def __init__(self):
            self.SimulationCases = self
            self._init_case_registry()

        def Add(self, path):
            case = ForeignCase(registry=self)
            case.Name = 'Case'
            self._cases.append(case)
            return case

    folder = Path(tempfile.mkdtemp(prefix='agent-foreign-'))
    foreign_app = ForeignApplication()
    result = run_spec(SPEC_TOULENE, folder, foreign_app)
    results.check('the run is rejected', result.get('status') == 'FAILED',
                  str(result.get('status')))
    results.check('nothing was closed', foreign_app.closed_cases == [],
                  'closed_cases=%r' % (foreign_app.closed_cases,))
    results.check('no -FAILED.hsc was written for a case we do not own',
                  not list(folder.glob('*-FAILED.hsc')),
                  str([p.name for p in folder.iterdir()]))
    results.check('the case was left out of basis-edit state',
                  foreign_app._cases
                  and not getattr(foreign_app._cases[0].BasisManager,
                                  'editing', True),
                  'editing=%r' % (getattr(foreign_app._cases[0].BasisManager,
                                          'editing', None),))
    results.check('the operator is told a foreign case was left alone',
                  any('untouched' in w or 'not blank' in w
                      for w in result.get('warnings', [])),
                  str(result.get('warnings'))[:240])

    print('')
    print('== offline pre-flight catches spec problems before any COM call ==')
    # Everything here used to surface only after a case was built, and sometimes only
    # after the solver ran, with misleading classifications.
    from . import precheck

    good = precheck.validate_spec(SPEC_TOULENE)
    results.check('a valid spec passes', good['ok'], str(good['errors']))
    results.check('the pre-flight reports what it derived',
                  'Toluene' in str(good['readback'].get('feed_molar_flows_kmol_h')),
                  str(good['readback'])[:200])

    bad = json.loads(json.dumps(SPEC_TOULENE))
    bad['feeds'].append({'name': 'EXTRA', 'basis': 'molar_flow',
                         'flows': {'Methane': 1.0}, 'temperature': 25,
                         'temperature_unit': 'C', 'pressure': 1,
                         'pressure_unit': 'bar'})
    bad['feeds'][0]['fractions'] = {'Toluene': 0.7}      # does not sum to 1
    del bad['feeds'][0]['temperature']                   # required field missing
    bad['reactor']['kind'] = 'nonsense'                  # not a supported kind
    report = precheck.validate_spec(bad)
    results.check('it fails', not report['ok'])
    joined = ' | '.join(report['errors'])
    results.check('a second feed is reported rather than silently ignored',
                  '2 entries' in joined, joined[:200])
    results.check('fractions that do not sum to 1 are reported',
                  'sum to 0.7' in joined, joined[:200])
    results.check('a missing required field is reported',
                  'temperature is required' in joined, joined[:200])
    results.check('an unsupported reactor kind is reported',
                  'not supported' in joined, joined[:200])
    results.check('every problem is reported at once, not just the first',
                  len(report['errors']) >= 4, '%d errors: %s'
                  % (len(report['errors']), joined))
    results.check('a missing field is NOT classified as a runtime KeyError',
                  'KeyError' not in joined, joined[:200])

    # A feed component missing from the fluid package is a spec problem, not something
    # to discover as a conservation failure after the simulation.
    mismatched = json.loads(json.dumps(SPEC_TOULENE))
    mismatched['feeds'][0]['fractions'] = {'Methane': 1.0}
    report = precheck.validate_spec(mismatched)
    results.check('a feed component missing from the package is reported up front',
                  any('not in fluid_package.components' in e
                      for e in report['errors']),
                  str(report['errors'])[:240])

    # An unbalanced reaction must be caught offline, not by HYSYS.
    unbalanced = json.loads(json.dumps(SPEC_TOULENE))
    unbalanced['reactions'][0]['stoichiometry'] = {'Toluene': -1.0, 'Benzene': 1.0}
    report = precheck.validate_spec(unbalanced)
    results.check('an unbalanced reaction is reported offline',
                  any('not element balanced' in e for e in report['errors']),
                  str(report['errors'])[:240])

    # equilibrium is REFUSED up front, not merely warned about: the path cannot work
    # on this workstation, so a warning would waste a case build before failing.
    eq = json.loads(json.dumps(SPEC_TOULENE))
    eq['reactor']['kind'] = 'equilibrium'
    report = precheck.validate_spec(eq)
    results.check('the unusable equilibrium path is refused before a case is built',
                  not report['ok']
                  and any('isothermal' in e for e in report['errors']),
                  str(report['errors'])[:240])
    results.check('the refusal explains the missing thermal boundary',
                  any('outlet_temperature' in e for e in report['errors']),
                  str(report['errors'])[:240])
    gibbs = json.loads(json.dumps(SPEC_TOULENE))
    gibbs['reactor']['kind'] = 'gibbs'
    report = precheck.validate_spec(gibbs)
    results.check('gibbs with reactions warns they will not be used',
                  any('will not be used' in w for w in report['warnings']),
                  str(report['warnings'])[:240])

    print('')
    print('== combinations that cannot work are refused, not left to fail late ==')
    # Each of these used to pass the pre-check and then fail after a case existed.
    adiabatic_gibbs = json.loads(json.dumps(SPEC_SMR))
    adiabatic_gibbs['reactor']['thermal_mode'] = 'adiabatic'
    report = precheck.validate_spec(adiabatic_gibbs)
    results.check('gibbs + adiabatic is refused (never verified, would time out)',
                  not report['ok']
                  and any('adiabatic' in e for e in report['errors']),
                  str(report['errors'])[:240])

    negative_pressure = json.loads(json.dumps(SPEC_TOULENE))
    negative_pressure['feeds'][0]['pressure'] = -5
    report = precheck.validate_spec(negative_pressure)
    results.check('a negative feed pressure is refused',
                  not report['ok'] and any('positive' in e for e in report['errors']),
                  str(report['errors'])[:240])

    zero_pressure = json.loads(json.dumps(SPEC_TOULENE))
    zero_pressure['feeds'][0]['pressure'] = 0
    results.check('a zero feed pressure is refused',
                  not precheck.validate_spec(zero_pressure)['ok'])

    cold = json.loads(json.dumps(SPEC_TOULENE))
    cold['feeds'][0]['temperature'] = -300
    report = precheck.validate_spec(cold)
    results.check('a temperature below absolute zero is refused',
                  not report['ok']
                  and any('absolute zero' in e for e in report['errors']),
                  str(report['errors'])[:240])

    overridden = json.loads(json.dumps(SPEC_TOULENE))
    overridden['feeds'][0]['molar_mass'] = {'Toluene': 50.0}
    report = precheck.validate_spec(overridden)
    results.check('a molar_mass override is refused with the reason',
                  not report['ok']
                  and any('HYSYS derives molar flows' in e for e in report['errors']),
                  str(report['errors'])[:240])

    print('')
    print('== the pre-check never crashes, whatever the field types are ==')
    # Model-generated specs routinely put the wrong type in a field. A traceback would
    # leave the caller with no JSON at all, and exit code 1 is indistinguishable from
    # "the checks failed".
    import copy as _copy
    base = _copy.deepcopy(SPEC_SMR)

    def mutate(fn):
        spec = _copy.deepcopy(base)
        fn(spec)
        return spec

    bad_type_cases = [
        ('temperature is a string',
         lambda s: s['feeds'][0].update({'temperature': 'hot'})),
        ('pressure is a string',
         lambda s: s['feeds'][0].update({'pressure': 'high'})),
        ('outlet_temperature is a string',
         lambda s: s['reactor'].update({'outlet_temperature': 'warm'})),
        ('total_flow is a string',
         lambda s: s['feeds'][0].update({'basis': 'molar_fraction',
                                         'fractions': {'Methane': 1.0},
                                         'total_flow': 'lots'})),
        ('feeds is an object, not a list', lambda s: s.update({'feeds': {'a': 1}})),
        ('reactor is a string', lambda s: s.update({'reactor': 'gibbs'})),
        ('reactions is a string', lambda s: s.update({'reactions': 'nope'})),
        ('fluid_package is a list', lambda s: s.update({'fluid_package': []})),
        ('components is a string',
         lambda s: s['fluid_package'].update({'components': 'Methane'})),
        ('a stoichiometry coefficient is a string',
         lambda s: (s.update({'reactor': {'kind': 'conversion',
                                          'thermal_mode': 'adiabatic'}}),
                    s.update({'reactions': [{'stoichiometry': {'Methane': 'x',
                                                              'CO': 1.0},
                                             'conversion_percent': 50,
                                             'base_component': 'Methane'}]}))),
        ('conversion_percent is a string',
         lambda s: (s.update({'reactor': {'kind': 'conversion',
                                          'thermal_mode': 'adiabatic'}}),
                    s.update({'reactions': [{'stoichiometry': {'Methane': -1.0,
                                                              'CO': 1.0},
                                             'conversion_percent': 'half',
                                             'base_component': 'Methane'}]}))),
        ('fractions is a list',
         lambda s: s['feeds'][0].update({'basis': 'molar_fraction',
                                         'fractions': [1.0]})),
        ('a pressure unit is a number',
         lambda s: s['feeds'][0].update({'pressure_unit': 5})),
        ('thermal_mode is a number',
         lambda s: s['reactor'].update({'thermal_mode': 7})),
        ('components contains a non-string',
         lambda s: s['fluid_package'].update({'components': [1, 2]})),
        ('the whole spec is a list', lambda s: None),
        ('the whole spec is null', lambda s: None),
    ]
    crashes = []
    for index, (label, fn) in enumerate(bad_type_cases):
        if label == 'the whole spec is a list':
            spec = [1, 2, 3]
        elif label == 'the whole spec is null':
            spec = None
        else:
            spec = mutate(fn)
        try:
            report = precheck.validate_spec(spec)
            if not isinstance(report, dict) or 'errors' not in report:
                crashes.append('%s -> no report' % label)
            elif report['ok']:
                crashes.append('%s -> wrongly accepted' % label)
        except Exception as exc:
            crashes.append('%s -> raised %s: %s' % (label, type(exc).__name__, exc))
    results.check('every malformed spec yields a JSON report instead of a traceback',
                  not crashes, '; '.join(crashes[:4]))
    results.check('all %d malformed cases were rejected' % len(bad_type_cases),
                  not crashes)

    print('')
    print('== an accepted alias is one the executor can actually use ==')
    # The pre-check resolves component and package names through the same mappers the
    # executor uses, so "passes the pre-check" and "runs" cannot disagree.
    aliased = _copy.deepcopy(SPEC_TOULENE)
    aliased['fluid_package']['components'] = ['Toluene', 'Benzene', 'o-Xylene',
                                             'm-Xylene', 'p-Xylene']
    aliased['fluid_package']['property_package'] = 'Peng-Robinson'
    aliased['feeds'][0]['fractions'] = {'Toluene': 1.0}
    report = precheck.validate_spec(aliased)
    results.check('the display spelling Peng-Robinson is accepted',
                  report['ok'], str(report['errors'])[:200])
    results.check('it is resolved to the internal name the executor sends',
                  report['readback'].get('property_package_resolved') == 'PengRob',
                  str(report['readback'].get('property_package_resolved')))
    results.check('an unknown property package is refused, not guessed',
                  not precheck.validate_spec(
                      {**aliased, 'fluid_package': {**aliased['fluid_package'],
                                                    'property_package': 'SRK'}})['ok'])
    for alias, library in (('CH4', 'Methane'), ('steam', 'Water'),
                           ('CO2', 'CO2'), ('methane', 'Methane')):
        # A Gibbs case whose candidates differ from the feed, so the alias under test
        # is the only thing being varied.
        other = [c for c in ('CO', 'Hydrogen', 'CO2', 'Methane', 'Water')
                 if core.canonical(c) != core.canonical(alias)][:2]
        report = precheck.validate_spec({
            **aliased,
            'fluid_package': {**aliased['fluid_package'],
                              'components': [alias] + other},
            'feeds': [{**aliased['feeds'][0], 'fractions': {alias: 1.0}}],
            'reactions': [],
            'reactor': {'kind': 'gibbs', 'thermal_mode': 'isothermal',
                        'outlet_temperature': 700}})
        results.check('the alias %r is accepted and resolves to %r'
                      % (alias, library),
                      report['ok']
                      and report['readback'].get(
                          'components_resolved_to_library_names', [None])[0] == library,
                      str(report['errors'])[:160])

    print('')
    print('== the full run refuses a bad spec without creating a case ==')
    folder = Path(tempfile.mkdtemp(prefix='agent-preflight-'))
    app = FakeApplication()
    result = build_case(bad, folder, PythonComStub, Win32ComStub, app)
    results.check('build_case rejects it', result.get('status') == 'FAILED',
                  str(result.get('status')))
    results.check('classified as a specification problem',
                  result.get('error_type') == 'specification',
                  'error_type=%r' % (result.get('error_type'),))
    results.check('no case was created, so nothing was left open',
                  app._cases == [], '%d case(s)' % len(app._cases))
    results.check('the pre-flight warnings are carried into the result',
                  'precheck' in result, str(list(result))[:200])
    results.check('a spec error is not masked by the connection step',
                  'CANNOT_CONNECT' not in str(result.get('status')),
                  str(result.get('status')))

    print('')
    print('== a stale case is classified as an environment problem ==')
    # Classified by exception type, not by matching the message text, so rewording the
    # error cannot silently break the classification.
    folder = Path(tempfile.mkdtemp(prefix='agent-stale-'))
    stale = run_spec(SPEC_TOULENE, folder, ForeignApplicationForStale())
    results.check('the run is rejected', stale.get('status') == 'FAILED',
                  str(stale.get('status')))
    results.check('classified as environment, not specification',
                  stale.get('error_type') == 'environment',
                  'error_type=%r' % (stale.get('error_type'),))
    results.check('the message tells the caller to use a new folder',
                  'unique output folder' in str(stale.get('error', '')),
                  str(stale.get('error'))[:200])

    print('')
    print('== the stated-basis feed flow (normal_volume) ==')
    # This path used to be exercised nowhere: it reached the workstation untested at
    # this level, and two defects only showed up on the real machine. The conversion is
    # arithmetic on the stated conditions, so it is checkable here.
    from .examples import gasification_native_spec
    from .native_flow import delegates_total, is_normal_volume, mode_of
    from . import precheck as _precheck
    native_spec = gasification_native_spec()
    native_feed = native_spec['feeds'][0]

    native_report = _precheck.validate_spec(native_spec)
    results.check('the stated-basis gasification spec passes the preflight',
                  native_report['ok'], str(native_report.get('errors'))[:200])
    conversion = ((native_report.get('readback') or {})
                  .get('normal_volume_conversion') or {})
    results.check('it converts on the basis the requester stated',
                  abs(conversion.get('molar_volume_m3_per_kmol', 0) - 22.41397) < 1e-4,
                  str(conversion.get('molar_volume_m3_per_kmol')))
    results.check('80000 Nm3/h becomes 3569.20 kmol/h',
                  abs(conversion.get('molar_flow_kmol_h', 0) - 3569.2027) < 0.001,
                  str(conversion.get('molar_flow_kmol_h')))
    results.check('the mode is recognised as delegated, not local',
                  delegates_total(native_feed) and is_normal_volume(native_feed),
                  'mode=%r' % mode_of(native_feed))

    # The unit is checked where the value is used, not only where it is reviewed.
    # Without this, `flow_input='normal_volume'` with kg/h, t/h or a nonsense string
    # was accepted and silently divided by 22.4.
    from .core import feed_molar_flows
    for bad_unit in ('kmol/h', 'kg/h', 't/h', 'Nm3/d', 'nonsense'):
        wrong = dict(native_feed, total_flow_unit=bad_unit)
        results.check('a %s total is refused for a normal-volume feed' % bad_unit,
                      _raises(lambda f=wrong: feed_molar_flows(f)),
                      'accepted, which would divide it by 22.4')

    negative = dict(native_feed, total_flow=-80000.0)
    results.check('a negative normal volume is refused',
                  _raises(lambda: feed_molar_flows(negative)),
                  'returned negative flows')

    # A Gibbs outlet can balance perfectly and still be impossible. The outlet below
    # is what the workstation actually returned at the first attempt.
    from .validate import ResultCheckError, check_gibbs_equilibrium
    measured = {'Carbon': 980.6967864262893, 'H2O': 5.0410966717477984e-20,
                'CO': 1035.4024305629503, 'Hydrogen': 3.8327569e-04,
                'CO2': 4.0690299e-14, 'Methane': 5.177010e+02}
    try:
        check_gibbs_equilibrium(measured, 1673.15, 40.0)
        results.check('the measured vertex outlet is rejected', False,
                      'accepted, so a wrong result could be reported as PASS')
    except ResultCheckError:
        results.check('the measured vertex outlet is rejected', True, '')

    plausible = {'Carbon': 1491.0, 'CO': 1017.0, 'Hydrogen': 981.0,
                 'Methane': 22.0, 'H2O': 11.0, 'CO2': 3.5}
    try:
        block = check_gibbs_equilibrium(plausible, 1673.15, 40.0)
        results.check('a plausible equilibrium outlet is accepted',
                      block.get('verdict') == 'CONSISTENT', str(block.get('verdict')))
    except ResultCheckError as exc:
        results.check('a plausible equilibrium outlet is accepted', False, str(exc)[:120])

    return results.report()


def _raises(fn) -> bool:
    try:
        fn()
        return False
    except Exception:
        return True


def main() -> int:
    print('HYSYS tool layer offline verification')
    print('These are NOT HYSYS results; they verify the tool layer itself.')
    print('')
    return run_offline_checks()


if __name__ == '__main__':
    sys.exit(main())
