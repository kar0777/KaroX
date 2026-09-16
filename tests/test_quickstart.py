"""``karox quickstart``: the first screen a new user sees.

The command reads stores, never writes, never prompts, and answers three
questions -- is this a repository, is anything connected, what is the one next
command. These tests pin those properties and the bilingual contract: every
string the screen can show exists in both languages, so a missing translation
fails here rather than producing a mixed-language first impression.
"""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _support import SRC  # noqa: F401 - inserts src on sys.path

from karox import cli
from karox import quickstart
from karox.entrypoint import main as entrypoint_main


def _git(returncode: int, stdout: str = ""):
    def run(*_args, **_kwargs):
        return SimpleNamespace(returncode=returncode, stdout=stdout)

    return run


class ReportDecisionTests(unittest.TestCase):
    """The next step is a pure function of four facts."""

    def test_outside_a_repository_the_next_step_is_to_enter_one(self) -> None:
        report = quickstart.build_report(
            Path("/tmp/nowhere"),
            language="en",
            provider_ids=["openai"],
            selected_model="openai/gpt",
            client_labels=["chatgpt-web (dev)"],
            session_repositories=[],
            toplevel=lambda _path: None,
        )
        self.assertFalse(report.repository_is_git)
        self.assertEqual(report.session_count, 0)
        self.assertIn("cd into a Git repository", report.next_step)

    def test_a_repository_with_nothing_connected_points_at_connect(self) -> None:
        report = quickstart.build_report(
            Path("/repo"),
            language="ru",
            provider_ids=[],
            selected_model=None,
            client_labels=[],
            session_repositories=[],
            toplevel=lambda _path: Path("/repo"),
        )
        self.assertTrue(report.repository_is_git)
        self.assertIn("/connect", report.next_step)
        # Russian language selects Russian prose, not English with a Russian label.
        self.assertIn("запусти", report.next_step)

    def test_a_connected_repository_without_sessions_invites_the_first_task(self) -> None:
        report = quickstart.build_report(
            Path("/repo"),
            language="en",
            provider_ids=["anthropic"],
            selected_model=None,
            client_labels=[],
            session_repositories=["/other"],
            toplevel=lambda _path: Path("/repo"),
        )
        self.assertEqual(report.session_count, 0)
        self.assertIn("first task", report.next_step)
        self.assertIn("Observe", report.next_step)

    def test_an_existing_session_in_this_repository_offers_resume(self) -> None:
        root = Path("/repo").resolve(strict=False)
        report = quickstart.build_report(
            root,
            language="en",
            provider_ids=[],
            selected_model=None,
            client_labels=["claude-web (main)"],
            session_repositories=[str(root), str(root), "/elsewhere"],
            toplevel=lambda _path: root,
        )
        self.assertEqual(report.session_count, 2)
        self.assertIn("resumes", report.next_step)

    def test_a_down_saved_bridge_steers_the_next_step_to_repair(self) -> None:
        report = quickstart.build_report(
            Path("/repo"),
            language="ru",
            provider_ids=[],
            selected_model=None,
            client_labels=["chatgpt-web (chatgpt-dev)"],
            session_repositories=[],
            down_bridges=("chatgpt-dev",),
            toplevel=lambda _path: Path("/repo"),
        )
        self.assertIn("karox bridge start --saved chatgpt-dev", report.next_step)
        self.assertIn("запусти", report.next_step)
        # Exactly one concrete repair command, and no jargon beyond that name.
        self.assertNotIn("supervisor", report.next_step.split("`karox bridge start")[0])

    def test_a_live_bridge_leaves_the_normal_next_step_alone(self) -> None:
        report = quickstart.build_report(
            Path("/repo"),
            language="en",
            provider_ids=["openai"],
            selected_model=None,
            client_labels=["chatgpt-web (chatgpt-dev)"],
            session_repositories=[],
            down_bridges=(),
            toplevel=lambda _path: Path("/repo"),
        )
        self.assertIn("first task", report.next_step)
        self.assertNotIn("bridge start", report.next_step)

    def test_unknown_language_falls_back_to_english_without_raising(self) -> None:
        report = quickstart.build_report(
            Path("/repo"),
            language="de",
            provider_ids=[],
            selected_model=None,
            client_labels=[],
            session_repositories=[],
            toplevel=lambda _path: Path("/repo"),
        )
        self.assertIsNone(report.language)
        self.assertIn("/connect", report.next_step)
        self.assertNotIn("запусти", report.next_step)


