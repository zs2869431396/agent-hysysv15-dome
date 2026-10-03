"""HYSYS case-building tool layer: contracts, component identity and element maths.

This is the layer an agent drives. An agent never touches COM; it only produces a
specification JSON, and the tools turn that into a real HYSYS case and a real
result. Everything here is deliberately independent of COM so it can be tested
locally.

Hard constraint that shapes the whole design: HYSYS COM objects cannot cross
process boundaries, so one command always means one complete, self-contained case.
The tool never leaves a half-configured case for a later call to continue.

Encoding rule, learned the hard way on this workstation (console code page is
cp1252): console output is ASCII only. Non-ASCII text goes into the JSON files,
which are always written as UTF-8.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

SPEC_SCHEMA = 'hysys-agent/spec/1'
RESULT_SCHEMA = 'hysys-agent/result/1'

# ---------------------------------------------------------------------------
# Reactor kinds and their V15 unit-operation factory names (verified on the
# remote V15 workstation).
# ---------------------------------------------------------------------------
REACTOR_FACTORY = {
    'conversion': 'conversionreactorop',
    'equilibrium': 'equilibriumreactorop',
    'gibbs': 'gibbsreactorop',
}

# The factory name is not the TypeName HYSYS reports back: 'conversionreactorop'
# reads back as 'ConversionReactor'. Both are recorded so the driver can verify the
# object it actually got.
REACTOR_TYPE_NAMES = {
    'conversion': 'ConversionReactor',
    'equilibrium': 'EquilibriumReactor',
    'gibbs': 'GibbsReactor',
}

# Reaction phase enumeration exported from the V15 type library.
PHASE_ENUM = {
    'vapour': 0, 'liquid': 1, 'liquid2': 2, 'combinedLiquid': 3,
    'solid': 4, 'combined': 5, 'polymer': 6, 'unknown': 7,
}

# Component library: internal key -> HYSYS library name to pass to Components.Add.
# The name HYSYS reads BACK may differ (Water -> 'H2O'), which is why
# Reactants.Add must always use the readback name, never the requested name.
COMPONENT_LIBRARY = {
    'methane': 'Methane',
    'water': 'Water',
    'carbon monoxide': 'CO',
    'hydrogen': 'Hydrogen',
    'carbon dioxide': 'CO2',
    'nitrogen': 'Nitrogen',
    'oxygen': 'Oxygen',
    'toluene': 'Toluene',
    'benzene': 'Benzene',
    'o-xylene': 'o-Xylene',
    'm-xylene': 'm-Xylene',
    'p-xylene': 'p-Xylene',
    'carbon': 'Carbon',
}

# Spellings seen in natural language, in readback names, or in HYSYS output.
NAME_ALIASES = {
    'ch4': 'methane', 'methane': 'methane',
    'h2o': 'water', 'water': 'water', 'steam': 'water',
    'co': 'carbon monoxide', 'carbonmonoxide': 'carbon monoxide',
    'carbon monoxide': 'carbon monoxide',
    'h2': 'hydrogen', 'hydrogen': 'hydrogen',
    'co2': 'carbon dioxide', 'carbondioxide': 'carbon dioxide',
    'carbon dioxide': 'carbon dioxide',
    'n2': 'nitrogen', 'nitrogen': 'nitrogen',
    'o2': 'oxygen', 'oxygen': 'oxygen',
    'c7h8': 'toluene', 'toluene': 'toluene', 'methylbenzene': 'toluene',
    'c6h6': 'benzene', 'benzene': 'benzene',
    'c8h10': 'xylene', 'xylene': 'xylene', 'xylenes': 'xylene',
    'o-xylene': 'o-xylene', 'ortho-xylene': 'o-xylene', 'orthoxylene': 'o-xylene',
    'm-xylene': 'm-xylene', 'meta-xylene': 'm-xylene', 'metaxylene': 'm-xylene',
    'p-xylene': 'p-xylene', 'para-xylene': 'p-xylene', 'paraxylene': 'p-xylene',
    'carbon': 'carbon', 'c': 'carbon', 'graphite': 'carbon', 'coal': 'carbon',
    'coke': 'carbon', 'char': 'carbon',
}

# Elemental composition, used for the independent atom balance.
ATOMS = {
    'methane': {'C': 1, 'H': 4},
    'water': {'H': 2, 'O': 1},
    'carbon monoxide': {'C': 1, 'O': 1},
    'hydrogen': {'H': 2},
    'carbon dioxide': {'C': 1, 'O': 2},
    'nitrogen': {'N': 2},
    'oxygen': {'O': 2},
    'toluene': {'C': 7, 'H': 8},
    'benzene': {'C': 6, 'H': 6},
    'o-xylene': {'C': 8, 'H': 10},
    'm-xylene': {'C': 8, 'H': 10},
    'p-xylene': {'C': 8, 'H': 10},
    'xylene': {'C': 8, 'H': 10},
    'carbon': {'C': 1},
}

# Unit conversion to the units HYSYS SetValue/GetValue calls use (C and kPa).
TEMPERATURE_TO_C = {
    'c': lambda v: v, 'degc': lambda v: v, 'celsius': lambda v: v,
    'k': lambda v: v - 273.15, 'kelvin': lambda v: v - 273.15,
    'f': lambda v: (v - 32.0) * 5.0 / 9.0, 'degf': lambda v: (v - 32.0) * 5.0 / 9.0,
}
PRESSURE_TO_KPA = {
    'pa': lambda v: v / 1000.0, 'kpa': lambda v: v, 'mpa': lambda v: v * 1000.0,
    'bar': lambda v: v * 100.0, 'mbar': lambda v: v / 10.0,
    'atm': lambda v: v * 101.325, 'psi': lambda v: v * 6.894757293168361,
}


class SpecError(Exception):
    """The specification cannot be executed as written."""


class ModelLimitationError(SpecError):
    """The simulator cannot model what the spec asks for.

    Deliberately NOT a plain SpecError in meaning, though it subclasses one so that
    existing `except SpecError` handlers keep working. The distinction matters to an
    agent: a specification problem says "edit the spec and retry", whereas this says
    "stop, no edit to the spec will help, a person has to make a modelling decision".

    Measured case: a Peng-Robinson package carries `Carbon` as gaseous atomic carbon
    (formation Gibbs energy +450.5 kJ/mol at 1673 K, where graphite is zero by
    definition), so a Gibbs reactor built on it minimises towards an impossible
    outlet. Retrying with a differently spelled component, or with another package
    that has not been verified, would burn remote runs and could produce a wrong PASS.
    """


class StaleCaseError(SpecError):
    """SimulationCases.Add returned a case that is not blank.

    A distinct type rather than a message match, so the classification cannot be
    broken by rewording the error. It means the output folder collides with a case
    still open in HYSYS: an environment problem, not a specification problem, and the
    fix is a new folder rather than an edit to the spec.
    """


class StepLog:
    """Ordered record of what the tool did, so a caller can see where it stopped."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.steps: list[dict[str, Any]] = []

    def add(self, name: str, status: str = 'OK', detail: Any = None) -> None:
        entry: dict[str, Any] = {'step': name, 'status': status}
        if detail is not None:
            entry['detail'] = detail
        self.steps.append(entry)

    def fail(self, name: str, error: Exception, detail: Any = None) -> None:
        entry: dict[str, Any] = {'step': name, 'status': 'FAILED',
                                 'error': '%s: %s' % (type(error).__name__, error)}
        if detail is not None:
            entry['detail'] = detail
        self.steps.append(entry)


