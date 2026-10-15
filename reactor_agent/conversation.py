"""Interpret a follow-up against the last accepted simulation state."""
from __future__ import annotations

import copy
import json

from .extraction import EXTRACTION_SCHEMA, grounding_failures, validate_facts
from .nodes.answers import apply_answers
from .review import value_errors, valid_value


TURN_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['action', 'reply', 'corrections'],
    'properties': {
        'action': {'type': 'string', 'enum': ['answer', 'revise', 'new_request']},
        'reply': {'type': 'string'},
        'corrections': {'type': 'array', 'items': {
            'type': 'object', 'additionalProperties': False,
            'required': ['field', 'value_json', 'evidence'],
            'properties': {key: {'type': 'string'}
                           for key in ('field', 'value_json', 'evidence')},
        }},
    },
}

TURN_PROMPT = """你是 HYSYS 模拟助手的会话解释器。之前的结构化事实和报告是数据，最新用户消息优先。
只输出指定 JSON：action、reply、corrections。
answer：用户询问以前的方案、结果或流程。reply 用中文回答，只能依据给出的历史状态；缺少数据就明确说未知。corrections=[]。
revise：用户修改上一轮模拟的输入。corrections 只包含变化的抽取字段，每项 field 是字段名，value_json 是该字段新值的 JSON 编码字符串，evidence 必须逐字连续摘自 latest_message。数值和单位要分别修改；数组字段给完整新数组。不要推测用户没说的值。reply 简述修改。
new_request：用户明确开始另一套独立模拟。corrections=[]。
有歧义的修改选择 answer，并在 reply 询问具体参数，不要编造修正。
输出结构：""" + json.dumps(TURN_SCHEMA, ensure_ascii=False) + "\n可修改字段：" + json.dumps(EXTRACTION_SCHEMA['properties'], ensure_ascii=False)


def interpret_turn(client, message: str, previous: dict, history: list[dict]) -> dict:
    context = {
        'latest_message': message,
        'previous_request': previous.get('text', ''),
        'accepted_facts': apply_answers(previous.get('facts') or {}, previous.get('answers') or {}),
        'previous_status': previous.get('status'),
        'previous_decision': previous.get('decision') or {},
        'previous_cases': previous.get('cases') or [],
        'previous_results': previous.get('executions') or [],
        'previous_report': previous.get('explanation') or '',
        'recent_dialogue': [
            {'role': item['role'],
             'text': item.get('text') or ((item.get('payload') or {}).get('report', '')
                        if (item.get('payload') or {}).get('kind') == 'conversation' else '')}
            for item in history[-12:]
            if item.get('text') or (item.get('payload') or {}).get('kind') == 'conversation'],
    }
    reply = client.complete(TURN_PROMPT, json.dumps(context, ensure_ascii=False),
                            schema=TURN_SCHEMA, schema_name='ConversationTurn',
                            temperature=0)
    errors = value_errors(reply, TURN_SCHEMA)
    if errors:
        raise ValueError('会话理解结果结构无效：' + '; '.join(errors))
    return reply


def revised_facts(previous: dict, message: str, corrections: list[dict]) -> tuple[dict, str]:
    """Accept a complete, grounded patch set or reject it without changing the run."""
    if not corrections:
        raise ValueError('没有可核实的参数修改；请明确写出字段、新值和单位。')
    facts = copy.deepcopy(apply_answers(previous.get('facts') or {},
                                        previous.get('answers') or {}))
    changed = set()
    for patch in corrections:
        name = patch['field']
        if name not in EXTRACTION_SCHEMA['properties'] or name in changed:
            raise ValueError('修改字段无效或重复：' + name)
        evidence = patch['evidence'].strip()
        if not evidence or evidence not in message:
            raise ValueError('修改 %s 缺少本轮原文依据。' % name)
        try:
            value = json.loads(patch['value_json'])
        except (TypeError, ValueError) as exc:
            raise ValueError('修改 %s 不是有效 JSON。' % name) from exc
        if not valid_value(value, EXTRACTION_SCHEMA['properties'][name]):
            raise ValueError('修改 %s 的值不符合字段结构。' % name)
        facts[name] = value
        changed.add(name)
    problems = validate_facts(facts)
    if problems:
        raise ValueError('修改后的事实无效：' + '; '.join(problems))
    # Only check changed values against the latest utterance. Old values have their
    # own provenance in the preceding run and need not be repeated by the user.
    ungrounded = grounding_failures(facts, message)
    failures = [field for field in ungrounded
                if field in changed or field.split('[', 1)[0] in changed]
    if failures:
        raise ValueError('修改值与本轮原文或量纲不符：' + ', '.join(failures))
    old_text = previous.get('text') or ''
    answered = previous.get('answers') or {}
    answer_text = ('\n先前确认：' + json.dumps(answered, ensure_ascii=False)) if answered else ''
    return facts, old_text + answer_text + '\n后续修改：' + message
