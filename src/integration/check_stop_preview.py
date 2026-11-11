"""Regression checks for preview process selection; never stop real processes."""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import runpy
import unittest
from unittest.mock import Mock, patch

import psutil

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'src/integration/stop_preview.py'


def process(cwd, cmdline=None):
    result = Mock()
    result.info = {
        'cwd': cwd,
        'cmdline': cmdline if cmdline is not None else [
            'python', 'src/integration/preview_phone.py'],
    }
    return result


class StopPreviewChecks(unittest.TestCase):
    def test_missing_cwd_does_not_interrupt_selection(self):
        missing = process(None)
        foreign = process(str(ROOT.parent / 'other-project'))
        unrelated = process(str(ROOT), ['python', 'main.py'])
        own = process(str(ROOT))
        with patch.object(psutil, 'process_iter', return_value=[
                missing, foreign, unrelated, own]), redirect_stdout(StringIO()):
            runpy.run_path(str(SCRIPT), run_name='__main__')
        for skipped in (missing, foreign, unrelated):
            skipped.terminate.assert_not_called()
            skipped.wait.assert_not_called()
        own.terminate.assert_called_once_with()
        own.wait.assert_called_once_with(timeout=5)


if __name__ == '__main__':
    unittest.main(verbosity=2)
