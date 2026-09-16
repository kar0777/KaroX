"""Regression tests for the ``karox`` default dispatch.

``karox`` with no arguments must open the interactive full-screen TUI when
both standard streams are live terminals, and fall back to the line-mode
reader (which exits cleanly on EOF) only for redirected streams. The rest of
the CLI surface -- ``--help``, ``--version``, subcommands, the console-script
entry point, the provider registry, and the Phase-1 hosted bridge tools --
must keep working and must never be bypassed by the no-args dispatch.
"""

from __future__ import annotations

import contextlib
import os
import importlib.metadata as md
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _support import SRC  # noqa: F401 - inserts src on sys.path

import karox.tui as tui
from karox.entrypoint import main as entrypoint_main


class _FakeTTYIn:
    """A stdin stand-in that reports itself as a terminal."""

    def __init__(self, data: str = "") -> None:
        self._buf = io.StringIO(data)

    def isatty(self) -> bool:
        return True

    def readline(self) -> str:  # pragma: no cover - exercised only on wrong branch
        return self._buf.readline()

    def read(self, *args, **kwargs) -> str:  # pragma: no cover
        return self._buf.read(*args, **kwargs)


class _FakeTTYOut:
    """A stdout stand-in that reports itself as a terminal and captures writes."""

    def __init__(self) -> None:
        self._buf = io.StringIO()

    def isatty(self) -> bool:
        return True

    def write(self, text: str) -> int:
        return self._buf.write(text)

    def flush(self) -> None:  # pragma: no cover
        pass

    def getvalue(self) -> str:
        return self._buf.getvalue()


class _PipedIn:
    """A redirected stdin (pipe / EOF) -- not a terminal."""

    def __init__(self, data: str = "") -> None:
        self._buf = io.StringIO(data)

    def isatty(self) -> bool:
        return False

    def readline(self) -> str:
        return self._buf.readline()


class _PipedOut:
    def __init__(self) -> None:
        self._buf = io.StringIO()

    def isatty(self) -> bool:
        return False

    def write(self, text: str) -> int:
        return self._buf.write(text)

    def flush(self) -> None:  # pragma: no cover
        pass

    def getvalue(self) -> str:
        return self._buf.getvalue()


class DefaultDispatchTests(unittest.TestCase):
    """``karox`` (no args) must route to the TUI launcher for an interactive
    console and to line mode for a redirected one."""

    def test_no_args_dispatches_to_fullscreen_tui(self) -> None:
        # An interactive console (stdin/stdout both tty) must launch the
        # Textual app, never line mode. The dispatch decision itself is not
        # mocked: only the blocking ``KaroXApp.run`` is stubbed so the test
        # does not hang in the event loop.
        if not tui._HAS_TEXTUAL:
            self.skipTest("textual not installed")
        sin, sout = _FakeTTYIn(), _FakeTTYOut()
        launched = {"run": False}

        def fake_run(self_app, *args, **kwargs):
            launched["run"] = True
            return 0

        def line_must_not_run(*args, **kwargs):
            raise AssertionError("line mode selected for an interactive console")

        with patch.object(tui.KaroXApp, "run", fake_run), \
                patch.object(tui, "_run_line_mode", line_must_not_run), \
                patch("sys.stdin", sin), patch("sys.stdout", sout):
            rc = entrypoint_main([])

        self.assertTrue(launched["run"], "no-args dispatch must launch KaroXApp")
        self.assertEqual(rc, 0)

    def test_redirected_stdin_dispatches_to_line_mode_and_exits_cleanly(self) -> None:
        # Redirected (non-tty) stdin must use line mode and return 0 on EOF,
        # not attempt a full-screen UI on a pipe.
        sin, sout = _PipedIn(""), _PipedOut()
        stack = contextlib.ExitStack()
        stack.enter_context(patch("sys.stdin", sin))
        stack.enter_context(patch("sys.stdout", sout))
        if tui._HAS_TEXTUAL:
            def boom(self_app, *args, **kwargs):  # noqa: E306
                raise AssertionError("TUI must not launch for redirected stdin")
            stack.enter_context(patch.object(tui.KaroXApp, "run", boom))
        with stack:
            rc = entrypoint_main([])
        self.assertEqual(rc, 0)
        self.assertIn("KaroX line mode", sout.getvalue())