def write_json(path: Path, payload: Any) -> None:
    """Atomically write JSON; a filesystem failure must reach the caller."""
    path = Path(path)
    text = json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', errors='backslashreplace',
                                         dir=path.parent, suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(text)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def load_spec(path: Path) -> dict[str, Any]:
    def reject_constant(value):
        raise SpecError('non-finite JSON constant is not allowed: %s' % value)

    data = json.loads(Path(path).read_text(encoding='utf-8-sig'), parse_constant=reject_constant)
    if not isinstance(data, dict):
        raise SpecError('spec must be a JSON object')
    schema = str(data.get('schema', SPEC_SCHEMA))
    if schema.split('/')[0] != SPEC_SCHEMA.split('/')[0]:
        raise SpecError('unsupported spec schema: %s' % schema)
    return data


def canonical(name: str) -> str:
    """Map any spelling (requested, readback, or output) to an internal key.

    HYSYS appends an asterisk to hypothetical component names (`Graphite` reads back as
    `Graphite*`). The marker is stripped HERE, once, so every path that identifies a
    component - atom balance, molar mass, readback resolution, the equilibrium gate -
    agrees. Stripping it in only one module left `solve_and_read` keying the outlet by
    an unknown `graphite*`, which the atom balance then rejects.
    """
    key = str(name).strip().strip('*').strip().casefold().replace('_', '-')
    key = key.replace('(s)', '').replace('(g)', '').replace('(l)', '').strip()
    if key in NAME_ALIASES:
        return NAME_ALIASES[key]
    return NAME_ALIASES.get(key.replace('-', '').replace(' ', ''), key)


