from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts" / "run_ci_test_command.py"


class IsolatedTestCommandTests(unittest.TestCase):
    def run_child(self, code: str, *, encoding: str = "utf-8") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(WRAPPER), "-c", code],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30,
            env=dict(os.environ, PYTHONIOENCODING=encoding),
        )

    def test_relays_stdout_stderr_and_preserves_failure(self) -> None:
        result = self.run_child("import sys; print('stdout sentinel'); print('stderr sentinel', file=sys.stderr); sys.exit(7)")
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertIn("stdout sentinel", result.stdout)
        self.assertIn("stderr sentinel", result.stdout)

    def test_unicode_diagnostic_survives_legacy_parent_encoding(self) -> None:
        result = self.run_child("print('Unicode: \\u2192 \\u043f')", encoding="cp1252")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Unicode:", result.stdout)
        self.assertIn("\\u2192", result.stdout)

    def test_stdin_is_closed_in_noninteractive_ci(self) -> None:
        result = self.run_child("import sys; assert sys.stdin.read() == ''; print('EOF confirmed')")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("EOF confirmed", result.stdout)

    def test_missing_command_is_an_error(self) -> None:
        result = subprocess.run([sys.executable, str(WRAPPER)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)
