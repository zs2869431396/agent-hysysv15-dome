"""Turning a user's answer into a fact.

Answers arrive as free text from a CLI, a web form, or a person typing "10000 kg/h".
This module reads all the reasonable spellings, and - more importantly - decides what
to do when it cannot read one.

The rule is: **leave the field alone and say so.** An unreadable answer must not
reach the contract, because pydantic would raise a traceback from deep inside the
graph, and it must not be quietly dropped either, because then the user's answer
disappears without a word.

The volumetric-flow question is the one that needs care. Its default answer is a
sentence ("总进料，0°C/101.325 kPa"), not a number, so answering it writes a *basis*
into the facts rather than a value: from that point on the total is a standard gas
volume and the tool layer's own normal-volume path does the conversion. An empty
answer is deliberately not treated as the default - accepting a default has to be an
explicit act, which the CLI performs by filling in the default text.
"""
from __future__ import annotations

import re
from typing import Any

from hysys_tools.core import (
    MASS_FLOW_UNITS,
    MOLAR_FLOW_UNITS,
    NORMAL_VOLUME_UNITS,
    to_kpa,
)

from ..normalize import (
    DEFAULT_STANDARD_PRESSURE_KPA,
    DEFAULT_STANDARD_TEMPERATURE_C,
    FLOW_UNITS,
)

# A leading number, then anything that could be a unit.
_LEADING_NUMBER = re.compile(r'^\s*(-?\d+(?:\.\d+)?)\s*([A-Za-z/%°]+.*)?$')

# The same number/unit pair, but allowed to sit behind a few words of preamble such
# as "总进料" or "进料流量". Anchored at the start so "0°C/101.325 kPa" (a temperature
# first) is not mistaken for a flow.
_FLOW_NUMBER = re.compile(
    r'^\s*(?:[^\d\s]{1,8}\s*)?(\d+(?:\.\d+)?)\s*([A-Za-z/%°][A-Za-z0-9/%°^ ]*)?')

_TEMPERATURE = re.compile(r'(-?\d+(?:\.\d+)?)\s*(°\s*C|℃|摄氏度|度|K\b|C\b)')
_PRESSURE = re.compile(r'(\d+(?:\.\d+)?)\s*(kPa|MPa|Pa|bar|atm|mbar)\b', re.I)

# Words that mean "no" rather than "yes". Shared with the coal confirmation, where the
# old affirmative-only test read "不可以" as "可以".
_NEGATIVE = ('不', '否', '没', '无', 'no', 'not', 'cannot')

# Other streams the user might have meant. Nm3/h of steam or of syngas is a different
# number of moles from Nm3/h of feed, so the answer cannot be used for the feed total.
_OTHER_STREAMS = ('水蒸气', '蒸汽', '出口', '合成气', 'syngas', 'steam', 'off-gas')

_DEFAULT_WORDS = ('默认', 'default', '总进料', '总量', 'total', '是', '对', '确认',
                  '可以', 'ok', 'yes')


def is_negative_text(text: Any) -> bool:
    """True when a short answer means "no"."""
    stripped = str(text or '').strip()
    if not stripped:
        return False
    head = stripped.casefold()
    if any(head.startswith(word) for word in ('不', '否', '没', '无', 'no', 'not')):
        return True
    return any(word in stripped for word in ('不能', '不可以', '不行', 'cannot'))


def _canonical_flow_unit(unit: str | None) -> str | None:
    text = str(unit or '').strip()
    if not text:
        return None
    return FLOW_UNITS.get(text.casefold().replace(' ', ''))


def _flow_answer(text: str) -> dict[str, Any] | None:
    """A number+unit answer that changes the flow itself, if that is what this is.

    A mass or molar unit means the user replaced the normal volume with something the
    tool layer can convert on its own. A normal-volume unit means they restated the
    total, so the standard-state defaults apply.
    """
    match = _FLOW_NUMBER.match(text)
    if not match:
        return None
    value = float(match.group(1))
    unit = _canonical_flow_unit(match.group(2))
    if unit in MASS_FLOW_UNITS or unit in MOLAR_FLOW_UNITS:
        return {'kind': 'flow', 'value': value, 'unit': unit}
    if unit in NORMAL_VOLUME_UNITS or unit == 'Nm3/h':
        return {'kind': 'normal_volume', 'total': value,
                'standard_temperature_C': DEFAULT_STANDARD_TEMPERATURE_C,
                'standard_pressure_kPa': DEFAULT_STANDARD_PRESSURE_KPA}
    return None


