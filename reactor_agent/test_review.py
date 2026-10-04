"""The recovery model is advisory; deterministic planning owns every question."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from langgraph.types import Command

from reactor_agent.fixtures.recovery_cases import (model_client, patch_field, EMPTY_REVIEW,
    LIVE_GASIFICATION, GASIFICATION_NOOP, LIVE_REVIEW, live_toluene_facts,
    TOLUENE_FACTS, TOLUENE_TEXT, PRESSURE_FACTS, PRESSURE_TEXT, RecordingAdapter)
from reactor_agent.graph import build_graph, initial_state
from reactor_agent.pipeline import run_pipeline
from reactor_agent.review import REVIEW_SCHEMA, value_errors
from reactor_agent.ui_backend import SessionApp, Settings
from reactor_agent.chat_service import ChatService


def run(facts, reply=EMPTY_REVIEW, text=TOLUENE_TEXT, **config):
    client, calls = model_client(facts, reply, **config)
    graph = build_graph(client)
    cfg = {'configurable': {'thread_id': 'recovery'}}
    state = graph.invoke(initial_state(text), cfg)
    return state, graph, cfg, calls


class RecoveryTests(unittest.TestCase):
    def test_latest_real_destructive_review_cannot_clear_flow_or_shrink_species(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures' /
                              'gasification_review_destructive.json').read_text(encoding='utf-8'))
        reply = {k: fixture[k] for k in ('corrections', 'questions')}
        facts = dict(LIVE_GASIFICATION['facts'], feed_pressure_unit=None)
        state, graph, cfg, calls = run(facts, reply, LIVE_GASIFICATION['text'])
        self.assertEqual(state['facts']['feed_total'], 80000)
        self.assertEqual(state['facts']['feed_unit'], 'Nm3/h')
        self.assertEqual(state['facts']['species'], facts['species'])
        self.assertEqual(state['facts']['feed_pressure_unit'], 'bar')
        self.assertEqual({q['id'] for q in state['blocking']}, {'q-coal-definition', 'q-volumetric-flow'})
        state = graph.invoke(Command(resume={q['id']: q['default'] for q in state['blocking']}), cfg)
        self.assertEqual(state['status'], 'READY', state.get('blocking'))
        self.assertEqual(len(calls), 1)

    def test_missing_flow_unit_is_recovered_without_choosing_its_physical_basis(self):
        state, _, _, calls = run(dict(LIVE_GASIFICATION['facts'], feed_unit=None),
                                text=LIVE_GASIFICATION['text'])
        self.assertEqual(state['facts']['feed_unit'], 'Nm3/h')
        self.assertEqual({q['id'] for q in state['blocking']}, {'q-coal-definition', 'q-volumetric-flow'})
        self.assertEqual(len(calls), 1)

    def test_real_destructive_reply_is_advisory_when_a_real_gap_requires_calling_model(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures' /
                              'gasification_review_destructive.json').read_text(encoding='utf-8'))
        reply = {k: fixture[k] for k in ('corrections', 'questions')}
        facts = dict(LIVE_GASIFICATION['facts'], feed_temperature=None, feed_pressure_unit=None)
        state, _, _, calls = run(facts, reply, LIVE_GASIFICATION['text'])
        self.assertEqual(state['facts']['feed_total'], 80000)
        self.assertEqual(state['facts']['feed_unit'], 'Nm3/h')
        self.assertEqual(state['facts']['species'], facts['species'])
        self.assertEqual({q['id'] for q in state['blocking']},
                         {'q-feed-temperature', 'q-coal-definition', 'q-volumetric-flow'})
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(state['review']['ignored_questions']), 2)

    def test_recovering_flow_value_also_recovers_its_missing_literal_unit(self):
        facts = dict(LIVE_GASIFICATION['facts'], feed_total=None, feed_unit=None)
        reply = {'corrections': [patch_field('feed_total', 80000, '流量80000Nm3/h')], 'questions': []}
        state, _, _, calls = run(facts, reply, LIVE_GASIFICATION['text'])
        self.assertEqual(state['facts']['feed_total'], 80000)
        self.assertEqual(state['facts']['feed_unit'], 'Nm3/h')
        self.assertEqual({q['id'] for q in state['blocking']}, {'q-coal-definition', 'q-volumetric-flow'})
        self.assertEqual(len(calls), 2)

    def test_in_scope_species_edit_cannot_drop_known_components(self):
        facts = dict(LIVE_GASIFICATION['facts'], species=LIVE_GASIFICATION['facts']['species'] + ['未知物质'])
        reply = {'corrections': [patch_field('species', ['煤炭', '水'], '进料为煤炭和水')], 'questions': []}
        state, _, _, calls = run(facts, reply, LIVE_GASIFICATION['text'])
        self.assertEqual(state['facts']['species'], facts['species'])
        self.assertEqual(state['review']['target_fields'], ['species'])
        self.assertTrue(any(q['field'] == 'fluid_package.components' for q in state['blocking']))
        self.assertEqual(len(calls), 2)

    def test_complete_toluene_uses_one_request_and_skips_recovery(self):
        state, _, _, calls = run(TOLUENE_FACTS, {'garbage': True})
        self.assertEqual(state['status'], 'READY')
        self.assertEqual(len(calls), 1)
        self.assertEqual(state['review']['status'], 'SKIPPED')

    def test_real_toluene_input_does_not_call_harmful_full_review(self):
        state, _, _, calls = run(live_toluene_facts(),
            {'corrections': LIVE_REVIEW['corrections'], 'questions': []}, LIVE_REVIEW['text'])
        self.assertEqual(state['status'], 'READY', state.get('blocking'))
        self.assertEqual(len(calls), 1)
        spec = state['cases'][0]['spec']
        self.assertEqual(spec['reactor']['thermal_mode'], 'adiabatic')
        self.assertIsNone(spec['reactor'].get('outlet_temperature'))
        for name in ('o-Xylene', 'm-Xylene', 'p-Xylene'):
            self.assertAlmostEqual(spec['reactions'][0]['stoichiometry'][name], 1 / 3)

    def test_real_gasification_noop_review_is_no_longer_needed(self):
        reply = {k: GASIFICATION_NOOP[k] for k in ('corrections', 'questions')}
        state, graph, cfg, calls = run(LIVE_GASIFICATION['facts'], reply, LIVE_GASIFICATION['text'])
        self.assertEqual({q['id'] for q in state['blocking']}, {'q-coal-definition', 'q-volumetric-flow'})
        self.assertEqual(state['review']['target_fields'], [])
        state = graph.invoke(Command(resume={q['id']: q['default'] for q in state['blocking']}), cfg)
        self.assertEqual(state['status'], 'READY', state.get('blocking'))
        self.assertEqual(state['cases'][0]['spec']['reactor']['solid_carbon'], 'saturation')
        self.assertEqual(len(calls), 1)

    def test_two_pressure_cases_keep_two_inlets_without_recovery(self):
        state, _, _, calls = run(PRESSURE_FACTS, text=PRESSURE_TEXT)
        self.assertEqual(state['status'], 'READY', state.get('blocking'))
        specs = [c['spec'] for c in state['cases']]
        self.assertEqual([s['feeds'][0]['pressure'] for s in specs], [5, 20])
        self.assertEqual([s['reactor']['outlet_temperature'] for s in specs], [750, 750])
        self.assertEqual([s['reactor']['pressure_drop_kPa'] for s in specs], [0, 0])
        self.assertEqual(len({s['case_name'] for s in specs}), 2)
        self.assertEqual(len(calls), 1)

    def test_missing_flow_is_recovered_with_evidence(self):
        reply = {'corrections': [patch_field('feed_total', 10000, '进料流量10000kg/h')], 'questions': []}
        state, _, _, calls = run(dict(TOLUENE_FACTS, feed_total=None), reply)
        self.assertEqual(state['status'], 'READY', state.get('blocking'))
        self.assertEqual(state['cases'][0]['spec']['feeds'][0]['total_flow'], 10000)
        self.assertEqual(len(calls), 2)
        request = json.loads(calls[1]['messages'][1]['content'])
        self.assertEqual(request['target_fields'], ['feed_total', 'feed_unit'])
        self.assertEqual(request['original_text'], TOLUENE_TEXT)
        self.assertEqual(request['precheck']['status'], 'WAITING_INPUT')

    def test_model_questions_do_not_create_new_user_questions(self):
        facts = dict(TOLUENE_FACTS, feed_temperature=None)
        reply = {'corrections': [], 'questions': [
            {'field': 'conversion_percent', 'question': '需要转化率还是平衡？', 'reason': '模型不确定'},
            {'field': 'unknown', 'question': '需要额外信息吗？', 'reason': '模型猜测'}]}
        state, _, _, _ = run(facts, reply)
        self.assertEqual([q['id'] for q in state['blocking']], ['q-feed-temperature'])
        self.assertEqual(len(state['review']['ignored_questions']), 2)

    def test_missing_condition_can_resume_without_model_recall(self):
        text = TOLUENE_TEXT.replace('进料温度380℃，', '')
        state, graph, cfg, calls = run(dict(TOLUENE_FACTS, feed_temperature=None), text=text)
        self.assertEqual([q['id'] for q in state['blocking']], ['q-feed-temperature'])
        state = graph.invoke(Command(resume={'q-feed-temperature': '不知道'}), cfg)
        self.assertEqual(state['status'], 'WAITING_INPUT')
        state = graph.invoke(Command(resume={'q-feed-temperature': '380 C'}), cfg)
        self.assertEqual(state['status'], 'READY', state.get('blocking'))
        self.assertEqual(len(calls), 2)

    def test_off_target_edits_cannot_destroy_known_facts(self):
        facts = dict(LIVE_GASIFICATION['facts'], feed_temperature=None)
        reply = {'corrections': [patch_field('species', ['煤炭', '水'], '进料为煤炭和水'),
            patch_field('feed_total', None, '流量80000Nm3/h'),
            patch_field('feed_unit', None, '流量80000Nm3/h'),
            patch_field('feed_temperature', 40, '进料温度40摄氏度')],
            'questions': [{'field': 'conversion_percent', 'question': '请提供转化率', 'reason': '未给出'}]}
        state, _, _, calls = run(facts, reply, LIVE_GASIFICATION['text'])
        self.assertEqual(state['facts']['species'], facts['species'])
        self.assertEqual(state['facts']['feed_total'], 80000)
        self.assertEqual(state['facts']['feed_unit'], 'Nm3/h')
        self.assertEqual({q['id'] for q in state['blocking']}, {'q-coal-definition', 'q-volumetric-flow'})
        self.assertEqual(len(state['review']['rejected_corrections']), 3)
        self.assertEqual(len(calls), 2)

    def test_in_scope_edit_cannot_clear_known_temperature_unit(self):
        reply = {'corrections': [patch_field('feed_temperature', 380, '进料温度380℃'),
            patch_field('feed_temperature_unit', None, '进料温度380℃')], 'questions': []}
        state, _, _, _ = run(dict(TOLUENE_FACTS, feed_temperature=None), reply)
        self.assertIsNone(state['facts']['feed_temperature'])
        self.assertEqual(state['facts']['feed_temperature_unit'], '℃')
        self.assertEqual([q['id'] for q in state['blocking']], ['q-feed-temperature'])

    def test_rejected_patch_leaves_program_question_instead_of_json_confirmation(self):
        state, _, _, _ = run(dict(TOLUENE_FACTS, feed_total=None),
            {'corrections': [patch_field('feed_total', 12345, '不存在的引用')], 'questions': []})
        self.assertEqual([q['id'] for q in state['blocking']], ['q-feed-flow'])
        self.assertNotIn('JSON', state['blocking'][0]['question'])
        self.assertIsNone(state['facts']['feed_total'])

    def test_ungrounded_number_remains_blocking_without_recovery(self):
        state, graph, cfg, calls = run(dict(TOLUENE_FACTS, feed_total=12345))
        self.assertEqual([q['id'] for q in state['blocking']], ['q-ungrounded:feed_total'])
        state = graph.invoke(Command(resume={'q-ungrounded:feed_total': '10000 kg/h'}), cfg)
        self.assertEqual(state['status'], 'READY', state.get('blocking'))
        self.assertEqual(len(calls), 2)

    def test_format_repair_is_bounded_and_preserves_path_diagnostics(self):
        bad = {'corrections': [], 'questions': [{'field': 'feed_total', 'question': 'q'}]}
        good = {'corrections': [patch_field('feed_total', 10000, '进料流量10000kg/h')], 'questions': []}
        state, _, _, calls = run(dict(TOLUENE_FACTS, feed_total=None), [bad, good])
        self.assertEqual(state['status'], 'READY')
        self.assertEqual(len(calls), 3)
        self.assertIn('$.questions[0].reason', state['review']['format_attempts'][0]['validation_errors'][0])

    def test_failed_format_repair_falls_back_to_actual_missing_input(self):
        state, _, _, calls = run(dict(TOLUENE_FACTS, feed_total=None), {'corrections': []})
        self.assertEqual(state['status'], 'WAITING_INPUT')
        self.assertEqual([q['id'] for q in state['blocking']], ['q-feed-flow'])
        self.assertIn('$.questions', state['review']['recovery_error']['error'])
        self.assertEqual(len(state['review']['recovery_error']['format_attempts']), 2)
        self.assertEqual(len(calls), 3)

    def test_budget_exhaustion_keeps_actual_question(self):
        state, _, _, calls = run(dict(TOLUENE_FACTS, feed_total=None), max_requests=1)
        self.assertEqual(state['status'], 'WAITING_INPUT')
        self.assertEqual([q['id'] for q in state['blocking']], ['q-feed-flow'])
        self.assertEqual(len(calls), 1)

    def test_missing_unit_is_recovered_from_explicit_original_without_model(self):
        facts = dict(LIVE_GASIFICATION['facts'], feed_pressure_unit=None)
        state, _, _, calls = run(facts, {'garbage': True}, LIVE_GASIFICATION['text'])
        self.assertEqual(state['facts']['feed_pressure_unit'], 'bar')
        self.assertIsNone(state['review']['original_facts']['feed_pressure_unit'])
        self.assertEqual(state['review']['source_recoveries'][0]['field'], 'feed_pressure_unit')
        self.assertEqual({q['id'] for q in state['blocking']}, {'q-coal-definition', 'q-volumetric-flow'})
        self.assertEqual(len(calls), 1)

    def test_truly_missing_unit_does_not_use_default_kpa(self):
        text = TOLUENE_TEXT.replace('压力2.5MPa', '压力2.5')
        state, graph, cfg, calls = run(dict(TOLUENE_FACTS, feed_pressure_unit=None), text=text)
        self.assertEqual([q['id'] for q in state['blocking']], ['q-ungrounded:feed_pressure'])
        state = graph.invoke(Command(resume={'q-ungrounded:feed_pressure': '2.5 MPa'}), cfg)
        self.assertEqual(state['status'], 'READY', state.get('blocking'))
        self.assertEqual(len(calls), 2)

    def test_ambiguous_units_are_not_resolved_by_matching_number_alone(self):
        text = TOLUENE_TEXT + '，另一处压力2.5bar'
        state, _, _, _ = run(dict(TOLUENE_FACTS, feed_pressure_unit=None), text=text)
        self.assertIsNone(state['facts']['feed_pressure_unit'])
        self.assertTrue(any(q['id'] in ('q-review:feed_pressure_unit', 'q-ungrounded:feed_pressure') for q in state['blocking']))

    def test_malformed_nested_candidate_does_not_replace_composition(self):
        facts = dict(TOLUENE_FACTS, feed_composition=[])
        state, _, _, _ = run(facts, {'corrections': [
            patch_field('feed_composition', ['bad'], '甲苯')], 'questions': []})
        self.assertEqual(state['facts']['feed_composition'], [])
        self.assertEqual(state['status'], 'WAITING_INPUT')

    def test_invalid_model_edits_never_execute_adapter(self):
        client, _ = model_client(dict(TOLUENE_FACTS, feed_total=None),
            {'corrections': [patch_field('feed_total', 12345, '进料流量10000kg/h')], 'questions': []})
        adapter = RecordingAdapter()
        state = build_graph(client, adapter=adapter, dry_run=False).invoke(initial_state(TOLUENE_TEXT),
            {'configurable': {'thread_id': 'no-execution'}})
        self.assertEqual(state['status'], 'WAITING_INPUT')
        self.assertFalse(adapter.calls)

    def test_single_pass_uses_same_targeted_recovery(self):
        client, calls = model_client(dict(TOLUENE_FACTS, feed_total=None),
            {'corrections': [patch_field('feed_total', 10000, '进料流量10000kg/h')], 'questions': []})
        outcome = run_pipeline(TOLUENE_TEXT, client=client)
        self.assertEqual(outcome.status, 'READY', outcome.problems)
        self.assertEqual(outcome.input_review['mode'], 'targeted_recovery')
        self.assertEqual(len(calls), 2)

    def test_duplicate_temperature_recovery_still_preserves_two_pressure_cases(self):
        quote = '工况一：进料和出口压力均为5 bar，出口750℃。工况二：进料和出口压力均为20 bar，出口750℃。'
        state, _, _, calls = run(dict(PRESSURE_FACTS, outlet_temperatures=[750]),
            {'corrections': [patch_field('outlet_temperatures', [750, 750], quote)], 'questions': []}, PRESSURE_TEXT)
        self.assertEqual(state['status'], 'READY', state.get('blocking'))
        self.assertEqual(len(state['cases']), 2)
        self.assertEqual(len(calls), 2)

    def test_streamed_ui_records_skipped_recovery_and_refresh_never_calls_model(self):
        client, calls = model_client(PRESSURE_FACTS, EMPTY_REVIEW)
        with tempfile.TemporaryDirectory() as folder:
            app = SessionApp(root=Path(folder), settings=Settings(key=client.config.key))
            with patch.object(app, 'client', return_value=client):
                payload = ChatService(app).start(PRESSURE_TEXT)
                self.assertEqual([e['status'] for e in payload['process'] if e['stage'] == 'review'], ['RUNNING', 'SKIPPED'])
                saved_run = next(iter(app.runs.values()))
                saved = json.loads((saved_run.folder / 'state.json').read_text(encoding='utf-8'))
                self.assertEqual(saved['input_review']['mode'], 'targeted_recovery')
                app.run_payload(saved_run, app.current_state(saved_run))
                self.assertEqual(len(calls), 1)

    def test_nested_format_errors_still_have_specific_paths(self):
        errors = value_errors({'corrections': [], 'questions': [{'field': 'feed_total', 'question': 'q'}]}, REVIEW_SCHEMA)
        self.assertIn('$.questions[0].reason: required field missing', errors)

    def test_format_records_do_not_expose_credential(self):
        bad = {'corrections': [], 'questions': [], 'extra': 'offline-review-secret-for-tests'}
        state, _, _, _ = run(dict(TOLUENE_FACTS, feed_total=None), bad)
        self.assertNotIn('offline-review-secret-for-tests', json.dumps(state['review']))
        self.assertIn('[REDACTED]', json.dumps(state['review']))


if __name__ == '__main__':
    unittest.main()
