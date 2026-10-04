"""Real graph + Streamlit reruns, with an offline model transport."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from reactor_agent.chat_service import ChatService, EXECUTION_LOCK, chat_answers
from reactor_agent.llm import ChatClient, LlmConfig
from reactor_agent.fixtures.ui_cases import (TOLUENE_FACTS, TOLUENE_TEXT,
                                    GASIFICATION_FACTS, GASIFICATION_TEXT)
from reactor_agent.ui_backend import SessionApp, Settings


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls = []
        self.facts = TOLUENE_FACTS
        self.app = SessionApp(root=Path(self.tmp.name), settings=Settings(
            base='https://example.test/v1', model='offline', key='offline-secret-for-tests-only'))
        def transport(*args):
            self.calls.append(args)
            return 200, json.dumps({'choices': [{'finish_reason': 'stop',
                'message': {'content': json.dumps(self.facts)}}]})
        client = ChatClient(LlmConfig(base='https://example.test/v1',
            key='offline-secret-for-tests-only', min_interval=0), transport=transport, sleeper=lambda _: None)
        self.patcher = patch.object(SessionApp, 'client', return_value=client)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.service = ChatService(self.app)

    def ui(self):
        at = AppTest.from_file(str(Path(__file__).with_name('streamlit_app.py')),
                               default_timeout=30)
        at.session_state['service'] = self.service
        return at.run()

    def test_send_rerun_download_and_new_conversation(self):
        at = self.ui()
        self.assertFalse(at.exception)
        at.chat_input[0].set_value(TOLUENE_TEXT).run()
        self.assertFalse(at.exception)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(at.chat_message), 2)
        history = at.session_state['conversations'][0]
        self.assertEqual(history[-1]['payload']['status'], 'READY')
        self.assertIn('explanation.txt', history[-1]['payload']['files'])
        self.assertIn('process.json', history[-1]['payload']['files'])
        self.assertTrue(any(e.label == '完整运行过程' for e in at.expander))
        at.run()
        self.assertEqual(len(self.calls), 1)
        at.button[0].click().run()
        self.assertEqual(at.session_state['conversation'], 1)
        self.assertEqual(len(self.calls), 1)
        for path in Path(self.tmp.name).rglob('*'):
            if path.is_file():
                self.assertNotIn(b'offline-secret-for-tests-only', path.read_bytes())

    def test_question_defaults_resume_same_run(self):
        self.facts = GASIFICATION_FACTS
        at = self.ui()
        at.chat_input[0].set_value(GASIFICATION_TEXT).run()
        self.assertFalse(at.exception)
        before = at.session_state['conversations'][0][-1]['payload']
        self.assertEqual(before['status'], 'WAITING_INPUT')
        self.assertTrue(at.text_area)
        at.chat_input[0].set_value('默认').run()
        self.assertFalse(at.exception)
        after = at.session_state['conversations'][0][-1]['payload']
        self.assertEqual(after['status'], 'READY')
        self.assertEqual(after['run_id'], before['run_id'])
        self.assertEqual(len(self.calls), 1)

    def test_question_form_resume(self):
        self.facts = GASIFICATION_FACTS
        at = self.ui()
        at.chat_input[0].set_value(GASIFICATION_TEXT).run()
        next(b for b in at.button if b.label == '提交回答并继续').click().run()
        self.assertFalse(at.exception)
        self.assertEqual(at.session_state['conversations'][0][-1]['payload']['status'], 'READY')
        self.assertEqual(len(self.calls), 1)

    def test_execute_requires_readiness(self):
        at = self.ui()
        at.radio[0].set_value('执行 HYSYS 模拟').run()
        at.chat_input[0].set_value(TOLUENE_TEXT).run()
        self.assertFalse(at.exception)
        self.assertTrue(at.error)
        self.assertEqual(len(self.calls), 0)

    def test_examples_only_prefill(self):
        at = self.ui()
        next(b for b in at.button if b.label == '甲苯歧化').click().run()
        self.assertEqual(len(self.calls), 0)
        self.assertIn('甲苯', at.chat_input[0].value)

    def test_busy_execution_does_not_call_graph(self):
        EXECUTION_LOCK.acquire()
        try:
            with self.assertRaisesRegex(RuntimeError, '其他任务'):
                self.service.start(TOLUENE_TEXT, execute=True)
            self.assertEqual(len(self.calls), 0)
        finally:
            EXECUTION_LOCK.release()

    def test_failure_releases_execution_lock(self):
        with patch.object(self.app, 'start', side_effect=RuntimeError('offline-error')):
            with self.assertRaises(RuntimeError):
                self.service.start(TOLUENE_TEXT, execute=True)
        self.assertFalse(EXECUTION_LOCK.locked())
        self.assertEqual(next(iter(self.app.runs.values())).status, 'FAILED')

    def test_multiple_answers_need_form_when_no_defaults(self):
        questions = [{'id': 'a', 'default': None}, {'id': 'b', 'default': 'yes'}]
        with self.assertRaises(ValueError):
            chat_answers('默认', questions)
        with self.assertRaises(ValueError):
            chat_answers('不明确的两个答案', questions)
        self.assertEqual(chat_answers('42', questions[:1]), {'a': '42'})

    def test_settings_are_session_local(self):
        other = SessionApp(root=Path(self.tmp.name) / 'other', settings=Settings(key='other-secret'))
        self.app.settings.key = 'changed'
        self.assertEqual(other.settings.resolve().key, 'other-secret')

    def test_progress_arrives_before_final_report_and_is_not_fake_execution(self):
        snapshots = []
        payload = self.service.start(TOLUENE_TEXT, on_progress=snapshots.append)
        events = payload['process']
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(any(e['stage'] == 'intake' and e['status'] == 'RUNNING' for e in events))
        intake = next(e for e in events if e['stage'] == 'intake' and e['status'] == 'DONE')
        self.assertEqual(intake['details']['facts']['conversion_percent'], 50)
        self.assertTrue(any(e['stage'] == 'execute' and e['status'] == 'SKIPPED' for e in events))
        self.assertFalse(any(e['stage'] == 'case' for e in events))
        self.assertTrue(any(s[-1]['stage'] == 'plan' and
                            all(e['stage'] != 'explain' for e in s) for s in snapshots))

    def test_trace_keeps_clarification_and_resume_without_another_model_call(self):
        self.facts = GASIFICATION_FACTS
        pending = self.service.start(GASIFICATION_TEXT)
        self.assertTrue(any(e['status'] == 'WAITING_INPUT' for e in pending['process']))
        payload = self.service.answer(pending['run_id'], chat_answers('默认', pending['questions']))
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(any(e['stage'] == 'answer' for e in payload['process']))
        self.assertEqual(sum(e['stage'] == 'intake' and e['status'] == 'DONE'
                             for e in payload['process']), 1)
        self.assertEqual(payload['process'][-1]['status'], 'READY')

    def test_real_mode_reports_each_actual_adapter_call_once(self):
        from reactor_agent.test_graph import RecordingAdapter
        adapter = RecordingAdapter()
        with patch('reactor_agent.adapters.hysys_cli.HysysCliAdapter', return_value=adapter):
            payload = self.service.start(TOLUENE_TEXT, execute=True)
        self.assertEqual(payload['status'], 'PASS')
        self.assertEqual(adapter.calls, ['single'])
        cases = [e for e in payload['process'] if e['stage'] == 'case']
        self.assertEqual([e['status'] for e in cases], ['RUNNING', 'PASS'])
        self.assertEqual(cases[-1]['details']['case_id'], 'single')
        self.assertTrue(any(e['stage'] == 'execute' and e['status'] == 'PASS'
                            for e in payload['process']))

    def test_three_demo_scenarios_trace_end_to_end_with_offline_workers(self):
        from reactor_agent.test_graph import RecordingAdapter
        from reactor_agent.test_live_intake import fixture
        from reactor_agent.ui_backend import scenarios
        for scenario, facts in (('toluene', TOLUENE_FACTS), ('smr', fixture('smr')),
                                ('gasification', GASIFICATION_FACTS)):
            with self.subTest(scenario=scenario):
                self.facts = facts
                adapter = RecordingAdapter()
                with patch('reactor_agent.adapters.hysys_cli.HysysCliAdapter', return_value=adapter):
                    payload = self.service.start(scenarios()[scenario]['text'],
                                                 scenario=scenario, execute=True)
                    if payload['status'] == 'WAITING_INPUT':
                        self.assertEqual(adapter.calls, [])
                        payload = self.service.answer(payload['run_id'],
                            chat_answers('默认', payload['questions']))
                self.assertEqual(payload['status'], 'PASS')
                expected = 2 if scenario == 'smr' else 1
                self.assertEqual(len(adapter.calls), expected)
                self.assertEqual(sum(e['stage'] == 'case' and e['status'] == 'PASS'
                                     for e in payload['process']), expected)
                self.assertEqual(payload['process'][-1]['status'], 'PASS')

    def test_streaming_preserves_the_existing_graph_specs_and_report(self):
        run = self.app.new_run('custom', False)
        before = self.app.start(run, TOLUENE_TEXT, '', '', 'mixed', 'mass_fraction')
        payload = self.service.start(TOLUENE_TEXT)
        after = self.app.current_state(self.app.get_run(payload['run_id']))
        self.assertEqual(before['cases'], after['cases'])
        self.assertEqual(before['decision'], after['decision'])
        self.assertEqual(before['explanation'], payload['report'])

    def test_timeout_stops_later_cases_and_is_recorded_as_failure(self):
        from reactor_agent.test_graph import RecordingAdapter
        from reactor_agent.test_live_intake import fixture
        from reactor_agent.ui_backend import scenarios
        self.facts = fixture('smr')
        adapter = RecordingAdapter(['TIMEOUT'])
        with patch('reactor_agent.adapters.hysys_cli.HysysCliAdapter', return_value=adapter):
            payload = self.service.start(scenarios()['smr']['text'], scenario='smr', execute=True)
        self.assertEqual(payload['status'], 'FAILED')
        self.assertEqual(len(adapter.calls), 1)
        returned = [e for e in payload['process'] if e['stage'] == 'case' and e['status'] != 'RUNNING']
        self.assertEqual([e['status'] for e in returned], ['TIMEOUT'])
        self.assertEqual(payload['process'][-1]['status'], 'FAILED')

    def test_callback_failure_cannot_repeat_or_abort_execution(self):
        def broken_callback(_events):
            raise RuntimeError('page disconnected')
        payload = self.service.start(TOLUENE_TEXT, on_progress=broken_callback)
        self.assertEqual(payload['status'], 'READY')
        self.assertEqual(len(self.calls), 1)

    def test_process_file_redacts_credential_even_in_provider_errors(self):
        secret = self.app.settings.resolve().key
        with patch.object(self.app, 'start', side_effect=RuntimeError('Error: ' + secret)):
            with self.assertRaises(RuntimeError):
                self.service.start(TOLUENE_TEXT)
        run = next(iter(self.app.runs.values()))
        raw = (run.folder / 'process.json').read_text(encoding='utf-8')
        self.assertNotIn(secret, raw)
        events = json.loads(raw)
        self.assertEqual(events[-1]['status'], 'FAILED')
        self.assertIn('[已隐藏]', events[-1]['details']['error'])

    def test_ui_failure_keeps_trace_and_does_not_run_on_refresh(self):
        at = self.ui()
        with patch.object(self.app, 'start', side_effect=RuntimeError('offline failure')):
            at.chat_input[0].set_value(TOLUENE_TEXT).run()
        self.assertFalse(at.exception)
        self.assertTrue(at.error)
        self.assertEqual(at.session_state['conversations'][0][-1]['payload']['status'], 'FAILED')
        self.assertTrue(any(e.label == '完整运行过程' for e in at.expander))
        at.run()
        self.assertEqual(len(self.calls), 0)


if __name__ == '__main__':
    unittest.main()
