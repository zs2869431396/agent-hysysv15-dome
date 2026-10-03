"""Accepted mixed line endings must survive a Windows Git checkout."""
import hashlib
from pathlib import Path
import tempfile
import unittest

from scripts.restore_accepted_line_endings import accepted_bytes, restore


def digest(data):
    return hashlib.sha256(data).hexdigest()


class AcceptedLineEndings(unittest.TestCase):
    def test_already_accepted_crlf_is_preserved(self):
        data = b'one\r\ntwo\r\n'
        self.assertEqual(accepted_bytes(data, digest(data)), data)

    def test_windows_crlf_restores_an_accepted_lf_file(self):
        expected = b'one\ntwo\n'
        self.assertEqual(accepted_bytes(b'one\r\ntwo\r\n', digest(expected)), expected)

    def test_lf_restores_the_accepted_crlf_main_file(self):
        expected = b'one\r\ntwo\r\n'
        self.assertEqual(accepted_bytes(b'one\ntwo\n', digest(expected)), expected)

    def test_code_changes_cannot_be_repaired_as_line_endings(self):
        self.assertIsNone(accepted_bytes(b'changed\r\n', digest(b'original\n')))

    def test_one_real_difference_prevents_all_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'lf.py').write_bytes(b'one\r\n')
            (root / 'main.py').write_bytes(b'changed\r\n')
            self.assertFalse(restore(root, {'lf.py': digest(b'one\n'),
                                            'main.py': digest(b'original\r\n')}, True))
            self.assertEqual((root / 'lf.py').read_bytes(), b'one\r\n')

    def test_apply_handles_a_mixed_acceptance(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'lf.py').write_bytes(b'one\r\n')
            (root / 'main.py').write_bytes(b'two\r\n')
            self.assertTrue(restore(root, {'lf.py': digest(b'one\n'),
                                           'main.py': digest(b'two\r\n')}, True))
            self.assertEqual((root / 'lf.py').read_bytes(), b'one\n')
            self.assertEqual((root / 'main.py').read_bytes(), b'two\r\n')


if __name__ == '__main__':
    unittest.main()