class CliSurfaceTests(unittest.TestCase):
    """The documented CLI surface must keep working and must not be bypassed
    by the no-args TUI dispatch."""

    def test_help_does_not_launch_tui(self) -> None:
        out = io.StringIO()
        with patch.object(tui, "run_tui", side_effect=AssertionError) as fake:
            with contextlib.redirect_stdout(out):
                rc = entrypoint_main(["--help"])
        self.assertEqual(rc, 0)
        self.assertFalse(fake.called)
        self.assertIn("KaroX — guarded local AI coding workspace", out.getvalue())

    def test_version_does_not_launch_tui(self) -> None:
        out = io.StringIO()
        with patch.object(tui, "run_tui", side_effect=AssertionError) as fake:
            with contextlib.redirect_stdout(out):
                with self.assertRaises(SystemExit):
                    entrypoint_main(["--version"])
        self.assertFalse(fake.called)
        self.assertIn("karox", out.getvalue())

    def test_subcommand_dispatches_without_tui(self) -> None:
        out = io.StringIO()
        with patch.object(tui, "run_tui", side_effect=AssertionError) as fake:
            with contextlib.redirect_stdout(out):
                rc = entrypoint_main(["paths", "--json"])
        self.assertEqual(rc, 0)
        self.assertFalse(fake.called)
        self.assertIn("config_dir", out.getvalue())

    def test_dispatch_does_not_swallow_tui_errors(self) -> None:
        # An exception from the TUI launcher must propagate, not be swallowed
        # by the dispatch path.
        def boom(*args, **kwargs):
            raise RuntimeError("boom-from-tui")

        with patch.object(tui, "run_tui", boom):
            with self.assertRaises(RuntimeError):
                entrypoint_main([])

    def test_console_entrypoint_is_callable(self) -> None:
        import karox.entrypoint as ep

        self.assertTrue(callable(ep.main))

    def test_pyproject_declares_karox_console_script(self) -> None:
        names = {e.name for e in md.entry_points(group="console_scripts")}
        self.assertIn("karox", names)

    def test_provider_list_command_and_builtin_catalog(self) -> None:
        # On a clean config the configured-provider list is legitimately
        # empty; asserting non-empty used to pass only against a developer
        # machine's real configuration -- the exact dependency the isolation
        # sandbox removes. The dispatch contract worth pinning: the command
        # succeeds, returns JSON, and the built-in preset catalog is intact.
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"KAROX_CONFIG_DIR": tmp, "KAROX_VNEXT_CONFIG_DIR": tmp},
        ), contextlib.redirect_stdout(out):
            rc = entrypoint_main(["provider", "list", "--json"])
        self.assertEqual(rc, 0)
        data = json.loads(out.getvalue())
        self.assertIsInstance(data, list)
        from karox.provider_presets import provider_presets

        self.assertIn("empiriolabs", {p.preset_id for p in provider_presets()})

    def test_new_bridge_tools_registered_and_imports_clean(self) -> None:
        from karox.hosted_bridge import KNOWN_HOSTED_TOOL_NAMES

        for name in (
            "karox.browser.open",
            "karox.dev_server.start",
            "karox.artifact.get",
            "karox.checks.run",
        ):
            self.assertIn(name, KNOWN_HOSTED_TOOL_NAMES)
        # Importing the new Phase-1 runtime modules must not raise.
        import karox.hosted_tools_runtime  # noqa: F401
        import karox.browser_session  # noqa: F401
        import karox.artifacts  # noqa: F401


