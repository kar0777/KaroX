"""Production/test isolation is an enforced guarantee, not a convention.

A rehearsal saved profile once landed in the real ``%APPDATA%\\KaroX\\vnext``
store and broke the TUI. These tests pin the architecture that makes that
impossible. Every environment-shaped assertion runs in a fresh subprocess:
the shared suite process accumulates deliberate environment mutations from
other tests, and this contract is about what a *new* test process gets.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

import _path_setup  # noqa: F401  (isolation bootstrap side effect under test)


TESTS_DIR = str(Path(__file__).resolve().parent)


def _fresh_bootstrap(code: str) -> subprocess.CompletedProcess[str]:
    """Run ``code`` in a fresh interpreter that imports the bootstrap cleanly."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("KAROX_")
    }
    prelude = (
        "import sys\n"
        f"sys.path.insert(0, {TESTS_DIR!r})\n"
        "import _path_setup\n"
        "import os\n"
        "from karox import paths\n"
    )
    return subprocess.run(
        [sys.executable, "-c", prelude + code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
    )


class FreshProcessBootstrapTests(unittest.TestCase):
    def _check(self, code: str) -> None:
        result = _fresh_bootstrap(code)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_every_spelling_flag_and_forbidden_list_are_armed(self) -> None:
        self._check(
            "names = ['KAROX_VNEXT_CONFIG_DIR', 'KAROX_CONFIG_DIR',"
            " 'KAROX_VNEXT_RUNTIME_DIR', 'KAROX_RUNTIME_DIR',"
            " 'KAROX_LEGACY_CONFIG_DIR']\n"
            "assert all(os.environ.get(n, '').strip() for n in names), names\n"
            "assert os.environ['KAROX_TEST_ISOLATION'] == '1'\n"
            "assert os.environ['KAROX_TEST_FORBIDDEN_DIRS'].strip()\n"
        )

    def test_aliases_agree_with_canonical_spellings(self) -> None:
        self._check(
            "assert os.environ['KAROX_VNEXT_CONFIG_DIR'] =="
            " os.environ['KAROX_CONFIG_DIR']\n"
            "assert os.environ['KAROX_VNEXT_RUNTIME_DIR'] =="
            " os.environ['KAROX_RUNTIME_DIR']\n"
        )

    def test_resolution_avoids_every_forbidden_location(self) -> None:
        self._check(
            "forbidden = [os.path.normcase(i) for i in"
            " os.environ['KAROX_TEST_FORBIDDEN_DIRS'].split(os.pathsep) if i.strip()]\n"
            "for resolved in (paths.config_dir(), paths.runtime_dir(),"
            " paths.legacy_config_dir()):\n"
            "    key = os.path.normcase(str(resolved))\n"
            "    for real in forbidden:\n"
            "        assert key != real and not key.startswith(real + os.sep),"
            " (resolved, real)\n"
        )

    def test_clearing_overrides_on_a_real_machine_refuses(self) -> None:
        # With the per-process sandbox present, resolution is transparently
        # re-pointed there (never the real dir). Without a sandbox -- an
        # unprepared child process -- it fails fast.
        self._check(
            "for n in ('KAROX_VNEXT_CONFIG_DIR', 'KAROX_CONFIG_DIR'):\n"
            "    os.environ.pop(n, None)\n"
            "sandbox = os.environ['KAROX_TEST_SANDBOX_DIR']\n"
            "resolved = str(paths.config_dir())\n"
            "assert resolved.startswith(sandbox), (resolved, sandbox)\n"
            "os.environ.pop('KAROX_TEST_SANDBOX_DIR', None)\n"
            "try:\n"
            "    paths.config_dir()\n"
            "except paths.TestIsolationViolation:\n"
            "    pass\n"
            "else:\n"
            "    raise AssertionError('expected TestIsolationViolation')\n"
        )

    def test_a_rebased_default_is_a_legitimate_isolation_style(self) -> None:
        self._check(
            "import tempfile\n"
            "tmp = tempfile.mkdtemp()\n"
            "for n in ('KAROX_VNEXT_CONFIG_DIR', 'KAROX_CONFIG_DIR'):\n"
            "    os.environ.pop(n, None)\n"
            "for n in ('APPDATA', 'LOCALAPPDATA', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME'):\n"
            "    os.environ[n] = tmp\n"
            "resolved = os.path.normcase(str(paths.config_dir()))\n"
            "import pathlib\n"
            "base = os.path.normcase(str(pathlib.Path(tmp).resolve()))\n"
            "assert resolved.startswith(base), (resolved, base)\n"
        )

    def test_guard_disarms_outside_test_processes(self) -> None:
        self._check(
            "for n in ('KAROX_VNEXT_CONFIG_DIR', 'KAROX_CONFIG_DIR',"
            " 'KAROX_TEST_ISOLATION'):\n"
            "    os.environ.pop(n, None)\n"
            "resolved = paths.config_dir()  # production behaviour: no raise\n"
            "assert str(resolved)\n"
        )


if __name__ == "__main__":
    unittest.main()
