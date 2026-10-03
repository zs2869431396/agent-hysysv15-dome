"""Ready-to-run example specifications.

These are the three exam scenarios expressed in the tool layer's own format. They
exist so a remote run needs no hand-written JSON, and so the tool layer can be
compared against the results the earlier scripts already produced.

Measured targets from the earlier verified runs (used to check the tool layer does
not drift):
  * toluene   : 10000 kg/h, 380 C, 2.5 MPa, 50% conversion -> 49.999999999999986%
  * reforming : 710 C -> CH4 conversion 54.0358%, H2 1942.81 kmol/h, duty 39988.61 kW
                600 C -> CH4 conversion 30.3524%, H2 1163.67 kmol/h, duty 20159.50 kW
Both were produced by dedicated scripts; reproducing them through this tool layer
is the acceptance test for the layer itself.
"""
from __future__ import annotations

from typing import Any

from .core import SPEC_SCHEMA


def toluene_spec() -> dict[str, Any]:
    """Scenario 2: toluene disproportionation, Conversion reactor."""
    return {
        'schema': SPEC_SCHEMA,
        'case_name': 'agent-toluene',
        'scenario': 'toluene_disproportionation',
        'fluid_package': {
            'name': 'TOL-PR',
            'property_package': 'PengRob',
            'components': ['Toluene', 'Benzene', 'o-Xylene', 'm-Xylene', 'p-Xylene'],
        },
        'feeds': [{
            'name': 'FEED',
            'basis': 'molar_fraction',
            'fractions': {'Toluene': 1.0},
            'total_flow': 10000.0,
            'total_flow_unit': 'kg/h',
            'temperature': 380.0,
            'temperature_unit': 'C',
            'pressure': 2.5,
            'pressure_unit': 'MPa',
        }],
        'reactions': [{
            'name': 'TOL-DISPROP',
            # One overall reaction: three xylene isomers at a third each is the
            # approved assumption, not a predicted selectivity.
            'stoichiometry': {'Toluene': -2.0, 'Benzene': 1.0, 'o-Xylene': 1 / 3,
                              'm-Xylene': 1 / 3, 'p-Xylene': 1 / 3},
            'conversion_percent': 50.0,
            'base_component': 'Toluene',
            'phase': 'combined',
        }],
        'reactor': {
            'name': 'TOL-RX',
            'kind': 'conversion',
            'thermal_mode': 'adiabatic',
            'pressure_drop_kPa': 0.0,
        },
        'assumptions': [
            'The o/m/p xylene split of one third each is a user-approved assumption, '
            'not an industrial selectivity or a thermodynamic prediction.',
            'Adiabatic with zero pressure drop: the exam gives no outlet temperature '
            'or duty, so the outlet temperature is computed by HYSYS.',
            'Peng-Robinson; 2.5 MPa treated as absolute.',
        ],
        'open_questions': [],
    }


def reforming_spec(outlet_temperature: float = 710.0) -> dict[str, Any]:
    """Scenario 1: methane steam reforming, Gibbs reactor, one temperature."""
    return {
        'schema': SPEC_SCHEMA,
        'case_name': 'agent-smr-%gC' % outlet_temperature,
        'scenario': 'methane_steam_reforming',
        'fluid_package': {
            'name': 'SMR-PR',
            'property_package': 'PengRob',
            'components': ['Methane', 'Water', 'CO', 'Hydrogen', 'CO2'],
        },
        'feeds': [{
            'name': 'FEED',
            'basis': 'molar_flow',
            'flows': {'Methane': 1000.0, 'Water': 2700.0},
            'temperature': 520.0,
            'temperature_unit': 'C',
            'pressure': 13.5,
            'pressure_unit': 'bar',
        }],
        'reactions': [],
        'reactor': {
            'name': 'SMR',
            'kind': 'gibbs',
            'thermal_mode': 'isothermal',
            'outlet_temperature': float(outlet_temperature),
            'outlet_temperature_unit': 'C',
            'pressure_drop_kPa': 0.0,
        },
        'assumptions': [
            'The Gibbs reactor is used rather than an Equilibrium reactor: this '
            'workstation cannot set the Equilibrium Ln(K) source to Gibbs (it stays '
            'FixedK = 2), so an Equilibrium reactor would silently use a fixed K. '
            'The exam allows whichever reactor type fits, and a reversible '
            'equilibrium-controlled high-temperature system is what Gibbs models.',
            'Outlet temperature is held by an external heat stream: this is an '
            'isothermal case and the reported duty is external heat, not adiabatic.',
            'The reported duty is the REACTOR duty, not the heat of reaction alone: '
            'since the feed enters at 520 C and leaves at the stated outlet '
            'temperature, it includes the sensible heat of raising the feed to that '
            'temperature as well as the endothermic heat of reaction. The exam does '
            'not define which figure is wanted, so the scope is stated explicitly.',
            '13.5 bar treated as absolute; zero pressure drop.',
            'Feed 1000/2700 kmol/h is a demonstration design basis (about 128 kt/y '
            'methane at 8000 h/y), not an actual plant figure.',
            'The key conversion for this scenario is the METHANE conversion, reported '
            'in checks.reactant_conversion_percent. A carbon conversion is not '
            'defined here because no solid carbon enters.',
        ],
        'open_questions': [
            'Earlier probes returned Gibbs temperature-limit numbers 25/426.85 without '
            'confirmed units. Do not label them K or C until the property units are '
            'verified. A good ln(K) fit and Q/K agreement do not prove the underlying '
            'Gibbs data are valid at the operating temperature.',
        ],
    }


