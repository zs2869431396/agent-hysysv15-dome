"""Specification pre-flight checks that do not touch HYSYS.

Why this exists: everything a spec can get wrong used to surface only after a case
had been created, and sometimes only after the solver ran. A missing field arrived
as a bare ``KeyError`` classified as ``runtime``; a second feed was silently ignored;
a component missing from the fluid package failed much later as a conservation
error. An agent hitting any of those has to guess. This module answers the whole
question in milliseconds, offline, before any COM call.

Design rules:
  * No COM, no HYSYS, no network - importable anywhere.
  * **It must never raise.** A model-generated spec routinely has the wrong type in a
    field ("temperature": "hot", feeds written as an object). A traceback would leave
    the caller with no JSON to parse, and exit code 1 is indistinguishable from "the
    checks failed". ``validate_spec`` therefore wraps everything.
  * Report EVERY problem it can find, not just the first, so one round trip fixes all.
  * Only accept what the executor can actually honour. Where the executor maps a
    spelling onto a library name, the pre-check uses the same mapper, so "passes the
    pre-check" and "runs correctly" cannot disagree.
  * A finding is either an ``error`` (the spec cannot be executed as written) or a
    ``warning`` (executable, but something needs stating).
"""
from __future__ import annotations

import math
import re
from typing import Any

from .core import (
    REACTOR_FACTORY,
    SPEC_SCHEMA,
    SpecError,
    canonical,
    feed_molar_flows,
    library_name,
    property_package_name,
    stoichiometry_balance,
    equation_text,
    to_celsius,
    to_kpa,
)

THERMAL_MODES = ('adiabatic', 'isothermal')
FEED_BASES = ('molar_fraction', 'mass_fraction', 'molar_flow', 'mass_flow')
_BASIS_ALIASES = {'mol_flow': 'molar_flow', 'mole_fraction': 'molar_fraction',
                  'weight_fraction': 'mass_fraction'}
ABSOLUTE_ZERO_C = -273.15


def validate_spec(spec: Any) -> dict[str, Any]:
    """Check a specification without touching HYSYS. Never raises.

    Returns ``{'ok', 'errors', 'warnings', 'readback'}``.
    """
    try:
        return _validate_spec(spec)
    except Exception as exc:
        # The pre-check itself must never crash. Any type error the individual checks
        # did not anticipate reports as a malformed specification instead of
        # escaping as a traceback.
        return {
            'ok': False,
            'errors': ['malformed specification: %s: %s'
                       % (type(exc).__name__, exc)],
            'warnings': [],
            'readback': {},
        }


def _as_number(value: Any, label: str, errors: list[str]) -> float | None:
    """Coerce to float, recording a clear error instead of raising."""
    if isinstance(value, bool):          # bool is an int subclass; reject it
        errors.append('%s must be a number, got a boolean' % label)
        return None
    if isinstance(value, (int, float, str)):
        try:
            number = float(value)
            if not math.isfinite(number):
                errors.append('%s must be finite' % label)
                return None
            return number
        except (ValueError, OverflowError):
            errors.append('%s must be a number, got %r' % (label, value))
            return None
    errors.append('%s must be a number, got %s' % (label, type(value).__name__))
    return None


def _check_case_name(value: Any, errors: list[str]) -> None:
    # Windows rules also apply when preflight runs on another OS.
    if (not isinstance(value, str) or not value or len(value) > 100
            or value in ('.', '..') or value.endswith((' ', '.'))
            or re.search(r'[<>:"/\\|?*\x00-\x1f]', value)
            or value.split('.')[0].upper() in
            {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$',
             *('COM%d' % i for i in range(1, 10)),
             *('LPT%d' % i for i in range(1, 10))}):
        errors.append('case_name must be a safe Windows file stem (1-100 characters), '
                      'without paths, reserved names or trailing dots/spaces')


