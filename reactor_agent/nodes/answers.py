"""Turning a user's answer into a fact.

Answers arrive as free text from a CLI, a web form, or a person typing "10000 kg/h".
This module reads all the reasonable spellings, and - more importantly - decides what
to do when it cannot read one.

The rule is: **leave the field alone and say so.** An unreadable answer must not
reach the contract, because pydantic would raise a traceback from deep inside the
graph, and it must not be quietly dropped either, because then the user's answer
disappears without a word.
"""
from __future__ import annotations

import re
from typing import Any

# A leading number, then anything that could be a unit.
_LEADING_NUMBER = re.compile(r'^\s*(-?\d+(?:\.\d+)?)\s*([A-Za-z/%°]+.*)?$')

# Which fact field each generated question writes to. Fixed here, in code, so a
# client cannot reach an arbitrary field by inventing a question id.
_DIRECT_IDS = {
    'q-feed-flow': ('feed_total', 'feed_unit'),
    'q-feed-temperature': ('feed_temperature', None),
    'q-feed-pressure': ('feed_pressure', 'feed_pressure_unit'),
    'q-volumetric-flow': ('feed_total', 'feed_unit'),
}

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
    text = str(answer).strip().casefold()
    if any(token in text for token in _AFFIRMATIVE):
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

    for question_id, answer in (answers or {}).items():
        if question_id == 'q-feed-composition' and isinstance(answer, list):
            merged['feed_composition'] = answer
            continue
        if question_id == 'q-conversion-basis' and isinstance(answer, str):
            merged['conversion_basis'] = answer
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