def library_name(name: str) -> str:
    """The name to pass to Components.Add."""
    key = canonical(name)
    if key not in COMPONENT_LIBRARY:
        raise SpecError('no HYSYS library mapping for component: %r' % name)
    return COMPONENT_LIBRARY[key]


# The property package name V15 accepts is the internal 'PengRob'. Spellings a model
# is likely to produce are mapped onto it. Only PengRob has been verified on the
# workstation, so anything unrecognised is refused rather than passed through: HYSYS
# reading back "Peng-Robinson" is the DISPLAY name, not an accepted input.
PROPERTY_PACKAGE_ALIASES = {
    'pengrob': 'PengRob',
    'peng-rob': 'PengRob',
    'peng rob': 'PengRob',
    'pengrobinson': 'PengRob',
    'peng-robinson': 'PengRob',
    'peng robinson': 'PengRob',
    'pr': 'PengRob',
    'pr78': 'PengRob',
}


def property_package_name(requested: str) -> str:
    """Map a requested property package onto the internal name V15 accepts."""
    key = str(requested).strip().casefold()
    if key in PROPERTY_PACKAGE_ALIASES:
        return PROPERTY_PACKAGE_ALIASES[key]
    raise SpecError(
        'unsupported property package %r. This build has verified only "PengRob" '
        '(Peng-Robinson); note that "Peng-Robinson" is what HYSYS displays on '
        'readback, not a name it accepts on input.' % requested)


def resolve_in_readback(wanted: str, readback_names: list[str]) -> str | None:
    """Find the requested component among the names HYSYS actually reports.

    Measured on this workstation: Reactants.Add('Water') raises E_FAIL while
    Add('H2O') succeeds, and 'water'/'WATER'/'Steam' all fail. So the requested
    name must never be used directly; it must be resolved against the readback
    names first.
    """
    target = canonical(wanted)
    for name in readback_names:
        if canonical(name) == target:
            return name
    return None


# Molar masses (kg/kmol) used for mass<->mole conversion and the mass balance.
# These are library values, not values read back from HYSYS: the mass balance is an
# independent check and must not reuse the simulator's own numbers.
MOLAR_MASS = {
    'methane': 16.043, 'water': 18.015, 'carbon monoxide': 28.010,
    'hydrogen': 2.016, 'carbon dioxide': 44.010, 'nitrogen': 28.013,
    'oxygen': 31.998, 'toluene': 92.1408, 'benzene': 78.114,
    'o-xylene': 106.168, 'm-xylene': 106.168, 'p-xylene': 106.168,
    'xylene': 106.168, 'carbon': 12.011,
}

# Flow units that are already molar, and their factor to kmol/h.
MOLAR_FLOW_UNITS = {
    'kmol/h': 1.0, 'kmol/hr': 1.0, 'kgmole/h': 1.0, 'kgmol/h': 1.0,
    'mol/h': 0.001,
}

# Flow units that are mass based, and their factor to kg/h.
MASS_FLOW_UNITS = {
    'kg/h': 1.0, 'kg/hr': 1.0, 'kgh': 1.0, 't/h': 1000.0, 'ton/h': 1000.0,
}