def _validate_spec(spec: Any) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    readback: dict[str, Any] = {}

    if not isinstance(spec, dict):
        return {'ok': False, 'errors': ['spec must be a JSON object, got %s'
                                        % type(spec).__name__],
                'warnings': [], 'readback': {}}

    # ------------------------------------------------------------ field types
    # Checked first and returned early: every later check assumes these shapes, and
    # continuing on a wrong shape is what used to raise.
    type_errors: list[str] = []
    for field, expected, type_name in (
            ('fluid_package', dict, 'an object'),
            ('reactor', dict, 'an object'),
            ('feeds', list, 'a list'),
            ('reactions', list, 'a list')):
        if field not in spec:
            if field != 'reactions':
                type_errors.append('missing required field: %s' % field)
            continue
        if not isinstance(spec[field], expected):
            type_errors.append('%s must be %s, got %s'
                               % (field, type_name, type(spec[field]).__name__))
    if type_errors:
        return {'ok': False, 'errors': type_errors, 'warnings': [], 'readback': {}}

    _check_case_name(spec.get('case_name', 'agent-case'), errors)
    for field in ('assumptions', 'open_questions', 'blocking_questions'):
        entries = spec.get(field, [])
        if not isinstance(entries, list) or any(not isinstance(x, str) for x in entries):
            errors.append('%s must be a list of strings' % field)
    if isinstance(spec.get('blocking_questions'), list) and spec['blocking_questions']:
        errors.append('unresolved blocking_questions: %s'
                      % '; '.join(str(q) for q in spec['blocking_questions']))

    # ---------------------------------------------------------------- schema
    schema = str(spec.get('schema', SPEC_SCHEMA))
    if schema.split('/')[0] != SPEC_SCHEMA.split('/')[0]:
        errors.append('unsupported schema %r (this build understands %r)'
                      % (schema, SPEC_SCHEMA))

    # --------------------------------------------------------- fluid package
    package = spec['fluid_package']
    components = package.get('components')
    if not isinstance(components, list):
        errors.append('fluid_package.components must be a list')
        components = []
    if not components:
        errors.append('fluid_package.components must list at least one component')
    resolved_components: list[str] = []
    unsupported: list[str] = []
    for name in components:
        try:
            resolved_components.append(library_name(name))
        except SpecError:
            unsupported.append(name)
    if unsupported:
        errors.append('unsupported component(s) %s. This build knows the library '
                      'names %s plus common spellings such as H2O, CH4, CO2, steam, '
                      'water, methane.' % (unsupported, sorted(_supported_components())))
    if len({canonical(c) for c in components if isinstance(c, str)}) != len(components):
        errors.append('fluid_package.components contains duplicates: %s' % components)

    package_requested = package.get('property_package', 'PengRob')
    try:
        resolved_package = property_package_name(package_requested)
    except SpecError as exc:
        errors.append(str(exc))
        resolved_package = None
    readback['components_resolved_to_library_names'] = resolved_components
    readback['property_package_resolved'] = resolved_package

    # ------------------------------------------------------------------ feeds
    feeds = spec['feeds']
    if not feeds:
        errors.append('feeds must contain at least one feed')
    if len(feeds) > 1:
        errors.append('feeds has %d entries but only one feed is supported; '
                      'feeds[0] is used and the rest would be silently ignored. '
                      'Split the case or merge the streams.' % len(feeds))
    if feeds and not isinstance(feeds[0], dict):
        errors.append('feeds[0] must be an object, got %s'
                      % type(feeds[0]).__name__)
    elif feeds:
        _check_feed(feeds[0], resolved_components, errors, warnings, readback)

    # --------------------------------------------------------------- reactor
    reactor = spec['reactor']
    kind = reactor.get('kind')
    if not isinstance(kind, str) or kind not in REACTOR_FACTORY:
        errors.append('reactor.kind %r is not supported; choose one of %s'
                      % (kind, sorted(REACTOR_FACTORY)))
        kind = None
    thermal = reactor.get('thermal_mode', 'adiabatic')
    if not isinstance(thermal, str) or thermal not in THERMAL_MODES:
        errors.append('reactor.thermal_mode %r is not supported; choose one of %s'
                      % (thermal, list(THERMAL_MODES)))
        thermal = None

    outlet_temperature = reactor.get('outlet_temperature')
    if thermal == 'isothermal':
        if outlet_temperature is None:
            errors.append('reactor.thermal_mode is isothermal, which needs '
                          'outlet_temperature')
        else:
            value = _as_number(outlet_temperature, 'reactor.outlet_temperature',
                               errors)
            if value is not None:
                celsius = _check_temperature(
                    value, reactor.get('outlet_temperature_unit', 'C'),
                    'reactor.outlet_temperature', errors)
                if celsius is not None:
                    readback['outlet_temperature_C'] = celsius
    if kind == 'gibbs' and thermal == 'adiabatic':
        # The executor sets the outlet temperature for an isothermal Gibbs reactor and
        # passes a zero-duty energy stream for an adiabatic one, but it deliberately
        # does NOT set a duty for a Gibbs reactor - so an adiabatic Gibbs case has no
        # stated thermal condition and was never verified. Refused rather than left to
        # fail as a 60-second solver timeout.
        errors.append('reactor.kind "gibbs" with thermal_mode "adiabatic" is not a '
                      'verified combination: the executor sets no thermal condition '
                      'for it. Use thermal_mode "isothermal" with an '
                      'outlet_temperature, which is the verified path.')

    pressure_drop = reactor.get('pressure_drop_kPa', 0.0)
    drop = _as_number(pressure_drop, 'reactor.pressure_drop_kPa', errors)
    if drop is not None and drop < 0:
        errors.append('reactor.pressure_drop_kPa must not be negative')
    if drop is not None and drop >= readback.get('feed_kPa', math.inf):
        errors.append('reactor.pressure_drop_kPa must be less than feed pressure')
    readback['reactor_kind'] = kind
    readback['thermal_mode'] = thermal

    # -------------------------------------------------------------- reactions
    reactions = spec.get('reactions', [])
    if kind == 'conversion' and not reactions:
        errors.append('a conversion reactor needs at least one entry in reactions')
    if kind == 'conversion' and len(reactions) > 1:
        errors.append('multiple conversion reactions are not supported by the '
                      'independent conversion check; use one balanced overall '
                      'reaction with a defined conversion basis')
    if kind == 'equilibrium' and not reactions:
        errors.append('an equilibrium reactor needs at least one entry in reactions')
    if kind == 'equilibrium':
        if thermal != 'isothermal':
            errors.append('equilibrium ln(K) fitting requires isothermal outlet_temperature; adiabatic equilibrium is unsupported')
        for entry in reactions:
            if isinstance(entry,dict) and entry.get('phase','vapour') != 'vapour':
                errors.append('equilibrium currently supports vapour reactions only')
            if isinstance(entry,dict) and any(canonical(n) == 'carbon' for n in (entry.get('stoichiometry') or {})):
                errors.append('solid carbon is outside the gas-phase equilibrium ln(K) path')
        warnings.append('Equilibrium uses HYSYS Gibbs data fitted to ln(K) in bar. Runtime coefficient readback and Q/K validation are mandatory; fit accuracy does not establish underlying Gibbs-data validity.')
    if kind == 'gibbs' and reactions:
        warnings.append('reactor.kind is gibbs: free-energy minimisation needs no '
                        'reaction equations, so the supplied reactions will not be '
                        'used. Remove them or switch reactor.kind.')
    for index, entry in enumerate(reactions):
        if not isinstance(entry, dict):
            errors.append('reactions[%d] must be an object, got %s'
                          % (index, type(entry).__name__))
            continue
        _check_reaction(entry, index, kind, resolved_components, errors, warnings)
        if kind == 'conversion' and isinstance(entry.get('base_component'), str):
            inlet = readback.get('feed_molar_flows_kmol_h', readback.get('feed_mole_fractions', {}))
            base = canonical(entry['base_component'])
            if inlet and sum(float(v) for n, v in inlet.items() if canonical(n) == base) <= 0:
                errors.append('reactions[%d].base_component must have a positive inlet flow '
                              'for the conversion to be verified' % index)

    if kind == 'gibbs':
        _check_gibbs_products(spec, resolved_components, errors, warnings)
    if 'solid_carbon' in reactor:
        # Graphite saturation imposed from outside (route two). Checked only when the
        # field is present, so every other spec is validated exactly as before.
        from .saturation import check_spec as _check_saturation
        _check_saturation(spec, resolved_components, errors, warnings)
        readback['solid_carbon'] = reactor.get('solid_carbon')

    if spec.get('open_questions'):
        questions = spec['open_questions']
        if isinstance(questions, list):
            warnings.append('%d open question(s) recorded in the spec: %s'
                            % (len(questions), '; '.join(str(q) for q in questions)))
        else:
            warnings.append('open_questions should be a list')

    return {'ok': not errors, 'errors': errors, 'warnings': warnings,
            'readback': readback}