def gasification_spec() -> dict[str, Any]:
    """Scenario 3: coal slurry gasification.

    Deliberately incomplete: the exam gives 80000 Nm3/h without saying which stream
    or which standard conditions, and it is not stated whether the coal may be
    treated as pure carbon. Those are substantive gaps, so this spec is written
    with open questions instead of silent assumptions. The runner will refuse it,
    which is the intended behaviour.
    """
    return {
        'schema': SPEC_SCHEMA,
        'case_name': 'agent-gasification',
        'scenario': 'coal_slurry_gasification',
        'fluid_package': {
            'name': 'GAS-PR',
            'property_package': 'PengRob',
            'components': ['Carbon', 'Water', 'CO', 'Hydrogen', 'CO2', 'Methane'],
        },
        'feeds': [{
            'name': 'SLURRY',
            'basis': 'mass_fraction',
            'fractions': {'Carbon': 0.62, 'Water': 0.38},
            'total_flow': 80000.0,
            'total_flow_unit': 'Nm3/h',
            'temperature': 40.0,
            'temperature_unit': 'C',
            'pressure': 40.0,
            'pressure_unit': 'bar',
        }],
        'reactions': [],
        'reactor': {
            'name': 'GASIFIER',
            'kind': 'gibbs',
            'thermal_mode': 'isothermal',
            'outlet_temperature': 1400.0,
            'outlet_temperature_unit': 'C',
            'pressure_drop_kPa': 0.0,
        },
        'assumptions': [
            'Slurry at 62 wt% coal, 40 C, 40 bar absolute, outlet 1400 C.',
            'Ash is neglected as the exam asks.',
        ],
        'open_questions': [
            'Which stream does 80000 Nm3/h refer to, and at which standard '
            'conditions (0 C / 101.325 kPa, or 15 C)? A coal-water slurry cannot be '
            'treated as a gas-equivalent normal volume flow without that.',
            'Does neglecting ash allow the coal to be treated as pure carbon? If '
            'elemental analysis is available it should be used instead.',
            'The exam provides no oxygen feed. Holding 1400 C therefore means an '
            'externally heated model with a reported duty, not autothermal '
            'gasification.',
            'Solid-carbon support in V15 Gibbs has not been verified beyond adding '
            'the component.',
        ],
        'blocking_questions': [
            'Define which stream 80000 Nm3/h describes and its standard conditions.',
            'Confirm whether the coal may be represented by pure solid carbon.',
        ],
    }


def gasification_native_spec() -> dict[str, Any]:
    """Exam clarification: one mixed feed, 80000 Nm3/h at 0 C / 101.325 kPa.

    The examiner stated the standard conditions, so the normal volume is converted
    here on exactly that basis and HYSYS is given a molar flow. Probe evidence
    (`probe-runs/native-flow-units-*`) shows no HYSYS property accepts `Nm3/h`, and
    that HYSYS's own normal basis on this workstation is 15 C - so delegating the
    volume would have applied a 5.2% different basis without saying so.

    Pure carbon remains an explicit modelling assumption, not an examiner fact.
    """
    spec = gasification_spec()
    spec['case_name'] = 'agent-gasification-native'
    spec['feeds'][0].update(flow_input='normal_volume',
                            standard_temperature_C=0.0,
                            standard_pressure_kPa=101.325)
    spec['blocking_questions'] = []
    spec['open_questions'] = spec['open_questions'][2:]
    spec['assumptions'].extend([
        'Examiner clarified: 80000 Nm3/h is the total of the single mixed inlet, at 0 C / 101.325 kPa.',
        'Coal is modelled as pure solid carbon (modelling assumption, not implied by neglecting ash).',
        "The normal volume is converted on the stated basis (22.41397 m3/kmol) and handed "
        "to HYSYS as kgmole/h. HYSYS's own standard gas basis here measured 15 C, so a "
        "HYSYS volume unit would have applied a 5.2% different basis silently.",
    ])
    return spec


