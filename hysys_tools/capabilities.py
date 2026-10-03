"""Machine-readable scope; historical verification is not a release acceptance."""

TOOL_REVISION = '2026-10-03-equilibrium-integration-1'


def capability_metadata():
    evidence = 'tool-layer-runs/20261002-111439'
    return {
        'tool_revision': TOOL_REVISION,
        'current_revision_remote_validation': 'pending',
        'native_flow': {'status': 'experimental', 'properties': ['MolarFlow', 'MassFlow'],
                        'unit_definition': 'delegated to HYSYS; exact COM unit token needs remote validation'},
        'max_feeds': 1,
        'max_conversion_reactions': 1,
        'parallel_hysys_calls_supported': False,
        'capability_combinations': [
            {'kind': 'conversion', 'thermal_mode': 'adiabatic',
             'scope': 'single fixed-conversion toluene reaction',
             'status': 'historically_verified', 'evidence': evidence + '/toluene/result.json'},
            {'kind': 'conversion', 'thermal_mode': 'isothermal',
             'scope': 'single reaction', 'status': 'experimental'},
            {'kind': 'conversion', 'scope': 'multiple reactions', 'status': 'unsupported'},
            {'kind': 'conversion', 'scope': 'temperature-dependent conversion coefficients',
             'status': 'experimental', 'independent_conversion_check': False},
            {'kind': 'gibbs', 'thermal_mode': 'isothermal',
             'scope': 'CH4/H2O/CO/H2/CO2 steam reforming',
             'status': 'historically_verified',
             'evidence': [evidence + '/smr-710C/result.json', evidence + '/smr-600C/result.json']},
            {'kind': 'gibbs', 'thermal_mode': 'isothermal',
             'scope': 'solid-carbon gasification, plain Gibbs with library Carbon',
             'status': 'refused before the solve',
             'reason': 'library Carbon carries gaseous-atomic-carbon Gibbs data '
                       '(probe-runs/carbon-properties-*, graphite-carbon-v3-*)'},
            {'kind': 'gibbs', 'thermal_mode': 'isothermal', 'solid_carbon': 'saturation',
             'scope': 'carbon + water feed; graphite saturation imposed',
             'status': 'experimental',
             'evidence': 'tool-layer-runs/native-flow-20261003-091907-5550b747/gasification/result.json',
             'reason': 'Historical standalone tool-path PASS; integrated full acceptance pending'},
            {'kind': 'gibbs', 'thermal_mode': 'adiabatic', 'status': 'unsupported'},
            {'kind': 'equilibrium', 'thermal_mode':'isothermal', 'phase':'vapour',
             'status': 'experimental', 'strategy':'lnk_equation_array',
             'evidence':'probe-runs/equilibrium-20261003-101446/probe-equilibrium-7.json',
             'reason':'Probe passed at 600/710 C. Generalised production path awaits remote acceptance; source=1, 8 coefficients, mandatory Q/K gate.'},
            {'kind': 'cstr', 'status': 'unsupported'},
            {'kind': 'pfr', 'status': 'unsupported'},
        ],
    }
