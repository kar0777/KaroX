"""Tests for the Phase 9 optional TUI shell.

The TUI is a thin presentation layer; it must contain no business logic.  These
tests exercise the slash-command-to-argv translation, the raw-input fallback,
unknown-command handling, and exit/EOF, without coupling to ``rich`` rendering
internals.
"""

from __future__ import annotations

import io
import unittest

from _support import SRC  # noqa: F401 - inserts src on sys.path
from karox import tui


class SlashTranslationTests(unittest.TestCase):
    def test_known_slash_commands_produce_argv(self) -> None:
        argv = tui._slash_to_argv("/bridge list --json", None, "/repo")
        self.assertEqual(argv, ["bridge", "list", "--json"])

    def test_provider_default_uses_list(self) -> None:
        argv = tui._slash_to_argv("/provider", None, "/repo")
        self.assertEqual(argv, ["provider", "list", "--json"])

    def test_session_aware_slash_uses_session_and_repository(self) -> None:
        argv = tui._slash_to_argv("/handoff", "s1", "/repo")
        self.assertEqual(argv, ["session", "handoff", "s1", "--repository", "/repo"])

    def test_status_without_session_falls_back_to_list(self) -> None:
        argv = tui._slash_to_argv("/status", None, "/repo")
        self.assertEqual(argv, ["session", "list", "--json"])

    def test_doctor_passes_through(self) -> None:
        argv = tui._slash_to_argv("/doctor", None, "/repo")
        self.assertEqual(argv, ["doctor"])

    def test_unknown_slash_command_returns_none(self) -> None:
        self.assertIsNone(tui._slash_to_argv("/bogus", None, "/repo"))


class TuiLoopTests(unittest.TestCase):
    def _run(self, lines: str, *, session_id: str | None = None) -> tuple[int, str]:
        out = io.StringIO()
        stdin = io.StringIO(lines)
        # input() reads from stdin; feed the lines and close.
        code = tui.run_tui(
            session_id=session_id,
            repository="/repo",
            input_stream=stdin,
            output_stream=out,
        )
        return code, out.getvalue()

    def test_exit_returns_zero(self) -> None:
        code, _ = self._run("/exit\n")
        self.assertEqual(code, 0)

    def test_eof_returns_zero(self) -> None:
        code, _ = self._run("")
        self.assertEqual(code, 0)

    def test_help_is_handled_by_shell(self) -> None:
        code, out = self._run("/help\n/exit\n")
        self.assertEqual(code, 0)
        self.assertIn("KaroX slash commands", out)

    def test_unknown_slash_reports_error_without_calling_backend(self) -> None:
        code, out = self._run("/bogus\n/exit\n")
        self.assertEqual(code, 0)
        self.assertIn("unknown slash command", out)

    def test_empty_lines_are_skipped(self) -> None:
        code, _ = self._run("\n\n/exit\n")
        self.assertEqual(code, 0)

    def test_plain_text_fallback_help(self) -> None:
        # Force the non-rich path to cover the degrade-to-plain-text branch and
        # confirm the help table is still rendered through the output stream.
        saved = tui._HAS_RICH
        tui._HAS_RICH = False
        try:
            code, out = self._run("/help\n/exit\n")
        finally:
            tui._HAS_RICH = saved
        self.assertEqual(code, 0)
        self.assertIn("KaroX slash commands", out)
        self.assertIn("/exit", out)


class BackendDelegationTests(unittest.TestCase):
    """The TUI must not reimplement handlers; it forwards to the real CLI."""

    def test_run_cli_invokes_main_entrypoint(self) -> None:
        import sys
        from unittest.mock import patch

        out = io.StringIO()
        captured: dict = {}
        from karox import cli as cli_mod

        def fake_main(argv):
            captured["argv"] = list(argv)
            return 0

        with patch.object(cli_mod, "main", side_effect=fake_main):
            code = tui._run_cli(["bridge", "list", "--json"], out.write)
        self.assertEqual(code, 0)
        self.assertEqual(captured["argv"], ["bridge", "list", "--json"])

    def test_raw_argv_input_is_forwarded(self) -> None:
        # Non-slash input is treated as raw CLI argv.
        argv = tui._slash_to_argv("bridge list --json", None, "/repo")
        self.assertIsNone(argv)  # not a slash command
        # The loop itself parses raw input with shlex; verify that path.
        import io
        out = io.StringIO()
        code = tui.run_tui(
            session_id=None, repository="/repo",
            input_stream=io.StringIO("bridge list --json\n/exit\n"),
            output_stream=out,
        )
        self.assertEqual(code, 0)
        # The backend wrote its output (the bridge list JSON).
        self.assertIn("notion", out.getvalue())


if __name__ == "__main__":
    unittest.main()
