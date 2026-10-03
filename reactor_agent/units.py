"""Units, and what physical quantity each one measures.

Shared by `extraction` and `normalize`, because both need to answer the same
question and they must answer it the same way: *is this number a temperature, a
pressure, or a flow?*

That question is the basis of the anti-fabrication check. An earlier version compared
values only, so a model that put the temperature (380) into the flow field passed:
380 does appear in the request. It appears next to `℃`, not next to `kg/h`. Comparing
the value *together with its unit* is what distinguishes "the user said this" from
"this number exists somewhere in the sentence".

Only the units this project actually meets are listed. A unit that is not here is
reported as unknown rather than guessed at.
"""
from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------- spellings

TEMPERATURE_UNITS = {
    'c': 'C', 'degc': 'C', 'celsius': 'C', 'centigrade': 'C', '°c': 'C',
    '℃': 'C', '度': 'C', '摄氏度': 'C', '摄氏': 'C',
    'k': 'K', 'kelvin': 'K', '开尔文': 'K', '°k': 'K',
    'f': 'F', 'degf': 'F', 'fahrenheit': 'F', '华氏度': 'F', '华氏': 'F', '°f': 'F',
}

PRESSURE_UNITS = {
    'pa': 'Pa', 'kpa': 'kPa', 'mpa': 'MPa', 'bar': 'bar', 'mbar': 'mbar',
    'atm': 'atm', 'psi': 'psi',
    '帕': 'Pa', '千帕': 'kPa', '兆帕': 'MPa', '巴': 'bar', '标准大气压': 'atm',
}

FLOW_UNITS = {
    'kg/h': 'kg/h', 'kgh': 'kg/h', 'kg/hr': 'kg/h', '公斤/小时': 'kg/h',
    'kg/s': 'kg/s', 't/h': 't/h', 'ton/h': 't/h', '吨/小时': 't/h',
    'kmol/h': 'kmol/h', 'kmol/hr': 'kmol/h', 'kgmol/h': 'kmol/h',
    'mol/h': 'mol/h', 'mol/s': 'mol/s',
    # Volumetric units are carried through as written; the tool layer refuses them
    # and that refusal is the desired behaviour.
    'nm3/h': 'Nm3/h', 'nm³/h': 'Nm3/h', 'nm3/hr': 'Nm3/h',
    'm3/h': 'm3/h', 'm³/h': 'm3/h',
}

VOLUMETRIC_UNITS = {'Nm3/h', 'm3/h'}

# Composition is expressed as a percentage or a ratio, not as a flow. Keeping these
# separate is what stops "62wt%" being read as a 62-unit flow.
COMPOSITION_UNITS = {
    'wt%': 'mass_percent', 'wt.%': 'mass_percent', 'wt': 'mass_percent',
    '质量%': 'mass_percent', '质量分数': 'mass_fraction',
    'mol%': 'mole_percent', 'mol.%': 'mole_percent', '摩尔%': 'mole_percent',
    'vol%': 'volume_percent', '体积%': 'volume_percent',
    'ppm': 'ppm', '%': 'percent', '％': 'percent',
}

# Which physical quantity a canonical unit measures.
QUANTITY_OF = {}
for _unit in TEMPERATURE_UNITS.values():
    QUANTITY_OF[_unit] = 'temperature'
for _unit in PRESSURE_UNITS.values():
    QUANTITY_OF[_unit] = 'pressure'
QUANTITY_OF.update({
    'kg/h': 'mass_flow', 'kg/s': 'mass_flow', 't/h': 'mass_flow',
    'kmol/h': 'molar_flow', 'mol/h': 'molar_flow', 'mol/s': 'molar_flow',
    'Nm3/h': 'volume_flow', 'm3/h': 'volume_flow',
})

# The quantity each fact field is expected to carry. Used to reject a value that is
# real but belongs to a different measurement.
FIELD_QUANTITY = {
    'feed_temperature': 'temperature',
    'feed_pressure': 'pressure',
}

# Flow is special: any of these are legitimate for `feed_total`.
FLOW_QUANTITIES = ('mass_flow', 'molar_flow', 'volume_flow')

_COMPOSITION_BASES = {
    'mass_fraction': 'mass_fraction', 'mass_percent': 'mass_fraction',
    'weight_fraction': 'mass_fraction', 'wt': 'mass_fraction', 'wt%': 'mass_fraction',
    'mole_ratio': 'molar_fraction', 'mol_ratio': 'molar_fraction',
    'molar_ratio': 'molar_fraction', 'molar_fraction': 'molar_fraction',
    'mole_fraction': 'molar_fraction', 'mol_fraction': 'molar_fraction',
    'pure': 'molar_fraction', 'single': 'molar_fraction',
}


def _fold(text: Any) -> str:
    return str(text or '').strip().casefold().replace(' ', '')


def canonical_unit(raw: Any, table: dict[str, str], default: str) -> tuple[str, bool]:
    """Return (canonical unit, changed?)."""
    text = str(raw or '').strip()
    if not text:
        return default, False
    canonical = table.get(_fold(text))
    if canonical is None:
        return text, False
    return canonical, canonical != text


def temperature_unit(raw: Any, default: str = 'C') -> tuple[str, bool]:
    return canonical_unit(raw, TEMPERATURE_UNITS, default)


def pressure_unit(raw: Any, default: str = 'kPa') -> tuple[str, bool]:
    return canonical_unit(raw, PRESSURE_UNITS, default)


def flow_unit(raw: Any, default: str = 'kg/h') -> tuple[str, bool]:
    return canonical_unit(raw, FLOW_UNITS, default)


def composition_basis(raw: Any, fallback: str) -> str:
    """Map a stated composition basis onto a spec basis."""
    return _COMPOSITION_BASES.get(_fold(raw), fallback)


def quantity_of(raw_unit: Any) -> str | None:
    """What does this unit measure? None when it is not recognised."""
    text = str(raw_unit or '').strip()
    if not text:
        return None
    return QUANTITY_OF.get(text) or QUANTITY_OF.get(
        canonical_unit(text, TEMPERATURE_UNITS, text)[0]) or QUANTITY_OF.get(
        canonical_unit(text, PRESSURE_UNITS, text)[0]) or QUANTITY_OF.get(
        canonical_unit(text, FLOW_UNITS, text)[0])


def is_volumetric(raw_unit: Any) -> bool:
    text = str(raw_unit or '').strip()
    _, canonical = flow_unit(text, text)
    return canonical in VOLUMETRIC_UNITS