# Normal (standard) gas volume units, and their factor to m3/h at the stated
# conditions. These are NOT convertible on their own: they need the standard
# temperature and pressure, which is why they are only accepted together with
# `flow_input='normal_volume'` and its two condition fields.
NORMAL_VOLUME_UNITS = {
    'nm3/h': 1.0, 'nm3/hr': 1.0, 'nm\u00b3/h': 1.0, 'nm^3/h': 1.0,
}

# Ideal gas constant, J/(mol K) = Pa m3/(mol K).
GAS_CONSTANT = 8.314462618


def molar_mass_of(name: str) -> float:
    key = canonical(name)
    if key not in MOLAR_MASS:
        raise SpecError('no molar mass known for component: %r' % name)
    return MOLAR_MASS[key]


def atoms_of(name: str) -> dict[str, int]:
    key = canonical(name)
    if key not in ATOMS:
        raise SpecError('no elemental composition known for component: %r' % name)
    return ATOMS[key]


def to_celsius(value: float, unit: str) -> float:
    key = str(unit).strip().casefold().replace('°', '').replace(' ', '')
    if key not in TEMPERATURE_TO_C:
        raise SpecError('unsupported temperature unit: %r' % unit)
    return float(TEMPERATURE_TO_C[key](float(value)))


def to_kpa(value: float, unit: str) -> float:
    key = str(unit).strip().casefold().replace('°', '').replace(' ', '')
    if key not in PRESSURE_TO_KPA:
        raise SpecError('unsupported pressure unit: %r' % unit)
    return float(PRESSURE_TO_KPA[key](float(value)))


def element_totals(moles: dict[str, float]) -> dict[str, float]:
    """Element totals for a set of component molar flows."""
    totals: dict[str, float] = {}
    for name, amount in moles.items():
        for element, count in atoms_of(name).items():
            totals[element] = totals.get(element, 0.0) + float(amount) * count
    return totals


def element_relative_errors(inlet: dict[str, float],
                            outlet: dict[str, float]) -> dict[str, float]:
    """Relative C/H/O (and any other element) error between inlet and outlet.

    An independent check: it recomputes the atom balance from the reported
    component flows and never assumes the simulation is right.
    """
    in_totals = element_totals(inlet)
    out_totals = element_totals(outlet)
    errors: dict[str, float] = {}
    for element, target in in_totals.items():
        if target == 0:
            continue
        errors[element] = (out_totals.get(element, 0.0) - target) / target
    return errors


def stoichiometry_balance(stoichiometry: dict[str, float]) -> dict[str, Any]:
    """Element balance of one reaction equation, from first principles."""
    totals: dict[str, float] = {}
    unknown = []
    for name, coefficient in stoichiometry.items():
        try:
            composition = atoms_of(name)
        except SpecError:
            unknown.append(name)
            continue
        for element, count in composition.items():
            totals[element] = totals.get(element, 0.0) + float(coefficient) * count
    return {
        'net_atoms': {k: round(v, 10) for k, v in sorted(totals.items())},
        'balanced': bool(totals) and not unknown
                    and all(abs(v) < 1e-9 for v in totals.values()),
        'unknown_components': unknown,
    }


def equation_text(stoichiometry: dict[str, float]) -> str:
    """Human-readable reaction equation for logs and reports."""

    def side(negative: bool) -> str:
        parts = []
        for name, coefficient in stoichiometry.items():
            if (coefficient < 0) != negative:
                continue
            amount = abs(coefficient)
            prefix = '' if math.isclose(amount, 1.0, abs_tol=1e-12) else '%g ' % amount
            parts.append('%s%s' % (prefix, canonical(name)))
        return ' + '.join(parts) if parts else '(none)'

    return '%s -> %s' % (side(True), side(False))


def feed_molar_mass(feed_spec: dict[str, Any]):
    """Return a molar-mass resolver honouring the spec's ``molar_mass`` overrides.

    Overrides are normalised to internal keys, so a caller may write either
    ``Methane`` or ``methane``. Both the feed conversion AND the mass flow written to
    HYSYS must use this same resolver: previously the override applied to the feed
    conversion only, so the mole and mass flows were derived from different molar
    masses and silently disagreed.
    """
    overrides = {canonical(k): float(v)
                 for k, v in (feed_spec.get('molar_mass') or {}).items()}

    def resolve(name: str) -> float:
        key = canonical(name)
        return overrides.get(key, molar_mass_of(key))

    return resolve


