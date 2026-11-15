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

    # A 59-line function that ends inside the window is whole; a longer one is cut.
    whole = b"def f():\n" + b"    x = 1\n" * 58 + b"def g():\n    return 2\n"
    cut = b"def f():\n" + b"    x = 1\n" * 80
    for name, source, complete in (("w", whole, True), ("c", cut, False)):
        found = _runtime(tmp_path / name, {"m.py": source})._search({"query": "def f"}, 30)
        assert found["matches"][0]["definition"]["complete"] is complete, name


def test_history_reads_are_one_call(tmp_path: Path) -> None:
    """Benchmark 2026-11-13 (click-3239 v39): 25 python -c git turns to read old code."""

    core = _runtime(tmp_path, {"core.py": b"OLD = 1\n"})
    repo = core.repository
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "Release version 8.2.0"], check=True)
    subprocess.run([*git, "tag", "v8.2.0"], check=True)
    (repo / "core.py").write_text("NEW = 2\n")
    subprocess.run([*git, "commit", "-qam", "refactor flags"], check=True)

    old = core._read_lines({"path": "core.py", "rev": "v8.2.0"}, 30)
    assert old["lines"] == ["OLD = 1"] and old["rev"] == "v8.2.0"
    assert core._read_lines({"path": "core.py"}, 30)["lines"] == ["NEW = 2"]
    log = core._git_log({"grep": "release version", "limit": 5}, 30)
    assert "Release version 8.2.0" in log["stdout"] and "refactor" not in log["stdout"]
    assert "refactor" in core._git_log({"path": "core.py", "rev": "v8.2.0..HEAD"}, 30)["stdout"]
    change = core._git_diff_paths({"rev": "HEAD", "paths": ["core.py"]}, 30)
    assert "refactor flags" in change["stdout"] and "+NEW = 2" in change["stdout"]
    assert "_evidence" not in change
    span = core._git_diff_paths({"rev": "v8.2.0..HEAD", "paths": ["core.py"]}, 30)["stdout"]
    assert "-OLD = 1" in span and "+NEW = 2" in span
    summary = core._git_diff_paths({"rev": "HEAD"}, 30)
    assert "core.py" in summary["stdout"] and "+NEW" not in summary["stdout"]
    assert "paths" in summary["note"]
    with pytest.raises(Exception):
        core._git_diff_paths({"rev": "--output=x"}, 30)
    for bad in ("--output=x", "a b", "../x"):
        with pytest.raises(Exception):
            core._read_lines({"path": "core.py", "rev": bad}, 30)


def test_an_edited_test_file_does_not_hide_a_regression(tmp_path: Path) -> None:
    """Benchmark 2026-11-13 (click-3239, 4/4 failures): a broken case was edited to pass."""

    import hashlib

    from karox import original_tests

    old_test = b"from mod import f\n\ndef test_one():\n    assert f(1) == 1\n\ndef test_two():\n    assert f(2) == 2\n"
    core = _runtime(tmp_path, {"mod.py": b"def f(x):\n    return x\n", "tests/test_mod.py": old_test})
    repo = core.repository
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    (repo / "mod.py").write_text("def f(x):\n    return 1\n")
    (repo / "tests/test_mod.py").write_bytes(old_test.replace(b"f(2) == 2", b"f(2) == 1"))
    edit = {"core_name": "repo.edit_file", "result": {"ok": True, "data": {
        "path": "tests/test_mod.py", "previous_sha256": hashlib.sha256(old_test).hexdigest()}}}
    record = SimpleNamespace(
        task="Make f constant.", requirements=[], changed_files=["mod.py", "tests/test_mod.py"],
        created_files=[], provider_history=[edit],
    )
    found = original_tests.regressions(core, record)
    assert found is not None and [n.split("::")[-1] for n in found] == ["test_two"]
    assert original_tests.undeclared(found, frozenset({"test_two"})) == []
    assert "test_two" in original_tests.reason(found)
    assert (repo / "tests/test_mod.py").read_bytes() != old_test  # the agent's bytes are back