class ManagedBridgeToolSurfaceTests(unittest.TestCase):
    """The TUI-managed web bridge must surface the Phase-1 browser/dev-server/
    checks/artifact tools to the external agent, passing --write and
    --verification-command as needed."""

    def test_input_tools_trigger_write_flag(self) -> None:
        # Input/dev-server tools are MUTATING_WEB_TOOLS: the wizard must
        # persist a saved profile that cannot launch READ_ONLY, and the
        # selected tools must survive into the profile the bridge child is
        # launched from (the modern equivalent of passing --write).
        from karox.web_bridge_profiles import WebBridgeProfileError

        with patch("karox.web_bridge_profiles.WebBridgeProfileStore") as store:
            store.return_value.get.side_effect = WebBridgeProfileError(
                "saved bridge profile does not exist: test"
            )
            tui._persist_tui_saved_bridge_profile(
                Path.cwd(),
                tui.BridgeSetup(
                    "chatgpt-web",
                    9880,
                    (
                        "karox.repo.read_file",
                        "karox.browser.snapshot",
                        "karox.browser.open",
                    ),
                    tunnel_provider="cloudflare",
                ),
                language="en",
            )
        saved = store.return_value.put.call_args.args[0]
        self.assertEqual(saved.access_profile, tui.AccessProfile.WORKSPACE_WRITE)
        self.assertIn("karox.browser.snapshot", saved.tools)
        self.assertIn("karox.browser.open", saved.tools)

    def test_read_only_tools_do_not_trigger_write_flag(self) -> None:
        # An observation-only selection contains no MUTATING_WEB_TOOLS, so
        # the wizard write decision stays off and a READ_ONLY config launches
        # with exactly these observation tools exposed (the modern equivalent
        # of the launcher not passing --write).
        from karox.web_bridge_launcher import (
            MUTATING_WEB_TOOLS,
            WebBridgeConnectConfig,
            _bridge_argv,
        )

        selection = (
            "karox.repo.read_file",
            "karox.browser.snapshot",
            "karox.browser.screenshot",
            "karox.dev_server.status",
        )
        self.assertFalse(any(tool in MUTATING_WEB_TOOLS for tool in selection))
        config = WebBridgeConnectConfig(
            profile="chatgpt-web",
            repository=Path.cwd(),
            port=9881,
            tools=selection,
            access_profile=tui.AccessProfile.READ_ONLY,
            tunnel="cloudflare",
        )
        argv = _bridge_argv(
            config, session_id="dispatch-ro", public_url="https://e2e.example"
        )
        self.assertIn("karox.browser.screenshot", argv)
        self.assertIn("karox.dev_server.status", argv)
        self.assertNotIn("--write", argv)

    def test_checks_run_passes_verification_command(self) -> None:
        # Selecting checks.run persists a verification allowlist, and the argv
        # the saved profile launches with serializes each command as a JSON
        # array of strings (an allowlist, not arbitrary shell).
        from karox.web_bridge_launcher import WebBridgeConnectConfig, _bridge_argv
        from karox.web_bridge_profiles import WebBridgeProfileError

        with patch("karox.web_bridge_profiles.WebBridgeProfileStore") as store:
            store.return_value.get.side_effect = WebBridgeProfileError(
                "saved bridge profile does not exist: test"
            )
            tui._persist_tui_saved_bridge_profile(
                Path.cwd(),
                tui.BridgeSetup(
                    "chatgpt-web",
                    9882,
                    ("karox.repo.read_file", "karox.checks.run"),
                    tunnel_provider="cloudflare",
                ),
                language="en",
            )
        saved = store.return_value.put.call_args.args[0]
        self.assertIn("karox.checks.run", saved.tools)
        self.assertTrue(saved.verification_commands)

        config = WebBridgeConnectConfig(
            profile="chatgpt-web",
            repository=Path.cwd(),
            port=9882,
            tools=tuple(saved.tools),
            access_profile=saved.access_profile,
            tunnel="cloudflare",
            verification_commands=tuple(
                tuple(command) for command in saved.verification_commands
            ),
        )
        argv = _bridge_argv(
            config, session_id="dispatch-test", public_url="https://e2e.example"
        )
        self.assertIn("karox.checks.run", argv)
        self.assertIn("--verification-command", argv)
        idx = argv.index("--verification-command")
        vc = json.loads(argv[idx + 1])
        self.assertIsInstance(vc, list)
        self.assertTrue(all(isinstance(s, str) and s for s in vc))

    def test_checks_run_without_verification_command_in_argv_is_rejected(self):
        # The launcher validates that karox.checks.run needs a verification
        # command, so the TUI must always pass one when checks is selected.
        from karox.web_bridge_launcher import WebBridgeConnectConfig

        with self.assertRaises(ValueError):
            WebBridgeConnectConfig(
                profile="chatgpt-web",
                repository=Path.cwd(),
                port=9883,
                tools=("karox.repo.read_file", "karox.checks.run"),
                session_id="s1",
                access_profile=None,
                tunnel="cloudflare",
            )


if __name__ == "__main__":
    unittest.main()