def normal_molar_volume(temperature_c: Any = 0.0, pressure_kpa: Any = 101.325
                        ) -> tuple[float, str]:
    """Ideal-gas molar volume for a stated standard condition, m3/kmol.

    Lives here, next to the other unit conversions, because more than one module needs
    it and a second copy would be a second thing to keep in step. Returns the volume
    and a human-readable description of the conditions, so the basis used can be
    recorded rather than inferred.

    Ideal gas is taken as stated. No real-gas correction is applied, because applying
    one would be a modelling choice the requester did not make.
    """
    try:
        t = float(temperature_c)
        p = float(pressure_kpa)
    except (TypeError, ValueError) as exc:
        raise SpecError('standard conditions must be numbers: %s' % exc) from exc
    if p <= 0:
        raise SpecError('standard_pressure_kPa must be positive, got %r' % p)
    if t <= -273.15:
        raise SpecError('standard_temperature_C is below absolute zero: %r' % t)
    volume = GAS_CONSTANT * (t + 273.15) / (p * 1000.0) * 1000.0
    return volume, '%g C / %g kPa' % (t, p)


def feed_molar_flows(feed_spec: dict[str, Any]) -> dict[str, float]:
    """Normalise any feed basis into component molar flows (kmol/h).

    Supports molar_fraction, mass_fraction, molar_flow and mass_flow. For a
    fraction basis the total flow's OWN unit decides the conversion: a total given
    in kg/h must be divided by the mixture molar mass. Treating it as kmol/h
    silently inflates the feed by about two orders of magnitude; that defect was
    measured remotely, where a 10000 kg/h toluene feed was reported as
    10000 kmol/h instead of 108.53 kmol/h.

    A feed whose total is a stated standard gas volume (``flow_input='normal_volume'``)
    is converted here, on the stated basis, into an equivalent molar total. Handling
    it in this one function is deliberate: three separate call sites used to decide
    for themselves whether a feed was "delegated", and when the normal-volume mode was
    added one of them was missed - which failed a run *after* the simulation had
    already solved. Anything that asks this function for molar flows now gets the
    right answer whatever mode the feed declared.
    """
    basis = str(feed_spec.get('basis', 'molar_fraction')).strip().casefold()
    fractions = feed_spec.get('fractions') or {}
    flows = feed_spec.get('flows') or {}
    total = feed_spec.get('total_flow')
    unit = str(feed_spec.get('total_flow_unit', 'kmol/h')).strip().casefold()
    component_molar_mass = feed_molar_mass(feed_spec)

    if str(feed_spec.get('flow_input', 'local')).strip() == 'normal_volume':
        if unit not in NORMAL_VOLUME_UNITS:
            raise SpecError(
                "flow_input='normal_volume' needs a normal volume unit (%s), got %r. "
                'The unit is checked here so that it is checked on the execution path '
                'too, not only in the pre-check.'
                % (sorted(NORMAL_VOLUME_UNITS), unit))
        if total is None:
            raise SpecError("flow_input='normal_volume' needs total_flow")
        normal_volume = float(total) * NORMAL_VOLUME_UNITS[unit]
        if normal_volume <= 0 or not math.isfinite(normal_volume):
            raise SpecError('normal volume flow must be positive and finite, got %r'
                            % total)
        molar_volume, _conditions = normal_molar_volume(
            feed_spec.get('standard_temperature_C', 0.0),
            feed_spec.get('standard_pressure_kPa', 101.325))
        molar_total = normal_volume / molar_volume
        # The composition basis and the flow basis are independent: the exam states
        # the total as a normal gas volume while giving the composition as mass
        # fractions. So the composition is reduced to mole fractions first, and only
        # then scaled by the molar total. Going straight to the mass-fraction branch
        # below would demand a mass unit for a molar total.
        reference = dict(
            feed_spec, flow_input='local', total_flow=1.0,
            total_flow_unit=('kg/h' if basis in ('mass_fraction', 'weight_fraction')
                             else 'kmol/h'))
        weights = feed_molar_flows(reference)
        weight_total = sum(weights.values())
        if not (weight_total > 0):
            raise SpecError('feed composition sums to nothing: %r' % weights)
        result = {name: molar_total * (value / weight_total)
                  for name, value in weights.items()}
        # This branch returns early, so it must run the same validity check the rest of
        # the function ends with. It used to skip it: a negative total_flow came back
        # as negative flows rather than an error.
        if not result or any(v < 0 or not math.isfinite(v) for v in result.values()):
            raise SpecError('invalid component molar flows: %s' % result)
        return result

    def mixture_molar_mass(composition: dict[str, float]) -> float:
        return sum(float(value) * component_molar_mass(name)
                   for name, value in composition.items())

    def volumetric_error() -> SpecError:
        return SpecError(
            'total_flow_unit %r is neither molar nor mass (molar: %s; mass: %s). '
            'Volumetric units such as Nm3/h need flow_input=normal_volume with the '
            'standard conditions stated, and a feed whose total is delegated to HYSYS '
            'cannot be known offline.'
            % (unit, sorted(MOLAR_FLOW_UNITS), sorted(MASS_FLOW_UNITS)))

    result: dict[str, float] = {}
    if basis in ('molar_flow', 'mol_flow'):
        if unit not in MOLAR_FLOW_UNITS:
            raise SpecError('molar_flow basis needs a molar total_flow_unit, got %r '
                            '(molar: %s)' % (unit, sorted(MOLAR_FLOW_UNITS)))
        factor = MOLAR_FLOW_UNITS[unit]
        result = {name: float(value) * factor for name, value in flows.items()}
    elif basis == 'mass_flow':
        if unit not in MASS_FLOW_UNITS:
            raise SpecError('mass_flow basis needs a mass total_flow_unit, got %r '
                            '(mass: %s)' % (unit, sorted(MASS_FLOW_UNITS)))
        factor = MASS_FLOW_UNITS[unit]
        result = {name: float(value) * factor / component_molar_mass(name)
                  for name, value in flows.items()}
    elif basis in ('molar_fraction', 'mole_fraction'):
        if total is None:
            raise SpecError('molar_fraction feed needs total_flow')
        if unit in MOLAR_FLOW_UNITS:
            total_molar = float(total) * MOLAR_FLOW_UNITS[unit]
            result = {name: total_molar * float(value)
                      for name, value in fractions.items()}
        elif unit in MASS_FLOW_UNITS:
            total_mass = float(total) * MASS_FLOW_UNITS[unit]
            average = mixture_molar_mass(fractions)
            if average <= 0:
                raise SpecError('cannot convert a mass total to molar: the mixture '
                                'molar mass is zero')
            # The fractions are MOLAR fractions, so convert the mass total into a
            # total molar flow via the mixture molar mass and scale the fractions.
            # Dividing each fraction by that component's own molar mass instead
            # treats molar fractions as if they were mass fractions: for an
            # equimolar CH4/H2O feed at 1000 kg/h it gave 31.17 / 27.75 kmol/h
            # instead of the correct 29.36 / 29.36. The error is silent, because the
            # conservation checks compare against the same wrong inlet.
            total_molar = total_mass / average
            result = {name: total_molar * float(value)
                      for name, value in fractions.items()}
        else:
            raise volumetric_error()
    elif basis in ('mass_fraction', 'weight_fraction'):
        if total is None:
            raise SpecError('mass_fraction feed needs total_flow')
        if unit not in MASS_FLOW_UNITS:
            raise volumetric_error()
        total_mass = float(total) * MASS_FLOW_UNITS[unit]
        result = {name: total_mass * float(value) / component_molar_mass(name)
                  for name, value in fractions.items()}
    else:
        raise SpecError('unsupported feed basis: %r' % basis)

    if not result or any(v < 0 or not math.isfinite(v) for v in result.values()):
        raise SpecError('invalid component molar flows: %s' % result)
    return result
