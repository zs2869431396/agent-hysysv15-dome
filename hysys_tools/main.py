"""Tool layer entry point.

One command builds exactly one complete HYSYS case from one specification:

    python -m hysys_tools.main --spec spec.json --result result.json

An agent drives this by producing the specification JSON. HYSYS COM objects cannot
cross process boundaries, so there is deliberately no "open a case and keep it"
mode: every call creates its own new blank case, configures it, solves it, verifies
it, saves it and writes a result file. A failure never leaves work for a later call
to pick up.

Console output is ASCII only (this workstation's console code page is cp1252).
Full text, including any non-ASCII, goes into the result JSON, which is UTF-8.
"""
from __future__ import annotations

import argparse
import math
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from .core import (  # noqa: F401  (re-exported for callers)
    REACTOR_FACTORY,
    RESULT_SCHEMA,
    SPEC_SCHEMA,
    ModelLimitationError,
    SpecError,
    StaleCaseError,
    StepLog,
    canonical,
    feed_molar_flows,
    load_spec,
    molar_mass_of,
    resolve_in_readback,
    stoichiometry_balance,
    write_json,
)
from . import validate as checks
from . import saturation
from . import precheck
from .reactor import CaseBuilder, close_case
from .capabilities import TOOL_REVISION, capability_metadata


def _reactor_kind(spec):
    value = spec.get('reactor')
    kind = value.get('kind') if isinstance(value, dict) else None
    return kind if isinstance(kind, str) else None


def _text_items(spec, key):
    value = spec.get(key, [])
    return value if isinstance(value, list) and all(isinstance(x, str) for x in value) else []


