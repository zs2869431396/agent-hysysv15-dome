"""Real graph + Streamlit reruns, with an offline model transport."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from reactor_agent.chat_service import ChatService, EXECUTION_LOCK, chat_answers
from reactor_agent.llm import ChatClient, LlmConfig
from reactor_agent.test_web import (TOLUENE_FACTS, TOLUENE_TEXT,
                                    GASIFICATION_FACTS, GASIFICATION_TEXT)
from reactor_agent.web import WebApp, Settings


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls = []
        self.facts = TOLUENE_FACTS
        self.app = WebApp(root=Path(self.tmp.name), settings=Settings(
            base='https://example.test/v1', model='offline', key='offline-secret-for-tests-only'))
        def transport(*args):
            self.calls.append(args)
            return 200, json.dumps({'choices': [{'finish_reason': 'stop',
                'message': {'content': json.dumps(self.facts)}}]})
        client = ChatClient(LlmConfig(base='https://example.test/v1',
            key='offline-secret-for-tests-only', min_interval=0), transport=transport, sleeper=lambda _: None)
        self.patcher = patch.object(WebApp, 'client', return_value=client)
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
        other = WebApp(root=Path(self.tmp.name) / 'other', settings=Settings(key='other-secret'))
        self.app.settings.key = 'changed'
        self.assertEqual(other.settings.resolve().key, 'other-secret')


if __name__ == '__main__':
    unittest.main()
