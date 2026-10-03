"""Reactor drivers: build, solve and read one HYSYS case from a spec.

Current verification scope (see capabilities for combination-level evidence):

  conversion   reaction equations plus a specified conversion (toluene 50.00%)
  equilibrium  blocked: the attempted Ln(K) source cannot be set reliably
  gibbs        fixed outlet temperature, verified for methane steam reforming;
               solid-carbon gasification still needs current-path verification

The COM idioms here are copied from the scripts that already passed remotely, not
reinvented. The ordering constraints below were each learned from a real failure:

  * EndBasisChange must happen BEFORE any stream value is written; writing a
    stream while the basis is still being edited returns 0x80070005.
  * Reactants.Add must receive the exact readback component name. Add('Water')
    raises E_FAIL while Add('H2O') succeeds.
  * Setting Basis to partial pressure silently changes ReactionPhase, so the
    phase is re-applied afterwards.
"""
from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any

from .validate import ResultCheckError
from .native_flow import (
    composition_weights,
    delegates_total,
    is_native,
    mode_of,
    set_delegated_total,
    set_native_total,
)

from .core import (
    PHASE_ENUM,
    REACTOR_FACTORY,
    REACTOR_TYPE_NAMES,
    ModelLimitationError,
    SpecError,
    StaleCaseError,
    StepLog,
    canonical,
    equation_text,
    feed_molar_flows,
    feed_molar_mass,
    library_name,
    molar_mass_of,
    property_package_name,
    resolve_in_readback,
    stoichiometry_balance,
    to_celsius,
    to_kpa,
)

SOLVER_TIMEOUT_SECONDS = 60.0
REQUIRED_STABLE_READS = 3

# HYSYS reports an uncomputed variable as this sentinel rather than raising.
# Measured: an operation left in the ignored state returns -32767 kmol/h on its
# product streams, which reads like a negative flow but actually means "no value".
UNKNOWN_SENTINEL = -32767.0
UNKNOWN_TOLERANCE = 0.5