def _check_temperature(value: float, unit: Any, label: str,
                       errors: list[str]) -> float | None:
    try:
        celsius = to_celsius(value, unit)
    except SpecError as exc:
        errors.append('%s: %s' % (label, exc))
        return None
    if not math.isfinite(celsius):
        errors.append('%s conversion is not finite' % label)
        return None
    if celsius < ABSOLUTE_ZERO_C:
        errors.append('%s is below absolute zero (%.2f C)' % (label, celsius))
        return None
    return celsius


def _check_feed(feed: dict, resolved_components: list[str], errors: list[str],
                warnings: list[str], readback: dict) -> None:
    if feed.get('molar_mass'):
        # Refused rather than honoured. HYSYS converts mass to moles with ITS OWN
        # molecular weights and never sees this override, so whenever the override
        # differs from the library value, the molar composition handed to HYSYS
        # disagrees with the mass fractions written in the spec - silently, because
        # the conservation checks would compare against the same wrong inlet. Every
        # supported component already has a library molar mass, so there is nothing
        # this option can legitimately add.
        errors.append(
            'feeds[0].molar_mass overrides are not supported: HYSYS derives molar '
            'flows from its own molecular weights and never sees this value, so an '
            'override that differs from the library value would make the simulated '
            'feed disagree with the spec without any error. Remove the field; every '
            'supported component already has a library molar mass.')
    raw_basis = feed.get('basis', 'molar_fraction')
    if not isinstance(raw_basis, str):
        errors.append('feeds[0].basis must be a string, got %s'
                      % type(raw_basis).__name__)
        return
    basis = _BASIS_ALIASES.get(raw_basis.strip().casefold(),
                               raw_basis.strip().casefold())
    if basis not in FEED_BASES:
        errors.append('feeds[0].basis %r is not supported; choose one of %s'
                      % (raw_basis, list(FEED_BASES)))
        return

    fraction_basis = basis in ('molar_fraction', 'mass_fraction')
    fractions = feed.get('fractions')
    flows = feed.get('flows')
    if fraction_basis:
        if not isinstance(fractions, dict) or not fractions:
            errors.append('feeds[0] uses the %s basis, which needs fractions as an '
                          'object' % basis)
            fractions = {}
        else:
            total_fraction = 0.0
            for name, value in fractions.items():
                number = _as_number(value, 'feeds[0].fractions[%r]' % name, errors)
                if number is None:
                    continue
                if number < 0:
                    errors.append('feeds[0].fractions[%r] must not be negative' % name)
                total_fraction += number
            if abs(total_fraction - 1.0) > 1e-6:
                errors.append('feeds[0].fractions sum to %.9g, not 1' % total_fraction)
        total_flow = feed.get('total_flow')
        if total_flow is None:
            errors.append('feeds[0] uses the %s basis, which needs total_flow' % basis)
        else:
            number = _as_number(total_flow, 'feeds[0].total_flow', errors)
            if number is not None and number <= 0:
                errors.append('feeds[0].total_flow must be positive')
    else:
        if not isinstance(flows, dict) or not flows:
            errors.append('feeds[0] uses the %s basis, which needs flows as an object'
                          % basis)
            flows = {}
        else:
            for name, value in flows.items():
                number = _as_number(value, 'feeds[0].flows[%r]' % name, errors)
                if number is not None and number < 0:
                    errors.append('feeds[0].flows[%r] must not be negative' % name)

    # Any component named in the feed must resolve to the same library name as one in
    # the fluid package, or HYSYS rejects it much later (or quietly ignores it).
    known_package = {canonical(c) for c in resolved_components}
    named = list(fractions or {}) + list(flows or {})
    active_names = list((fractions if fraction_basis else flows) or {})
    normalized_names = [canonical(n) for n in active_names if isinstance(n, str)]
    if len(normalized_names) != len(set(normalized_names)):
        errors.append('feeds[0] contains duplicate aliases for the same component')
    for name in named:
        if not isinstance(name, str):
            errors.append('feeds[0] has a non-string component name: %r' % (name,))
            continue
        try:
            key = canonical(library_name(name))
        except SpecError:
            errors.append('feeds[0] names component %r, which this build does not '
                          'know' % name)
            continue
        if key not in known_package:
            errors.append('feeds[0] names component %r, which is not in '
                          'fluid_package.components' % name)

    for label, key, unit_key, checker in (
            ('temperature', 'temperature', 'temperature_unit',
             lambda v, u: _check_temperature(v, u, 'feeds[0].temperature', errors)),
            ('pressure', 'pressure', 'pressure_unit',
             lambda v, u: _check_pressure(v, u, errors))):
        if feed.get(key) is None:
            errors.append('feeds[0].%s is required' % label)
            continue
        value = _as_number(feed.get(key), 'feeds[0].%s' % label, errors)
        if value is None:
            continue
        result = checker(value, feed.get(unit_key, 'C' if label == 'temperature'
                                         else 'kPa'))
        if result is not None:
            readback['feed_%s' % ('C' if label == 'temperature' else 'kPa')] = result

    if errors:
        return
    try:
        from .native_flow import (
            check_native_unit,
            composition_weights,
            is_native,
            is_normal_volume,
            stated_molar_volume,
        )
        if is_native(feed):
            # The unit is handed to HYSYS verbatim, so it is checked here against what
            # HYSYS accepts, rather than discovered by a failing COM call later. This
            # rule exists because `MolarFlow` + `Nm3/h` cost a remote run before the
            # probe proved no HYSYS property accepts that unit.
            check_native_unit(feed)
            weights = composition_weights(feed)
            total = sum(weights.values())
            readback['feed_mole_fractions'] = {k: v / total for k, v in weights.items()}
            readback['native_flow'] = {k: feed[k] for k in ('flow_property', 'total_flow', 'total_flow_unit')}
            warnings.append('Native flow unit acceptance and absolute flow are deferred to HYSYS; offline precheck does not verify COM unit support.')
        elif is_normal_volume(feed):
            # The conversion is `core.feed_molar_flows` rather than arithmetic repeated
            # here. Dividing total_flow by the molar volume locally skipped the unit
            # check, so `flow_input: 'normal_volume'` with kg/h, t/h, Nm3/d or a
            # nonsense string passed precheck and was silently divided by 22.4.
            component_flows = feed_molar_flows(feed)
            total_molar = sum(component_flows.values())
            weights = composition_weights(feed)
            total_weight = sum(weights.values())
            readback['feed_mole_fractions'] = {k: v / total_weight
                                               for k, v in weights.items()}
            molar_volume, conditions = stated_molar_volume(feed)
            readback['normal_volume_conversion'] = {
                'requested_normal_flow': feed['total_flow'],
                'requested_normal_unit': feed.get('total_flow_unit'),
                'standard_conditions': conditions,
                'molar_volume_m3_per_kmol': molar_volume,
                'molar_flow_kmol_h': total_molar,
                'component_flows_kmol_h': dict(component_flows),
            }
            warnings.append(
                'Normal volume converted on the stated basis %s (%.6f m3/kmol); HYSYS '
                "receives a molar flow, so HYSYS's own standard gas basis is not used."
                % (conditions, molar_volume))
        else:
            readback['feed_molar_flows_kmol_h'] = dict(feed_molar_flows(feed))
    except SpecError as exc:
        errors.append('feeds[0] cannot be converted to molar flows: %s' % exc)


