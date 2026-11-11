"""Regression checks for version gates and diagnostic failure reporting.

No application listeners, capture or GUI are started.
Run: python src/integration/check_python_contract.py
"""
import builtins
import contextlib
import importlib.abc
import io
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import main as entry
from src.integration import check_python_compat as diagnostic


class BlockQt(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'PySide6' or fullname.startswith('PySide6.'):
            raise AssertionError('web mode attempted Qt import: ' + fullname)


class PythonContract(unittest.TestCase):
    def test_old_entry_refuses_before_application_imports(self):
        original_import = builtins.__import__
        def guarded(name, *args, **kwargs):
            if name.startswith(('src.', 'PySide6', 'aiohttp')):
                raise AssertionError('dependency imported before version refusal')
            return original_import(name, *args, **kwargs)
        output = io.StringIO()
        with patch.object(sys, 'version_info', (3, 12)), patch.object(
            builtins, '__import__', guarded
        ), contextlib.redirect_stderr(output):
            self.assertEqual(entry.main(), 1)
        self.assertIn('Python 3.13', output.getvalue())

    def test_old_diagnostic_refuses_before_dependency_imports(self):
        output = io.StringIO()
        with patch.object(sys, 'version_info', (3, 12)), patch.object(
            diagnostic.importlib, 'import_module', side_effect=AssertionError('imported')
        ), contextlib.redirect_stdout(output):
            self.assertEqual(diagnostic.main(), 1)
        self.assertIn('FAIL Python:', output.getvalue())
        self.assertTrue(output.getvalue().rstrip().endswith('COMPATIBILITY FAILED'))

    def test_missing_dependency_is_failure(self):
        original_import = diagnostic.importlib.import_module
        def missing(name):
            if name == 'qrcode':
                raise ModuleNotFoundError("No module named 'qrcode'")
            return original_import(name)
        output = io.StringIO()
        with patch.object(diagnostic.importlib, 'import_module', side_effect=missing), contextlib.redirect_stdout(output):
            self.assertEqual(diagnostic.main(), 1)
        self.assertIn("FAIL qrcode: import qrcode failed: No module named 'qrcode'", output.getvalue())
        self.assertNotIn('COMPATIBILITY OK', output.getvalue())

    def test_metadata_errors_are_failures(self):
        original_version = diagnostic.metadata.version
        for error in (ValueError('invalid metadata'), OSError('metadata unreadable')):
            with self.subTest(error=type(error).__name__):
                def broken_version(distribution):
                    if distribution == 'qrcode':
                        raise error
                    return original_version(distribution)
                output = io.StringIO()
                with patch.object(diagnostic.metadata, 'version', side_effect=broken_version), contextlib.redirect_stdout(output):
                    self.assertEqual(diagnostic.main(), 1)
                self.assertIn('FAIL qrcode: version metadata raised ' + type(error).__name__ + ': ' + str(error), output.getvalue())
                self.assertIn('OK psutil ', output.getvalue())
                self.assertTrue(output.getvalue().rstrip().endswith('COMPATIBILITY FAILED'))
                self.assertNotIn('COMPATIBILITY OK', output.getvalue())

    def test_missing_metadata_remains_optional(self):
        output = io.StringIO()
        with patch.object(diagnostic.metadata, 'version', side_effect=diagnostic.metadata.PackageNotFoundError('qrcode')), contextlib.redirect_stdout(output):
            self.assertTrue(diagnostic.check_import('qrcode', 'qrcode', 'qrcode'))
        self.assertEqual(output.getvalue().strip(), 'OK qrcode (version metadata unavailable)')

    def test_a_web_entry_without_qt(self):
        # Keep this before the diagnostic success test, which legitimately imports Qt.
        self.assertFalse(any(name == 'PySide6' or name.startswith('PySide6.') for name in sys.modules))
        blocker = BlockQt()
        sys.meta_path.insert(0, blocker)
        try:
            with patch.object(sys, 'argv', ['main.py', '--web']), patch.object(entry, '_run_web', return_value=0) as runner:
                self.assertEqual(entry.main(), 0)
                runner.assert_called_once()
            self.assertNotIn('PySide6', sys.modules)
        finally:
            sys.meta_path.remove(blocker)

    def test_z_diagnostic_success_without_listeners(self):
        output = io.StringIO()
        with patch.object(socket.socket, 'bind', side_effect=AssertionError('diagnostic opened a port')), contextlib.redirect_stdout(output):
            self.assertEqual(diagnostic.main(), 0)
        self.assertTrue(output.getvalue().startswith('PYTHON '))
        self.assertTrue(output.getvalue().rstrip().endswith('COMPATIBILITY OK'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
