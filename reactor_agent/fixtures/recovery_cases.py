"""Inputs and offline transport for pre-check driven recovery regressions."""
import copy
import json
from pathlib import Path
from reactor_agent.llm import ChatClient, LlmConfig
from reactor_agent.test_graph import RecordingAdapter
from reactor_agent.test_normalize import SMR_FACTS
from reactor_agent.fixtures.ui_cases import TOLUENE_FACTS, TOLUENE_TEXT

PRESSURE_TEXT = ('模拟甲烷蒸汽重整：CH4 + H2O ⇌ CO + 3 H2；CO + H2O ⇌ CO2 + H2。'
    '进料总流量2000 kmol/h，摩尔组成为甲烷25%、水蒸气75%，进料500℃。'
    '没有动力学参数，也不指定转化率。工况一：进料和出口压力均为5 bar，出口750℃。'
    '工况二：进料和出口压力均为20 bar，出口750℃。两个工况均无压降，等温。')
PRESSURE_FACTS = dict(SMR_FACTS, feed_total=2000, feed_unit='kmol/h',
    feed_composition=[{'name': '甲烷', 'fraction': 25}, {'name': '水蒸气', 'fraction': 75}],
    composition_basis='mole_percent', feed_temperature=500,
    outlet_temperatures=[750, 750], case_pressures=[5, 20], missing_information=[])
PRESSURE_FACTS['reactions'] = [
    {'name': 'SMR', 'reversible': True, 'species': [
        {'name': name, 'coefficient': coefficient} for name, coefficient in
        [('甲烷', -1), ('水', -1), ('一氧化碳', 1), ('氢气', 3)]]},
    {'name': 'WGS', 'reversible': True, 'species': [
        {'name': name, 'coefficient': coefficient} for name, coefficient in
        [('一氧化碳', -1), ('水', -1), ('二氧化碳', 1), ('氢气', 1)]]},
]
EMPTY_REVIEW = {'corrections': [], 'questions': []}
LIVE_REVIEW = json.loads((Path(__file__).parent /
                         'toluene_review_failure.json').read_text(encoding='utf-8'))
LIVE_GASIFICATION = json.loads((Path(__file__).parent /
                         'gasification_review_input.json').read_text(encoding='utf-8'))
GASIFICATION_NOOP = json.loads((Path(__file__).parent /
                         'gasification_review_noop.json').read_text(encoding='utf-8'))


def live_toluene_facts():
    facts = copy.deepcopy(TOLUENE_FACTS)
    facts.update(species=['甲苯', '苯', '邻二甲苯', '间二甲苯', '对二甲苯'],
        feed_composition=[{'name': '甲苯', 'fraction': 100}], composition_basis='mass_fraction',
        feed_total=6000, feed_temperature=400, feed_pressure=2, conversion_percent=30,
        missing_information=['反应热数据，无法计算绝热出口温度',
            '产物比热容数据，无法进行能量衡算', '催化剂装填量或反应器体积，无法验证停留时间或压降假设'])
    facts['reactions'][0]['species'] += [
        {'name': '间二甲苯', 'coefficient': 1}, {'name': '对二甲苯', 'coefficient': 1}]
    return facts


def patch_field(field, value, quote):
    return {'field': field, 'value_json': json.dumps(value, ensure_ascii=False), 'evidence': quote}


def model_client(facts, review, **config):
    calls = []
    replies = [facts] + (review if isinstance(review, list) else [review])
    def transport(url, payload, headers, timeout):
        calls.append(copy.deepcopy(payload))
        reply = replies[min(len(calls) - 1, len(replies) - 1)]
        return 200, json.dumps({'choices': [{'finish_reason': 'stop',
            'message': {'content': json.dumps(reply, ensure_ascii=False)}}]})
    client = ChatClient(LlmConfig(base='https://example.test/v1',
        key='offline-review-secret-for-tests', min_interval=0, attempts=1, **config),
        transport=transport, sleeper=lambda _: None)
    return client, calls