def _check_pressure(value: float, unit: Any, errors: list[str]) -> float | None:
    try:
        kpa = to_kpa(value, unit)
    except SpecError as exc:
        errors.append('feeds[0].pressure: %s' % exc)
        return None
    if not math.isfinite(kpa) or kpa <= 0:
        errors.append('feeds[0].pressure must be positive (got %g kPa)' % kpa)
        return None
    return kpa


def _check_reaction(entry: dict, index: int, kind: str | None,
                    resolved_components: list[str], errors: list[str],
                    warnings: list[str]) -> None:
    label = 'reactions[%d]' % index
    stoichiometry = entry.get('stoichiometry')
    if not isinstance(stoichiometry, dict) or not stoichiometry:
        errors.append('%s needs stoichiometry as a non-empty object' % label)
        return

    numbers: dict[str, float] = {}
    normalized_names = [canonical(n) for n in stoichiometry if isinstance(n, str)]
    if len(normalized_names) != len(set(normalized_names)):
        errors.append('%s.stoichiometry contains duplicate component aliases' % label)
    for name, value in stoichiometry.items():
        number = _as_number(value, '%s.stoichiometry[%r]' % (label, name), errors)
        if number is not None:
            numbers[name] = number
    known_package = {canonical(c) for c in resolved_components}
    for name in stoichiometry:
        if not isinstance(name, str):
            errors.append('%s has a non-string component name: %r' % (label, name))
            continue
        try:
            key = canonical(library_name(name))
        except SpecError:
            errors.append('%s names component %r, which this build does not know'
                          % (label, name))
            continue
        if key not in known_package:
            errors.append('%s names component %r, which is not in '
                          'fluid_package.components' % (label, name))

    if not any(v < 0 for v in numbers.values()):
        errors.append('%s has no reactant (no negative coefficient)' % label)
    if not any(v > 0 for v in numbers.values()):
        errors.append('%s has no product (no positive coefficient)' % label)

    balance = stoichiometry_balance(numbers)
    if not balance['unknown_components'] and not balance['balanced'] and numbers:
        errors.append('%s is not element balanced: %s (net atoms %s)'
                      % (label, equation_text(numbers), balance['net_atoms']))

    if kind == 'conversion':
        percent = entry.get('conversion_percent')
        coefficients = entry.get('conversion_coefficients')
        if percent is not None and coefficients is not None:
            errors.append('%s must provide only one of conversion_percent and '
                          'conversion_coefficients' % label)
        if percent is None and not coefficients:
            errors.append('%s is a conversion reaction, which needs '
                          'conversion_percent or conversion_coefficients' % label)
        if percent is not None:
            value = _as_number(percent, '%s.conversion_percent' % label, errors)
            if value is not None and not 0 < value <= 100:
                errors.append('%s.conversion_percent must be in (0, 100]; got %g. '
                              'Percentages are written as 50, not 0.5.'
                              % (label, value))
        if coefficients is not None:
            warnings.append('%s uses experimental conversion_coefficients; independent '
                            'conversion verification is unavailable' % label)
            if not isinstance(coefficients, list) or len(coefficients) != 3:
                errors.append('%s.conversion_coefficients must be a list of three '
                              'numbers (c0, c1, c2)' % label)
            else:
                for position, item in enumerate(coefficients):
                    _as_number(item, '%s.conversion_coefficients[%d]'
                               % (label, position), errors)
        if not entry.get('base_component'):
            errors.append('%s needs base_component: the conversion is measured '
                          'against one reactant' % label)
        elif isinstance(entry['base_component'], str):
            try:
                base = canonical(library_name(entry['base_component']))
                reactants = {canonical(library_name(n)) for n, v in numbers.items()
                             if v < 0}
                if reactants and base not in reactants:
                    errors.append('%s.base_component %r is not a reactant of this '
                                  'reaction' % (label, entry['base_component']))
            except SpecError as exc:
                errors.append('%s.base_component is invalid: %s' % (label, exc))
        else:
            errors.append('%s.base_component must be a string' % label)


def _check_gibbs_products(spec: dict, resolved_components: list[str],
                          errors: list[str], warnings: list[str]) -> None:
    """Warn when a Gibbs case has no product it could possibly form."""
    feeds = spec.get('feeds') or []
    feed_components: set[str] = set()
    if feeds and isinstance(feeds[0], dict):
        feed = feeds[0]
        for name in list(feed.get('fractions') or {}) + list(feed.get('flows') or {}):
            if isinstance(name, str):
                feed_components.add(canonical(name))
    package_components = {canonical(c) for c in resolved_components}
    extra = package_components - feed_components
    if not extra:
        warnings.append(
            'reactor.kind is gibbs but fluid_package.components contains only the '
            'feed components, so there is no species the free-energy minimisation '
            'could produce. List every candidate product (for example CO, CO2, H2, '
            'CH4, H2O) in fluid_package.components.')


def _supported_components() -> set[str]:
    from .core import COMPONENT_LIBRARY
    return set(COMPONENT_LIBRARY.values())
