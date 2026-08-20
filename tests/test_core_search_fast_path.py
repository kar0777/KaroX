from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _support import initialize_git_repository
from karox.core import CoreRuntime
from karox.models import AccessProfile, Capability, CoreCommand, Origin, OriginKind
from karox.policy import CapabilityPolicy
from karox.sessions import SessionStore


class CoreSearchFastPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        initialize_git_repository(self.repository)
        (self.repository / "searchable.md").write_text(
            "alpha\nBANANA marker\ngamma\n", encoding="utf-8"
        )
        self.sessions = SessionStore(self.root / "sessions")
        self.sessions.create(
            self.repository,
            "search fixture",
            AccessProfile.WORKSPACE_WRITE,
            session_id="session",
        )
        self.origin = Origin(OriginKind.NATIVE_AGENT, "test-agent")
        self.policy = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
        self.policy.set_grants(self.origin, {Capability.REPO_READ})
        self.runtime = CoreRuntime(
            self.repository,
            self.policy,
            self.sessions,
            self.root / "audit.jsonl",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def command(self, arguments: dict[str, object]) -> CoreCommand:
        return CoreCommand("repo.search", arguments, "session", self.origin)

    def test_prefers_ripgrep_and_preserves_search_contract(self) -> None:
        payload = "\n".join(
            [
                json.dumps(
                    {
                        "type": "match",
                        "data": {
                            "path": {"text": "./searchable.md"},
                            "lines": {"text": "BANANA marker\n"},
                            "line_number": 2,
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "summary",
                        "data": {"stats": {"searches": 7, "matches": 1}},
                    }
                ),
            ]
        )
        fake_result = {
            "exit_code": 0,
            "stdout": payload,
            "stdout_truncated": False,
            "timed_out": False,
        }
        with (
            patch.object(self.runtime, "_ripgrep_path", "rg"),
            patch.object(self.runtime, "_run", return_value=fake_result) as runner,
        ):
            result = self.runtime.execute(
                self.command({"query": "BANANA", "pattern": "*.md"})
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.data["backend"], "ripgrep")
        self.assertEqual(result.data["files_scanned"], 7)
        self.assertEqual(result.data["match_count"], 1)
        self.assertEqual(result.data["matches"][0]["path"], "searchable.md")
        self.assertEqual(result.data["matches"][0]["line"], 2)
        argv = runner.call_args.args[0]
        self.assertIn("--fixed-strings", argv)
        self.assertIn("--ignore-case", argv)
        self.assertIn("--hidden", argv)
        self.assertIn("--no-ignore", argv)
        self.assertIn("*.md", argv)
        self.assertIn("!.git/**", argv)
        self.assertIn("!.karox/**", argv)

    def test_uses_small_tree_scanner_without_ripgrep(self) -> None:
        with patch.object(self.runtime, "_search_ripgrep", return_value=None):
            result = self.runtime.execute(self.command({"query": "BANANA"}))
        self.assertTrue(result.ok)
        self.assertEqual(result.data["backend"], "python")
        self.assertEqual(result.data["match_count"], 1)
        self.assertEqual(result.data["matches"][0]["path"], "searchable.md")

    def test_symlink_candidate_fails_closed_to_git_grep(self) -> None:
        with (
            patch.object(self.runtime, "_search_ripgrep", return_value=None),
            patch.object(
                Path,
                "is_symlink",
                autospec=True,
                side_effect=lambda path: path.name == "searchable.md",
            ),
            patch.object(
                self.runtime,
                "_search_git_grep",
                wraps=self.runtime._search_git_grep,
            ) as git_grep,
        ):
            result = self.runtime.execute(self.command({"query": "BANANA"}))
        self.assertTrue(result.ok)
        self.assertEqual(result.data["backend"], "git-grep")
        # The same mocked symlink is then rejected by the established safe_path
        # checks inside git-grep, so no repository content is returned.
        self.assertEqual(result.data["match_count"], 0)
        git_grep.assert_called_once()

    def test_large_tree_falls_back_to_git_grep(self) -> None:
        generated = self.repository / "generated"
        generated.mkdir()
        for index in range(self.runtime.SMALL_SEARCH_FILE_PROBE_LIMIT + 1):
            (generated / f"noise-{index:04d}.txt").write_text("noise\n", encoding="utf-8")
        with (
            patch.object(self.runtime, "_search_ripgrep", return_value=None),
            patch.object(
                self.runtime,
                "_search_git_grep",
                wraps=self.runtime._search_git_grep,
            ) as git_grep,
        ):
            result = self.runtime.execute(self.command({"query": "BANANA"}))
        self.assertTrue(result.ok)
        self.assertEqual(result.data["backend"], "git-grep")
        self.assertEqual(result.data["match_count"], 1)
        git_grep.assert_called_once()

    def test_regex_keeps_python_semantics_without_ripgrep(self) -> None:
        with patch.object(self.runtime, "_search_ripgrep", return_value=None):
            result = self.runtime.execute(
                self.command({"query": r"BAN\w+", "regex": True})
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["backend"], "python")
        self.assertEqual(result.data["match_count"], 1)

    def test_ripgrep_and_git_grep_failure_falls_back_to_python(self) -> None:
        failed_native = {
            "exit_code": 2,
            "stdout": "",
            "stdout_truncated": False,
            "timed_out": False,
        }
        with (
            patch.object(self.runtime, "_search_ripgrep", return_value=None),
            patch.object(self.runtime, "_search_small_tree", return_value=None),
            patch.object(self.runtime, "_git", return_value=failed_native),
        ):
            result = self.runtime.execute(self.command({"query": "BANANA"}))
        self.assertTrue(result.ok)
        self.assertEqual(result.data["backend"], "python")
        self.assertEqual(result.data["match_count"], 1)


if __name__ == "__main__":
    unittest.main()
