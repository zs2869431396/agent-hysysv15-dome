"""Machine-readable capability lookup for reactor/model combinations.

The tool layer already publishes `hysys_tools.capabilities.capability_metadata()`.
This module turns that list into a lookup that answers one question precisely:

    given (reactor kind, thermal mode, phase, reaction count, solid phase),
    is this combination verified, experimental or unsupported, and on what
    evidence?

Why a lookup rather than prose: the plan's complaint about the old capability
table was that "Gibbs is verified" reads as though every Gibbs combination were
verified, including adiabatic operation and solid carbon, which it is not. The
answer the agent needs is per combination.

Nothing here invents status. Every entry either cites a real HYSYS result on this
workstation or says plainly that there is none.
"""
from __future__ import annotations

from typing import Any

from hysys_tools import capabilities as tool_capabilities

# The one real result set that exists, used by the tool layer's own table.
EVIDENCE_ROOT = 'tool-layer-runs/20261002-111439'
REMOTE_ACCEPTANCE = 'tool-layer-runs/acceptance-20261002-144131-d442afae'


def capability_report() -> dict[str, Any]:
    """The tool layer's capability metadata, plus what the agent adds to it."""
    report = dict(tool_capabilities.capability_metadata())
    report['agent_notes'] = {
        'evidence_root': EVIDENCE_ROOT,
        'remote_acceptance': REMOTE_ACCEPTANCE,
        'remote_acceptance_status': 'BASELINE_PASS_GASIFICATION_STILL_BLOCKED',
        'remote_acceptance_scope': (
            'toluene (conversion, adiabatic), SMR 710 C and 600 C (gibbs, '
            'isothermal). All three matched the historical baseline bit for bit.'),
        'not_proven_by_acceptance': [
            'gibbs with solid carbon entering the reactor (gasification)',
            'any adiabatic gibbs case',
            'isothermal conversion',
            'more than one conversion reaction',
            'CSTR or PFR of any kind',
        ],
    }
    return report


def _unsupported(reason: str, rule: str) -> dict[str, Any]:
    return {'status': 'unsupported', 'reason': reason, 'evidence': [],
            'rule': rule}


def combination_status(reactor_kind: str, thermal_mode: str | None = None,
                       phase: str | None = None, reaction_count: int = 1,
                       solid_phase: bool = False) -> dict[str, Any]:
    """Status of one reactor/model combination.

    `solid_phase` means a solid component actually takes part (carbon in the
    gasifier), not merely that one appears in the component list.
    """
    kind = str(reactor_kind).strip().casefold()
    thermal = None if thermal_mode is None else str(thermal_mode).strip().casefold()

    # ------------------------------------------------------- not implemented
    if kind in ('cstr', 'pfr'):
        return _unsupported(
            'No %s implementation exists in the tool layer. The exam\'s selection '
            'logic names kinetic reactors, but no scenario supplies a rate law, so '
            'this is a declared capability boundary rather than a modelling '
            'failure.' % kind.upper(), 'no_implementation')

    if kind == 'equilibrium':
        return _unsupported(
            'The equilibrium reactor cannot be used on this workstation: the Ln(K) '
            'source cannot be set to Gibbs (it stays FixedK=2), so the tool layer '
            'refuses the path instead of silently using a fixed K. Use gibbs.',
            'lnk_source_unavailable')

    # ------------------------------------------------------------- conversion
    if kind == 'conversion':
        if reaction_count > 1:
            return _unsupported(
                'The independent conversion check cannot separate the individual '
                'conversions of several reactions that share a base component, so '
                'the tool layer refuses more than one conversion reaction rather '
                'than let a total conversion masquerade as each reaction\'s.',
                'multiple_conversion_reactions')
        if thermal == 'adiabatic':
            return {'status': 'verified',
                    'reason': 'Single fixed-conversion reaction, adiabatic duty fixed '
                              'at zero.',
                    'evidence': [EVIDENCE_ROOT + '/toluene/result.json',
                                 REMOTE_ACCEPTANCE + '/toluene/result.json'],
                    'rule': 'conversion_adiabatic_single'}
        if thermal == 'isothermal':
            return {'status': 'experimental',
                    'reason': 'Isothermal conversion has no accepted real run; the '
                              'code path exists.',
                    'evidence': [], 'rule': 'conversion_isothermal_single'}
        return {'status': 'experimental',
                'reason': 'Thermal mode not stated; the verified conversion case was '
                          'adiabatic.',
                'evidence': [], 'rule': 'conversion_unspecified_thermal'}

    # ------------------------------------------------------------------ gibbs
    if kind == 'gibbs':
        if thermal == 'adiabatic':
            return _unsupported(
                'Adiabatic operation is not implemented for the Gibbs reactor: the '
                'executor sets no thermal condition for it, so the case would have '
                'no stated boundary and was never verified.',
                'gibbs_adiabatic_unimplemented')
        if thermal != 'isothermal':
            return {'status': 'experimental',
                    'reason': 'Thermal mode not stated; only isothermal Gibbs has '
                              'been verified.',
                    'evidence': [], 'rule': 'gibbs_unspecified_thermal'}
        if solid_phase:
            return {'status': 'experimental',
                    'reason': 'Gibbs with a solid component taking part has not been '
                              'verified beyond being able to add the component. '
                              'Solid-phase readback and residual carbon must be '
                              'checked on the real case.',
                    'evidence': [], 'rule': 'gibbs_isothermal_solid'}
        return {'status': 'verified',
                'reason': 'Isothermal Gibbs with a gas-phase candidate set. Verified '
                          'on CH4/H2O/CO/H2/CO2 steam reforming at 710 C and 600 C.',
                'evidence': [EVIDENCE_ROOT + '/smr-710C/result.json',
                             EVIDENCE_ROOT + '/smr-600C/result.json',
                             REMOTE_ACCEPTANCE + '/smr-710C/result.json',
                             REMOTE_ACCEPTANCE + '/smr-600C/result.json'],
                'rule': 'gibbs_isothermal_gas'}

    return _unsupported('Unknown or unhandled reactor kind: %r' % reactor_kind,
                        'unknown_kind')


def is_executable(reactor_kind: str, thermal_mode: str | None = None,
                  reaction_count: int = 1, solid_phase: bool = False) -> bool:
    """True when the tool layer can actually build and run the combination."""
    return combination_status(reactor_kind, thermal_mode, reaction_count=reaction_count,
                              solid_phase=solid_phase)['status'] != 'unsupported'