def build_case(spec: dict[str, Any], folder: Path, pythoncom, win32com,
               app) -> dict[str, Any]:
    """Create, configure, solve, verify and save one case."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    log = StepLog(folder)
    result: dict[str, Any] = {
        'schema': RESULT_SCHEMA,
        'tool_revision': TOOL_REVISION,
        'status': 'RUNNING',
        'started': datetime.now().isoformat(timespec='seconds'),
        'reactor_kind': _reactor_kind(spec),
        'assumptions': _text_items(spec, 'assumptions'),
        'open_questions': _text_items(spec, 'open_questions'),
        'blocking_questions': _text_items(spec, 'blocking_questions'),
        'warnings': [],
    }

    def finish(status: str) -> dict[str, Any]:
        result['status'] = status
        result['steps'] = log.steps
        result['finished'] = datetime.now().isoformat(timespec='seconds')
        write_json(folder / 'result.json', result)
        write_json(folder / 'steps.json', {'steps': log.steps})
        return result

    builder = CaseBuilder(spec, folder, pythoncom, win32com, log)
    # Tracked outside the try so the failure path can always tidy up, even when the
    # exception happened before the case object existed.
    case = None
    try:
        # Offline pre-flight before any COM call. A specification problem should cost
        # milliseconds, not a created case and a failed run - and reporting every
        # problem at once means one round trip fixes all of them.
        preflight = precheck.validate_spec(spec)
        result['precheck'] = {'ok': preflight['ok'],
                              'warnings': list(preflight['warnings'])}
        for note in preflight['warnings']:
            result['warnings'].append(note)
        if not preflight['ok']:
            raise SpecError('specification is not executable: %s'
                            % '; '.join(preflight['errors']))

        package_spec = spec['fluid_package']
        reactor_spec = spec['reactor']
        feed_spec = spec['feeds'][0]
        reactor_kind = str(reactor_spec['kind'])
        if reactor_kind not in REACTOR_FACTORY:
            raise SpecError('unsupported reactor kind: %r (supported: %s)'
                            % (reactor_kind, sorted(REACTOR_FACTORY)))

        # Two entries name this step: the first marks the attempt so a failure
        # before the case exists is still attributable, the second records what it
        # produced. Step order in the log is otherwise unchanged.
        log.add('create_blank_case')
        case_name = '%s.hsc' % str(spec.get('case_name', 'agent-case'))
        target = folder / case_name
        if target.exists():
            raise StaleCaseError('case file already exists; use a unique output folder')
        case = app.SimulationCases.Add(str(target))
        if case is None:
            raise SpecError('SimulationCases.Add returned no case object')
        log.add('create_blank_case', detail={'case_file': case_name,
                                            'active_document_used': False})

        builder.configure_basis(case, package_spec)

        reactions = list(spec.get('reactions', []))
        reaction_set = None
        # Graphite saturation imposed from outside ("route two"): see saturation.py.
        # Only when the spec asks for it; every other spec takes the unchanged path.
        saturation_mode = reactor_kind == 'gibbs' and saturation.is_requested(reactor_spec)
        if saturation_mode:
            saturation_x0 = saturation.initial_conversion(feed_spec)
            reaction_set = saturation.configure_reactions(builder, saturation_x0)
            builder.carbon_gibbs_unused = True
        elif reactor_kind == 'conversion':
            if not reactions:
                raise SpecError('a conversion reactor needs at least one reaction')
            reaction_set = builder._reaction_set('AGENT-SET')
            builder.configure_conversion_reactions(reaction_set, reactions)
            # Associate the fluid package while the basis edit is still open: the
            # verified script does exactly this, and doing it after EndBasisChange
            # was measured to fail later at Feeds.Add.
            reaction_set.AssociateFluidPackage(builder._package)
        elif reactor_kind == 'equilibrium':
            if not reactions:
                raise SpecError('an equilibrium reactor needs at least one reaction')
            reaction_set = builder._reaction_set('AGENT-SET')
            builder.configure_equilibrium_reactions(reaction_set, reactions)
        else:   # gibbs
            if reactions:
                log.add('gibbs_ignores_reactions',
                        detail='Gibbs minimises free energy and needs no reaction '
                               'equations; %d supplied reactions were not used.'
                               % len(reactions))
            for entry in reactions:
                balance = stoichiometry_balance(entry['stoichiometry'])
                if not balance['balanced']:
                    raise SpecError('supplied reaction %r is not element balanced'
                                    % entry.get('name'))

        # End the basis edit only now that the reaction system is fully configured.
        builder.end_basis()

        if saturation_mode:
            solved, feed_molar_total = saturation.run(
                case, builder, reactor_spec, feed_spec, log, reaction_set, saturation_x0)
        else:
            reaction_set_for_reactor = reaction_set if reactor_kind != 'gibbs' else None
            reactor, streams, outlet_c, feed_molar_total = builder.create_streams_and_reactor(
                case, reactor_spec, feed_spec, reaction_set_for_reactor)
            log.add('reactor_connected', detail={
                'reaction_set_attached': reaction_set_for_reactor is not None})

            solved = builder.solve_and_read(case, reactor, streams, outlet_c)

        log.add('verify_results')
        # Scale the feed composition to the molar flow HYSYS actually derived, so the
        # conservation checks compare against the simulator's own basis rather than
        # a molar flow computed independently here.
        #
        # `delegates_total`, not `is_native`: a `normal_volume` feed also has no
        # locally convertible unit - its total is a standard gas volume - so testing
        # only for the hysys mode sent it to `feed_molar_flows`, which rejects Nm3/h.
        # That is what failed the run after the simulation had already solved.
        from .native_flow import composition_weights, delegates_total
        feed_long = (composition_weights(feed_spec) if delegates_total(feed_spec)
                     else feed_molar_flows(feed_spec))
        feed_total = sum(feed_long.values())
        inlet = {canonical(k): (v / feed_total) * feed_molar_total
                 for k, v in feed_long.items()}
        outlet_internal = solved['component_flows_internal']
        try:
            masses = {canonical(name): molar_mass_of(name) for name in feed_long}
        except SpecError:
            masses = None
        # `products` carries the per-product composition, so a declared solid can be
        # confirmed to have left as a solid rather than dissolved in the gas.
        if reactor_kind == 'equilibrium':
            from .equilibrium import assert_readback
            for obj, item in zip(builder.equilibrium_objects, builder.equilibrium_evidence):
                item['after_solve_coefficients'] = assert_readback(obj, item['coefficients'])
            result['equilibrium_evidence'] = builder.equilibrium_evidence
        quality = checks.verify_case(spec, inlet, outlet_internal, reactor_kind,
                                     solved['heat_duty_kW'],
                                     str(reactor_spec.get('thermal_mode', 'adiabatic')),
                                     masses, products=solved.get('outlet'),
                                     equilibrium_evidence=getattr(builder,'equilibrium_evidence',None))
        # Report conversions under the names HYSYS uses, matching the keys of
        # component_flows_kmol_h. Mixing "methane" with "H2O" in one object forces a
        # caller to know two naming conventions for no benefit.
        conversions = quality.get('reactant_conversion_percent') or {}
        quality['reactant_conversion_percent'] = {
            (resolve_in_readback(k, builder.readback_names) or k): v
            for k, v in conversions.items()}

        log.add('save_case')
        case.SaveAs(str(target))
        if not target.is_file() or target.stat().st_size == 0:
            raise SpecError('saved case file is missing or empty: %s' % target)

        # Close the case once it is saved on disk. Leaving it open is what made
        # earlier runs fail: HYSYS resolves a duplicate case file path against a case
        # that is still open, so the next run's "new" case came back non-blank.
        # Closing happens through the case object itself (see close_case) because
        # closing by name is ambiguous when several cases share a name.
        # The saved .hsc keeps every result, so nothing is lost.
        close_entry = close_case(app, case, result['warnings'])
        case = None                 # already closed; the failure path must not redo it
        log.add('close_saved_case', detail=close_entry)

        result.update({
            'case_file': case_name,
            'case_file_bytes': target.stat().st_size,
            'readback_component_names': builder.readback_names,
            'native_flow_readback': getattr(builder, 'native_flow_readback', None),
            'feed_molar_flows_kmol_h': {
                resolve_in_readback(k, builder.readback_names) or k: v
                for k, v in inlet.items()},
            'outlet': solved['outlet'],
            'component_flows_kmol_h': solved['component_flows_kmol_h'],
            'heat_duty_kW': solved['heat_duty_kW'],
            'solver_is_solving': solved['solver_is_solving'],
            'solver_evidence': solved['solver_evidence'],
            'checks': quality,
            **({'solid_carbon_saturation': solved['saturation']}
               if solved.get('saturation') else {}),
            'notes': list(builder.notes),
            'warnings': list(result['warnings']) + list(builder.warnings),
        })
        return finish('PASS')
    except Exception as exc:
        log.fail('build_case', exc)
        result['error'] = '%s: %s' % (type(exc).__name__, exc)
        # Classify the failure so a caller knows what to do. Four cases matter:
        #   environment      - the run cannot proceed for a reason outside the spec
        #                      (a stale case left open). Retry in a new folder.
        #   result_check     - the spec was accepted and the simulation output
        #                      disagreed. Do NOT edit the spec.
        #   model_limitation - the simulator cannot represent what the spec asks for.
        #                      Do NOT retry and do NOT edit the spec: no edit fixes it,
        #                      and only one property package has been verified here. A
        #                      modelling decision is needed from a person.
        #   specification    - the spec itself is unusable. Edit it and retry.
        message = str(exc)
        result['error_type'] = (
            'environment' if isinstance(exc, StaleCaseError)
            else 'result_check' if isinstance(exc, checks.ResultCheckError)
            else 'model_limitation' if isinstance(exc, ModelLimitationError)
            else 'specification' if isinstance(exc, SpecError)
            else 'runtime')
        result['traceback'] = traceback.format_exc()
        # A failed run used to leave its case open, which matters most when an agent
        # is retrying: failures are the common case. Save the half-built case so it
        # can be inspected afterwards, then close it.
        #
        # Only ever touch a case this run created. When the output folder is reused,
        # HYSYS resolves the duplicate file path against an already-open case and
        # SimulationCases.Add hands back somebody else's case (see the blank-case
        # check). Saving or closing that would rewrite its file path and destroy the
        # operator's work, so it is left completely untouched.
        if case is not None and builder.owns_case:
            failed_name = '%s-FAILED.hsc' % str(spec.get('case_name', 'agent-case'))
            failed_path = folder / failed_name
            try:
                case.SaveAs(str(failed_path))
                result['failed_case_file'] = failed_name
            except Exception as save_exc:
                result['warnings'].append(
                    'the failed case could not be saved for inspection: %s'
                    % save_exc)
            log.add('close_failed_case',
                    detail=close_case(app, case, result['warnings']))
        elif case is not None:
            result['warnings'].append(
                'SimulationCases.Add returned a case that was not blank, so this run '
                'never took ownership of it; it was left untouched and is NOT one '
                'this run created')
            log.add('left_foreign_case_alone',
                    detail={'reason': 'case was not blank when inspected',
                            'actions_taken': 'none'})
        # Notes are recorded on the failure path too: a failure is exactly when the
        # reader wants to know what the driver observed along the way.
        result['notes'] = list(builder.notes)
        return finish('FAILED')


def health_check(pythoncom, win32com) -> dict[str, Any]:
    result: dict[str, Any] = {'status': 'FAILED'}
    try:
        app = win32com.client.GetActiveObject('HYSYS.Application')
        if app is None:
            raise RuntimeError('GetActiveObject returned None')
        result.update({
            'status': 'PASS',
            'application': str(getattr(app, 'Name', 'unknown')),
            'python': sys.version,
        })
        try:
            result['case_count'] = int(app.SimulationCases.Count)
        except Exception as exc:
            result['case_count_error'] = str(exc)
    except Exception as exc:
        result['error'] = '%s: %s' % (type(exc).__name__, exc)
    return result


def list_capabilities() -> dict[str, Any]:
    """Self-description, so an agent can learn what this tool accepts."""
    from .core import COMPONENT_LIBRARY, MASS_FLOW_UNITS, MOLAR_FLOW_UNITS

    return {
        'schema': SPEC_SCHEMA,
        **capability_metadata(),
        'reactor_kinds': {
            'conversion': 'Reaction equations plus a specified conversion. Needs '
                          'reactions[].stoichiometry, conversion_percent (or '
                          'conversion_coefficients) and base_component. VERIFIED on '
                          'the workstation for a single fixed-conversion adiabatic '
                          'toluene reaction; other combinations are not implied.',
            'equilibrium': 'Gas-phase isothermal reactions; ln(K) is fitted from HYSYS '
                           'Gibbs data and written using source 1 and an 8-value array. '
                           'Readback and measured-outlet Q/K gate are mandatory. '
                           'Probe verified; integrated release awaits remote acceptance.',
            'gibbs': 'No reaction equations. Product distribution comes from free '
                     'energy minimisation. Needs the full candidate component list '
                     'in fluid_package.components. Historically verified for '
                     'fixed-temperature steam reforming. Solid carbon: library Carbon '
                     'carries the Gibbs data of GASEOUS atomic carbon, so a plain Gibbs '
                     'case with carbon is refused before the solve; set '
                     'reactor.solid_carbon = "saturation" instead.',
        },
        'solid_carbon': {
            'saturation': 'Isothermal Gibbs with a carbon + water feed. Builds a '
                          'conversion reactor (bookkeeping 3C + 2H2O -> 2CO + CH4) '
                          'feeding a gas-only Gibbs reactor, and solves the conversion '
                          'so the gas carbon activity is 1 (graphite = 0 by '
                          'definition). Library Carbon\'s Gibbs data is never used. '
                          'Result adds a solid_carbon_saturation block. Verified by '
                          'probe five (probe-runs/route2-20261003-084249).',
        },
        'feed_basis': ['molar_fraction', 'mass_fraction', 'molar_flow', 'mass_flow'],
        'flow_units': {
            'molar': sorted(MOLAR_FLOW_UNITS),
            'mass': sorted(MASS_FLOW_UNITS),
            'note': 'Default local conversion accepts molar/mass units. '
                    "flow_input=normal_volume takes a stated standard gas volume "
                    '(Nm3/h plus standard_temperature_C and standard_pressure_kPa), '
                    'converts it on that basis here, and hands HYSYS a molar flow. '
                    'flow_input=hysys delegates the exact total_flow_unit to HYSYS '
                    'SetValue/GetValue and is restricted offline to the units a probe '
                    'measured HYSYS accepting (MolarFlow: gmole/h, kgmole/h, lbmole/h; '
                    'MassFlow: kg/h, lb/h), because no HYSYS property accepts Nm3/h '
                    "and HYSYS's own normal basis measured 15 C, not the exam's 0 C.",
        },
        'thermal_modes': ['adiabatic (duty fixed at 0 kW, outlet T computed)',
                          'isothermal (outlet_temperature required; the reported '
                          'duty includes sensible heating of the feed, not only the '
                          'heat of reaction)'],
        'units': {
            'temperature': ['C', 'K', 'F'],
            'pressure': ['kPa', 'Pa', 'MPa', 'bar', 'mbar', 'atm', 'psi'],
        },
        'components': {
            'accepted_spellings': sorted(set(COMPONENT_LIBRARY) | {
                'H2O', 'CH4', 'CO2', 'H2', 'O2', 'N2', 'steam', 'water',
                'methane', 'hydrogen', 'carbon monoxide', 'carbon dioxide',
                'toluene', 'benzene', 'o-xylene', 'm-xylene', 'p-xylene',
                'carbon', 'coal', 'graphite'}),
            'hysys_library_names': sorted(set(COMPONENT_LIBRARY.values())),
            'note': 'A component is passed to HYSYS under its library name, and '
                    'reaction reactants are addressed by the name HYSYS reports '
                    'back (water reads back as H2O), never by the requested '
                    'spelling.',
        },
        'one_case_per_call': True,
        'notes': [
            'One call builds one case and one operating condition. For several '
            'conditions, call once per condition.',
            'Each run must use a NEW output folder. Reusing one makes HYSYS resolve '
            'the duplicate case file path against a case that is still open, and the '
            '"new" case then comes back non-blank.',
            'Call --validate-only before --spec to check a specification offline in '
            'milliseconds.',
        ],
    }


# ---------------------------------------------------------------------------
# Local dry run: exercises the whole path with fake COM objects so coding
# mistakes surface here instead of on a remote run.
# ---------------------------------------------------------------------------
class _FakeVariable:
    def __init__(self, value=0.0):
        self.value = float(value)
        self.IsKnown = True

    def SetValue(self, value, unit):
        self.value = float(value)

    def GetValue(self, unit):
        return self.value


class _FakeDryRunApp:
    """Minimal stand-in that returns a converged-looking outlet."""

    Name = 'Fake HYSYS (dry run)'

    def __init__(self):
        self.SimulationCases = self

    def Add(self, path):
        return _FakeCase()


class _FakeCase:
    def __init__(self):
        self.calls = 0

    def SaveAs(self, path):
        Path(path).write_text('dry-run placeholder', encoding='utf-8')


def dry_run(spec: dict[str, Any]) -> dict[str, Any]:
    """Explain why a full local rehearsal is not possible and stop cleanly.

    A meaningful rehearsal needs a fake that reproduces the readback naming, the
    basis-edit ordering and the thermodynamics. That lives in the verification
    suite, so this entry point refuses rather than pretending to have run.
    """
    return {
        'status': 'DRY_RUN_NOT_AVAILABLE_HERE',
        'message': 'Run the offline verification suite instead: '
                   'python -m hysys_tools.selfcheck',
        'reactor_kind': str(spec.get('reactor', {}).get('kind')),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='hysys_tools',
        description='Build one HYSYS case from one specification JSON.')
    parser.add_argument('--spec', type=Path, help='specification JSON path')
    parser.add_argument('--result', type=Path,
                        help='where to write the result JSON (default: beside spec)')
    parser.add_argument('--folder', type=Path,
                        help='working folder for the case file (default: beside spec)')
    parser.add_argument('--health-check', action='store_true',
                        help='check the HYSYS COM connection and exit')
    parser.add_argument('--list-capabilities', action='store_true',
                        help='print what this tool accepts, as JSON, and exit')
    parser.add_argument('--validate-only', action='store_true',
                        help='check the specification and exit WITHOUT touching '
                             'HYSYS. Takes milliseconds, and reports every problem '
                             'it can find rather than only the first.')
    parser.add_argument('--write-examples', type=Path, metavar='FOLDER',
                        help='write the three exam scenarios as example specs and exit')
    args = parser.parse_args(argv)

    if args.write_examples:
        from .examples import write_examples
        written = write_examples(args.write_examples)
        for name in written:
            _console('wrote %s' % (args.write_examples / name))
        return 0

    if args.list_capabilities:
        _emit(list_capabilities())
        return 0

    if args.validate_only:
        if not args.spec:
            parser.error('--validate-only needs --spec')
        try:
            spec = load_spec(args.spec)
        except Exception as exc:
            _emit({'ok': False, 'source': str(args.spec),
                   'errors': ['could not read the specification: %s: %s'
                              % (type(exc).__name__, exc)],
                   'warnings': [], 'readback': {}})
            return 2
        report = precheck.validate_spec(spec)
        report['source'] = str(args.spec)
        _emit(report)
        return 0 if report['ok'] else 1

    if args.health_check:
        initialized = False
        try:
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()
            initialized = True
            payload = health_check(pythoncom, win32com)
        except Exception as exc:
            payload = {'status': 'FAILED', 'error_type': 'environment',
                       'error': '%s: %s' % (type(exc).__name__, exc),
                       'python': sys.version}
        finally:
            if initialized:
                pythoncom.CoUninitialize()
        _emit(payload)
        return 0 if payload.get('status') == 'PASS' else 1

    if not args.spec:
        parser.error('--spec is required unless --health-check, --validate-only or '
                     '--list-capabilities is used')
    spec_path = args.spec
    folder = args.folder or spec_path.parent
    result_path = args.result or (folder / 'result.json')

    try:
        spec = load_spec(spec_path)
    except Exception as exc:
        # Also written to the result file: a caller that only reads result.json
        # should still learn that the spec could not be parsed.
        payload = {
            'schema': RESULT_SCHEMA,
            'tool_revision': TOOL_REVISION,
            'status': 'SPEC_ERROR',
            'error_type': 'specification',
            'error': '%s: %s' % (type(exc).__name__, exc),
            'source': str(spec_path),
            'warnings': [],
        }
        try:
            result_path.parent.mkdir(parents=True, exist_ok=True)
            write_json(result_path, payload)
        except OSError as exc:
            payload['result_write_error'] = str(exc)
        _emit(payload)
        return 2

    # Pre-flight BEFORE connecting to HYSYS. If it ran only inside build_case, a spec
    # error would be masked by "HYSYS not reachable" whenever HYSYS happened to be
    # closed, and the caller would debug the wrong thing.
    preflight = precheck.validate_spec(spec)
    if not preflight['ok']:
        payload = {
            'schema': RESULT_SCHEMA,
            'tool_revision': TOOL_REVISION,
            'status': 'FAILED',
            'error_type': 'specification',
            'error': 'specification is not executable: %s'
                     % '; '.join(preflight['errors']),
            'precheck': preflight,
            'reactor_kind': _reactor_kind(spec),
            'warnings': list(preflight['warnings']),
            'assumptions': _text_items(spec, 'assumptions'),
            'open_questions': _text_items(spec, 'open_questions'),
            'blocking_questions': _text_items(spec, 'blocking_questions'),
        }
        try:
            result_path.parent.mkdir(parents=True, exist_ok=True)
            write_json(result_path, payload)
        except OSError as exc:
            payload['result_write_error'] = str(exc)
        _emit_summary(payload, result_path)
        return 1

    initialized = False
    stage = 'connect'
    try:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        initialized = True
        app = win32com.client.GetActiveObject('HYSYS.Application')
        if app is None:
            raise RuntimeError('GetActiveObject returned None; is HYSYS running?')
        stage = 'build'
        payload = build_case(spec, folder, pythoncom, win32com, app)
    except Exception as exc:
        payload = {
            'schema': RESULT_SCHEMA,
            'tool_revision': TOOL_REVISION,
            'status': 'CANNOT_CONNECT_TO_HYSYS' if stage == 'connect' else 'FAILED',
            # Outside the spec's control, so it is classified with the other
            # environment problems rather than as a specification error.
            'error_type': 'environment' if stage == 'connect' else 'runtime',
            'error': '%s: %s' % (type(exc).__name__, exc),
            'traceback': traceback.format_exc(),
            'reactor_kind': str(spec.get('reactor', {}).get('kind')),
            'assumptions': list(spec.get('assumptions', [])),
            'open_questions': list(spec.get('open_questions', [])),
            'warnings': (['HYSYS was not reachable. Start HYSYS, close any modal '
                          'dialogs, wait for it to finish loading, then retry.']
                         if stage == 'connect' else ['Build or result persistence failed; inspect the logs.']),
        }
    finally:
        if initialized:
            pythoncom.CoUninitialize()

    # build_case creates this folder, but the connection-failure path never reaches
    # build_case, so the parent must exist here or the result file is lost. That is
    # exactly how a whole run once produced only the specs folder and no results.
    try:
        write_json(result_path, payload)
    except (OSError, ValueError) as exc:
        payload.update(status='FAILED', error_type='runtime',
                       error='Could not persist result: %s' % exc)
        _emit(payload)
        return 1
    _emit_summary(payload, result_path)
    return 0 if payload.get('status') == 'PASS' else 1


def _console(message='', **kwargs):
    print(str(message).encode('ascii', 'backslashreplace').decode('ascii'), **kwargs)


def _emit(payload: Any) -> None:
    import json
    _console(json.dumps(payload, indent=2, ensure_ascii=True), flush=True)


def _emit_summary(payload: dict[str, Any], result_path: Path) -> None:
    """ASCII one-screen summary on the console; the JSON holds everything."""
    _console('status          : %s' % payload.get('status'))
    _console('reactor kind    : %s' % payload.get('reactor_kind'))
    if payload.get('case_file'):
        _console('case file       : %s (%s bytes)'
              % (payload['case_file'], payload.get('case_file_bytes')))
    checks = payload.get('checks') or {}
    if checks:
        _console('element error   : %s'
              % {k: round(v, 15)
                 for k, v in (checks.get('element_relative_error') or {}).items()})
        _console('mass error      : %.3e' % checks.get('mass_relative_error', 0.0))
        # Conversion of each feed component: the figure that matters per scenario.
        # Shown first because it is what a reader looks for.
        for name, percent in (checks.get('reactant_conversion_percent') or {}).items():
            _console('conversion      : %-10s %.4f %%' % (name, percent))
        conversions = checks.get('specified_conversion') or []
        for entry in conversions:
            if entry.get('checked'):
                _console('conversion chk  : %s requested %g%%, measured %g%%'
                      % (entry.get('base_component'), entry['requested_percent'],
                         entry['measured_percent']))
        co = checks.get('co_yield')
        if co:
            _console('CO yield        : %.4f%% (carbon fed %.2f kmol/h)'
                  % (co['co_yield_percent'], co['carbon_fed_kmol_h']))
            carbon_conversion = co.get('carbon_conversion_percent')
            if carbon_conversion is None:
                _console('carbon conv.    : not applicable (no solid carbon in the feed)')
            else:
                _console('carbon conv.    : %.4f%% (of solid carbon fed)'
                      % carbon_conversion)
        _console('heat duty       : %.2f kW' % checks.get('heat_duty_kW', 0.0))
    for warning in payload.get('warnings', []):
        _console('warning         : %s' % warning.encode('ascii', 'replace').decode())
    if payload.get('error'):
        _console('error           : %s'
              % str(payload['error']).encode('ascii', 'replace').decode())
    _console('result file     : %s' % result_path)


if __name__ == '__main__':
    sys.exit(main())