def _is_unknown(value: float) -> bool:
    """True when a HYSYS number is the 'no value' sentinel, not a real reading."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return True
    if not math.isfinite(number):
        return True
    return abs(number - UNKNOWN_SENTINEL) < UNKNOWN_TOLERANCE


def _flows_match(previous: dict[str, float], current: dict[str, float],
                 tolerance: float = 1e-9) -> bool:
    """True when two consecutive outlet readings agree component by component."""
    if set(previous) != set(current):
        return False
    return all(abs(value - previous[name]) <= tolerance
               for name, value in current.items())


def diagnose_feed_attachment(flow, streams, reactor, feed_spec, win32com, pythoncom,
                             readback_names, molar_flows) -> dict[str, Any]:
    """Self-diagnosis for a failed Feeds.Add.

    Builds a brand-new stream in the same case, configured the same way, and tries
    to attach it. That separates "this particular stream object is unusable" from
    "this reactor refuses to accept any feed", so the next remote run answers the
    question instead of costing another round of guessing.

    A failure here is diagnostic only; the original exception still propagates.
    """
    diagnosis: dict[str, Any] = {'purpose': 'isolate why Feeds.Add was rejected'}
    try:
        streams.Add('DIAG-FEED')
        candidate = streams.Item('DIAG-FEED')
        total = sum(molar_flows.values())
        fractions = []
        for name in readback_names:
            used = resolve_in_readback(name, list(molar_flows.keys()))
            fractions.append(float(molar_flows[used]) / total if used else 0.0)
        candidate.ComponentMolarFraction.Values = win32com.client.VARIANT(
            pythoncom.VT_ARRAY | pythoncom.VT_R8, tuple(fractions))
        candidate.Temperature.SetValue(
            to_celsius(feed_spec['temperature'],
                       feed_spec.get('temperature_unit', 'C')), 'C')
        candidate.Pressure.SetValue(
            to_kpa(feed_spec['pressure'],
                   feed_spec.get('pressure_unit', 'kPa')), 'kPa')
        mass_total = sum(float(amount) * molar_mass_of(canonical(name))
                         for name, amount in molar_flows.items())
        candidate.MassFlow.SetValue(mass_total, 'kg/h')
        diagnosis['candidate_stream_configured'] = {
            'fraction_sum': sum(fractions),
            'molar_flow_kmol_h': float(candidate.MolarFlow.GetValue('kgmole/h')),
            'temperature_C': float(candidate.Temperature.GetValue('C')),
            'pressure_kPa': float(candidate.Pressure.GetValue('kPa')),
        }
        try:
            reactor.Feeds.Add(candidate)
        except Exception as exc:
            diagnosis['second_attempt'] = 'FAILED'
            diagnosis['second_attempt_error'] = '%s: %s' % (type(exc).__name__, exc)
            diagnosis['conclusion'] = (
                'A brand-new stream is also rejected, so this is not a defect in the '
                'original FEED object: the operation is refusing any feed at this '
                'point.')
        else:
            diagnosis['second_attempt'] = 'OK'
            diagnosis['feed_count'] = int(reactor.Feeds.Count)
            diagnosis['conclusion'] = (
                'A brand-new stream was accepted, so the original FEED object itself '
                'is the problem, not the operation.')
    except Exception as exc:
        diagnosis['setup_failed'] = '%s: %s' % (type(exc).__name__, exc)
        diagnosis['conclusion'] = ('Could not build a comparison stream, so the cause '
                                   'is still undetermined.')
    return diagnosis


def close_case(app, case, warnings: list[str]) -> dict[str, Any]:
    """Close a case that has just been saved, using the case object itself.

    Closing through ``SimulationCases.Remove(name)`` is ambiguous: every run writes a
    new timestamped folder, so HYSYS can hold several cases that all report the same
    Name. Removing by name can then close an earlier one and leave this one open,
    and nothing fails, so the mistake stays invisible.

    ``SimulationCase.Close(SaveChanges, FileName)`` is declared with two optional
    VARIANT arguments, so ``SaveChanges=False`` is passed explicitly. Without it HYSYS
    may raise a save prompt, and a modal dialog blocks the COM call (and the whole
    launcher) until somebody answers it. The case has already been written with
    SaveAs, so there is nothing to save.

    The case count is read before and after and reported, so "closed" means the count
    actually dropped rather than the call merely returning without raising.
    """
    entry: dict[str, Any] = {'call': 'close_saved_case'}
    try:
        entry['case_name_readback'] = str(case.Name)
    except Exception as exc:
        entry['case_name_error'] = str(exc)
    try:
        entry['case_count_before'] = int(app.SimulationCases.Count)
    except Exception as exc:
        entry['case_count_before_error'] = str(exc)

    try:
        case.Close(False)
        entry['method'] = 'case.Close(SaveChanges=False)'
        entry['result'] = 'OK'
    except Exception as exc:
        # Deliberately NO fallback to SimulationCases.Remove(name). On this
        # workstation case.Name reads back as "Case" for every case, so
        # Remove("Case") could close an unrelated case - possibly the operator's own
        # work - while the case count still dropped by one and this log reported
        # success, leaving the mistake completely invisible. A failed close is a
        # warning, nothing more; the .hsc is already on disk.
        entry['result'] = 'FAILED'
        entry['close_error'] = '%s: %s' % (type(exc).__name__, exc)

    try:
        entry['case_count_after'] = int(app.SimulationCases.Count)
    except Exception as exc:
        entry['case_count_after_error'] = str(exc)

    before = entry.get('case_count_before')
    after = entry.get('case_count_after')
    if isinstance(before, int) and isinstance(after, int):
        entry['case_count_change'] = before - after
        if before - after <= 0:
            entry['result'] = 'FAILED'
            entry['error'] = (
                'the case count did not drop (%d -> %d), so no case was actually '
                'closed and this one may still be open' % (before, after))

    if entry.get('result') != 'OK':
        warnings.append(
            'the saved case could not be closed (%s); a stale case may remain open, '
            'so run the next build into a different folder'
            % (entry.get('error') or entry.get('close_error') or 'reason unknown'))
    return entry


class CaseBuilder:
    """Builds exactly one case. Never reuses or reads an existing case."""
    def __init__(self, spec: dict[str, Any], folder: Path, pythoncom, win32com,
                 log: StepLog):
        self.spec = spec
        self.folder = folder
        self.pythoncom = pythoncom
        self.win32com = win32com
        self.log = log
        self.readback_names: list[str] = []
        self.warnings: list[str] = []
        self.notes: list[str] = []
        # True only once configure_basis has proved this case is the blank one this run
        # created. Until then the object from SimulationCases.Add may belong to
        # somebody else, and the cleanup path must leave it alone.
        self.owns_case = False
        # Component molar flows of the feed, kept here rather than on any HYSYS
        # object (see the note in create_streams_and_reactor).
        self.feed_values: dict[str, float] = {}
        # Set by the graphite-saturation path, where library Carbon only enters a
        # conversion reactor and its (wrong) Gibbs data is never used. The pre-solve
        # reference check is then skipped - and the skip is logged, never silent.
        self.carbon_gibbs_unused = False

    # ------------------------------------------------------------------ basis
    def configure_basis(self, case, package_spec: dict[str, Any]) -> None:
        """Open the basis and add components. Leaves the basis edit OPEN.

        The script that is verified on this workstation configures the reaction
        system while the basis is still being edited and only then calls
        EndBasisChange. That ordering is reproduced exactly here, because attaching
        the reaction system after ending the basis was measured to fail later, at
        Feeds.Add, with a bare E_FAIL.
        """
        basis = case.BasisManager
        package_name = str(package_spec.get('name', 'AGENT-PR'))
        # Check for a pre-existing fluid package BEFORE opening the basis edit, if the
        # count can be read that way. Opening the edit first would leave a case that
        # turns out not to be ours sitting in basis-edit state, which counts as
        # modifying somebody else's case.
        existing_packages: int | None = None
        try:
            existing_packages = int(basis.FluidPackages.Count)
        except Exception as exc:
            self.notes.append(
                'fluid package count is only readable after StartBasisChange (%s); '
                'the blank-case check therefore happens after the edit opens' % exc)
        if existing_packages not in (None, 0):
            raise StaleCaseError(self._not_blank_message(existing_packages,
                                                         'fluid package(s)'))

        # Check the flowsheet BEFORE editing the basis or claiming ownership.
        # A case with streams but no fluid package is not ours to modify either.
        flow = case.Flowsheet
        existing = (int(flow.MaterialStreams.Count), int(flow.Operations.Count))
        if existing != (0, 0):
            raise StaleCaseError(self._not_blank_message(sum(existing),
                                                         'stream(s)/operation(s)'))

        basis.StartBasisChange()
        packages = basis.FluidPackages
        # Re-check: either this is the authoritative read, or the pre-check could not
        # read the collection at all.
        existing_packages = int(packages.Count)
        if existing_packages != 0:
            basis.EndBasisChange()
            raise StaleCaseError(self._not_blank_message(existing_packages,
                                                        'fluid package(s)'))
        # From this point failures must save/close our partially built case.
        self.owns_case = True
        self._basis = basis
        packages.Add(package_name)
        package = packages.Item(0)
        # Map the requested spelling onto the internal name V15 accepts. The pre-check
        # accepts aliases such as 'Peng-Robinson', so the executor must resolve them
        # the same way or a spec could pass the pre-check and then fail here.
        internal = property_package_name(
            package_spec.get('property_package', 'PengRob'))
        package.PropertyPackageName = internal
        for name in package_spec.get('components', []):
            # Same reasoning: the pre-check resolves component names through
            # library_name, so Components.Add must receive the library name rather
            # than whatever spelling the spec used. For every already-verified name
            # this returns the string unchanged, so the verified call sequence is
            # untouched; aliases such as CH4 or steam now resolve instead of being
            # handed to HYSYS verbatim.
            package.Components.Add(library_name(name))
        self.readback_names = [
            str(package.Components.Item(i).Name)
            for i in range(int(package.Components.Count))]
        # Only now is this case known to be the blank one this run created. Until this
        # point the object returned by SimulationCases.Add could belong to somebody
        # else (HYSYS resolves a duplicate case file path against an open case), so the
        # cleanup path must not touch it.
        self.owns_case = True
        self.log.add('configure_basis', detail={
            'property_package_requested': internal,
            'property_package_readback': str(package.PropertyPackageName),
            'components_readback': self.readback_names,
            'basis_still_being_edited': True,
            'case_ownership_established': True,
        })
        self._basis = basis
        self._package = package
        self._case = case

    @staticmethod
    def _not_blank_message(count: int, what: str) -> str:
        return ('new case is not blank: it already has %d %s. A case with the same '
                'file path is probably still open in HYSYS. Close it without saving, '
                'or use a unique output folder, then retry.' % (count, what))

    def end_basis(self) -> None:
        """Close the basis edit; called after the reaction system is configured."""
        if not bool(self._basis.CanEndBasisChange):
            raise SpecError('basis configuration incomplete; CanEndBasisChange is false')
        self._basis.EndBasisChange()
        self.log.add('end_basis_change', detail={
            'is_changing_basis': bool(getattr(self._basis, 'IsChangingBasis', False)),
        })
        self.verify_solid_carbon_reference()

    # Components whose standard state is a pure condensed phase, so their Gibbs energy
    # of formation is zero by definition. Evaluating any of these at a real temperature
    # is a direct test of whether HYSYS is using the solid reference state.
    CONDENSED_REFERENCE_SPECIES = ('Carbon',)
    # Graphite's formation Gibbs energy is exactly zero, so anything beyond a small
    # numerical slack means the component is not the solid.
    CONDENSED_REFERENCE_TOLERANCE_KJ = 25.0
    REFERENCE_PROBE_TEMPERATURES_K = (1000.0, 1500.0)

    def verify_solid_carbon_reference(self) -> None:
        """Refuse a solid-carbon case when HYSYS's Carbon is not graphite.

        Measured on the workstation: `Carbon` in a Peng-Robinson package reports
        `EvaluateGibbs(1673.15) = +450.5 kJ/mol`, while graphite is zero at every
        temperature. The value matches gaseous atomic carbon to about 10 kJ/mol over
        1000-1673 K (predicted +461.8 kJ/mol at 1673 K from the sublimation enthalpy
        of graphite). Every other component is correct - methane reports +93.66 kJ/mol
        at 1673 K, against a JANAF value of +94.

        That single wrong reference state is enough to invert a reaction. With HYSYS's
        Carbon, `C + 2H2 -> CH4` is favourable by -356.9 kJ/mol; with graphite it is
        unfavourable by +93.7. So the Gibbs minimiser drives every hydrogen atom into
        methane and every oxygen atom into CO, and the outlet lands on the vertex where
        carbon consumption is maximised with 980.70 kmol/h left over - the result this
        check exists to prevent.

        Checking here rather than after the solve turns a wasted remote run into an
        immediate refusal. The post-solve equilibrium check stays as well: this one
        needs HYSYS to answer, so it only guards the live path, not a stored result.
        """
        if getattr(self, 'carbon_gibbs_unused', False):
            self.log.add('solid_carbon_reference', status='SKIPPED', detail={
                'reason': ('reactor.solid_carbon = saturation: carbon enters only a '
                           'conversion reactor, which does no equilibrium, and the Gibbs '
                           'reactor receives no carbon. Library Carbon\'s Gibbs data is '
                           'never used, so its known defect cannot affect the result.'),
                'authoritative_check': 'validate.check_gibbs_equilibrium on the '
                                       'combined outlet'})
            return
        package = getattr(self, '_package', None)
        if package is None:
            return
        try:
            components = package.Components
            count = int(components.Count)
            names = [str(components.Item(i).Name) for i in range(count)]
        except Exception as exc:                        # noqa: BLE001
            # Cannot read the package at all. Do not fail the run - the post-solve
            # gate still guards it - but do not let the record show the check passed
            # either. A check that silently did not run is worse than no check.
            self.log.add('solid_carbon_reference', status='WARNING', detail={
                'verdict': 'UNVERIFIED',
                'reason': 'could not read the component list: %s' % exc,
                'consequence': ('the pre-solve reference-state check did not run; the '
                                'post-solve equilibrium gate remains authoritative'),
            })
            return

        # Match on the canonical name, the way the rest of the code base does, rather
        # than on the exact string 'Carbon' - the library name, an alias and a spec
        # spelling can all differ.
        wanted = set(self.CONDENSED_REFERENCE_SPECIES)
        if not any(canonical(name) in {canonical(w) for w in wanted} for name in names):
            return

        checked = 0
        for index in range(count):
            try:
                component = components.Item(index)
                name = str(component.Name)
            except Exception:                           # noqa: BLE001
                continue
            if canonical(name) not in {canonical(w) for w in wanted}:
                continue
            readings: dict[str, float] = {}
            for kelvin in self.REFERENCE_PROBE_TEMPERATURES_K:
                try:
                    readings['%g' % kelvin] = float(component.EvaluateGibbs(kelvin)) / 1000.0
                except Exception:                       # noqa: BLE001
                    continue
            if not readings:
                self.log.add('solid_carbon_reference', status='WARNING', detail={
                    'component': name,
                    'verdict': 'UNVERIFIED',
                    'reason': 'EvaluateGibbs returned nothing at any probe temperature',
                    'consequence': ('the pre-solve reference-state check did not run; '
                                    'the post-solve equilibrium gate remains '
                                    'authoritative'),
                })
                continue
            checked += 1
            worst = max(readings.values(), key=abs)
            self.log.add('solid_carbon_reference', detail={
                'component': name,
                'gibbs_kj_per_mol': readings,
                'graphite_reference_kj_per_mol': 0.0,
                'tolerance_kj_per_mol': self.CONDENSED_REFERENCE_TOLERANCE_KJ,
                # "Reference state correct", not "graphite-like" or "solid carbon
                # supported". This check only reads the standard-state Gibbs energy; it
                # does not establish that carbon is in a condensed phase with unit
                # activity. A hypothetical component with dGf set to zero would pass
                # here and still be a fluid in the package. Whether a result can be
                # trusted is decided by the post-solve equilibrium gate, not here.
                'verdict': ('reference state correct'
                            if abs(worst) <= self.CONDENSED_REFERENCE_TOLERANCE_KJ
                            else 'NOT_GRAPHITE'),
                'checks': 'standard-state Gibbs energy of formation only',
                'does_not_check': ('that carbon is a condensed phase with unit activity; '
                                   'the post-solve equilibrium gate is authoritative'),
                'authoritative_check': 'validate.check_gibbs_equilibrium',
            })
            if abs(worst) > self.CONDENSED_REFERENCE_TOLERANCE_KJ:
                raise ModelLimitationError(
                    "component %r is not solid graphite: its Gibbs energy of formation "
                    'is %+.1f kJ/mol at %s K, where graphite is 0 by definition '
                    '(tolerance %.0f kJ/mol). HYSYS is treating carbon as a fluid '
                    'species, most likely gaseous atomic carbon. A Gibbs reactor built '
                    'on this reference state will minimise towards consuming as much '
                    'carbon as possible and produce a chemically impossible outlet, so '
                    'the case is refused before the solve rather than reported '
                    'afterwards. DO NOT RETRY AUTOMATICALLY: no edit to the '
                    'specification changes this, and only a Peng-Robinson package has '
                    'been verified on this workstation. A person has to decide the '
                    'modelling route - whether a solid-carbon component or a different '
                    'approach exists - and that decision belongs in the report.'
                    % (name, worst, ', '.join(sorted(readings)),
                       self.CONDENSED_REFERENCE_TOLERANCE_KJ))

    # -------------------------------------------------------------- reactions
    def _reaction_set(self, name: str):
        manager = self._basis.ReactionPackageManager
        manager.ReactionSets.Add(name)
        return manager.ReactionSets.Item(name)

    def _add_reactants(self, reaction, stoichiometry: dict[str, float]) -> list[dict]:
        """Add reactants using the readback name, then write the coefficients.

        The type library shows the equilibrium reaction exposes
        ReactantStoichCoefValue as the settable coefficient property. Two spellings
        are attempted so the same driver works for every reaction kind.
        """
        resolution = []
        for name, coefficient in stoichiometry.items():
            used = resolve_in_readback(name, self.readback_names)
            if used is None:
                raise SpecError('component %r was never added to the fluid package '
                                '(readback: %s)' % (name, self.readback_names))
            try:
                reaction.Reactants.Add(used)
            except Exception as exc:
                raise SpecError(
                    'Reactants.Add(%r) failed: %s. The readback names are %s.'
                    % (used, exc, self.readback_names)) from None
            element = reaction.Reactants.Item(used)
            written = None
            for attribute in ('ReactantStoichCoefValue',
                              'StoichiometricCoefficientValue'):
                try:
                    setattr(element, attribute, float(coefficient))
                    written = attribute
                    break
                except Exception:
                    continue
            if written is None:
                raise SpecError('no writable stoichiometric coefficient on %r' % used)
            readback = float(getattr(element, written))
            if not math.isclose(readback, float(coefficient), rel_tol=1e-9,
                                abs_tol=1e-12):
                raise SpecError('stoichiometric coefficient readback mismatch for %r: '
                                '%r != %r' % (used, readback, coefficient))
            resolution.append({'requested': name, 'used': used,
                               'coefficient': float(coefficient),
                               'attribute': written})
        return resolution

    def configure_conversion_reactions(self, reaction_set, reactions: list[dict]):
        manager = self._basis.ReactionPackageManager
        detail = []
        for index, entry in enumerate(reactions):
            name = str(entry.get('name') or 'RXN-%d' % (index + 1))
            stoichiometry = {k: float(v) for k, v in entry['stoichiometry'].items()}
            balance = stoichiometry_balance(stoichiometry)
            if not balance['balanced']:
                raise SpecError('%s is not element balanced: %s (%s)'
                                % (name, equation_text(stoichiometry),
                                   balance['net_atoms']))
            manager.Reactions.Add(name, 'conversionrxn')
            reaction = manager.Reactions.Item(name)
            resolution = self._add_reactants(reaction, stoichiometry)

            base = entry.get('base_component')
            if base:
                base_name = resolve_in_readback(base, self.readback_names)
                if base_name is None:
                    raise SpecError('base component %r not in the package' % base)
                route = self._set_base_component(reaction, base_name)
                # Read the setter back: a BaseComponent that did not stick leaves a
                # reaction that cannot be solved, which later shows up as an
                # unexplained failure when the reactor is connected.
                try:
                    readback_name = str(reaction.BaseComponent.Component.Name)
                except Exception:
                    try:
                        readback_name = str(reaction.BaseComponent.Name)
                    except Exception as exc:
                        readback_name = 'unreadable: %s' % exc
                resolution.append({'base_component': base_name, 'route': route,
                                   'readback': readback_name})

            coefficients = self._conversion_coefficients(entry)
            reaction.ConversionCoefficientsValue = self.win32com.client.VARIANT(
                self.pythoncom.VT_ARRAY | self.pythoncom.VT_R8, tuple(coefficients))
            readback = [float(v) for v in reaction.ConversionCoefficientsValue]
            if len(readback) != 3 or any(
                    not math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
                    for a, b in zip(readback, coefficients)):
                raise SpecError('conversion coefficient readback mismatch: %s vs %s'
                                % (readback, coefficients))

            phase = str(entry.get('phase', 'combined'))
            if phase not in PHASE_ENUM:
                raise SpecError('unknown reaction phase: %r' % phase)
            reaction.ReactionPhase = PHASE_ENUM[phase]
            if int(reaction.ReactionPhase) != PHASE_ENUM[phase]:
                raise SpecError('reaction phase not applied for %s' % name)
            reaction_set.ActiveReactions.Add(reaction)
            detail.append({'reaction': name, 'equation': equation_text(stoichiometry),
                           'coefficients': coefficients,
                           'phase_readback': int(reaction.ReactionPhase),
                           'reactants': resolution})
        self.log.add('configure_conversion_reactions', detail=detail)
        return detail

    def _conversion_coefficients(self, entry: dict) -> list[float]:
        coefficients = entry.get('conversion_coefficients')
        if coefficients:
            values = [float(v) for v in coefficients]
            if len(values) != 3:
                raise SpecError('conversion_coefficients needs exactly 3 values '
                                '(c0, c1, c2)')
            return values
        percent = entry.get('conversion_percent')
        if percent is None:
            raise SpecError('a conversion reaction needs conversion_percent or '
                            'conversion_coefficients')
        percent = float(percent)
        if not 0 < percent <= 100:
            raise SpecError('conversion_percent must be in (0, 100]; got %r. '
                            'Percentages are given as 50, not 0.5.' % percent)
        return [percent, 0.0, 0.0]

    def _set_base_component(self, reaction, readback_name: str) -> str:
        """Resolve BaseComponent's referenced interface instead of guessing.

        The setter expects either a Reactant or a Component depending on what the
        type library declares, so the declaration is read rather than assumed. If
        the declaration cannot be inspected, fall back to the Reactant form, which
        is the one verified on the remote workstation for conversion reactions, and
        report which route was taken.
        """
        try:
            info = reaction._oleobj_.GetTypeInfo()
        except Exception as exc:
            reaction.BaseComponent = reaction.Reactants.Item(readback_name)
            self.notes.append('BaseComponent set via the Reactant form: the setter '
                              'declaration could not be inspected (%s)' % exc)
            return 'reactant-fallback'
        pythoncom = self.pythoncom
        for index in range(info.GetTypeAttr().cFuncs):
            desc = info.GetFuncDesc(index)
            if (info.GetNames(desc.memid)[0] == 'BaseComponent'
                    and desc.invkind == pythoncom.DISPATCH_PROPERTYPUT):
                type_desc = desc.args[0][0]
                if type_desc[0] == pythoncom.VT_PTR:
                    type_desc = type_desc[1]
                ref_name = info.GetRefTypeInfo(type_desc[1]).GetDocumentation(-1)[0]
                lowered = ref_name.casefold()
                if 'reactant' in lowered:
                    reaction.BaseComponent = reaction.Reactants.Item(readback_name)
                    return 'reactant'
                if 'component' in lowered:
                    reaction.BaseComponent = self._package.Components.Item(
                        readback_name)
                    return 'component'
                raise SpecError('unsupported BaseComponent interface: %s' % ref_name)
        raise SpecError('BaseComponent setter not present on this reaction')

    def configure_equilibrium_reactions(self, reaction_set, reactions: list[dict]):
        from .equilibrium import derive, assert_readback
        manager = self._basis.ReactionPackageManager
        detail = []
        source = 'lnk_equation_from_hysys_gibbs'
        reactor_spec = self.spec['reactor']
        target_K = to_celsius(reactor_spec['outlet_temperature'], reactor_spec.get('outlet_temperature_unit','C')) + 273.15
        self.equilibrium_objects = []
        self.equilibrium_evidence = detail
        for index, entry in enumerate(reactions):
            name = str(entry.get('name') or 'EQ-%d' % (index + 1))
            stoichiometry = {k: float(v) for k, v in entry['stoichiometry'].items()}
            balance = stoichiometry_balance(stoichiometry)
            if not balance['balanced']:
                raise SpecError('%s is not element balanced: %s (%s)'
                                % (name, equation_text(stoichiometry),
                                   balance['net_atoms']))
            manager.Reactions.Add(name, 'equilibriumrxn')
            reaction = manager.Reactions.Item(name)
            resolution = self._add_reactants(reaction, stoichiometry)

            state = {'reaction': name, 'equation': equation_text(stoichiometry),
                     'requested_source': source,
                     'initial_lnk_source': int(reaction.LnKSource),
                     'reactants': resolution}
            reaction.AutoDetect = False
            # Setting Basis silently flips ReactionPhase, so apply the phase after.
            reaction.Basis = 2                 # partial-pressure basis
            reaction.BasisUnits2 = 'bar'
            reaction.TemperatureApproachValue = 0.0
            phase = str(entry.get('phase', 'vapour'))
            if phase not in PHASE_ENUM:
                raise SpecError('unknown reaction phase: %r' % phase)
            reaction.ReactionPhase = PHASE_ENUM[phase]
            fitted = derive(self._package, stoichiometry, target_K)
            reaction.LnKSource = 1             # eqrxn_LnKEquation, confirmed by probe 7
            reaction.EquilibriumConstantParameterArrayValue = self.win32com.client.VARIANT(
                self.pythoncom.VT_ARRAY | self.pythoncom.VT_R8, tuple(fitted['coefficients']))
            state.update(fitted)
            state['coefficients_readback'] = assert_readback(reaction, fitted['coefficients'])
            state['final_lnk_source'] = int(reaction.LnKSource)
            state['reaction_phase'] = int(reaction.ReactionPhase)
            state['basis'] = int(reaction.Basis)
            state['basis_units'] = str(reaction.BasisUnits2)
            state['auto_detect'] = bool(reaction.AutoDetect)
            detail.append(state)
            self.equilibrium_objects.append(reaction)
            reaction_set.ActiveReactions.Add(reaction)
        reaction_set.AssociateFluidPackage(self._package)
        # Finish the reaction system inside the basis edit, matching the verified
        # script: active reactions are registered and the package associated before
        # the basis edit ends. EndBasisChange is called by the caller afterwards.
        self.log.add('configure_equilibrium_reactions', detail=detail)
        return detail

    # ----------------------------------------------------------------- streams
    def create_streams_and_reactor(self, case, reactor_spec: dict[str, Any],
                                  feed_spec: dict[str, Any], reaction_set=None):
        kind = str(reactor_spec['kind'])
        if kind not in REACTOR_FACTORY:
            raise SpecError('unknown reactor kind: %r' % kind)
        flow = case.Flowsheet
        streams = flow.MaterialStreams
        # A newly created case must be empty. HYSYS resolves a duplicate case file
        # path against a case that is already open, and then SimulationCases.Add
        # returns something that is not clean: stream names get suffixed, and
        # streams.Item('FEED') can hand back a stream that is already attached to
        # another operation. Attaching it again fails with a bare E_FAIL that looks
        # like a connection bug. Detecting it here names the real cause instead.
        existing = (int(streams.Count), int(flow.Operations.Count))
        if existing != (0, 0):
            raise SpecError(
                'new case is not blank: it already contains %d stream(s) and %d '
                'operation(s). A case with the same file path (%s) is probably still '
                'open in HYSYS. Close it without saving, or use a unique output '
                'folder, then retry.' % (existing[0], existing[1],
                                         getattr(case, 'Name', 'see the case file')))
        for name in ('FEED', 'VAPOUR', 'LIQUID'):
            streams.Add(name)
        feed = streams.Item('FEED')
        native = delegates_total(feed_spec)
        molar_flows = composition_weights(feed_spec) if native else feed_molar_flows(feed_spec)
        total = sum(molar_flows.values())
        targets = []
        for name in self.readback_names:
            used = resolve_in_readback(name, list(molar_flows.keys()))
            targets.append(float(molar_flows[used]) / total if used else 0.0)
        feed.ComponentMolarFraction.Values = self.win32com.client.VARIANT(
            self.pythoncom.VT_ARRAY | self.pythoncom.VT_R8, tuple(targets))
        basis = str(feed_spec.get('basis', 'molar_fraction')).strip().casefold()
        # Composition and the total are specified the same way the verified script
        # does it: composition plus a MASS flow, letting HYSYS derive the molar
        # flow from its own molecular weights. Passing our own molar flow instead
        # was the one substantive difference from the script that already works on
        # this workstation, and it is also more accurate.
        # Use the same molar-mass resolver as the feed conversion, so a spec's
        # molar_mass override cannot make the mole and mass flows disagree.
        resolve_mass = feed_molar_mass(feed_spec)
        mass_total = sum(float(amount) * resolve_mass(name)
                         for name, amount in molar_flows.items())
        native_readback = None
        if native:
            # Set T/P first so both absolute flow readbacks have a defined state.
            feed.Temperature.SetValue(to_celsius(feed_spec['temperature'], feed_spec.get('temperature_unit', 'C')), 'C')
            feed.Pressure.SetValue(to_kpa(feed_spec['pressure'], feed_spec.get('pressure_unit', 'kPa')), 'kPa')
            # 'hysys' hands the unit to HYSYS verbatim; 'normal_volume' converts the
            # stated standard volume here and gives HYSYS a molar unit, because
            # HYSYS's own normal basis is 15 C and the exam states 0 C.
            native_readback = set_delegated_total(feed, feed_spec)
            self.native_flow_readback = native_readback
            self.log.add('native_feed_flow', detail=native_readback)
            flow_basis = ('hysys_native' if mode_of(feed_spec) == 'hysys'
                          else 'normal_volume_stated_basis')
        elif basis in ('molar_flow', 'mol_flow', 'mass_fraction', 'weight_fraction'):
            # The total was already given in mass or molar terms.
            if basis in ('molar_flow', 'mol_flow'):
                feed.MolarFlow.SetValue(total, 'kgmole/h')
                flow_basis = 'molar_flow'
            else:
                feed.MassFlow.SetValue(mass_total, 'kg/h')
                flow_basis = 'mass_flow'
        else:
            feed.MassFlow.SetValue(mass_total, 'kg/h')
            flow_basis = 'mass_flow'
        feed.Temperature.SetValue(
            to_celsius(feed_spec['temperature'], feed_spec.get('temperature_unit', 'C')),
            'C')
        feed.Pressure.SetValue(
            to_kpa(feed_spec['pressure'], feed_spec.get('pressure_unit', 'kPa')), 'kPa')
        # Read the molar flow back from HYSYS: this is the value HYSYS will use, and
        # every outlet comparison is scaled from it.
        hysys_molar = float(feed.MolarFlow.GetValue('kgmole/h'))
        if _is_unknown(hysys_molar) or hysys_molar <= 0:
            raise SpecError('HYSYS did not derive a molar flow from the feed: %.6g'
                            % hysys_molar)
        # Keep the feed molar flows on this builder. Do NOT attach a Python
        # attribute to any HYSYS COM object: writing an unrecognised property onto
        # the basis left the FEED stream unusable and made Feeds.Add fail with a
        # bare E_FAIL, while a brand-new stream built from the same code was
        # accepted. That was isolated by the self-diagnosis below.
        if native:
            molar_flows = {k: v / total * hysys_molar for k, v in molar_flows.items()}
            total = hysys_molar
        self.feed_values = {canonical(k): float(v) for k, v in molar_flows.items()}

        flow.EnergyStreams.Add('DUTY')
        operations = flow.Operations
        operations.Add(str(reactor_spec.get('name', 'RX')), REACTOR_FACTORY[kind])
        reactor = operations.Item(str(reactor_spec.get('name', 'RX')))
        type_name = str(reactor.TypeName)
        # Measured on the remote V15 workstation: TypeName returns the factory
        # identifier itself ('conversionreactorop'), NOT the interface class name
        # ('ConversionReactor'). Accept either so the check works on both, and keep
        # the observed value in the log rather than assuming which one came back.
        factory = REACTOR_FACTORY[kind]
        accepted = {factory.casefold(), REACTOR_TYPE_NAMES[kind].casefold()}
        if type_name.replace('_', '').casefold() not in accepted:
            raise SpecError('operation type mismatch: asked for %s, HYSYS created %s'
                            % (kind, type_name))

        # Every COM call below is recorded individually. A failure inside this block
        # previously surfaced only as a bare E_FAIL with no indication of which call
        # produced it, which cost a whole remote run to localise.
        trace: list[dict[str, Any]] = []

        def step(label, action, store=None, on_failure=None):
            entry: dict[str, Any] = {'call': label}
            try:
                value = action()
                entry['result'] = 'OK'
                if store is not None:
                    entry['value'] = store(value)
                return value
            except Exception as exc:
                entry['result'] = 'FAILED'
                entry['error'] = '%s: %s' % (type(exc).__name__, exc)
                trace.append(entry)
                if on_failure is not None:
                    try:
                        entry['diagnosis'] = on_failure()
                    except Exception as diag_exc:
                        entry['diagnosis_error'] = '%s: %s' % (
                            type(diag_exc).__name__, diag_exc)
                self.log.add('reactor_connection', status='FAILED', detail=trace)
                raise
            finally:
                if entry not in trace:
                    trace.append(entry)

        # Record whether the FEED stream is actually complete before attaching it.
        # HYSYS refuses to connect a stream whose values are still unknown, and that
        # reason must be visible here instead of surfacing later as a bare E_FAIL.
        feed_state: dict[str, Any] = {'call': 'feed_state_before_attach'}
        try:
            fractions = [float(v) for v in feed.ComponentMolarFraction.Values]
            feed_state.update({
                'fraction_sum': sum(fractions),
                'fractions_finite': all(math.isfinite(v) for v in fractions),
                'temperature_is_known': bool(feed.Temperature.IsKnown),
                'pressure_is_known': bool(feed.Pressure.IsKnown),
                'molar_flow_is_known': bool(feed.MolarFlow.IsKnown),
                'temperature_C': float(feed.Temperature.GetValue('C')),
                'pressure_kPa': float(feed.Pressure.GetValue('kPa')),
                'molar_flow_kmol_h': float(feed.MolarFlow.GetValue('kgmole/h')),
            })
            feed_state['result'] = 'OK'
        except Exception as exc:
            feed_state['result'] = 'FAILED'
            feed_state['error'] = '%s: %s' % (type(exc).__name__, exc)
        trace.append(feed_state)

        step('hold_ignored', lambda: setattr(reactor, 'IsIgnored', True),
             lambda _: bool(reactor.IsIgnored))
        # Attach the reaction set before the streams, matching the order of the
        # script that is already verified on this workstation. A reactor whose
        # reaction set is attached last is the one shape that has never been
        # proven to work here.
        if reaction_set is not None:
            step('set_reaction_set', lambda: setattr(reactor, 'ReactionSet',
                                                     reaction_set),
                 lambda _: str(reactor.ReactionSet.Name))
        step('feeds_add', lambda: reactor.Feeds.Add(feed),
             lambda _: int(reactor.Feeds.Count),
             on_failure=lambda: diagnose_feed_attachment(
                 flow, streams, reactor, feed_spec, self.win32com, self.pythoncom,
                 self.readback_names, self.feed_values))
        step('set_vapour_product', lambda: setattr(
            reactor, 'VapourProduct', streams.Item('VAPOUR')),
            lambda _: str(reactor.VapourProduct.Name))
        step('set_liquid_product', lambda: setattr(
            reactor, 'LiquidProduct', streams.Item('LIQUID')),
            lambda _: str(reactor.LiquidProduct.Name))
        step('set_energy_stream', lambda: setattr(
            reactor, 'EnergyStream', flow.EnergyStreams.Item('DUTY')),
            lambda _: str(reactor.EnergyStream.Name))
        step('set_pressure_drop', lambda: reactor.PressureDrop.SetValue(
            float(reactor_spec.get('pressure_drop_kPa', 0.0)), 'kPa'),
            lambda _: float(reactor.PressureDrop.GetValue('kPa')))

        thermal = str(reactor_spec.get('thermal_mode', 'adiabatic'))
        if thermal == 'isothermal':
            if 'outlet_temperature' not in reactor_spec:
                raise SpecError('isothermal mode needs outlet_temperature')
            outlet_c = to_celsius(reactor_spec['outlet_temperature'],
                                  reactor_spec.get('outlet_temperature_unit', 'C'))
            step('set_outlet_temperature', lambda: streams.Item('VAPOUR')
                 .Temperature.SetValue(outlet_c, 'C'),
                 lambda _: float(streams.Item('VAPOUR').Temperature.GetValue('C')))
        elif thermal == 'adiabatic':
            outlet_c = None
            if kind != 'gibbs':
                # A zero-duty energy stream is the verified way to hold adiabatic.
                step('set_adiabatic_duty', lambda: reactor.HeatFlow.SetValue(0.0, 'kW'),
                     lambda _: float(reactor.HeatFlow.GetValue('kW')))
        else:
            raise SpecError('unknown thermal_mode: %r' % thermal)

        self.log.add('create_streams_and_reactor', detail={
            'reactor_kind': kind,
            'reactor_type_name': type_name,
            'feed_molar_flows_kmol_h': molar_flows,
            'feed_total_kmol_h': total,
            'feed_flow_basis': flow_basis,
            'native_flow_readback': native_readback,
            'hysys_molar_flow_kmol_h': hysys_molar,
            'hysys_mass_flow_kg_h': float(feed.MassFlow.GetValue('kg/h')),
            'thermal_mode': thermal,
            'outlet_temperature_C': outlet_c,
            'connections': trace,
        })
        return reactor, streams, outlet_c, hysys_molar

    # ------------------------------------------------------------------ solve
    def solve_and_read(self, case, reactor, streams, outlet_c):
        # Reactivating the operation is what makes HYSYS solve it. The verified
        # scripts all did this; without it every product stream stays at the
        # "unknown" sentinel and nothing is ever computed.
        entry: dict[str, Any] = {'call': 'release_ignored'}
        try:
            reactor.IsIgnored = False
            entry['result'] = 'OK'
            entry['value'] = bool(reactor.IsIgnored)
        except Exception as exc:
            entry['result'] = 'FAILED'
            entry['error'] = '%s: %s' % (type(exc).__name__, exc)
        self.log.add('release_ignored_before_solve', detail=entry)
        if entry['result'] != 'OK':
            raise RuntimeError('could not reactivate the operation before solving: '
                               '%s' % entry.get('error'))
        deadline = time.monotonic() + SOLVER_TIMEOUT_SECONDS
        stable = 0
        last_error = ''
        best = None
        previous: dict[str, float] | None = None
        polls = 0
        while time.monotonic() < deadline:
            polls += 1
            try:
                if bool(case.Solver.IsSolving):
                    raise ValueError('solver busy')
                flows: dict[str, float] = {}
                outlet: dict[str, Any] = {}
                snapshot: dict[str, float] = {}
                for name in ('VAPOUR', 'LIQUID'):
                    stream = streams.Item(name)
                    amount = float(stream.MolarFlow.GetValue('kgmole/h'))
                    if _is_unknown(amount):
                        # -32767 is HYSYS's "no value" sentinel, not a negative
                        # flow: it means the operation never solved.
                        raise ValueError(
                            'flow on %s reads the HYSYS unknown sentinel (%.6g), so '
                            'the operation has not been solved' % (name, amount))
                    if amount < -1e-6:
                        raise ValueError('negative flow on %s (%.6g kmol/h)'
                                         % (name, amount))
                    entry: dict[str, Any] = {'molar_flow_kmol_h': amount}
                    snapshot[name + '.molar_flow'] = amount
                    if amount > 1e-8:
                        temperatures = float(stream.Temperature.GetValue('C'))
                        pressures = float(stream.Pressure.GetValue('kPa'))
                        if _is_unknown(temperatures) or _is_unknown(pressures):
                            raise ValueError(
                                'temperature/pressure on %s read the HYSYS unknown '
                                'sentinel (%.6g C, %.6g kPa)'
                                % (name, temperatures, pressures))
                        fractions = [float(v)
                                     for v in stream.ComponentMolarFraction.Values]
                        if len(fractions) != len(self.readback_names):
                            raise ValueError('component vector length mismatch on %s' % name)
                        if any(not math.isfinite(v) or v < -1e-8 for v in fractions):
                            raise ValueError('invalid composition on %s' % name)
                        if abs(sum(fractions) - 1) > 1e-6:
                            raise ValueError('mole fractions do not sum to 1 on %s'
                                             % name)
                        entry['temperature_C'] = temperatures
                        entry['pressure_kPa'] = pressures
                        mass_flow = float(stream.MassFlow.GetValue('kg/h'))
                        if _is_unknown(mass_flow) or mass_flow < 0:
                            raise ValueError('invalid mass flow on %s' % name)
                        if temperatures < -273.15 or pressures <= 0:
                            raise ValueError('nonphysical temperature/pressure on %s' % name)
                        entry['mass_flow_kg_h'] = mass_flow
                        snapshot[name + '.temperature_C'] = temperatures
                        snapshot[name + '.pressure_kPa'] = pressures
                        snapshot[name + '.mass_flow'] = mass_flow
                        entry['mole_fractions'] = {
                            self.readback_names[i]: fractions[i]
                            for i in range(len(self.readback_names))}
                        for name_i, fraction in entry['mole_fractions'].items():
                            snapshot[name + '.fraction.' + name_i] = fraction
                            key = canonical(name_i)
                            flows[key] = flows.get(key, 0.0) + amount * fraction
                    outlet[name] = entry
                if sum(flows.values()) <= 0:
                    raise ValueError('outlet totals zero')
                duty = float(reactor.HeatFlow.GetValue('kW'))
                if _is_unknown(duty):
                    raise ValueError('heat duty is unknown or non-finite')
                snapshot['heat_duty_kW'] = duty
                if bool(case.Solver.IsSolving):
                    raise ValueError('solver restarted during readback')
                # Require consecutive readings to AGREE, not merely to be valid. This
                # guards against returning while the solver is still moving.
                # It does NOT detect a wrong answer: a consistent wrong answer is
                # consistent, so three identical pass-through readings still look
                # stable here. Only the conversion check in validate.py catches a
                # reactor whose reaction never took effect.
                if previous is not None and _flows_match(previous, snapshot):
                    stable += 1
                else:
                    stable = 1
                previous = snapshot
                best = (flows, outlet, duty)
                if stable >= REQUIRED_STABLE_READS:
                    break
            except Exception as exc:
                stable = 0
                previous = None
                last_error = str(exc)
            time.sleep(0.4)
        if best is None:
            raise RuntimeError('no valid outlet within %.0fs: %s'
                               % (SOLVER_TIMEOUT_SECONDS, last_error))
        if stable < REQUIRED_STABLE_READS:
            raise ResultCheckError(
                'outlet readings never reached %d consecutive stable reads within '
                '%.0fs (last error: %s); no solved result is accepted'
                % (REQUIRED_STABLE_READS, SOLVER_TIMEOUT_SECONDS, last_error))
        if bool(case.Solver.IsSolving):
            raise ResultCheckError('solver restarted after stable readback')
        flows, outlet, duty = best
        # Report the requested/readback name pairs alongside internal keys.
        named = {resolve_in_readback(key, self.readback_names) or key: value
                 for key, value in flows.items()}
        result = {
            'outlet': outlet,
            'component_flows_kmol_h': named,
            'component_flows_internal': flows,
            'heat_duty_kW': duty,
            'solver_is_solving': False,
            'solver_evidence': {
                'stable_reads': stable, 'required_stable_reads': REQUIRED_STABLE_READS,
                'polls': polls, 'absolute_stability_tolerance': 1e-9,
                'checks': 'phase flows, composition, temperature, pressure, mass flow, duty',
                'note': 'Stable readback and idle solver; not an independent proof of thermodynamic equilibrium.',
            },
        }
        if outlet_c is not None:
            for name, entry in outlet.items():
                if entry['molar_flow_kmol_h'] > 1e-8:
                    if abs(entry['temperature_C'] - outlet_c) > 0.01:
                        raise ResultCheckError(
                            'outlet temperature on %s is %.4f C but %.4f C was '
                            'requested' % (name, entry['temperature_C'], outlet_c))
        feed_spec = self.spec['feeds'][0]
        expected_pressure = to_kpa(feed_spec['pressure'], feed_spec.get('pressure_unit', 'kPa'))
        expected_pressure -= float(self.spec['reactor'].get('pressure_drop_kPa', 0.0))
        for name, entry in outlet.items():
            if entry['molar_flow_kmol_h'] > 1e-8:
                if not math.isclose(entry['pressure_kPa'], expected_pressure,
                                    rel_tol=1e-6, abs_tol=0.01):
                    raise ResultCheckError('outlet pressure on %s is %.6g kPa; expected %.6g kPa'
                                           % (name, entry['pressure_kPa'], expected_pressure))
        if self.spec['reactor'].get('thermal_mode', 'adiabatic') == 'adiabatic':
            if abs(duty) > 1e-6:
                raise ResultCheckError('adiabatic duty is not zero: %.6g kW' % duty)
        self.log.add('solve_and_read_outputs', detail={
            'component_flows_kmol_h': named,
            'heat_duty_kW': result['heat_duty_kW'],
            'solver_is_solving': result['solver_is_solving'],
            'solver_evidence': result['solver_evidence'],
        })
        return result