def test_the_dossier_finds_the_function_a_task_is_about(tmp_path: Path) -> None:
    """Benchmark 2026-11-13: 36% of steps searched for code the task names."""

    from karox import task_dossier

    files = {
        "pkg/core.py": SOURCE.replace(b"flag_value", b"flag_value  # flag_value"),
        "pkg/other.py": b"def unrelated():\n    return 2\n",
        "tests/test_core.py": b"from pkg.core import Option\n\ndef test_flag_value():\n    Option.flag_value\n",
    }
    core = _runtime(tmp_path, files)
    git = ["git", "-C", str(core.repository), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    task = "Option ignores `flag_value` when default is True\n\nThe `flag_value` is dropped."
    dossier = task_dossier.build(core, task)
    assert [f["path"] for f in dossier["files"]][:1] == ["pkg/core.py"]
    assert "get_default" in dossier["text"] and "tests/test_core.py" in dossier["text"]
    assert "pkg/other.py" not in dossier["text"]


SCOPED = {
    "src/pkg/a.py": b"needle = 1\n",
    "src/pkg/notes.txt": b"needle\n",
    "top.py": b"needle = 3\n",
    ".venv/lib/b.py": b"needle = 2\n",
    "node_modules/x/c.py": b"needle = 4\n",
}


def test_a_glob_scope_still_skips_dependency_trees(tmp_path: Path) -> None:
    """pattern="*.py" searched every .py file in the venv too."""

    found = _runtime(tmp_path, SCOPED)._search({"query": "needle", "pattern": "*.py"}, 30)
    assert sorted(m["path"] for m in found["matches"]) == ["src/pkg/a.py", "top.py"]


def test_path_and_pattern_together_mean_the_files_under_path(tmp_path: Path) -> None:
    found = _runtime(tmp_path, SCOPED)._search(
        {"query": "needle", "path": "src/pkg", "pattern": "*.py"}, 30
    )
    assert [m["path"] for m in found["matches"]] == ["src/pkg/a.py"]


def test_a_glob_naming_a_dependency_tree_is_honoured(tmp_path: Path) -> None:
    found = _runtime(tmp_path, SCOPED)._search(
        {"query": "needle", "pattern": ".venv/**/*.py"}, 30
    )
    assert [m["path"] for m in found["matches"]] == [".venv/lib/b.py"]



def _plain_tree(tmp_path: Path, files: dict[str, bytes]) -> ExtendedCoreRuntime:
    """A project that is not a git checkout, like a benchmark's /app."""

    core = _runtime(tmp_path, files)
    for item in sorted((core.repository / ".git").rglob("*"), reverse=True):
        item.unlink() if item.is_file() else item.rmdir()
    (core.repository / ".git").rmdir()
    return core


def test_list_files_outside_a_git_checkout_still_lists_the_project(tmp_path: Path) -> None:
    """ls-files failed outside git and the listing came back empty."""

    core = _plain_tree(tmp_path, SCOPED)
    assert core._list_files({}, 30)["files"] == ["src/pkg/a.py", "src/pkg/notes.txt", "top.py"]
    assert core._list_files({"pattern": "**/*.py"}, 30)["files"] == ["src/pkg/a.py", "top.py"]


def test_related_tests_prefer_the_edited_package(tmp_path: Path) -> None:
    from karox import task_dossier

    files = {
        "lib/core/basic.py": b"X = 1\n",
        "lib/core/__init__.py": b"",
        "lib/assumptions/tests/test_basic.py": b"def test_a():\n    pass\n",
        "lib/matrices/tests/test_basic.py": b"def test_b():\n    pass\n",
        "lib/core/tests/test_basic.py": b"def test_c():\n    pass\n",
        "lib/core/tests/test_core.py": b"def test_d():\n    pass\n",
    }
    core = _plain_tree(tmp_path, files)
    assert task_dossier.related_tests(core, ["lib/core/basic.py"], limit=1) == ["lib/core/tests/test_basic.py"]
    assert task_dossier.related_tests(core, ["lib/core/__init__.py"], limit=1) == ["lib/core/tests/test_core.py"]


def test_the_dossier_works_outside_a_git_checkout(tmp_path: Path) -> None:
    from karox import task_dossier

    files = {
        "pkg/core.py": SOURCE.replace(b"flag_value", b"flag_value  # flag_value"),
        "pkg/other.py": b"def unrelated():\n    return 2\n",
    }
    core = _plain_tree(tmp_path, files)
    dossier = task_dossier.build(core, "Option ignores `flag_value` when default is True")
    assert [f["path"] for f in dossier["files"]][:1] == ["pkg/core.py"]


def test_list_files_and_search_work_without_a_git_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """TB 4.1 images have no git at all: both tools raised FileNotFoundError."""

    core = _plain_tree(tmp_path, {**SCOPED, **{f"pad/f{i}.txt": b"x\n" for i in range(400)}})

    def no_git(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError(2, "No such file or directory: 'git'")

    monkeypatch.setattr(core, "_git", no_git)
    # The ripgrep and small-tree paths answer first where they can; force the git one.
    monkeypatch.setattr(core, "_search_ripgrep", lambda **kwargs: None)
    monkeypatch.setattr(core, "_search_small_tree", lambda **kwargs: None)
    assert "top.py" in core._list_files({}, 30)["files"]
    found = core._search({"query": "needle"}, 30)
    assert sorted(m["path"] for m in found["matches"]) == ["src/pkg/a.py", "src/pkg/notes.txt", "top.py"]
