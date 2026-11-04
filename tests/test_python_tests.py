"""Which Python test command a repository gets suggested."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from karox.python_tests import PYTEST_COMMAND, python_test_command
from karox.verification import discover_verification_commands

UNITTEST_CASE = (
    "import unittest\n\n\n"
    "class MathTest(unittest.TestCase):\n"
    "    def test_adds(self):\n"
    "        self.assertEqual(1 + 1, 2)\n"
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _run(repository: Path, argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, *argv[1:]] if argv[0] == "python" else list(argv)
    return subprocess.run(
        command, cwd=repository, capture_output=True, text=True, timeout=60
    )


def test_no_python_tests_suggests_nothing(tmp_path: Path) -> None:
    _write(tmp_path / "main.py", "print('hi')\n")
    assert python_test_command(tmp_path) is None


@pytest.mark.parametrize("package", [True, False])
def test_unittest_only_tests_get_unittest_discover_that_really_runs(
    tmp_path: Path, package: bool
) -> None:
    _write(tmp_path / "tests" / "test_math.py", UNITTEST_CASE)
    if package:
        _write(tmp_path / "tests" / "__init__.py", "")
    command = python_test_command(tmp_path)
    assert command is not None and command[:4] == ("python", "-m", "unittest", "discover")
    completed = _run(tmp_path, command)
    assert completed.returncode == 0, completed.stderr
    assert "Ran 1 test" in completed.stderr


def test_root_level_unittest_files_discover_from_the_root(tmp_path: Path) -> None:
    _write(tmp_path / "test_math.py", UNITTEST_CASE)
    command = python_test_command(tmp_path)
    assert command == ("python", "-m", "unittest", "discover")
    completed = _run(tmp_path, command)
    assert completed.returncode == 0 and "Ran 1 test" in completed.stderr


def test_a_failing_unittest_suite_fails_the_suggested_check(tmp_path: Path) -> None:
    _write(
        tmp_path / "tests" / "test_math.py",
        UNITTEST_CASE.replace("1 + 1, 2", "1 + 1, 3"),
    )
    completed = _run(tmp_path, python_test_command(tmp_path) or ())
    assert completed.returncode != 0
    assert "FAILED" in completed.stderr


@pytest.mark.parametrize(
    "extra",
    [
        {"conftest.py": ""},
        {"pytest.ini": "[pytest]\n"},
        {"pyproject.toml": "[tool.pytest.ini_options]\naddopts = '-q'\n"},
        {"setup.cfg": "[tool:pytest]\n"},
        {"requirements-dev.txt": "pytest>=8\n"},
    ],
)
def test_a_project_that_configures_pytest_keeps_pytest(
    tmp_path: Path, extra: dict[str, str]
) -> None:
    _write(tmp_path / "tests" / "test_math.py", UNITTEST_CASE)
    for name, text in extra.items():
        _write(tmp_path / name, text)
    assert python_test_command(tmp_path) == PYTEST_COMMAND


@pytest.mark.parametrize(
    "body",
    [
        "import pytest\n\n\ndef test_x():\n    assert True\n",
        "def test_bare():\n    assert True\n",
        "from unittest.mock import patch\n\n\ndef test_patched():\n    assert patch\n",
        UNITTEST_CASE + "\n\n@pytest.mark.slow\ndef helper():\n    pass\n",
    ],
)
def test_pytest_style_tests_keep_pytest(tmp_path: Path, body: str) -> None:
    _write(tmp_path / "tests" / "test_style.py", body)
    assert python_test_command(tmp_path) == PYTEST_COMMAND


def test_mixed_styles_prefer_pytest_which_runs_both(tmp_path: Path) -> None:
    _write(tmp_path / "tests" / "test_case.py", UNITTEST_CASE)
    _write(tmp_path / "tests" / "test_bare.py", "def test_bare():\n    assert True\n")
    assert python_test_command(tmp_path) == PYTEST_COMMAND


def test_pyproject_layout_without_test_files_still_suggests_pytest(tmp_path: Path) -> None:
    _write(tmp_path / "pyproject.toml", "[project]\nname = 'demo'\n")
    (tmp_path / "tests").mkdir()
    assert python_test_command(tmp_path) == PYTEST_COMMAND


def test_verification_discovery_suggests_unittest_for_unittest_repositories(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "tests" / "__init__.py", "")
    _write(tmp_path / "tests" / "test_math.py", UNITTEST_CASE)
    commands = discover_verification_commands(tmp_path)
    assert ("python", "-m", "unittest", "discover", "-s", "tests", "-t", ".") in commands
    assert PYTEST_COMMAND not in commands
