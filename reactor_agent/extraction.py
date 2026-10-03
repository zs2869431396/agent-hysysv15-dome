"""Turn a natural-language request into structured facts, then verify them.

Layering follows GWOA's parser (`backend/oa/parsers.py`), which solved the same
class of problem for amounts and dates:

    ① rules        fast, free, deterministic
    ② LLM          only for what the rules cannot read
    ③ validation   independent of the LLM, specifically to catch fabrication
    ④ confirmation the caller/user confirms before anything is executed

This module is ② and ③. Two design points come from measurement, not taste:

**The model-facing schema is not the internal contract.** It is flat, its repeating
parts are ARRAYS, and it avoids open dictionaries entirely. Handing a model the full
three-layer contract produced mangled keys (`"stoichiometry": { ": ": -2 }`), because
`dict[str, float]` becomes `additionalProperties` and under `strict` mode the model
must invent the keys itself.

**Field names must cover the semantics the text actually uses.** Steam reforming
states "压力 13.5 bar" per operating case, not as a feed pressure. With only
`feed_pressure` in the schema the model had nowhere to put the value and silently
dropped it, so `case_pressures` exists. Deterministic code decides later whether the
two mean the same thing.

**The validation layer is the point.** A number can be perfectly well-formed and
still be invented: asked for a flow the exam explicitly leaves to us, one candidate
filled in `100 mol/s` on its own. Format checks cannot catch that. `grounding_failures`
can, because it asks a different question - did the user actually say this number?
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .llm import ChatClient, LlmError
from .units import (
    COMPOSITION_UNITS,
    FIELD_QUANTITY,
    FLOW_UNITS,
    PRESSURE_UNITS,
    QUANTITY_OF,
    TEMPERATURE_UNITS,
    canonical_unit,
    quantity_of,
)

# --------------------------------------------------------------------- ① schema

def _obj(properties: dict[str, Any], required: list[str] | None = None) -> dict:
    return {'type': 'object', 'properties': properties,
            'required': list(properties) if required is None else required,
            'additionalProperties': False}


def _num_or_null() -> dict:
    return {'type': ['number', 'null']}


def _stoich_entry() -> dict:
    """One species with its signed coefficient.

    A single {name, coefficient} pair, never a dict keyed by species: a keyed object
    becomes `additionalProperties`, which forces the model to invent key names. That
    mistake produced `"stoichiometry": { ": ": -2 }` once already.
    """
    return _obj({'name': {'type': 'string'}, 'coefficient': {'type': 'number'}})


def _reaction_entry() -> dict:
    """A reaction as ONE signed species list.

    Deliberately not separate `reactants` and `products` arrays. That version asked
    the model to keep two lists in step *and* to use a sign convention, and a real
    model resolved the conflict by putting every species - products included - into
    `reactants`, which made the reaction balance to nothing:

        2 toluene + benzene + 0.5 o-xylene + 0.5 m-xylene + 0.5 p-xylene -> (none)

    One list with signed coefficients cannot be read two ways: negative is consumed,
    positive is produced.
    """
    return _obj({
        'name': {'type': 'string'},
        'species': {'type': 'array', 'items': _stoich_entry()},
        'reversible': {'type': 'boolean'},
    })


EXTRACTION_SCHEMA: dict = _obj({
    'species': {'type': 'array', 'items': {'type': 'string'}},
    # Feed composition. `fraction` holds whatever the user wrote - a mole ratio like
    # 2.7 in "1:2.7", or a mass percentage like 62 - and `composition_basis` says
    # which, so deterministic code can normalise it without guessing.
    'feed_composition': {'type': 'array', 'items': _obj({
        'name': {'type': 'string'},
        'fraction': {'type': ['number', 'null']},
    })},
    'composition_basis': {'type': 'string'},
    # Reactions are needed by the conversion path; a Gibbs case may leave them empty,
    # because free-energy minimisation needs no equations.
    'reactions': {'type': 'array', 'items': _reaction_entry()},
    'conversion_percent': _num_or_null(),
    # Which reactant the conversion figure is measured against.
    'conversion_basis': {'type': 'string'},
    # Rate-based data. Extracted so that a request which DOES state a rate law is
    # never read as "no kinetics" and quietly turned into a Conversion reactor: the
    # selection rules need it, and without these fields the information was dropped
    # before anything could look at it.
    # `rate_law` accepts null because "the request states no rate law" is the normal
    # case, and a strict string there would reject a correct reply.
    'rate_law': {'type': ['string', 'null']},
    'pre_exponential': _num_or_null(),
    'activation_energy': _num_or_null(),
    'activation_energy_unit': {'type': 'string'},
    'reaction_order': {'type': 'array', 'items': _obj({
        'name': {'type': 'string'}, 'order': {'type': 'number'}})},
    'reactor_volume': _num_or_null(),
    'reactor_volume_unit': {'type': 'string'},
    'residence_time': _num_or_null(),
    'residence_time_unit': {'type': 'string'},
    'catalyst_mass': _num_or_null(),
    'catalyst_mass_unit': {'type': 'string'},
    # Reactant phase, when the request makes it clear. It decides whether a stated
    # rate law means CSTR or PFR, so guessing it would pick the wrong reactor.
    'phase': {'type': 'string'},
    'feed_total': _num_or_null(),
    'feed_unit': {'type': 'string'},
    'feed_temperature': _num_or_null(),
    'feed_temperature_unit': {'type': 'string'},
    'feed_pressure': _num_or_null(),
    'feed_pressure_unit': {'type': 'string'},
    'case_pressures': {'type': 'array', 'items': {'type': 'number'}},
    'case_pressure_unit': {'type': 'string'},
    'outlet_temperatures': {'type': 'array', 'items': {'type': 'number'}},
    'outlet_temperature_unit': {'type': 'string'},
    'missing_information': {'type': 'array', 'items': {'type': 'string'}},
})

# The prompt encodes two measured lessons: copy figures verbatim (one candidate read
# "50%" as 0.5), and say so when something was not stated, rather than filling it in.
# The output contract, written out as text.
#
# Until this existed the field names lived ONLY in `response_format.json_schema`. That
# is fine while the server actually enforces the schema, and silently wrong the moment
# it does not: an OpenAI-compatible gateway may accept `json_schema` and ignore it, or
# reject it outright (`HTTP 400`), and the client then falls back to a plain request
# that carries no schema at all. A real reply captured from that fallback path kept
# every field the prompt happened to name and invented the rest -
# `feed: {flow_kg_per_h: 10000}`, `conversion: 50`, `species: [{name, role}]` - because
# the model had never been told the real names. The prompt has to carry the contract.
#
# Including the word "json" is not cosmetic either: DeepSeek answers
# `HTTP 400 Prompt must contain the word 'json' in some form to use 'response_format'
# of type 'json_object'` without it.
_EXAMPLE: dict[str, Any] = {
    'species': ['甲苯', '苯', '邻二甲苯', '间二甲苯', '对二甲苯'],
    'feed_composition': [{'name': '甲苯', 'fraction': 100}],
    'composition_basis': 'mass_fraction',
    'reactions': [{
        'name': '歧化',
        'species': [{'name': '甲苯', 'coefficient': -2},
                    {'name': '苯', 'coefficient': 1},
                    {'name': '邻二甲苯', 'coefficient': 1},
                    {'name': '间二甲苯', 'coefficient': 1},
                    {'name': '对二甲苯', 'coefficient': 1}],
        'reversible': False}],
    'conversion_percent': 50,
    'conversion_basis': '甲苯',
    'rate_law': None,
    'pre_exponential': None,
    'activation_energy': None,
    'activation_energy_unit': '',
    'reaction_order': [],
    'reactor_volume': None,
    'reactor_volume_unit': '',
    'residence_time': None,
    'residence_time_unit': '',
    'catalyst_mass': None,
    'catalyst_mass_unit': '',
    'phase': 'liquid',
    'feed_total': 10000,
    'feed_unit': 'kg/h',
    'feed_temperature': 380,
    'feed_temperature_unit': '℃',
    'feed_pressure': 2.5,
    'feed_pressure_unit': 'MPa',
    'case_pressures': [],
    'case_pressure_unit': '',
    'outlet_temperatures': [],
    'outlet_temperature_unit': '',
    'missing_information': [],
}

OUTPUT_SPEC = (
    '\nReturn ONE JSON object with exactly the keys below, all of them, no others.\n'
    'A value the request does not state is null (arrays: []); never omit a key.\n'
    'species      array of strings - the substance NAMES\n'
    'feed_composition  array of {name, fraction} - fraction is the number as written\n'
    'composition_basis  "mass_fraction" | "mole_ratio" | "pure"\n'
    'reactions    array of {name, species, reversible}\n'
    '             species is array of {name, coefficient}, negative = consumed,\n'
    '             positive = formed. Use `coefficient` and the sign - there is no\n'
    '             role/type/direction key anywhere in this contract.\n'
    'conversion_percent  number as written, 50 for 50%\n'
    'conversion_basis    the reactant the percentage refers to\n'
    'rate_law / pre_exponential / activation_energy / activation_energy_unit /\n'
    'reaction_order / reactor_volume / reactor_volume_unit / residence_time /\n'
    'residence_time_unit / catalyst_mass / catalyst_mass_unit   kinetic data\n'
    'phase        "gas" | "liquid" | "mixed" | "unknown"\n'
    'feed_total / feed_unit                              feed flow, as written\n'
    'feed_temperature / feed_temperature_unit            feed inlet temperature\n'
    'feed_pressure / feed_pressure_unit                  feed pressure\n'
    'case_pressures / case_pressure_unit                 per operating case, if given\n'
    'outlet_temperatures / outlet_temperature_unit       per operating case, if given\n'
    'missing_information  array of strings - what the request did not say\n'
    'Do NOT nest the flow, temperature or pressure under a "feed" object (no\n'
    'feed.flow_kg_per_h, no feed.temperature_C), and do not invent keys such as\n'
    '"reactor_type", "conversion", "kinetic", "components" or "flows": every value\n'
    'belongs at the top level, under the names above.\n'
    'Example of exactly one correct JSON document (values are illustrative):\n'
    + json.dumps(_EXAMPLE, ensure_ascii=False, indent=2)
)


def allowed_keys(schema: dict | None = None) -> set[str]:
    """Top-level keys the extractor may return."""
    return set((schema or EXTRACTION_SCHEMA)['properties'])


# ------------------------------------------------------- ③ schema-shape validation

def _type_matches(value: Any, expected: Any) -> bool:
    """JSON Schema "type" for a value, with int accepted where number is expected."""
    if isinstance(expected, list):
        return any(_type_matches(value, item) for item in expected)
    if expected == 'null':
        return value is None
    if expected == 'boolean':
        return isinstance(value, bool)
    if expected == 'number':
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == 'integer':
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == 'string':
        return isinstance(value, str)
    if expected == 'array':
        return isinstance(value, list)
    if expected == 'object':
        return isinstance(value, dict)
    return True


def validate_facts(facts: Any, schema: dict | None = None) -> list[str]:
    """Every way a reply drifts from the contract's SHAPE.

    Deliberately about shape, not completeness. A missing field is already handled by
    `Extraction.gaps()` against the scenario's own required set, and flagging all 29
    keys here would reject the small partial replies the tests use while telling the
    model nothing it cannot see for itself.

    What this catches is the failure that started it: a reply where the model invented
    its own key names - `feed: {flow_kg_per_h: ...}`, `conversion: 50`,
    `species: [{name, role}]`. That reply parses as JSON, so `json.loads` is happy, and
    the loss only shows up much later as blocking questions about fields the user did
    state. Reporting the drift here makes it one repairable message instead.
    """
    schema = schema or EXTRACTION_SCHEMA
    if not isinstance(facts, dict):
        return ['the reply is not a JSON object (got %s)' % type(facts).__name__]

    problems: list[str] = []
    expected = schema['properties']
    extra = [name for name in facts if name not in expected]
    if extra:
        problems.append('unexpected keys: %s' % ', '.join(extra))

    wrong = [name for name, spec in expected.items()
             if name in facts and not _type_matches(facts[name], spec.get('type'))]
    if wrong:
        problems.append('wrong type: %s' % ', '.join(
            '%s should be %s' % (name, expected[name].get('type')) for name in wrong))

    # The two nested shapes the fallback path got wrong: `species` as objects rather
    # than names, and a reaction listing its substances under anything but `species`.
    if isinstance(facts.get('species'), list):
        if any(not isinstance(name, str) for name in facts['species']):
            problems.append('species must be an array of plain names, e.g. '
                            '["甲苯", "苯"] - not objects')
    reactions = facts.get('reactions')
    if isinstance(reactions, list):
        for index, reaction in enumerate(reactions):
            if not isinstance(reaction, dict):
                problems.append('reactions[%d] is not an object' % index)
                continue
            unknown = [key for key in reaction
                       if key not in ('name', 'species', 'reversible')]
            if unknown:
                problems.append('reactions[%d] has unexpected keys: %s'
                                % (index, ', '.join(unknown)))
            for entry in (reaction.get('species') or []):
                if not isinstance(entry, dict):
                    problems.append('reactions[%d].species entries must be objects '
                                    'with name and coefficient' % index)
                    break
                if 'role' in entry or 'type' in entry:
                    problems.append(
                        'reactions[%d].species uses a role/type key; use a SIGNED '
                        '`coefficient` instead (negative = consumed)' % index)
                    break
    return problems


SYSTEM_PROMPT = (
    'You extract chemical-engineering facts from a user request.\n'
    'Copy every number and unit EXACTLY as written. Do not convert units. Do not turn '
    'a percentage into a fraction or the reverse.\n'
    'Every figure the user DID state - flow, temperature, pressure, conversion, and '
    'every named substance - MUST appear in its field. Leaving a stated value empty '
    'is an error.\n'
    'Put a value in the field whose meaning matches the text. If the pressure is '
    'stated per operating case rather than for the feed, use case_pressures.\n'
    'Write species as NAMES, not molecular formulae: "甲苯"/"toluene", never "C7H8". '
    'If the user names several isomers, list each one separately - ortho-, meta- and '
    'para-xylene are three species, and collapsing them into one formula such as '
    'C8H10 loses the distinction.\n'
    'Use exactly the same names inside reactions as in the species list. If the '
    'species list has o-xylene, m-xylene and p-xylene as separate entries, do not '
    'write a single "xylene" in a reaction.\n'
    'Record each reaction the user writes in `reactions`, as ONE list called '
    '`species` holding every substance with a SIGNED coefficient: NEGATIVE for a '
    'reactant that is consumed, POSITIVE for a product that is formed, in the ratio '
    'the user gave. For 2C7H8 -> C6H6 + C8H10 that is toluene -2, benzene +1, '
    'xylene +1. Do not split them into two lists.\n'
    'Only record reactions the user actually stated - leave the list empty if none '
    'are given.\n'
    'conversion_basis is the reactant the stated conversion percentage refers to.\n'
    'If the user gives a rate law or kinetic parameters - a rate expression, a '
    'pre-exponential factor, an activation energy, reaction orders - record them in '
    'the rate_law and kinetic fields, together with any equipment size (reactor '
    'volume, catalyst mass, residence time). Record the reactant phase in `phase` '
    '("gas", "liquid", "mixed" or "unknown") when the text makes it clear. Leaving '
    'these empty when the user did state them is an error.\n'
    'Record the feed composition in feed_composition, copying the numbers the user '
    'gave. Set composition_basis to "mass_fraction" when they are mass percentages '
    '(62wt% -> 62), to "mole_ratio" when they are molar proportions (a 1:2.7 ratio '
    '-> 1 and 2.7), or to "pure" when the feed is a single substance. Do not '
    'normalise the numbers yourself.\n'
    'List anything the user did NOT state in missing_information. '
    'Never invent a value.\n'
    + OUTPUT_SPEC
)

# Fields whose value is a number the user should have stated.
SCALAR_NUMERIC_FIELDS = ('conversion_percent', 'feed_total', 'feed_temperature',
                         'feed_pressure')
ARRAY_NUMERIC_FIELDS = ('case_pressures', 'outlet_temperatures')

# Required per scenario, NOT one list for everything. A Gibbs case has no conversion
# figure at all, and the exam says the reformer flow may be chosen freely - so a
# single list marks correct behaviour as failure. That mistake was made once already.
REQUIRED_BY_KIND: dict[str, tuple[str, ...]] = {
    'conversion': ('feed_total', 'feed_temperature', 'feed_pressure',
                   'conversion_percent'),
    'gibbs': ('feed_temperature', 'feed_pressure', 'outlet_temperatures'),
    'equilibrium': ('feed_temperature', 'feed_pressure', 'outlet_temperatures'),
}


@dataclass
class Extraction:
    """Raw facts as returned by the model, plus how the call went."""
    facts: dict[str, Any] = field(default_factory=dict)
    attempts: int = 1
    model: str = ''
    error: str | None = None
    missing_information: list[str] = field(default_factory=list)
    # Fields whose value could not be found in the user's own words. Kept as its own
    # list because it must BLOCK execution, not merely be reported: a fabricated flow
    # that reaches HYSYS produces a confident, wrong answer.
    ungrounded: list[str] = field(default_factory=list)

    def get(self, name: str, default: Any = None) -> Any:
        return self.facts.get(name, default)

    def gaps(self, kind: str) -> list[str]:
        """Required fields that came back empty for this scenario.

        An empty `kind` means the scenario is not known yet, so nothing is required:
        the required set belongs to the reactor that the facts will select.

        `feed_pressure` counts as present when the model supplied it per operating
        case instead: the exam states "压力 13.5 bar" for each reformer condition, and
        normalisation merges those into a feed pressure. Asking the model again for a
        field it has already answered in another field just burns retries.
        """
        if not kind:
            return []
        required = REQUIRED_BY_KIND.get(kind, ())
        missing: list[str] = []
        for name in required:
            if self.facts.get(name) is not None:
                continue
            if name == 'feed_pressure' and any(
                    value is not None
                    for value in (self.facts.get('case_pressures') or [])):
                continue
            missing.append(name)
        return missing


# ------------------------------------------------------------- ③ grounding check

_NUMBER = re.compile(r'\d+(?:[.,]\d+)?')

# A number, then whatever unit follows it. The tail may start with whitespace, which
# is what makes "0.5 kg/h" work as well as "10000kg/h", but may not contain another
# digit - otherwise "1:2.7" would swallow the 2.7 into the first match's tail and the
# ratio would never be seen as a value.
_MEASUREMENT = re.compile(r'(\d+(?:[.,]\d+)?)(\s*[^\s\d,;，；、。()（）\[\]]*)')

# Conversions the check will accept as a correct re-expression of the same quantity.
# Deliberately tiny: 1000 covers MPa<->kPa and t<->kg, 0.001 the reverse. An earlier
# version also allowed 100, which let an invented `feed_total = 100` match the "1" in
# "摩尔比 1:2.7". Without unit context a factor rule eventually licenses a fabrication.
_UNIT_FACTORS = (1000.0, 0.001)

# Unit spellings, longest first, so "kg/h" wins over "kg" and "mol%" over "%".
_ALL_SPELLINGS = sorted(
    set(list(TEMPERATURE_UNITS) + list(PRESSURE_UNITS) + list(FLOW_UNITS)
        + list(COMPOSITION_UNITS)),
    key=len, reverse=True)


def _unit_at(tail: str) -> tuple[str, str | None]:
    """Read the canonical unit at the start of a tail, and what it measures."""
    folded = tail.strip().casefold()
    if not folded:
        return '', None
    for spelling in _ALL_SPELLINGS:
        if folded.startswith(spelling):
            canonical, _ = canonical_unit(spelling, TEMPERATURE_UNITS, spelling)
            if canonical not in QUANTITY_OF:
                canonical, _ = canonical_unit(spelling, PRESSURE_UNITS, spelling)
            if canonical not in QUANTITY_OF:
                canonical, _ = canonical_unit(spelling, FLOW_UNITS, spelling)
            if canonical not in QUANTITY_OF:
                return COMPOSITION_UNITS.get(spelling, spelling), 'composition'
            return canonical, QUANTITY_OF[canonical]
    return '', None


def measurements_in(text: str) -> list[tuple[float, str, str | None]]:
    """Every (value, unit, quantity) the text states.

    This is the "原文依据" the check compares against. Reading the unit as well as the
    number is what lets it tell "380 next to ℃" from "380 next to kg/h".

    The unit is matched by looking for a known spelling immediately after the number,
    rather than by capturing "everything up to whitespace". The capture approach broke
    on units that contain digits - `Nm3/h` and `m3/h` were truncated to `Nm`/`m`, so
    a volume flow could never be matched against the text that stated it.
    """
    found: list[tuple[float, str, str | None]] = []
    for match in _NUMBER.finditer(text or ''):
        try:
            value = float(match.group(0).replace(',', ''))
        except ValueError:
            continue
        unit, quantity = _unit_at_position(text, match.end())
        found.append((value, unit, quantity))
    return found


def _unit_at_position(text: str, start: int) -> tuple[str, str | None]:
    """Read a known unit spelling beginning at or just after `start`."""
    tail = text[start:start + 24]
    stripped = tail.lstrip()
    folded = stripped.casefold()
    if not folded:
        return '', None
    for spelling in _ALL_SPELLINGS:
        if folded.startswith(spelling):
            canonical, _ = canonical_unit(spelling, TEMPERATURE_UNITS, spelling)
            if canonical not in QUANTITY_OF:
                canonical, _ = canonical_unit(spelling, PRESSURE_UNITS, spelling)
            if canonical not in QUANTITY_OF:
                canonical, _ = canonical_unit(spelling, FLOW_UNITS, spelling)
            if canonical not in QUANTITY_OF:
                return COMPOSITION_UNITS.get(spelling, spelling), 'composition'
            return canonical, QUANTITY_OF[canonical]
    return '', None


def _close(a: float, b: float, tolerance: float = 1e-6) -> bool:
    return abs(a - b) <= tolerance * max(1.0, abs(a), abs(b))


def _stated_as(number: float, unit: Any, quantity: str,
               measurements: list[tuple[float, str, str | None]],
               tolerance: float = 1e-6) -> bool:
    """Is this value stated in the text *as this quantity*?

    Three ways to qualify, and no others:
      * the same number with the same unit;
      * the same number re-expressed in a convertible unit;
      * the same quantity reached by a known conversion factor.
    A bare matching number next to a different unit does not qualify - that is the
    case where a temperature was copied into the flow field.
    """
    want_unit, _ = canonical_unit(unit, TEMPERATURE_UNITS, str(unit or ''))
    for text_value, text_unit, text_quantity in measurements:
        if text_quantity != quantity:
            continue
        if _close(number, text_value, tolerance) and (
                not unit or not text_unit or text_unit == want_unit
                or canonical_unit(text_unit, PRESSURE_UNITS,
                                  canonical_unit(text_unit, FLOW_UNITS, text_unit))[0]
                == want_unit):
            return True
        if text_value:
            ratio = number / text_value
            for factor in _UNIT_FACTORS:
                if _close(ratio, factor, tolerance) or _close(ratio, 1.0 / factor,
                                                              tolerance):
                    return True
    return False


# Where each field keeps its unit. The flow is the odd one: its unit lives in
# `feed_unit`, not `feed_total_unit`, and looking in the wrong place made the flow
# fall back to "mass" and reject a volume flow the text plainly stated.
_UNIT_FIELD = {'feed_total': 'feed_unit',
               'feed_temperature': 'feed_temperature_unit',
               'feed_pressure': 'feed_pressure_unit',
               'activation_energy': 'activation_energy_unit'}


def unit_of(facts: dict[str, Any], field: str) -> str:
    """The unit recorded for a field, whichever name it uses."""
    return str(facts.get(_UNIT_FIELD.get(field, field + '_unit')) or '')


def _percent_read_as_fraction(value: float, source_text: str) -> bool:
    """True when a percentage was copied as a decimal fraction.

    Measured failure: one candidate returned `conversion_percent = 0.5` for "转化率
    50%". Generic unit matching would accept that (0.5 = 50 x 0.01), so it is checked
    separately. Deliberately narrow: it only fires when the text actually contains a
    percent sign and the scaled value is present.
    """
    if not 0 < abs(value) < 1:
        return False
    if '%' not in source_text and '％' not in source_text:
        return False
    scaled = abs(value) * 100.0
    return any(_close(scaled, candidate) for candidate, _u, _q in
               measurements_in(source_text))


def grounding_failures(facts: dict[str, Any], source_text: str,
                       allowed: set[str] | None = None,
                       tolerance: float = 1e-6) -> list[str]:
    """Fields whose value cannot be traced to the user's own words.

    `allowed` names fields that may legitimately be chosen rather than quoted - the
    exam says the reformer feed flow "可以自定", so that field may come from a
    recorded assumption instead of the text. Everything else must be traceable, and
    traceable *as the right physical quantity*: a number that appears in the sentence
    attached to a different unit is not evidence that the user said it here.
    """
    allowed = allowed or set()
    measurements = measurements_in(source_text)
    failures: list[str] = []

    for name in SCALAR_NUMERIC_FIELDS:
        if name in allowed:
            continue
        value = facts.get(name)
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            failures.append(name)
            continue
        if name == 'conversion_percent':
            if _percent_read_as_fraction(number, source_text):
                failures.append(name)
                continue
            # A conversion figure is a bare percentage; it needs no unit.
            if not any(_close(number, v, tolerance) for v, _u, _q in measurements):
                failures.append(name)
            continue

        unit = unit_of(facts, name)
        if name == 'feed_total':
            quantity = quantity_of(unit) or 'mass_flow'
            if not _stated_as(number, unit, quantity, measurements, tolerance):
                failures.append(name)
            continue
        quantity = FIELD_QUANTITY.get(name)
        if quantity is None:
            # No declared quantity: fall back to a plain number match.
            if not any(_close(number, v, tolerance) for v, _u, _q in measurements):
                failures.append(name)
            continue
        if not _stated_as(number, unit, quantity, measurements, tolerance):
            failures.append(name)

    for name in ARRAY_NUMERIC_FIELDS:
        if name in allowed:
            continue
        quantity = FIELD_QUANTITY.get(name)
        for position, value in enumerate(facts.get(name) or []):
            try:
                number = float(value)
            except (TypeError, ValueError):
                failures.append('%s[%d]' % (name, position))
                continue
            if quantity:
                stated = _stated_as(number, '', quantity, measurements, tolerance)
            else:
                # No declared quantity for this array: fall back to a plain number
                # match rather than skipping the check. Skipping would mean the
                # element-wise check silently did nothing at all.
                stated = any(_close(number, v, tolerance) for v, _u, _q in measurements)
            if not stated:
                failures.append('%s[%d]' % (name, position))

    failures.extend(_composition_failures(facts, measurements, tolerance))
    return failures


# Subscript digits, so a formula copied from a document ("C₇H₈") matches a typed one.
_SUBSCRIPT_DIGITS = str.maketrans('₀₁₂₃₄₅₆₇₈₉', '0123456789')

# The elements a written formula may contain. Deliberately generous but finite: the
# point is to reject things that merely LOOK like a formula. `Nm3` (Nm is not an
# element), `MPa` (M is not) and `5M` all fail this test, which is exactly what the
# earlier regex `\d\s*[A-Z][a-z]?` got wrong - it matched the `2O` in `H2O`, the `5M` in
# `2.5MPa` and the `0N` in `80000Nm3/h`, so nearly every request looked like a written
# equation.
ELEMENTS = frozenset((
    'H', 'He', 'Li', 'Be', 'B', 'C', 'N', 'O', 'F', 'Ne', 'Na', 'Mg', 'Al', 'Si',
    'P', 'S', 'Cl', 'Ar', 'K', 'Ca', 'Sc', 'Ti', 'V', 'Cr', 'Mn', 'Fe', 'Co', 'Ni',
    'Cu', 'Zn', 'Ga', 'Ge', 'As', 'Se', 'Br', 'Kr', 'Rb', 'Sr', 'Y', 'Zr', 'Nb',
    'Mo', 'Tc', 'Ru', 'Rh', 'Pd', 'Ag', 'Cd', 'In', 'Sn', 'Sb', 'Te', 'I', 'Xe',
    'Cs', 'Ba', 'La', 'Ce', 'Pt', 'Au', 'Hg', 'Pb', 'Bi',
))

_SYMBOL = r'[A-Z][a-z]?'
_FORMULA = r'(?:%s\d*)+' % _SYMBOL
_TERM = r'(?:(\d+(?:\.\d+)?)\s*)?(%s)' % _FORMULA
# Subscripts '=' and ':' repeat a group in Python's re, so every group inside the
# arrows below is non-capturing.
_ARROWS = r'(?:<=>|<->|⇌|⇄|↔|⟶|→|->|=>|=)'
_SIDE = r'\s*%s(?:\s*\+\s*%s)*\s*' % (_TERM, _TERM)
_WRITTEN_EQUATION = re.compile(r'%s%s%s' % (_SIDE, _ARROWS, _SIDE))

# The parser is applied to each side on its own; this finds one term at a time.
_TERM_RE = re.compile(r'^\s*(\d+(?:\.\d+)?)?\s*(%s)\s*$' % _FORMULA)


def _elements_of(formula: str) -> list[str] | None:
    """Element symbols in a formula, or None if it is not a formula at all."""
    tokens = re.findall(r'%s\d*' % _SYMBOL, formula)
    # The tokens must account for the whole string, and every symbol must be an
    # element. Otherwise "Nm3" parses as N + m3 and would be accepted.
    if ''.join(tokens) != formula or not tokens:
        return None
    symbols = [re.match(_SYMBOL, token).group(0) for token in tokens]
    if any(symbol not in ELEMENTS for symbol in symbols):
        return None
    return symbols


def _split_side(side: str) -> list[tuple[float, str]] | None:
    """The coefficient and formula of every term on one side, or None."""
    terms: list[tuple[float, str]] = []
    for raw in side.split('+'):
        match = _TERM_RE.match(raw)
        if match is None:
            return None
        formula = match.group(2)
        if _elements_of(formula) is None:
            return None
        coefficient = float(match.group(1)) if match.group(1) else 1.0
        terms.append((coefficient, formula))
    return terms or None


def written_equations(text: str) -> list[dict[str, float]]:
    """Every balanced-looking equation the request actually wrote out.

    Only a construction with chemical formulas on BOTH sides of an arrow counts. The
    left side's coefficients are negative, the right side's positive; a formula without
    a written coefficient counts as 1. A match is dropped entirely if any term is not a
    plausible formula, so a pressure, a unit or a bare number next to an arrow cannot
    produce one.
    """
    folded = str(text or '').translate(_SUBSCRIPT_DIGITS)
    equations: list[dict[str, float]] = []
    for match in _WRITTEN_EQUATION.finditer(folded):
        arrow = re.search(_ARROWS, match.group(0))
        if arrow is None:
            continue
        left = _split_side(match.group(0)[:arrow.start()])
        right = _split_side(match.group(0)[arrow.end():])
        if not left or not right:
            continue
        equation: dict[str, float] = {}
        for coefficient, formula in left:
            equation[formula] = equation.get(formula, 0.0) - coefficient
        for coefficient, formula in right:
            equation[formula] = equation.get(formula, 0.0) + coefficient
        equations.append(equation)
    return equations


def states_numeric_equation(source_text: str) -> bool:
    """True when the request writes the chemistry as an actual equation.

    The name is kept for its callers; what it now means is "the text contains an
    equation", not "the text contains a digit next to a capital letter".
    """
    return bool(written_equations(source_text))


def reaction_grounding_failures(reactions: list[dict[str, Any]], source_text: str,
                                assumed: set[str] | None = None,
                                tolerance: float = 1e-6) -> list[str]:
    """Reaction coefficients that contradict or outrun what the request states.

    The rule depends on how the request worded the chemistry, and getting that wrong
    produced a false alarm on the reforming scenario:

      * **The request writes an equation** (`2C7H8 -> C6H6 + C8H10`). Then every
        coefficient must be traceable to the equation's own coefficients, and an
        invented one is a real fabrication. Only the coefficients of the written
        equations count - taking every number in the text let a pressure, a flow or a
        temperature "justify" a coefficient.
      * **The request states the chemistry in words** ("methane and water react to
        give carbon monoxide and hydrogen"). Then the coefficients have to be derived
        from the element balance - there is nothing to copy. Deriving a balanced
        equation is chemistry, not invention, and the tool layer independently checks
        the balance afterwards. Nothing is flagged here; the caller records the
        derived stoichiometry as an assumption instead.

    A coefficient of 1 is implicit in a formula in either case.

    Returns entries like `reactions[0].Toluene`.
    """
    equations = written_equations(source_text)
    if not equations:
        return []
    assumed = assumed or set()
    stated = [abs(coefficient)
              for equation in equations for coefficient in equation.values()]
    failures: list[str] = []
    for index, reaction in enumerate(reactions or []):
        label = str(reaction.get('name') or 'RXN-%d' % (index + 1))
        for entry in (reaction.get('species') or []):
            name = str(entry.get('name') or '').strip()
            if not name or name in assumed:
                continue
            try:
                value = abs(float(entry.get('coefficient') or 0.0))
            except (TypeError, ValueError):
                failures.append('reactions[%s].%s' % (label, name))
                continue
            if value <= 1.0 or any(_close(value, v, tolerance) for v in stated):
                continue
            failures.append('reactions[%s].%s' % (label, name))
    return failures


def reaction_is_derived(reaction: dict[str, Any], source_text: str,
                        tolerance: float = 1e-6) -> bool:
    """True when the request worded this reaction, so its coefficients are ours.

    Used by the caller to declare the derived stoichiometry as an assumption rather
    than presenting it as something the user wrote.
    """
    return not states_numeric_equation(source_text)


def _composition_failures(facts: dict[str, Any],
                          measurements: list[tuple[float, str, str | None]],
                          tolerance: float) -> list[str]:
    """Check the stated feed composition against the text.

    A single-component feed has a derived fraction of 1.0, which the user never wrote,
    so it is not checked. Everything else must appear in the text - as a plain number
    or as a percentage.

    The 62 wt% slurry is the reason the percentage forms are included: the number in
    the text is 62, and the fraction the run needs is 0.62.
    """
    entries = facts.get('feed_composition') or []
    if len(entries) < 2:
        return []
    failures: list[str] = []
    ungrounded: list[int] = []
    values: dict[int, float] = {}
    for position, entry in enumerate(entries):
        value = entry.get('fraction')
        if value is None:
            continue
        try:
            number = abs(float(value))
        except (TypeError, ValueError):
            failures.append('feed_composition[%d]' % position)
            continue
        values[position] = number
        if any(_close(number, v, tolerance) or _close(number * 100.0, v, tolerance)
               or _close(number / 100.0, v, tolerance)
               for v, _u, _q in measurements):
            continue
        ungrounded.append(position)

    # A two-component feed usually states one figure and lets the other follow:
    # "浓度62wt%" means 62 and 38. The complement is derived, not invented, so it is
    # accepted - but only when exactly one figure is missing and the rest are
    # grounded, which is what makes it a complement rather than a guess.
    if len(ungrounded) == 1 and len(values) == len(entries):
        missing = ungrounded[0]
        others = sum(v for p, v in values.items() if p != missing)
        if _close(values[missing], 100.0 - others, tolerance) or _close(
                values[missing], 1.0 - others, tolerance):
            ungrounded = []

    failures.extend('feed_composition[%d]' % position for position in ungrounded)
    return failures


# ------------------------------------------------------------------ ② LLM call

# How many times a drifted reply is re-asked, across the whole call. One: the repair
# round carries the validator's message back to the model, and a model that ignores the
# contract once will usually ignore it again - re-asking per gap-retry round only
# multiplied the request count while the endpoint was rate-limiting us.
_REPAIR_LIMIT = 1

REPAIR_PROMPT = (
    'Your previous reply did not match the required JSON contract.\n'
    'Fix it and return the complete object again.\n'
)


def _call_once(client: ChatClient, system: str, text: str) -> dict[str, Any]:
    return client.complete(system, text, schema=EXTRACTION_SCHEMA,
                           schema_name='Extraction')


def extract(client: ChatClient, text: str, kind: str | None = None,
            max_tries: int = 3) -> Extraction:
    """Ask the model for the facts, retrying while required values are missing.

    Three different retries live around here and they are not the same thing:
      * the client retries transient HTTP failures (see `llm.RETRYABLE_STATUS`);
      * this function retries a *successful* call whose answer is missing a required
        value, because those gaps proved to be random - 83% of calls were complete
        first time and 100% were complete within three;
      * a reply that drifts from the JSON contract gets ONE repair round in total,
        carrying the validator's message back to the model. Before this, a reply with
        invented key names was accepted and handed downstream, where `normalize`
        recognised nothing and the run stopped asking about fields the user had stated.

    The repair budget is deliberately global rather than per attempt: a model that
    ignores the contract once will usually ignore it again, and re-asking inside every
    gap-retry round multiplied the request count while the endpoint was already
    rate-limiting us.

    Pass `kind` to enable the gap retry; without it the first usable reply is returned
    as is. The required set depends on the scenario, so the check cannot be global.
    """
    extraction = Extraction(model=client.config.model)
    last_error: str | None = None
    repaired = False

    for attempt in range(1, max_tries + 1):
        try:
            facts = _call_once(client, SYSTEM_PROMPT, text)
        except LlmError as exc:
            extraction.attempts = attempt
            extraction.error = str(exc)
            if exc.kind == 'auth':
                return extraction
            last_error = str(exc)
            continue

        problems = validate_facts(facts)
        if problems and not repaired:
            repaired = True
            repair_system = SYSTEM_PROMPT + REPAIR_PROMPT + '\n'.join(
                '- %s' % problem for problem in problems)
            try:
                facts = _call_once(client, repair_system, text)
            except LlmError as exc:
                extraction.attempts = attempt
                extraction.error = str(exc)
                if exc.kind == 'auth':
                    return extraction
                last_error = str(exc)
                continue
            problems = validate_facts(facts)

        if problems:
            extraction.attempts = attempt
            last_error = 'reply did not match the contract: %s' % '; '.join(problems)
            continue

        extraction.facts = facts
        extraction.attempts = attempt
        extraction.error = None
        extraction.missing_information = list(facts.get('missing_information') or [])
        if kind is None or not extraction.gaps(kind):
            return extraction
        last_error = 'incomplete: %s' % ', '.join(extraction.gaps(kind))

    extraction.error = last_error or 'no usable reply'
    return extraction


def extract_verified(client: ChatClient, text: str, kind: str = '',
                     allowed: set[str] | None = None) -> tuple[Extraction, list[str]]:
    """Extract and then check required fields and grounding.

    Returns the extraction and a list of problems. An empty list means the facts are
    complete for this scenario and every number is traceable to the request.

    `kind` may be empty, and for a request that did not come from a known scenario it
    should be. Which fields are required depends on the reactor that the facts will
    select, and selection needs the facts - so requiring `conversion_percent` before
    anything has been selected asked a Gibbs request for a figure it could never have.
    The caller checks the required set once selection has decided, via
    `check_required`.

    The ungrounded fields are recorded on the extraction as well as returned, because
    the caller must turn them into blocking questions - a hallucinated value that
    merely appears in a warning list will still be used to build a case.
    """
    extraction = extract(client, text, kind=kind or None)
    problems: list[str] = []
    if extraction.error:
        problems.append('model call failed: %s' % extraction.error)
    for name in extraction.gaps(kind):
        problems.append('required field %r came back empty for a %s case' % (name, kind))
    if extraction.facts:
        extraction.ungrounded = grounding_failures(extraction.facts, text, allowed)
        for name in extraction.ungrounded:
            problems.append('%r cannot be found in the request, so it may be invented'
                            % name)
    return extraction, problems


def check_required(extraction: Extraction, kind: str) -> list[str]:
    """Required-field problems for a scenario decided after extraction."""
    if not kind:
        return []
    return ['required field %r came back empty for a %s case' % (name, kind)
            for name in extraction.gaps(kind)]


def required_kind_for(reactor: str | None) -> str:
    """Which required-field set applies to a selected reactor."""
    if not reactor:
        return ''
    lowered = str(reactor).casefold()
    if lowered in ('gibbs', 'equilibrium'):
        return 'gibbs'
    if lowered in ('conversion',):
        return 'conversion'
    # CSTR/PFR need kinetics and volume, not a conversion figure; refusing them is
    # the selection layer's job, and asking for a conversion here would be wrong.
    return ''
