"""Backend checks retained after removing the legacy HTTP frontend."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reactor_agent.ui_backend import SessionApp, Settings, is_downloadable, parse_env_file
from reactor_agent.fixtures.ui_cases import FAKE_KEY


class SettingsTests(unittest.TestCase):
    def test_env_parsing_comments_quotes_and_export(self):
        parsed = parse_env_file('# ignored\n\nTR_KEY="fake"\n'
                                "TR_MODEL='model'\nexport TR_BASE=https://example.test/v1\ninvalid\n")
        self.assertEqual(parsed, {'TR_KEY': 'fake', 'TR_MODEL': 'model',
                                  'TR_BASE': 'https://example.test/v1'})

    def test_page_values_override_environment(self):
        settings = Settings(env={'TR_BASE': 'env-base', 'TR_MODEL': 'env-model', 'TR_KEY': 'env-key'})
        self.assertEqual(settings.resolve().key, 'env-key')
        settings.base, settings.model, settings.key = 'page-base', 'page-model', 'page-key'
        self.assertEqual((settings.resolve().base, settings.resolve().model, settings.resolve().key),
                         ('page-base', 'page-model', 'page-key'))

    def test_public_settings_do_not_contain_key(self):
        public = Settings(key=FAKE_KEY).public()
        self.assertEqual(sorted(public), ['base', 'key_set', 'model'])
        self.assertTrue(public['key_set'])
        self.assertNotIn(FAKE_KEY, str(public))

    def test_environment_overrides_readonly_dotenv(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch('reactor_agent.ui_backend.read_dotenv', return_value={'TR_MODEL': 'file-model'}), \
                 patch.dict('os.environ', {'TR_MODEL': 'env-model'}):
                app = SessionApp(root=Path(tmp))
            self.assertEqual(app.settings.resolve().model, 'env-model')


class DownloadTests(unittest.TestCase):
    def test_artifacts_are_whitelisted(self):
        for name in ('spec-case-1.json', 'state.json', 'explanation.txt', 'request.json',
                     'run.json', 'paused.json', 'process.json'):
            with self.subTest(name=name):
                self.assertTrue(is_downloadable(name))

    def test_credentials_checkpoints_and_paths_are_refused(self):
        for name in ('../state.json', '..\\state.json', 'a/b.json', '.env',
                     'checkpoints.sqlite', 'C:\\Windows\\win.ini', '', 'spec-X.json.bak'):
            with self.subTest(name=name):
                self.assertFalse(is_downloadable(name))


class RunMetadataTests(unittest.TestCase):
    def test_reload_restores_run_metadata_without_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = SessionApp(root=root, settings=Settings(key=FAKE_KEY))
            run = app.new_run('custom', False)
            restored = SessionApp(root=root, settings=Settings(key='other-key'))
            self.assertEqual(restored.get_run(run.run_id).thread_id, run.thread_id)
            self.assertNotIn(FAKE_KEY, (root / 'web-runs.json').read_text(encoding='utf-8'))

    def test_payload_reads_do_not_invoke_the_graph(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = SessionApp(root=Path(tmp), settings=Settings(key=FAKE_KEY))
            run = app.new_run('custom', False)
            with patch.object(app, '_build', side_effect=AssertionError('must not execute')):
                payload = app.run_payload(run)
                self.assertEqual(payload['run_id'], run.run_id)
                self.assertEqual(payload['files'], [])


if __name__ == '__main__':
    unittest.main()
