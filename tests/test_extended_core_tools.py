"""Coverage for the Core tools an external coding agent needs.

These exercise the real :class:`ExtendedCoreRuntime` through the hosted bridge,
so policy, repository confinement, mutation leases, idempotency and evidence are
all on the path rather than being bypassed.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from _support import SRC, initialize_git_repository  # noqa: F401

from karox.core_tools import is_ignored_path
from karox.hosted_bridge import CoreToolBridge
from karox.models import AccessProfile
from karox.sessions import SessionStore


def _commit_everything(repository: Path, message: str) -> None:
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "KaroX Test",
            "GIT_AUTHOR_EMAIL": "karox@example.invalid",
            "GIT_COMMITTER_NAME": "KaroX Test",
            "GIT_COMMITTER_EMAIL": "karox@example.invalid",
        }
    )
    for arguments in (
        ["git", "add", "-A"],
        ["git", "commit", "--quiet", "--no-verify", "-m", message],
    ):
        subprocess.run(
            arguments,
            cwd=repository,
            env=env,
            check=True,
            capture_output=True,
        )


class IgnoredPathTests(unittest.TestCase):
    def test_dependency_and_build_directories_are_ignored(self) -> None:
        for path in (
            ".venv/Lib/site-packages/attrs/__init__.py",
            "venv/lib/python3.13/site-packages/x.py",
            "src/karox/__pycache__/core.cpython-313.pyc",
            "src/karox_runtime.egg-info/PKG-INFO",
            "node_modules/left-pad/index.js",
            "build/lib/karox/core.py",
            ".zcode/state.json",
        ):
            with self.subTest(path=path):
                self.assertTrue(is_ignored_path(path))

    def test_real_source_is_not_ignored(self) -> None:
        for path in (
            "src/karox/core.py",
            "tests/test_core.py",
            "docs/vNext/README.md",
            "pyproject.toml",
        ):
            with self.subTest(path=path):
                self.assertFalse(is_ignored_path(path))

    def test_a_file_named_like_an_ignored_directory_is_kept(self) -> None:
        # Only directory components are matched, so a file called build stays.
        self.assertFalse(is_ignored_path("scripts/build"))


class ExtendedCoreToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        initialize_git_repository(self.repository)
        (self.repository / "sample.txt").write_bytes(b"before\n")
        self.sessions = SessionStore(self.root / "sessions")
        self.sessions.create(
            self.repository,
            "extended tools",
            AccessProfile.WORKSPACE_WRITE,
            session_id="extended",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _bridge(self, *tools: str) -> CoreToolBridge:
        return CoreToolBridge(
            self.repository,
            self.sessions,
            "extended",
            list(tools),
            audit_path=self.root / "audit.jsonl",
        )

    def _text(self, name: str = "sample.txt") -> str:
        return (self.repository / name).read_text(encoding="utf-8")

    # -- repo.edit_file ---------------------------------------------------

    def test_edit_replaces_exact_match_and_reports_evidence(self) -> None:
        bridge = self._bridge("karox.repo.edit_file")
        result = bridge.execute(
            "karox.repo.edit_file",
            {
                "path": "sample.txt",
                "old_string": "before",
                "new_string": "after",
            },
            idempotency_key="edit-1",
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["data"]["changed"])
        self.assertEqual(result["data"]["replacements"], 1)
        self.assertEqual(self._text(), "after\n")
        kinds = [item["kind"] for item in result["evidence"]]
        self.assertIn("file_edit", kinds)

    def test_edit_is_idempotent_on_a_repeated_key(self) -> None:
        bridge = self._bridge("karox.repo.edit_file")
        arguments = {
            "path": "sample.txt",
            "old_string": "before",
            "new_string": "after",
        }
        first = bridge.execute(
            "karox.repo.edit_file", arguments, idempotency_key="edit-2"
        )
        second = bridge.execute(
            "karox.repo.edit_file", arguments, idempotency_key="edit-2"
        )
        self.assertTrue(first["data"]["changed"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(self._text(), "after\n")

    def test_edit_refuses_an_occurrence_count_mismatch(self) -> None:
        (self.repository / "twice.txt").write_bytes(b"one\none\n")
        bridge = self._bridge("karox.repo.edit_file")
        with self.assertRaisesRegex(Exception, "but found 2"):
            bridge.execute(
                "karox.repo.edit_file",
                {
                    "path": "twice.txt",
                    "old_string": "one",
                    "new_string": "two",
                },
                idempotency_key="edit-mismatch",
            )
        self.assertEqual(self._text("twice.txt"), "one\none\n")

    def test_edit_replaces_every_declared_occurrence(self) -> None:
        (self.repository / "twice.txt").write_bytes(b"one\none\n")
        bridge = self._bridge("karox.repo.edit_file")
        result = bridge.execute(
            "karox.repo.edit_file",
            {
                "path": "twice.txt",
                "old_string": "one",
                "new_string": "two",
                "expected_occurrences": 2,
            },
            idempotency_key="edit-both",
        )
        self.assertEqual(result["data"]["replacements"], 2)
        self.assertEqual(self._text("twice.txt"), "two\ntwo\n")

    def test_edit_rejects_an_absent_match_and_a_no_op(self) -> None:
        bridge = self._bridge("karox.repo.edit_file")
        with self.assertRaisesRegex(Exception, "but found 0"):
            bridge.execute(
                "karox.repo.edit_file",
                {
                    "path": "sample.txt",
                    "old_string": "absent",
                    "new_string": "value",
                },
                idempotency_key="edit-absent",
            )
        with self.assertRaisesRegex(Exception, "identical"):
            bridge.execute(
                "karox.repo.edit_file",
                {
                    "path": "sample.txt",
                    "old_string": "before",
                    "new_string": "before",
                },
                idempotency_key="edit-noop",
            )
        self.assertEqual(self._text(), "before\n")

    def test_edit_cannot_escape_the_repository(self) -> None:
        bridge = self._bridge("karox.repo.edit_file")
        with self.assertRaises(Exception):
            bridge.execute(
                "karox.repo.edit_file",
                {
                    "path": "../escape.txt",
                    "old_string": "a",
                    "new_string": "b",
                },
                idempotency_key="edit-escape",
            )

    # -- repo.read_lines --------------------------------------------------

    def test_read_lines_returns_a_bounded_window(self) -> None:
        (self.repository / "many.txt").write_bytes(b"a\nb\nc\nd\ne\n")
        bridge = self._bridge("karox.repo.read_lines")
        result = bridge.execute(
            "karox.repo.read_lines",
            {"path": "many.txt", "start": 2, "count": 2},
        )
        data = result["data"]
        self.assertEqual(data["lines"], ["b", "c"])
        self.assertEqual(data["start"], 2)
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["total_lines"], 5)
        self.assertTrue(data["has_more"])

    def test_read_lines_reports_the_end_of_a_file(self) -> None:
        (self.repository / "many.txt").write_bytes(b"a\nb\nc\n")
        bridge = self._bridge("karox.repo.read_lines")
        result = bridge.execute(
            "karox.repo.read_lines",
            {"path": "many.txt", "start": 3},
        )
        self.assertEqual(result["data"]["lines"], ["c"])
        self.assertFalse(result["data"]["has_more"])

    def test_read_lines_returns_secret_shaped_lines_byte_for_byte(self) -> None:
        line = 'KEY = "sk-' + "b" * 24 + '"'
        (self.repository / "settings.py").write_bytes(f"{line}\nrest\n".encode("utf-8"))
        bridge = self._bridge("karox.repo.read_lines")

        data = bridge.execute(
            "karox.repo.read_lines", {"path": "settings.py", "start": 1, "count": 1}
        )["data"]

        # An edit anchor is taken from these lines, so a rewritten one cannot be
        # used to change the file it came from.
        self.assertEqual(data["lines"], [line])
        self.assertTrue(data["secret_like"])

    def test_read_lines_rejects_a_zero_start(self) -> None:
        bridge = self._bridge("karox.repo.read_lines")
        with self.assertRaisesRegex(Exception, "start must be 1 or greater"):
            bridge.execute(
                "karox.repo.read_lines", {"path": "sample.txt", "start": 0}
            )

    # -- git.log ----------------------------------------------------------

    def test_git_log_reports_committed_history(self) -> None:
        _commit_everything(self.repository, "seed the history")
        bridge = self._bridge("karox.git.log")
        result = bridge.execute("karox.git.log", {"limit": 5})
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["limit"], 5)
        self.assertIn("seed the history", result["data"]["stdout"])
        kinds = [item["kind"] for item in result["evidence"]]
        self.assertIn("git_log", kinds)

    def test_git_log_without_history_is_not_reported_as_success(self) -> None:
        bridge = self._bridge("karox.git.log")
        result = bridge.execute("karox.git.log", {})
        # A repository with no commits makes git exit non-zero. The result must
        # not claim success, otherwise a model could treat an error as history.
        self.assertFalse(result["ok"])

    # -- git.diff ---------------------------------------------------------

    def test_diff_can_be_limited_to_explicit_paths(self) -> None:
        (self.repository / "other.txt").write_bytes(b"other\n")
        _commit_everything(self.repository, "seed both files")
        (self.repository / "sample.txt").write_bytes(b"changed sample\n")
        (self.repository / "other.txt").write_bytes(b"changed other\n")
        bridge = self._bridge("karox.git.diff")
        everything = bridge.execute("karox.git.diff", {})["data"]
        self.assertIn("sample.txt", everything["stdout"])
        self.assertIn("other.txt", everything["stdout"])
        self.assertEqual(everything["paths"], [])
        limited = bridge.execute("karox.git.diff", {"paths": ["sample.txt"]})[
            "data"
        ]
        self.assertIn("sample.txt", limited["stdout"])
        self.assertNotIn("other.txt", limited["stdout"])
        self.assertEqual(limited["paths"], ["sample.txt"])

    def test_diff_still_accepts_staged_only(self) -> None:
        # The property was added alongside the existing one, so a caller that
        # passes only staged must keep working.
        bridge = self._bridge("karox.git.diff")
        result = bridge.execute("karox.git.diff", {"staged": True})
        self.assertIn("--cached", result["data"]["argv"])

    def test_diff_rejects_a_path_outside_the_repository(self) -> None:
        bridge = self._bridge("karox.git.diff")
        with self.assertRaises(Exception):
            bridge.execute("karox.git.diff", {"paths": ["../escape.txt"]})

    # -- ignored directories ----------------------------------------------

    def _seed_dependency_noise(self) -> None:
        vendored = self.repository / ".venv" / "Lib" / "site-packages" / "dep"
        vendored.mkdir(parents=True, exist_ok=True)
        (vendored / "noise.py").write_bytes(b"needle in a dependency\n")
        cache = self.repository / "src" / "__pycache__"
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "stale.py").write_bytes(b"needle in a cache\n")
        source = self.repository / "src"
        (source / "real.py").write_bytes(b"needle in real source\n")

    def test_list_files_skips_dependency_directories(self) -> None:
        self._seed_dependency_noise()
        bridge = self._bridge("karox.repo.list_files")
        files = bridge.execute("karox.repo.list_files", {})["data"]["files"]
        self.assertIn("src/real.py", files)
        self.assertFalse([item for item in files if ".venv" in item])
        self.assertFalse([item for item in files if "__pycache__" in item])

    def test_search_only_reports_real_source(self) -> None:
        self._seed_dependency_noise()
        bridge = self._bridge("karox.repo.search")
        data = bridge.execute("karox.repo.search", {"query": "needle"})["data"]
        paths = [item["path"] for item in data["matches"]]
        self.assertEqual(paths, ["src/real.py"])
        self.assertEqual(data["match_count"], 1)


if __name__ == "__main__":
    unittest.main()
