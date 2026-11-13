"""Audit wave 2 (2026-11-13): search scope and completion-rule false positives."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from karox.completion_rules import (
    _explicit_test_change_required,
    _is_test_path,
    _test_edit_ban,
    docs_only,
)
from karox.core_read_tools import is_ignored_path
from karox.core_tools import ExtendedCoreRuntime
from karox.policy import AccessProfile, CapabilityPolicy
from karox.sessions import SessionStore

SWE_PREAMBLE = (
    "I've already taken care of all changes to any of the test files described in "
    "the <pr_description>. This means you DON'T have to modify the testing logic or "
    "any of the tests in any way!"
)


def _record(task: str, changed: list[str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(task=task, requirements=[], changed_files=changed or [])


@pytest.mark.parametrize(
    "task",
    [
        SWE_PREAMBLE,
        "Change the date parser to follow the ISO 8601 spec.",
        "Update the renderer so the output matches the CommonMark spec for links.",
        "The change in 3.1 broke the test suite of our project.",
        "When I add a fixture to my test the parametrize ids are wrong.",
    ],
)
def test_prose_about_tests_is_not_a_request_to_edit_them(task: str) -> None:
    assert not _explicit_test_change_required(_record(task))


@pytest.mark.parametrize(
    "task",
    [
        "Add a regression test for the parser.",
        "Update the tests to cover the new flag.",
        "Write tests for foo().",
        "Extend the test cases in test_api.py.",
    ],
)
def test_a_real_request_to_edit_tests_still_counts(task: str) -> None:
    assert _explicit_test_change_required(_record(task))


def test_the_swe_preamble_keeps_its_test_edit_ban() -> None:
    assert _test_edit_ban(_record(SWE_PREAMBLE))


def test_a_bug_fix_that_only_touched_docs_is_not_docs_only() -> None:
    assert not docs_only(_record("Fix the TypeError raised by foo() when given None", ["README.md"]))
    assert not docs_only(_record("The app crashes on start", ["README.md"]))
    assert docs_only(_record("Fix typo in README", ["README.md"]))


def test_django_tests_module_and_conftest_are_test_files() -> None:
    assert _is_test_path("myapp/tests.py") and _is_test_path("testing/conftest.py")
    assert not _is_test_path("src/app.py")


def _runtime(tmp_path: Path, files: dict[str, bytes]) -> ExtendedCoreRuntime:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for name, data in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return ExtendedCoreRuntime(
        repo, CapabilityPolicy(AccessProfile.WORKSPACE_WRITE), SessionStore(tmp_path / "s")
    )


FILES = {
    name: b"needle needle\n"
    for name in (
        "a.py", "build/a.py", "dist/d.py", "node_modules/x.js",
        "lib/build/b.py", "lib/dist/e.py", "coverage/c.py",
    )
}
WANTED = ["a.py", "coverage/c.py", "lib/build/b.py", "lib/dist/e.py"]


def test_output_names_are_skipped_at_the_root_only(tmp_path: Path) -> None:
    """pip's operations/build and coverage.py's own package were invisible."""

    found = _runtime(tmp_path, FILES)._search({"query": "needle"}, 30)
    assert sorted(m["path"] for m in found["matches"]) == WANTED
    # Two hits on one line are one result line, not a truncated result.
    assert not found.get("truncated")
    assert is_ignored_path("build/x.py") and not is_ignored_path("lib/build/x.py")


def test_the_python_backend_skips_the_same_directories(tmp_path: Path) -> None:
    with (
        mock.patch.object(ExtendedCoreRuntime, "_search_ripgrep", return_value=None),
        mock.patch.object(ExtendedCoreRuntime, "_search_git_grep", return_value=None),
    ):
        found = _runtime(tmp_path, FILES)._search({"query": "needle"}, 30)
    assert sorted(m["path"] for m in found["matches"]) == WANTED


SOURCE = (
    b"import os\n\n\nclass Option:\n    def get_default(self, ctx):\n"
    b"        value = self.default\n        if value is True:\n"
    b"            value = self.flag_value\n        return value\n\n"
    b"    def other(self):\n        return 1\n"
)


def test_a_definition_lookup_carries_the_body(tmp_path: Path) -> None:
    """Benchmark 2026-11-13: 'search def X' then 'read_lines' cost a turn each time."""

    from karox.engine_tool_bodies import _plain_body

    found = _runtime(tmp_path, {"core.py": SOURCE})._search({"query": "def get_default"}, 30)
    body = found["matches"][0]["definition"]
    assert body["start"] == 5 and body["end"] == 9 and "return value" in body["text"]
    assert "def other" not in body["text"]
    shown = _plain_body("repo.search", found)
    assert shown is not None and "[definition, lines 5-9]" in shown and "return value" in shown

    plain = _runtime(tmp_path / "b", {"core.py": SOURCE})._search({"query": "flag_value"}, 30)
    assert "definition" not in plain["matches"][0]