def parse_normal_volume_answer(answer: Any) -> dict[str, Any]:
    """Interpret an answer to the volumetric-flow question.

    Returns one of three shapes:

    * `{'kind': 'normal_volume', 'standard_temperature_C': T,
       'standard_pressure_kPa': P, 'total': optional}` - the total is a standard gas
      volume of the feed;
    * `{'kind': 'flow', 'value': v, 'unit': u}` - the user gave a mass or molar flow;
    * `{'kind': 'unreadable', 'note': ...}` - anything else, including "no".

    A temperature or a pressure on its own is enough: the other one takes the default.
    The defaults are the same numbers the question offers, and they only take effect
    because the user answered this question.
    """
    if isinstance(answer, dict):
        if 'standard_temperature_C' in answer or 'standard_pressure_kPa' in answer:
            return {
                'kind': 'normal_volume',
                'standard_temperature_C': float(answer.get(
                    'standard_temperature_C', DEFAULT_STANDARD_TEMPERATURE_C)),
                'standard_pressure_kPa': float(answer.get(
                    'standard_pressure_kPa', DEFAULT_STANDARD_PRESSURE_KPA))}
        answer = answer.get('value', answer.get('answer', ''))

    text = str(answer or '').strip()
    if not text:
        return {'kind': 'unreadable', 'note': '没有读到回答内容。'}
    if is_negative_text(text):
        return {'kind': 'unreadable',
                'note': '回答被理解为否定，进料的体积流量仍按原样保留，没有采用任何默认状态。'}

    flow = _flow_answer(text)
    if flow is not None:
        return flow

    if any(word in text for word in _OTHER_STREAMS):
        return {'kind': 'unreadable',
                'note': ('目前只支持把 Nm³ 理解为进料总量；如果指其他物流，'
                         '请直接给出进料的质量或摩尔流量。')}

    temperature = None
    match = _TEMPERATURE.search(text)
    if match:
        value = float(match.group(1))
        unit = match.group(2).replace(' ', '')
        temperature = (value - 273.15 if unit in ('K', 'K') else value)

    pressure = None
    match = _PRESSURE.search(text)
    if match:
        try:
            pressure = to_kpa(float(match.group(1)), match.group(2))
        except Exception:                               # noqa: BLE001
            pressure = None

    if temperature is not None or pressure is not None:
        return {'kind': 'normal_volume',
                'standard_temperature_C': (temperature
                                           if temperature is not None
                                           else DEFAULT_STANDARD_TEMPERATURE_C),
                'standard_pressure_kPa': (pressure if pressure is not None
                                          else DEFAULT_STANDARD_PRESSURE_KPA)}

    lowered = text.casefold()
    if any(word in lowered for word in _DEFAULT_WORDS):
        return {'kind': 'normal_volume',
                'standard_temperature_C': DEFAULT_STANDARD_TEMPERATURE_C,
                'standard_pressure_kPa': DEFAULT_STANDARD_PRESSURE_KPA}

    return {'kind': 'unreadable',
            'note': ('没有看出标准状态或流量单位；如果认可默认状态，请回答'
                     '"总进料，0°C/101.325 kPa"或"默认"。')}


# Which fact field each generated question writes to. Fixed here, in code, so a
# client cannot reach an arbitrary field by inventing a question id.
_DIRECT_IDS = {
    'q-feed-flow': ('feed_total', 'feed_unit'),
    'q-feed-temperature': ('feed_temperature', None),
    'q-feed-pressure': ('feed_pressure', 'feed_pressure_unit'),
}

