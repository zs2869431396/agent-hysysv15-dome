"""Web interface tests: the endpoints, the answering loop, and the key's containment.

Everything here runs against a real `ThreadingHTTPServer` on a temporary run root,
with the graph built over the same fake client the other suites use. No network, no
HYSYS, no real credential.

The containment tests are the point of most of this file. The key is the one secret
this program handles, and the claim being tested is strong: after it is set through the
page, that exact string appears nowhere - not in a response body, not in any file in
the run folder, not in a checkpoint.
"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from reactor_agent.llm import ChatClient, LlmConfig
from reactor_agent.web import (
    Settings,
    WebApp,
    is_downloadable,
    make_server,
    parse_env_file,
)

FAKE_KEY = 'sk-' + 'w' * 30

TOLUENE_FACTS = {
    'species': ['甲苯', '苯', '邻二甲苯'],
    'feed_composition': [{'name': '甲苯', 'fraction': 1.0}],
    'composition_basis': 'pure',
    'reactions': [{'name': '歧化',
                   'species': [{'name': '甲苯', 'coefficient': -2},
                               {'name': '苯', 'coefficient': 1},
                               {'name': '邻二甲苯', 'coefficient': 1}],
                   'reversible': False}],
    'conversion_percent': 50, 'conversion_basis': '甲苯',
    'feed_total': 10000, 'feed_unit': 'kg/h',
    'feed_temperature': 380, 'feed_temperature_unit': '℃',
    'feed_pressure': 2.5, 'feed_pressure_unit': 'MPa',
    'case_pressures': [], 'case_pressure_unit': '',
    'outlet_temperatures': [], 'outlet_temperature_unit': '',
    'missing_information': [],
}

GASIFICATION_FACTS = {
    'species': ['碳', '水', '一氧化碳', '氢气'],
    'feed_composition': [{'name': '煤炭', 'fraction': 62},
                         {'name': '水', 'fraction': 38}],
    'composition_basis': 'mass_percent',
    'reactions': [{'name': '气化',
                   'species': [{'name': '碳', 'coefficient': -1},
                               {'name': '水', 'coefficient': -1},
                               {'name': '一氧化碳', 'coefficient': 1},
                               {'name': '氢气', 'coefficient': 1}],
                   'reversible': False}],
    'conversion_percent': None, 'conversion_basis': '',
    'feed_total': 80000, 'feed_unit': 'Nm3/h',
    'feed_temperature': 40, 'feed_temperature_unit': 'C',
    'feed_pressure': 40, 'feed_pressure_unit': 'bar',
    'case_pressures': [], 'case_pressure_unit': '',
    'outlet_temperatures': [1400], 'outlet_temperature_unit': 'C',
    'missing_information': [],
}

GASIFICATION_TEXT = ('水煤浆气化：C+H2O → CO+H2，进料煤炭和水，流量80000Nm3/h，'
                     '压力40bar，进料温度40摄氏度，出口1400度，浓度62wt%')

TOLUENE_TEXT = ('甲苯歧化 2C₇H₈ → C₆H₆ + C₈H₁₀，甲苯进料流量10000kg/h，'
                '进料温度380℃，压力2.5MPa，转化率50%')


class ServerCase(unittest.TestCase):
    """A running server on a temporary root, with an injectable fake model."""

    facts: dict = TOLUENE_FACTS
    text = TOLUENE_TEXT

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.calls = []
        body = json.dumps({
            'choices': [{'finish_reason': 'stop',
                         'message': {'content': json.dumps(self.facts)}}],
            'usage': {}})

        def counted(url, payload, headers, timeout):
            # One canned extraction, counted so a test can prove that re-reading a
            # run does not call the model again.
            self.calls.append(url)
            return 200, body

        self.client = ChatClient(
            LlmConfig(base='https://example.test/v1', key=FAKE_KEY, min_interval=0),
            transport=counted, sleeper=lambda _s: None)
        self.settings = Settings(base='https://example.test/v1', model='fake-model',
                                 key=FAKE_KEY)
        self.app = WebApp(root=self.root / 'agent-runs', settings=self.settings)
        # Two seams: the checkpointer (so each run uses this temporary root) and the
        # model client (so no test ever opens a socket).
        opener = mock.patch.object(WebApp, '_open', self._open)
        opener.start()
        self.addCleanup(opener.stop)
        client = mock.patch.object(WebApp, 'client', lambda _self: self.client)
        client.start()
        self.addCleanup(client.stop)
        self.server = make_server(port=0, app=self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)
        host, port = self.server.server_address[:2]
        self.base = 'http://%s:%d' % (host, port)

    def _open(self, run):
        """The real checkpointer, on this run's own folder.

        Replaces `WebApp._open`, which is the one place the real server reaches for a
        checkpointer; everything else is the production code path.
        """
        from reactor_agent.graph import open_checkpointer

        return open_checkpointer(run.folder / 'checkpoints.sqlite')

    def _stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self._tmp.cleanup()

    # ------------------------------------------------------------- helpers
    def get(self, path: str) -> tuple[int, dict]:
        request = urllib.request.Request(self.base + path)
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode('utf-8'))

    def post(self, path: str, payload: dict) -> tuple[int, dict]:
        body = json.dumps(payload).encode('utf-8')
        request = urllib.request.Request(
            self.base + path, data=body,
            headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode('utf-8'))

    def raw_get(self, path: str) -> tuple[int, bytes]:
        request = urllib.request.Request(self.base + path)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()


class SettingsEndpoint(ServerCase):

    def test_the_key_is_reported_as_set_and_never_returned(self):
        status, payload = self.get('/api/settings')
        self.assertEqual(status, 200)
        self.assertTrue(payload['key_set'])
        self.assertNotIn('key', payload)
        self.assertNotIn(FAKE_KEY, json.dumps(payload))

    def test_the_page_can_replace_the_key_without_echoing_it(self):
        status, payload = self.post('/api/settings', {'base': 'https://x.test/v1',
                                                      'model': 'm', 'key': 'sk-' + 'z' * 30})
        self.assertEqual(status, 200)
        self.assertTrue(payload['key_set'])
        self.assertNotIn('sk-' + 'z' * 30, json.dumps(payload))
        self.assertEqual(self.app.settings.resolve().base, 'https://x.test/v1')

    def test_the_page_is_served_with_the_three_inputs(self):
        status, body = self.raw_get('/')
        self.assertEqual(status, 200)
        text = body.decode('utf-8')
        self.assertIn('type="password"', text)
        self.assertIn('id="base"', text)
        self.assertIn('id="model"', text)


class TheKeyIsContained(ServerCase):
    facts = GASIFICATION_FACTS
    text = GASIFICATION_TEXT

    def test_no_response_body_or_run_file_contains_the_key(self):
        status, first = self.post('/api/run', {'scenario': 'gasification',
                                               'text': self.text, 'execute': False})
        self.assertEqual(status, 200)
        self.assertNotIn(FAKE_KEY, json.dumps(first))
        defaults = {question['id']: question['default']
                    for question in first['questions']}
        status, second = self.post('/api/answer', {'run_id': first['run_id'],
                                                   'answers': defaults})
        self.assertEqual(status, 200)
        self.assertNotIn(FAKE_KEY, json.dumps(second))

        status, current = self.get('/api/run/%s' % first['run_id'])
        self.assertNotIn(FAKE_KEY, json.dumps(current))

        run = self.app.get_run(first['run_id'])
        files = [path for path in run.folder.rglob('*') if path.is_file()]
        self.assertTrue(files)
        for path in files:
            with self.subTest(file=path.name):
                self.assertNotIn(FAKE_KEY, path.read_bytes().decode('utf-8', 'replace'))

    def test_a_download_never_serves_the_key(self):
        _, first = self.post('/api/run', {'scenario': 'gasification',
                                          'text': self.text, 'execute': False})
        for name in first.get('files') or []:
            with self.subTest(file=name):
                status, body = self.raw_get(
                    '/api/run/%s/files/%s' % (first['run_id'], name))
                self.assertEqual(status, 200)
                self.assertNotIn(FAKE_KEY, body.decode('utf-8', 'replace'))


class GasificationRoundTrip(ServerCase):
    facts = GASIFICATION_FACTS
    text = GASIFICATION_TEXT

    def test_two_questions_with_defaults_then_ready(self):
        status, first = self.post('/api/run', {'scenario': 'gasification',
                                               'text': self.text, 'execute': False})
        self.assertEqual(status, 200)
        self.assertEqual(first['status'], 'WAITING_INPUT')
        self.assertEqual(len(first['questions']), 2)
        ids = [question['id'] for question in first['questions']]
        self.assertEqual(sorted(ids), ['q-coal-definition', 'q-volumetric-flow'])
        for question in first['questions']:
            self.assertTrue(question.get('default'), question)

        answers = {question['id']: question['default']
                   for question in first['questions']}
        status, second = self.post('/api/answer', {'run_id': first['run_id'],
                                                   'answers': answers})
        self.assertEqual(status, 200)
        self.assertEqual(second['status'], 'READY', second.get('problems'))
        self.assertEqual(second['questions'], [])
        self.assertIn('dry run', second['report'])
        self.assertTrue(any(name.startswith('spec-') for name in second['files']))

    def test_the_second_poll_does_not_call_the_model_again(self):
        _, first = self.post('/api/run', {'scenario': 'gasification',
                                          'text': self.text, 'execute': False})
        before = len(self.calls)
        self.get('/api/run/%s' % first['run_id'])
        self.get('/api/run/%s' % first['run_id'])
        self.assertEqual(len(self.calls), before)

    def test_a_run_id_is_required(self):
        status, payload = self.post('/api/answer', {'run_id': 'nope',
                                                    'answers': {'q': 'x'}})
        self.assertEqual(status, 404)
        self.assertIn('error', payload)

    def test_an_empty_answer_is_refused(self):
        _, first = self.post('/api/run', {'scenario': 'gasification',
                                          'text': self.text, 'execute': False})
        status, payload = self.post('/api/answer', {'run_id': first['run_id'],
                                                    'answers': {}})
        self.assertEqual(status, 400)
        self.assertIn('error', payload)


class DownloadsAreWhitelisted(ServerCase):

    def test_the_path_traversal_and_absolute_names_are_refused(self):
        _, first = self.post('/api/run', {'scenario': 'toluene', 'text': self.text,
                                          'execute': False})
        run_id = first['run_id']
        for name in ('../state.json', '..%5Cstate.json', '%2e%2e%2fstate.json',
                     'C:Windows\\win.ini', '.env', 'checkpoints.sqlite',
                     'somethingelse.json', 'spec-X.json.bak'):
            with self.subTest(name=name):
                status, _ = self.raw_get('/api/run/%s/files/%s' % (run_id, name))
                self.assertEqual(status, 404)

    def test_the_whitelist_function_itself(self):
        for name in ('spec-case-1.json', 'state.json', 'explanation.txt',
                     'request.json', 'run.json', 'paused.json'):
            with self.subTest(name=name):
                self.assertTrue(is_downloadable(name))
        for name in ('../a.json', 'a/b.json', '.env', 'checkpoints.sqlite', '',
                     'notes.md'):
            with self.subTest(name=name):
                self.assertFalse(is_downloadable(name))


class PreviewHasNoSideEffects(ServerCase):

    def test_a_preview_creates_no_run_folder(self):
        status, payload = self.post('/api/preview', {'scenario': 'toluene',
                                                     'text': self.text})
        self.assertEqual(status, 200)
        self.assertIn('status', payload)
        self.assertIn('note', payload)
        self.assertIn('creates no run folder', payload['note'])
        self.assertEqual(self.app.runs, {})
        runs_dir = self.root / 'agent-runs'
        leftovers = [path for path in runs_dir.rglob('*') if path.is_file()]
        self.assertEqual(leftovers, [])


class TheServerIsLoopbackOnly(unittest.TestCase):

    def test_the_default_binding_is_loopback(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = WebApp(root=Path(tmp), settings=Settings(key=FAKE_KEY))
            server = make_server(port=0, app=app)
            try:
                self.assertEqual(server.server_address[0], '127.0.0.1')
            finally:
                server.server_close()


class EnvFileParsing(unittest.TestCase):

    def test_blank_lines_comments_and_quotes(self):
        parsed = parse_env_file(
            '# a comment\n'
            '\n'
            'TR_KEY="sk-abc"\n'
            "TR_MODEL='qwen3.7-flash'\n"
            'TR_BASE=https://tokenrhythm.studio/v1\n'
            'export TR_GAP=1\n'
            'not a pair\n')
        self.assertEqual(parsed['TR_KEY'], 'sk-abc')
        self.assertEqual(parsed['TR_MODEL'], 'qwen3.7-flash')
        self.assertEqual(parsed['TR_BASE'], 'https://tokenrhythm.studio/v1')
        self.assertEqual(parsed['TR_GAP'], '1')
        self.assertNotIn('not a pair', parsed)

    def test_precedence_is_page_then_environment_then_env_file(self):
        settings = Settings(base='', model='', key='',
                            env={'TR_BASE': 'env-base', 'TR_MODEL': 'env-model',
                                 'TR_KEY': 'env-key'})
        resolved = settings.resolve()
        self.assertEqual((resolved.base, resolved.model, resolved.key),
                         ('env-base', 'env-model', 'env-key'))
        page = Settings(base='page-base', model='page-model', key='page-key',
                        env=settings.env).resolve()
        self.assertEqual((page.base, page.model, page.key),
                         ('page-base', 'page-model', 'page-key'))
        nothing = Settings(env={}).resolve()
        self.assertTrue(nothing.base)
        self.assertTrue(nothing.model)
        self.assertEqual(nothing.key, '')

    def test_the_key_is_never_part_of_the_public_view(self):
        resolved = Settings(key='sk-' + 'q' * 30, env={}).public()
        self.assertEqual(sorted(resolved), ['base', 'key_set', 'model'])
        self.assertTrue(resolved['key_set'])


class RealExecutionNeedsConfirmation(ServerCase):

    def test_a_missing_key_is_refused_before_anything_runs(self):
        self.app.settings = Settings(env={})
        status, payload = self.post('/api/run', {'scenario': 'toluene',
                                                 'text': self.text})
        self.assertEqual(status, 400)
        self.assertIn('key', payload['error'])

    def test_an_empty_request_is_refused(self):
        status, payload = self.post('/api/run', {'scenario': 'toluene', 'text': '  '})
        self.assertEqual(status, 400)
        self.assertIn('empty', payload['error'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