def gasification_saturation_spec() -> dict[str, Any]:
    """The native gasification spec, solved by imposed graphite saturation.

    A plain Gibbs case cannot be used: HYSYS V15's library Carbon carries the Gibbs data
    of gaseous atomic carbon (EvaluateGibbs(298.15 K) = +671.28 kJ/mol; graphite is 0),
    so the minimiser consumes as much carbon as O and H allow. The library component is
    read-only, and a hypothetical created over COM is a fluid with no writable enthalpy
    data. `reactor.solid_carbon = "saturation"` never uses carbon's Gibbs data: see
    saturation.py. Verified by probe five (probe-runs/route2-20261003-084249).
    """
    spec = gasification_native_spec()
    spec['case_name'] = 'agent-gasification-saturation'
    spec['reactor']['solid_carbon'] = 'saturation'
    spec['open_questions'] = [q for q in spec['open_questions']
                              if 'Solid-carbon support' not in q]
    spec['assumptions'].extend([
        'HYSYS V15 library Carbon carries the Gibbs data of gaseous atomic carbon '
        '(+671.28 kJ/mol at 298.15 K against the textbook +671.3; graphite is 0 by '
        'definition) while its enthalpy data is graphite\'s. A plain Gibbs reactor would '
        'therefore return an impossible outlet; it is not used.',
        'Graphite saturation is imposed instead: a conversion reactor converts a fraction '
        'X of the carbon by the bookkeeping reaction 3C + 2H2O -> 2CO + CH4, a gas-only '
        'Gibbs reactor re-equilibrates CO/CO2/H2/H2O/CH4 with HYSYS\'s own gas data, and X '
        'is solved so that the gas carbon activity is exactly 1. The bookkeeping '
        'reaction does not affect the result; the reported duty is the sum of both '
        'reactors.',
    ])
    return spec


def equilibrium_reforming_spec(outlet_temperature: float = 710.0) -> dict[str, Any]:
    spec = reforming_spec(outlet_temperature)
    spec['case_name'] = 'agent-smr-equilibrium-%gC' % outlet_temperature
    spec['reactor'].update(kind='equilibrium', name='SMR-EQ')
    spec['reactions'] = [
        {'name':'SMR-EQ', 'phase':'vapour', 'stoichiometry':{'Methane':-1,'Water':-1,'CO':1,'Hydrogen':3}},
        {'name':'WGS-EQ', 'phase':'vapour', 'stoichiometry':{'CO':-1,'Water':-1,'CO2':1,'Hydrogen':1}},
    ]
    spec['assumptions'][0] = 'Gas-phase Equilibrium with ln(K) fitted from HYSYS Gibbs data on a 1 atm reference, converted to bar partial-pressure basis. Gibbs is retained as a separate cross-check.'
    return spec


EXAMPLES = {
    'coal-slurry-gasification-saturation': gasification_saturation_spec,
    'coal-slurry-gasification-native': gasification_native_spec,
    'toluene-disproportionation': toluene_spec,
    'methane-steam-reforming-710C': lambda: equilibrium_reforming_spec(710.0),
    'methane-steam-reforming-600C': lambda: equilibrium_reforming_spec(600.0),
    'methane-steam-reforming-gibbs-710C': lambda: reforming_spec(710.0),
    'methane-steam-reforming-gibbs-600C': lambda: reforming_spec(600.0),
    'coal-slurry-gasification': gasification_saturation_spec,
    'coal-slurry-gasification-unclarified': gasification_spec,
}


def write_examples(folder) -> list[str]:
    """Write every example spec into a folder; returns the file names written."""
    import json
    from pathlib import Path
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for name, factory in EXAMPLES.items():
        path = folder / ('%s.json' % name)
        path.write_text(json.dumps(factory(), indent=2, ensure_ascii=False),
                        encoding='utf-8')
        written.append(path.name)
    return written