# Questions the compiler raises about a feed, keyed by the group in their id. The ids
# are `q-flow-missing-<n>` and friends, and they were previously unroutable: the graph
# asked them and no answer could ever satisfy them.
_COMPILER_QUESTION_FIELDS = {
    'flow-missing': ('feed_total', 'feed_unit'),
    'temp-missing': ('feed_temperature', None),
    'press-missing': ('feed_pressure', 'feed_pressure_unit'),
}
_COMPILER_QUESTION = re.compile(r'^q-(flow-missing|temp-missing|press-missing)-\d+$')
_FLOW_BASIS = re.compile(r'^q-flow-basis-\d+$')


# The unit field is not always `<field>_unit`: the flow's unit lives in `feed_unit`.
# Getting this wrong leaves a corrected value paired with a stale unit, which then
# contradicts the composition basis.
_UNIT_FIELDS = {'feed_total': 'feed_unit',
                'feed_pressure': 'feed_pressure_unit',
                'feed_temperature': 'feed_temperature_unit'}


def parse_answer_value(answer: Any) -> tuple[float | None, str | None]:
    """Pull a number and an optional unit out of whatever the user supplied.

    Accepts a bare number, a number with a unit, or a {value, unit} object. Returns
    (None, None) for anything unusable, which the caller treats as "leave it alone".
    """
    if isinstance(answer, bool):
        return None, None
    if isinstance(answer, (int, float)):
        return float(answer), None
    if isinstance(answer, dict):
        raw = answer.get('value', answer.get('answer'))
        unit = answer.get('unit')
        value, unit_from_text = parse_answer_value(raw)
        return value, (str(unit).strip() if unit else unit_from_text)
    if isinstance(answer, str):
        match = _LEADING_NUMBER.match(answer)
        if match:
            unit = (match.group(2) or '').strip() or None
            return float(match.group(1)), unit
    return None, None


def resolve_ungrounded(ungrounded: list[str], answers: dict[str, Any]
                       ) -> tuple[list[str], list[str], list[str]]:
    """Split the ungrounded fields into those the user has now answered and the rest.

    Returns (resolved, unresolved, notes).

    This exists because of a real defect. `ask_node` used to clear the entire
    ungrounded list on any resume, so replying "not a number" to a question about a
    fabricated flow released that flow: the fabricated value was still sitting in the
    facts, and it went straight to the adapter. Blocking correctly once and then
    handing the value through on an unreadable answer is worse than never blocking,
    because it looks like it was checked.

    A field counts as resolved only when an answer for it arrived AND that answer
    parsed into a number. Anything else stays blocking, and the user is asked again.
    """
    resolved: list[str] = []
    unresolved: list[str] = []
    notes: list[str] = []
    for field in ungrounded:
        answered = answers.get('q-ungrounded:%s' % field)
        if answered is None:
            unresolved.append(field)
            continue
        value, _unit = parse_answer_value(answered)
        if value is None:
            unresolved.append(field)
            notes.append('answer for %r could not be read as a number (%r); it is '
                         'still unconfirmed and still blocks the run'
                         % (field, answered))
            continue
        resolved.append(field)
        notes.append('%r confirmed by the user as %r' % (field, answered))
    return resolved, unresolved, notes


def answer_note(field: str, answer: Any) -> str:
    """A one-line record of an answer, for the run's problem list."""
    value, unit = parse_answer_value(answer)
    if value is None:
        return ('answer for %r could not be read as a number: %r'
                % (field, answer))
    return '%r set to %g%s from the user\'s answer' % (
        field, value, (' ' + unit) if unit else '')


# Answers that mean "yes, do it that way". The coal question is the one that needs
# this: `coal_questions` decides whether to ask by looking for exactly these phrases
# in the request text, so the way to answer it is to put the confirmation into the
# text rather than to invent a new fact field.
_AFFIRMATIVE = (
    '是', '对', '可以', '行', '好', '确认', '同意', '按纯碳', '按碳', '纯碳',
    'yes', 'ok', 'sure', 'confirm', 'agreed', 'pure carbon', 'as carbon',
)

