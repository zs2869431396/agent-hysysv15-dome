"""Tests for the submission packager's safety logic.

What is worth testing here is not the zipping - that is the standard library's job -
but the two rules that protect the submission:

  * certain things must never be inside the archive (an RDP endpoint, a large HYSYS
    case, a byte-code cache);
  * the build must refuse to produce a package at all if it finds a credential.

Both are tested directly, because a packaging script that quietly ships a key is
worse than no packaging script.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_submission as pack  # noqa: E402


class Exclusions(unittest.TestCase):

    def test_a_remote_desktop_file_is_never_packaged(self):
        """It carries the workstation address."""
        self.assertTrue(pack.is_excluded(Path('cand-zhongshuai.rdp')))
        self.assertTrue(pack.is_excluded(Path('config/session.RDP')))

    def test_hysys_case_files_are_never_packaged(self):
        self.assertTrue(pack.is_excluded(Path('tool-layer-runs/x/case.hsc')))
        self.assertTrue(pack.is_excluded(Path('whatever/case.HSC')))

    def test_byte_code_and_caches_are_never_packaged(self):
        self.assertTrue(pack.is_excluded(Path('hysys_tools/__pycache__/a.pyc')))
        self.assertTrue(pack.is_excluded(Path('reactor_agent/.pytest_cache/x')))

    def test_archives_and_local_archives_are_never_packaged(self):
        self.assertTrue(pack.is_excluded(Path('_archive/packages/old.zip')))
        self.assertTrue(pack.is_excluded(Path('packages/tool-layer.zip')))

    def test_ordinary_source_is_not_excluded(self):
        for name in ('reactor_agent/graph.py', 'REPORT.md', 'docs/GWOA_MIGRATION.md',
                     'Run-Agent-On-Workstation.cmd', '.gitignore'):
            with self.subTest(name=name):
                self.assertFalse(pack.is_excluded(Path(name)))


class CredentialScan(unittest.TestCase):
    """Fixtures are assembled at runtime rather than written as literals.

    A literal `sk-...` in this file would be indistinguishable from a real leak to
    the project's own pre-flight scanner, and the honest fix is to change the fixture,
    not to loosen the scanner. Splitting the prefix means no such token appears in the
    source, while the scanner is still tested against a realistic string.
    """

    KEY_PREFIX = 'sk' + '-tr-'
    TOKEN = 'Authorization: Bearer ' + 'a' * 32

    def test_a_key_in_a_file_is_detected(self):
        contents = {'notes.md': ('the key is ' + self.KEY_PREFIX + 'x' * 30).encode()}
        hits = pack.scan_for_secrets(contents)
        self.assertTrue(hits)
        self.assertEqual(hits[0][0], 'notes.md')

    def test_a_bearer_token_is_detected(self):
        contents = {'log.txt': self.TOKEN.encode()}
        self.assertTrue(pack.scan_for_secrets(contents))

    def test_an_rdp_endpoint_in_a_text_file_is_detected(self):
        contents = {'config.txt': ('full ' + 'address:s:203.0.113.10:3389').encode()}
        self.assertTrue(pack.scan_for_secrets(contents))

    def test_a_scanner_is_not_reported_as_a_leak(self):
        """The scanner source contains the patterns verbatim; it must not trip."""
        contents = {
            'build_submission.py': b"SECRET_PATTERNS = (r'full address:s:',)",
            'prepare_git.py': b"# SECRET_PATTERNS includes r'full address:s:'",
        }
        self.assertEqual(pack.scan_for_secrets(contents), [])

    def test_clean_text_passes(self):
        contents = {'README.md': '这是一个正常的文档'.encode('utf-8'),
                    'code.py': b'def f(): return 1'}
        self.assertEqual(pack.scan_for_secrets(contents), [])

    def test_binary_content_is_skipped_rather_than_crashing(self):
        contents = {'blob.json': bytes(range(256))}
        self.assertEqual(pack.scan_for_secrets(contents), [])


class Collection(unittest.TestCase):

    def test_the_real_project_collects_the_expected_pieces(self):
        """The allowlist must actually cover both layers and the documentation."""
        contents, _ = pack.collect()
        keys = set(contents)

        for required in ('README.md', 'REPORT.md', 'PROJECT_PLAN.md',
                         'TECH_STACK.md', 'requirements.txt', '.gitignore',
                         'Run-Agent-On-Workstation.cmd',
                         'reactor_agent/graph.py', 'reactor_agent/llm.py',
                         'reactor_agent/adapters/hysys_cli.py',
                         'hysys_tools/main.py', 'hysys_tools/precheck.py',
                         'scripts/run-all-tests.cmd'):
            with self.subTest(required=required):
                self.assertIn(required, keys)

    def test_acceptance_evidence_is_included(self):
        contents, _ = pack.collect()
        evidence = [key for key in contents if 'acceptance-' in key]
        self.assertTrue(evidence)
        self.assertTrue(any(key.endswith('summary.json') for key in evidence))

    def test_no_excluded_file_reaches_the_collection(self):
        contents, _ = pack.collect()
        offenders = [key for key in contents
                     if Path(key).suffix.lower() in {'.hsc', '.pyc', '.rdp', '.zip'}]
        self.assertEqual(offenders, [])

    def test_no_credentials_in_what_would_be_packaged(self):
        contents, _ = pack.collect()
        self.assertEqual(pack.scan_for_secrets(contents), [])

    def test_the_collection_is_a_sane_size(self):
        """A submission that balloons is usually a stray artifact."""
        contents, _ = pack.collect()
        total = sum(len(data) for data in contents.values())
        self.assertLess(total, 20 * 1024 * 1024)
        self.assertGreater(total, 100 * 1024)

    def test_local_run_output_is_not_packaged(self):
        """Dry-run artifacts are working files, not deliverables."""
        contents, _ = pack.collect()
        self.assertFalse([key for key in contents if key.startswith('agent-runs/')])


if __name__ == '__main__':
    unittest.main(verbosity=2)
