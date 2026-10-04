"""Two genuine model calls via offline transport; no network or HYSYS required."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from langgraph.types import Command

from reactor_agent.chat_service import ChatService
from reactor_agent.graph import build_graph, initial_state
from reactor_agent.llm import ChatClient, LlmConfig, LlmError
from reactor_agent.pipeline import run_pipeline
from reactor_agent.review import review_facts
from reactor_agent.test_graph import RecordingAdapter
from reactor_agent.test_normalize import SMR_FACTS
from reactor_agent.fixtures.ui_cases import TOLUENE_FACTS, TOLUENE_TEXT
from reactor_agent.ui_backend import SessionApp, Settings

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
LIVE_REVIEW = json.loads((Path(__file__).parent / 'fixtures' /
                         'toluene_review_failure.json').read_text(encoding='utf-8'))


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
    replies = [facts, review]
    def transport(url, payload, headers, timeout):
        calls.append(copy.deepcopy(payload))
        reply = replies[min(len(calls) - 1, 1)]
        return 200, json.dumps({'choices': [{'finish_reason': 'stop',
            'message': {'content': json.dumps(reply, ensure_ascii=False)}}]})
    client = ChatClient(LlmConfig(base='https://example.test/v1',
        key='offline-review-secret-for-tests', min_interval=0, attempts=1, **config),
        transport=transport, sleeper=lambda _: None)
    return client, calls


class ReviewTests(unittest.TestCase):
    def test_user_live_review_replays_to_ready_without_extra_questions(self):
        client, calls = model_client(live_toluene_facts(),
            {'corrections': LIVE_REVIEW['corrections'], 'questions': []})
        state = build_graph(client).invoke(initial_state(LIVE_REVIEW['text']),
            {'configurable': {'thread_id': 'live-review'}})
        self.assertEqual(state['status'], 'READY', state.get('blocking'))
        self.assertEqual(state['blocking'], [])
        self.assertEqual(len(calls), 2)
        spec = state['cases'][0]['spec']
        self.assertEqual(spec['reactor']['thermal_mode'], 'adiabatic')
        self.assertIsNone(spec['reactor'].get('outlet_temperature'))
        self.assertEqual(spec['feeds'][0]['total_flow'], 6000)
        self.assertEqual(spec['feeds'][0]['fractions'], {'Toluene': 1.0})
        self.assertEqual(spec['feeds'][0]['pressure'], 2)
        self.assertEqual(spec['reactor']['pressure_drop_kPa'], 0)
        self.assertTrue(state['review']['rejected_corrections'])
        self.assertNotIn('模型指出未提供：反应热', state.get('explanation', ''))
        stoich = spec['reactions'][0]['stoichiometry']
        self.assertEqual(stoich['Toluene'], -2)
        self.assertEqual(stoich['Benzene'], 1)
        for name in ('o-Xylene', 'm-Xylene', 'p-Xylene'):
            self.assertAlmostEqual(stoich[name], 1 / 3)

    def test_quoted_inlet_temperature_cannot_become_adiabatic_outlet(self):
        client, _ = model_client(live_toluene_facts(), {'corrections': [
            patch_field('outlet_temperatures', [400], '温度400℃')], 'questions': []})
        state = build_graph(client).invoke(initial_state(LIVE_REVIEW['text']),
            {'configurable': {'thread_id': 'inlet-not-outlet'}})
        self.assertEqual(state['status'], 'READY', state.get('blocking'))
        self.assertEqual(state['facts']['outlet_temperatures'], [])
        self.assertEqual(state['cases'][0]['spec']['reactor']['thermal_mode'], 'adiabatic')

    def test_reviewer_questions_cannot_override_source_proofs(self):
        questions = [{'field': field, 'question': '请确认该条件。', 'reason': '模型认为缺失。'}
                     for field in ('feed_composition', 'composition_basis', 'reactions',
                                   'outlet_temperatures', 'case_pressures', 'missing_information')]
        client, _ = model_client(live_toluene_facts(), {'corrections': [], 'questions': questions})
        state = build_graph(client).invoke(initial_state(LIVE_REVIEW['text']),
            {'configurable': {'thread_id': 'source-proofs'}})
        self.assertEqual(state['status'], 'READY', state.get('blocking'))

    def test_wrong_other_coefficient_still_needs_confirmation(self):
        facts = live_toluene_facts()
        facts['reactions'][0]['species'][0]['coefficient'] = -3
        client, _ = model_client(facts, {'corrections': [
            patch_field('reactions', live_toluene_facts()['reactions'], '模型自己的解释')], 'questions': []})
        state = build_graph(client).invoke(initial_state(LIVE_REVIEW['text']),
            {'configurable': {'thread_id': 'wrong-coefficient'}})
        self.assertEqual(state['status'], 'WAITING_INPUT')
        self.assertFalse(state['cases'])
        self.assertTrue(any(q['id'] == 'q-review:reactions' for q in state['blocking']))

    def test_xylene_group_without_stated_isomers_still_blocks(self):
        facts = dict(live_toluene_facts(), species=['甲苯', '苯', '二甲苯'])
        text = LIVE_REVIEW['text'].replace('邻、间、对二甲苯，三种二甲苯按等摩尔比例分配', '二甲苯')
        client, _ = model_client(facts, EMPTY_REVIEW)
        state = build_graph(client).invoke(initial_state(text),
            {'configurable': {'thread_id': 'unknown-isomer'}})
        self.assertEqual(state['status'], 'WAITING_INPUT')
        self.assertTrue(any(q['field'] == 'fluid_package.components' for q in state['blocking']))

    def test_negated_or_multiple_pure_feeds_do_not_discharge_composition_question(self):
        from reactor_agent.normalize import stated_pure_feed
        for text in ('进料不是纯甲苯', '进料为非纯甲苯', '进料为纯甲苯，另一股进料为水'):
            with self.subTest(text=text):
                self.assertIsNone(stated_pure_feed(text))

    def test_isothermal_setpoint_is_retained(self):
        facts = dict(live_toluene_facts(), outlet_temperatures=[450], outlet_temperature_unit='℃')
        text = LIVE_REVIEW['text'].replace('反应器绝热', '反应器等温，出口温度450℃')
        client, _ = model_client(facts, EMPTY_REVIEW)
        state = build_graph(client).invoke(initial_state(text),
            {'configurable': {'thread_id': 'isothermal-setpoint'}})
        self.assertEqual(state['status'], 'READY', state.get('blocking'))
        self.assertEqual(state['cases'][0]['spec']['reactor']['outlet_temperature'], 450)

    def test_review_is_enabled_by_default_in_cli_and_ui(self):
        self.assertTrue(LlmConfig().review)
        self.assertTrue(LlmConfig.from_env({}).review)
        self.assertTrue(Settings().config().review)
        self.assertFalse(LlmConfig.from_env({'TR_REVIEW': '0'}).review)

    def test_review_receives_original_and_extracted_facts(self):
        client, calls = model_client(TOLUENE_FACTS, EMPTY_REVIEW)
        state = build_graph(client).invoke(initial_state(TOLUENE_TEXT), {'configurable': {'thread_id': 'review-test'}})
        self.assertEqual(state['status'], 'READY')
        self.assertEqual(len(calls), 2)
        data = json.loads(calls[1]['messages'][1]['content'])
        self.assertEqual(data['original_text'], TOLUENE_TEXT)
        self.assertEqual(data['extracted_facts'], TOLUENE_FACTS)
        self.assertEqual(state['review']['status'], 'PASS')

    def test_review_recovers_omitted_flow_with_evidence(self):
        facts = dict(TOLUENE_FACTS, feed_total=None)
        review = {'corrections': [patch_field('feed_total', 10000, '进料流量10000kg/h')],
                  'questions': []}
        client, calls = model_client(facts, review)
        state = build_graph(client).invoke(initial_state(TOLUENE_TEXT, kind='conversion'), {'configurable': {'thread_id': 'review-test'}})
        self.assertEqual(state['status'], 'READY')
        self.assertEqual(state['cases'][0]['spec']['feeds'][0]['total_flow'], 10000)
        self.assertEqual(len(calls), 2)  # No extraction gap retries before reviewing.

    def test_two_pressure_cases_compile_distinct_inlets_and_names(self):
        client, calls = model_client(PRESSURE_FACTS, EMPTY_REVIEW)
        state = build_graph(client).invoke(initial_state(PRESSURE_TEXT), {'configurable': {'thread_id': 'review-test'}})
        self.assertEqual(state['status'], 'READY', state.get('problems'))
        self.assertEqual(len(calls), 2)
        specs = [c['spec'] for c in state['cases']]
        self.assertEqual([s['feeds'][0]['pressure'] for s in specs], [5, 20])
        self.assertEqual([s['feeds'][0]['pressure_unit'] for s in specs], ['bar', 'bar'])
        self.assertEqual([s['reactor']['outlet_temperature'] for s in specs], [750, 750])
        self.assertEqual([s['reactor']['pressure_drop_kPa'] for s in specs], [0, 0])
        self.assertEqual(len({s['case_name'] for s in specs}), 2)

    def test_reviewer_restores_duplicate_temperatures(self):
        client, _ = model_client(dict(PRESSURE_FACTS, outlet_temperatures=[750]),
            {'corrections': [patch_field('outlet_temperatures', [750, 750],
              '工况一：进料和出口压力均为5 bar，出口750℃。工况二：进料和出口压力均为20 bar，出口750℃。')],
             'questions': []})
        state = build_graph(client).invoke(initial_state(PRESSURE_TEXT), {'configurable': {'thread_id': 'review-test'}})
        self.assertEqual(state['status'], 'READY')
        self.assertEqual(len(state['cases']), 2)

    def test_ambiguous_varying_pressure_still_asks_once(self):
        text = PRESSURE_TEXT.replace('进料和出口压力均为', '出口压力为').replace('两个工况均无压降，', '')
        client, _ = model_client(PRESSURE_FACTS, EMPTY_REVIEW)
        state = build_graph(client).invoke(initial_state(text), {'configurable': {'thread_id': 'review-test'}})
        self.assertEqual(state['status'], 'WAITING_INPUT')
        pressure_questions = [q for q in state['blocking'] if q['field'] == 'feeds[0].pressure']
        self.assertEqual(len(pressure_questions), 1)
        self.assertFalse(state['cases'])

    def test_temperature_pressure_count_mismatch_blocks(self):
        client, _ = model_client(dict(PRESSURE_FACTS, outlet_temperatures=[750]), EMPTY_REVIEW)
        state = build_graph(client).invoke(initial_state(PRESSURE_TEXT), {'configurable': {'thread_id': 'review-test'}})
        self.assertEqual(state['status'], 'WAITING_INPUT')
        self.assertFalse(state['cases'])

    def test_missing_condition_asks_and_resumes_without_model_recall(self):
        text = TOLUENE_TEXT.replace('进料温度380℃，', '')
        facts = dict(TOLUENE_FACTS, feed_temperature=None)
        review = {'corrections': [], 'questions': [{'field': 'feed_temperature',
            'question': '请给出进料温度。', 'reason': '原文未给出进料温度。'}]}
        client, calls = model_client(facts, review)
        graph = build_graph(client)
        config = {'configurable': {'thread_id': 'review-ask'}}
        state = graph.invoke(initial_state(text), config)
        self.assertEqual([q['id'] for q in state['blocking']], ['q-review:feed_temperature'])
        invalid = graph.invoke(Command(resume={'q-review:feed_temperature': '不知道'}), config)
        self.assertEqual(invalid['status'], 'WAITING_INPUT')
        state = graph.invoke(Command(resume={'q-review:feed_temperature': '380 C'}), config)
        self.assertEqual(state['status'], 'READY')
        self.assertEqual(len(calls), 2)

    def test_composition_review_question_accepts_only_valid_json(self):
        review = {'corrections': [], 'questions': [{'field': 'feed_composition',
            'question': '请确认组成。', 'reason': '组成含义存在歧义。'}]}
        client, calls = model_client(TOLUENE_FACTS, review)
        graph = build_graph(client)
        config = {'configurable': {'thread_id': 'review-composition'}}
        state = graph.invoke(initial_state(TOLUENE_TEXT), config)
        state = graph.invoke(Command(resume={'q-review:feed_composition': '["bad"]'}), config)
        self.assertEqual(state['status'], 'WAITING_INPUT')
        state = graph.invoke(Command(resume={'q-review:feed_composition':
            '[{"name":"甲苯","fraction":1}]'}), config)
        self.assertEqual(state['status'], 'READY')
        self.assertEqual(len(calls), 2)

    def test_invalid_reviewer_reply_never_executes(self):
        replies = [
            {'corrections': [patch_field('feed_total', 12345, '进料流量10000kg/h')], 'questions': []},
            {'corrections': [patch_field('feed_total', 10000, '不存在的句子')], 'questions': []},
            {'corrections': [patch_field('reactor_type', 'gibbs', '甲苯')], 'questions': []},
            {'corrections': [{'field': 5, 'value_json': '1', 'evidence': '甲苯'}], 'questions': []},
            {'corrections': [patch_field('feed_composition', ['bad'], '甲苯')], 'questions': []},
            {'corrections': [], 'questions': [{'field': 'unknown', 'question': 'q', 'reason': 'r'}]},
            {'corrections': [], 'questions': [], 'approved': True},
            {'corrections': [patch_field('feed_total', float('nan'), '甲苯')], 'questions': []},
        ]
        for index, reply in enumerate(replies):
            with self.subTest(reply=reply):
                client, calls = model_client(TOLUENE_FACTS, reply)
                adapter = RecordingAdapter()
                state = build_graph(client, adapter=adapter, dry_run=False).invoke(initial_state(TOLUENE_TEXT), {'configurable': {'thread_id': 'review-test'}})
                expected = 'WAITING_INPUT' if index in (0, 1, 4, 7) else 'FAILED'
                self.assertEqual(state['status'], expected)
                self.assertEqual(state['review']['status'], expected)
                self.assertFalse(adapter.calls)
                if expected == 'WAITING_INPUT':
                    self.assertTrue(state['blocking'])
                    for field, value in TOLUENE_FACTS.items():
                        self.assertEqual(state['facts'][field], value)

    def test_exhausted_request_budget_fails_review_without_execution(self):
        client, calls = model_client(TOLUENE_FACTS, EMPTY_REVIEW, max_requests=1)
        adapter = RecordingAdapter()
        state = build_graph(client, adapter=adapter, dry_run=False).invoke(initial_state(TOLUENE_TEXT), {'configurable': {'thread_id': 'review-test'}})
        self.assertEqual(state['status'], 'FAILED')
        self.assertEqual(len(calls), 1)
        self.assertFalse(adapter.calls)

    def test_single_pass_pipeline_also_reviews_and_preserves_case_pressures(self):
        client, calls = model_client(PRESSURE_FACTS, EMPTY_REVIEW)
        run = run_pipeline(PRESSURE_TEXT, client=client)
        self.assertEqual(run.status, 'READY', run.problems)
        self.assertEqual(len(calls), 2)
        self.assertEqual([c.spec['feeds'][0]['pressure'] for c in run.plan.cases], [5, 20])
        self.assertEqual(run.summary()['input_review']['status'], 'PASS')

    def test_both_pressure_cases_execute_once_with_offline_adapter(self):
        client, calls = model_client(PRESSURE_FACTS, EMPTY_REVIEW)
        adapter = RecordingAdapter()
        with tempfile.TemporaryDirectory() as folder:
            state = build_graph(client, adapter=adapter, dry_run=False,
                run_root=Path(folder)).invoke(initial_state(PRESSURE_TEXT),
                {'configurable': {'thread_id': 'review-execute'}})
        self.assertEqual(state['status'], 'PASS')
        self.assertEqual(adapter.calls, ['case-1', 'case-2'])
        self.assertEqual(len(calls), 2)

    def test_review_failure_in_single_pass_pipeline_stops_before_adapter(self):
        client, _ = model_client(TOLUENE_FACTS,
            {'corrections': [patch_field('feed_total', 12345, '甲苯')], 'questions': []})
        adapter = RecordingAdapter()
        run = run_pipeline(TOLUENE_TEXT, client=client, adapter=adapter, dry_run=False)
        self.assertEqual(run.status, 'WAITING_INPUT')
        self.assertFalse(adapter.calls)

    def test_unquoted_composition_correction_pauses_and_user_can_resume(self):
        client, calls = model_client(PRESSURE_FACTS, {'corrections': [
            patch_field('feed_composition', [{'name': '甲烷', 'fraction': 50},
                {'name': '水', 'fraction': 50}], '甲烷占四分之一，水占四分之三')], 'questions': []})
        graph = build_graph(client)
        config = {'configurable': {'thread_id': 'rejected-composition'}}
        state = graph.invoke(initial_state(PRESSURE_TEXT), config)
        self.assertEqual(state['status'], 'WAITING_INPUT')
        self.assertEqual([q['id'] for q in state['blocking']], ['q-review:feed_composition'])
        self.assertEqual(state['facts']['feed_composition'], PRESSURE_FACTS['feed_composition'])
        self.assertTrue(state['review']['rejected_corrections'])
        state = graph.invoke(Command(resume={'q-review:feed_composition': '不知道'}), config)
        self.assertEqual(state['status'], 'WAITING_INPUT')
        state = graph.invoke(Command(resume={'q-review:feed_composition': json.dumps(
            PRESSURE_FACTS['feed_composition'], ensure_ascii=False)}), config)
        self.assertEqual(state['status'], 'READY')
        self.assertEqual(len(state['cases']), 2)
        self.assertEqual(len(calls), 2)

    def test_rejected_composition_rolls_back_basis_but_keeps_unrelated_valid_patch(self):
        facts = dict(PRESSURE_FACTS, feed_total=None)
        reply = {'corrections': [
            patch_field('feed_composition', [{'name': '甲烷', 'fraction': 50}], '无效引用'),
            patch_field('composition_basis', 'mass_fraction', '摩尔组成为甲烷25%、水蒸气75%'),
            patch_field('feed_total', 2000, '进料总流量2000 kmol/h')], 'questions': []}
        client, _ = model_client(facts, reply)
        state = build_graph(client).invoke(initial_state(PRESSURE_TEXT),
            {'configurable': {'thread_id': 'rollback-basis'}})
        self.assertEqual(state['status'], 'WAITING_INPUT')
        self.assertEqual(state['facts']['composition_basis'], facts['composition_basis'])
        self.assertEqual(state['facts']['feed_total'], 2000)
        self.assertEqual([p['field'] for p in state['review']['corrections']], ['feed_total'])

    def test_ui_honors_total_request_budget_from_environment(self):
        config = Settings(env={'TR_MAX_REQUESTS': '2', 'TR_ATTEMPTS': '1', 'TR_GAP': '0'}).config()
        self.assertEqual(config.max_requests, 2)
        self.assertEqual(config.attempts, 1)
        self.assertEqual(config.min_interval, 0)

    def test_confirmed_review_value_releases_ungrounded_field(self):
        client, calls = model_client(dict(TOLUENE_FACTS, feed_total=12345),
            {'corrections': [], 'questions': [{'field': 'feed_total',
                'question': '请确认进料流量。', 'reason': '抽取值与原文不一致。'}]})
        graph = build_graph(client)
        config = {'configurable': {'thread_id': 'review-ungrounded'}}
        state = graph.invoke(initial_state(TOLUENE_TEXT), config)
        self.assertEqual(state['status'], 'WAITING_INPUT')
        self.assertEqual(len(state['blocking']), 1)
        state = graph.invoke(Command(resume={'q-review:feed_total': '10000 kg/h'}), config)
        self.assertEqual(state['status'], 'READY')
        self.assertEqual(state['cases'][0]['spec']['feeds'][0]['total_flow'], 10000)
        self.assertEqual(len(calls), 2)

    def test_streamed_ui_records_review_and_refresh_never_calls_model(self):
        client, calls = model_client(PRESSURE_FACTS, EMPTY_REVIEW)
        with tempfile.TemporaryDirectory() as folder:
            app = SessionApp(root=Path(folder), settings=Settings(key=client.config.key))
            with patch.object(app, 'client', return_value=client):
                payload = ChatService(app).start(PRESSURE_TEXT)
                reviews = [e for e in payload['process'] if e['stage'] == 'review']
                self.assertEqual([e['status'] for e in reviews], ['RUNNING', 'PASS'])
                run = next(iter(app.runs.values()))
                saved = json.loads((run.folder / 'state.json').read_text(encoding='utf-8'))
                self.assertEqual(saved['input_review']['status'], 'PASS')
                app.run_payload(run, app.current_state(run))
                self.assertEqual(len(calls), 2)


if __name__ == '__main__':
    unittest.main()