# What an affirmative answer to `q-coal-definition` appends to the request text.
COAL_CONFIRMATION = '（用户确认：煤按纯固体碳处理。）'


def confirmation_text(answers: dict[str, Any], source_text: str) -> str:
    """Fold confirmations that live in the *text* rather than in a fact field.

    Only `q-coal-definition` works this way today. It is not a numeric field, so
    there is nothing to put in `facts`; what the compiler actually looks for is the
    phrase in the request. Appending it keeps one source of truth - the text - and
    means an answered question stops being asked without special-casing the compiler.
    """
    answer = (answers or {}).get('q-coal-definition')
    if answer is None:
        return source_text
    if COAL_CONFIRMATION.strip('（）') in source_text:
        return source_text
    text = str(answer).strip()
    # "不可以" contains "可以", so the negative test has to come first: reading it as
    # agreement appended a confirmation the user had just refused.
    if is_negative_text(text):
        return source_text
    if any(token in text.casefold() for token in _AFFIRMATIVE):
        return source_text + COAL_CONFIRMATION
    return source_text


def apply_answers(facts: dict[str, Any], answers: dict[str, Any],
                  note: list[str] | None = None) -> dict[str, Any]:
    """Fold answers into the extracted facts.

    The result is re-normalised from scratch afterwards rather than patching the
    compiled spec: patching would let the two descriptions of the same request drift
    apart.
    """
    merged = dict(facts)

    def assign(field: str, answer: Any, unit_field: str | None) -> None:
        value, unit = parse_answer_value(answer)
        if value is None:
            if note is not None:
                note.append('answer for %r could not be read as a number: %r'
                            % (field, answer))
            return
        merged[field] = value
        target = unit_field or _UNIT_FIELDS.get(field)
        if target and unit:
            merged[target] = unit

    def assign_normal_volume(answer: Any, question_id: str) -> None:
        """Route an answer to the volumetric-flow family of questions."""
        outcome = parse_normal_volume_answer(answer)
        kind = outcome['kind']
        if kind == 'normal_volume':
            merged['normal_volume_basis'] = {
                'standard_temperature_C': outcome['standard_temperature_C'],
                'standard_pressure_kPa': outcome['standard_pressure_kPa']}
            if outcome.get('total') is not None:
                merged['feed_total'] = outcome['total']
                merged['feed_unit'] = 'Nm3/h'
            if note is not None:
                note.append(
                    '%s: 进料总量按标准气体体积理解，标准状态 %g°C / %g kPa'
                    % (question_id, outcome['standard_temperature_C'],
                       outcome['standard_pressure_kPa']))
            return
        if kind == 'flow':
            merged['feed_total'] = outcome['value']
            merged['feed_unit'] = outcome['unit']
            # A mass or molar flow replaces the normal-volume reading: keeping the
            # basis would leave two different totals describing one stream.
            merged.pop('normal_volume_basis', None)
            return
        if note is not None:
            note.append('%s: %s' % (question_id, outcome['note']))

    for question_id, answer in (answers or {}).items():
        if question_id == 'q-feed-composition' and isinstance(answer, list):
            merged['feed_composition'] = answer
            continue
        if question_id == 'q-conversion-basis' and isinstance(answer, str):
            merged['conversion_basis'] = answer
            continue
        if question_id == 'q-volumetric-flow' or _FLOW_BASIS.match(question_id):
            assign_normal_volume(answer, question_id)
            continue
        compiler_question = _COMPILER_QUESTION.match(question_id)
        if compiler_question:
            field, unit_field = _COMPILER_QUESTION_FIELDS[compiler_question.group(1)]
            assign(field, answer, unit_field)
            continue
        if question_id.startswith('q-ungrounded:'):
            # The id carries the field name verbatim after the colon, so an answer
            # routes straight back to the field it belongs to.
            assign(question_id.split(':', 1)[1], answer, None)
            continue
        if question_id in _DIRECT_IDS:
            field, unit_field = _DIRECT_IDS[question_id]
            assign(field, answer, unit_field)
    return merged


# Backwards-compatible private alias: the graph used to own these names.
_apply_answers = apply_answers
