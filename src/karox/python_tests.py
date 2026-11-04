"""Which Python test runner a repository actually uses.

Suggesting ``python -m pytest`` for every repository with ``test_*.py`` files
broke unittest-only projects: without pytest installed the approved check
fails before running a single test, and the user had to notice and replace it.
The choice here is static and bounded (no interpreter is started): pytest when
the project configures or imports it, ``unittest discover`` when the tests only
use the standard library, pytest otherwise (it also runs unittest suites).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional, Tuple

PYTEST_COMMAND: Tuple[str, ...] = ("python", "-m", "pytest", "-q")
_SAMPLE_FILES = 25
_SAMPLE_BYTES = 64 * 1024
# pytest-only constructs: the pytest module itself or bare test functions,
# which unittest never collects. ``from unittest.mock import patch`` alone
# does not make a file a unittest suite.
_PYTEST_STYLE = re.compile(
    r"^\s*(?:import\s+pytest\b|from\s+pytest\b)|@pytest\.|^(?:async\s+)?def\s+test\w*\s*\(",
    re.MULTILINE,
)
_TEST_CASE = re.compile(r"^class\s+\w+\s*\([^)]*TestCase\b", re.MULTILINE)


def _read(path: Path, limit: int = _SAMPLE_BYTES) -> str:
    try:
        with path.open("rb") as handle:
            return handle.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _test_files(repository: Path) -> List[Path]:
    found: List[Path] = []
    for pattern_root, pattern in (
        (repository, "test_*.py"),
        (repository / "tests", "test_*.py"),
        (repository / "tests", "*/test_*.py"),
        (repository / "test", "test_*.py"),
    ):
        if not pattern_root.is_dir():
            continue
        for path in sorted(pattern_root.glob(pattern)):
            if path.is_file():
                found.append(path)
            if len(found) >= _SAMPLE_FILES:
                return found
    return found


def _unittest_command(repository: Path, files: List[Path]) -> Tuple[str, ...]:
    """``unittest discover`` rooted where the tests are.

    Since Python 3.11 discovery neither recurses into nor starts from a
    directory without ``__init__.py`` when a different top level is given, so
    a plain ``tests/`` folder is discovered as the top level itself.
    """

    for name in ("tests", "test"):
        folder = repository / name
        if not any(folder in path.parents for path in files):
            continue
        if (folder / "__init__.py").is_file():
            return ("python", "-m", "unittest", "discover", "-s", name, "-t", ".")
        return ("python", "-m", "unittest", "discover", "-s", name)
    return ("python", "-m", "unittest", "discover")


def pytest_configured(repository: Path) -> bool:
    if (repository / "pytest.ini").is_file() or (repository / "conftest.py").is_file():
        return True
    if (repository / "tests" / "conftest.py").is_file():
        return True
    for name, marker in (
        ("pyproject.toml", "pytest"),
        ("setup.cfg", "[tool:pytest]"),
        ("tox.ini", "[pytest]"),
        ("requirements-dev.txt", "pytest"),
        ("requirements_test.txt", "pytest"),
        ("requirements-test.txt", "pytest"),
        ("dev-requirements.txt", "pytest"),
        ("requirements.txt", "pytest"),
    ):
        path = repository / name
        if path.is_file() and marker in _read(path).lower():
            return True
    return False


def python_test_command(repository: Path) -> Optional[Tuple[str, ...]]:
    """The test argv for this repository, or None when it has no Python tests."""

    files = _test_files(repository)
    has_layout = (repository / "pyproject.toml").is_file() and (repository / "tests").is_dir()
    if not files and not has_layout:
        return None
    if pytest_configured(repository):
        return PYTEST_COMMAND
    texts = [_read(path) for path in files]
    if any(_PYTEST_STYLE.search(text) for text in texts):
        return PYTEST_COMMAND
    if any("unittest" in text and _TEST_CASE.search(text) for text in texts):
        return _unittest_command(repository, files)
    return PYTEST_COMMAND


__all__ = ["PYTEST_COMMAND", "pytest_configured", "python_test_command"]