class RenderTests(unittest.TestCase):
    def _report(self, **overrides):
        base = dict(
            repository="/repo",
            repository_is_git=True,
            language="en",
            providers=("openai",),
            selected_model="openai/gpt",
            clients=(),
            session_count=1,
            next_step="run `karox`",
            notes=("note",),
        )
        base.update(overrides)
        return quickstart.QuickstartReport(**base)

    def test_render_is_short_and_ends_with_the_next_step(self) -> None:
        text = quickstart.render_report(self._report(), "en")
        lines = [line for line in text.splitlines() if line.strip()]
        self.assertLessEqual(len(lines), 8)
        self.assertIn("Next: run `karox`", text)
        self.assertIn("(selected: openai/gpt)", text)
        self.assertIn("Hosted clients: none connected", text)

    def test_render_in_russian_uses_only_russian_labels(self) -> None:
        text = quickstart.render_report(self._report(language="ru"), "ru")
        for english_label in ("Repository:", "Language:", "API models:", "Next:"):
            self.assertNotIn(english_label, text)
        self.assertIn("Дальше:", text)

    def test_first_screen_does_not_use_bridge_vocabulary_when_nothing_is_configured(self) -> None:
        text = quickstart.render_report(
            self._report(providers=(), selected_model=None, clients=(), session_count=0),
            "en",
        ).lower()
        for jargon in ("bridge", "tunnel", "oauth", "profile"):
            self.assertNotIn(jargon, text)

    def test_every_string_exists_in_both_languages(self) -> None:
        for key, entry in quickstart._TEXT.items():
            for language in ("en", "ru"):
                self.assertTrue(entry.get(language), f"{key} missing {language}")


class GitToplevelTests(unittest.TestCase):
    def test_a_successful_rev_parse_is_the_root(self) -> None:
        root = quickstart.git_toplevel(Path("."), run=_git(0, "/work/repo\n"))
        self.assertEqual(root, Path("/work/repo"))

    def test_a_failing_rev_parse_is_not_a_repository(self) -> None:
        self.assertIsNone(quickstart.git_toplevel(Path("."), run=_git(128)))

    def test_a_missing_git_binary_is_not_a_repository(self) -> None:
        def run(*_args, **_kwargs):
            raise FileNotFoundError("git")

        self.assertIsNone(quickstart.git_toplevel(Path("."), run=run))

    def test_a_hung_git_is_not_a_repository(self) -> None:
        def run(*_args, **_kwargs):
            raise subprocess.TimeoutExpired(cmd="git", timeout=10)

        self.assertIsNone(quickstart.git_toplevel(Path("."), run=run))


class CliWiringTests(unittest.TestCase):
    """The argparse tree and the documented command agree, and --json is one document."""

    def test_quickstart_is_a_registered_command_with_json(self) -> None:
        parser = cli._parser()
        args = parser.parse_args(["quickstart", "--json"])
        self.assertEqual(args.command, "quickstart")
        self.assertTrue(args.json)

    def test_root_help_names_quickstart_first_after_karox(self) -> None:
        text = cli._human_root_help()
        self.assertIn("karox quickstart", text)
        self.assertLess(text.index("karox quickstart"), text.index("karox models"))

    def test_console_entrypoint_uses_the_lightweight_quickstart_path(self) -> None:
        with (
            mock.patch("karox.quickstart.cli_main", return_value=0) as fast,
            mock.patch("karox.entrypoint._legacy", side_effect=AssertionError("heavy CLI loaded")),
        ):
            self.assertEqual(entrypoint_main(["quickstart", "--json"]), 0)
        fast.assert_called_once_with(["--json"])

    def test_json_output_is_one_document_and_carries_no_secret_fields(self) -> None:
        report = quickstart.QuickstartReport(
            repository="/repo",
            repository_is_git=True,
            language="en",
            providers=("openai",),
            selected_model="openai/gpt",
            clients=("chatgpt-web (dev)",),
            session_count=0,
            next_step="run `karox`",
        )
        printed: list[str] = []
        with (
            mock.patch("karox.quickstart.collect_report", return_value=report),
            mock.patch.object(cli, "_write_line", side_effect=printed.append),
        ):
            self.assertEqual(cli.main(["quickstart", "--json"]), 0)
        self.assertEqual(len(printed), 1)
        payload = json.loads(printed[0])
        self.assertEqual(payload["next_step"], "run `karox`")
        self.assertEqual(
            set(payload),
            {
                "repository",
                "repository_is_git",
                "language",
                "providers",
                "selected_model",
                "clients",
                "session_count",
                "next_step",
                "notes",
            },
        )

    def test_collect_report_survives_every_store_failing(self) -> None:
        """A malformed JSON file must not turn the first screen into a traceback."""
        boom = mock.Mock(side_effect=RuntimeError("store is broken"))
        with (
            mock.patch("karox.registry.ProviderRegistry", boom),
            mock.patch("karox.web_bridge_profiles.WebBridgeProfileStore", boom),
            mock.patch("karox.sessions.SessionStore", boom),
            mock.patch.object(quickstart, "_load_language_from_preferences", boom),
            mock.patch.object(quickstart, "git_toplevel", return_value=None),
        ):
            report = quickstart.collect_report(Path("."))
        self.assertFalse(report.repository_is_git)
        self.assertEqual(report.providers, ())
        self.assertEqual(report.clients, ())
        self.assertIsNone(report.language)


if __name__ == "__main__":
    unittest.main()
