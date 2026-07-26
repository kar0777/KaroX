"""Tests for the one-command ChatGPT/Claude web bridge launcher."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from _support import SRC

from karox.cli import main
from karox.models import AccessProfile
from karox.web_bridge_launcher import (
    DEFAULT_WEB_TOOLS,
    WRITE_WEB_TOOLS,
    WebBridgeConnectConfig,
    WebBridgeLaunchError,
    _bridge_argv,
    _child_options,
    ephemeral_url_warning,
    parent_death_hook,
    run_web_bridge,
    start_cloudflare_quick_tunnel,
)


class _FakeProcess:
    def __init__(self, output: str, *, exit_code: int | None = None) -> None:
        self.stdout = io.StringIO(output)
        self._exit_code = exit_code
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        if self.terminated or self.killed:
            return 0
        return self._exit_code

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return self.poll() or 0


class CloudflareQuickTunnelTests(unittest.TestCase):
    def test_quick_tunnel_parses_public_url_and_owns_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "cloudflared.exe"
            executable.touch()
            process = _FakeProcess(
                "INF Requesting new quick Tunnel\n"
                "INF | https://small-tree-123.trycloudflare.com |\n"
            )
            calls: list[tuple[object, ...]] = []

            def popen(*args: object, **kwargs: object) -> _FakeProcess:
                calls.append((args, kwargs))
                return process

            tunnel = start_cloudflare_quick_tunnel(
                9123,
                executable=str(executable),
                popen=popen,
            )
            self.assertEqual(
                tunnel.public_url,
                "https://small-tree-123.trycloudflare.com",
            )
            argv = calls[0][0][0]
            self.assertEqual(
                argv,
                [
                    str(executable.resolve()),
                    "tunnel",
                    "--url",
                    "http://127.0.0.1:9123",
                    "--no-autoupdate",
                ],
            )
            tunnel.stop()
            self.assertTrue(process.terminated)

    def test_quick_tunnel_reports_early_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "cloudflared.exe"
            executable.touch()
            process = _FakeProcess("ERR tunnel failed\n", exit_code=7)
            with self.assertRaisesRegex(
                WebBridgeLaunchError,
                r"exited with code 7: ERR tunnel failed",
            ):
                start_cloudflare_quick_tunnel(
                    9123,
                    executable=str(executable),
                    popen=lambda *args, **kwargs: process,
                )


class WebBridgeConfigTests(unittest.TestCase):
    def test_custom_tunnel_requires_https_origin(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires --public-url"):
            WebBridgeConnectConfig(
                profile="chatgpt-web",
                repository=Path.cwd(),
                tunnel="custom",
            )
        with self.assertRaisesRegex(ValueError, "must be an HTTPS origin"):
            WebBridgeConnectConfig(
                profile="chatgpt-web",
                repository=Path.cwd(),
                tunnel="custom",
                public_url="https://example.com/mcp",
            )

    def test_bridge_argv_contains_managed_identity_and_tools(self) -> None:
        config = WebBridgeConnectConfig(
            profile="claude-web",
            repository=Path("repo"),
            tools=("karox.repo.read_file", "karox.repo.edit_file"),
            tunnel="custom",
            public_url="https://bridge.example.com",
            verification_commands=('["python", "-m", "pytest"]',),
        )
        argv = _bridge_argv(
            config,
            session_id="web-test",
            public_url="https://bridge.example.com",
        )
        self.assertIn("claude-web", argv)
        self.assertIn("web-test", argv)
        self.assertIn("https://bridge.example.com", argv)
        self.assertEqual(argv.count("--tool"), 2)
        self.assertEqual(argv.count("--verification-command"), 1)


class WebBridgeCliTests(unittest.TestCase):
    def test_connect_builds_managed_cloudflare_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("karox.cli.run_web_bridge", return_value=0) as launch:
                code = main(
                    (
                        "bridge",
                        "connect",
                        "chatgpt-web",
                        "--repository",
                        str(root),
                        "--write",
                    )
                )
        self.assertEqual(code, 0)
        config = launch.call_args.args[0]
        self.assertEqual(config.profile, "chatgpt-web")
        self.assertEqual(config.tunnel, "cloudflare")
        self.assertEqual(config.access_profile, AccessProfile.WORKSPACE_WRITE)
        self.assertEqual(config.tools[: len(DEFAULT_WEB_TOOLS)], DEFAULT_WEB_TOOLS)
        for tool in WRITE_WEB_TOOLS:
            self.assertIn(tool, config.tools)

    def test_connect_supports_cli_managed_custom_origin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("karox.cli.run_web_bridge", return_value=0) as launch:
                code = main(
                    (
                        "bridge",
                        "connect",
                        "claude-web",
                        "--repository",
                        str(root),
                        "--tunnel",
                        "custom",
                        "--public-url",
                        "https://bridge.example.com",
                        "--tool",
                        "karox.git.status",
                    )
                )
        self.assertEqual(code, 0)
        config = launch.call_args.args[0]
        self.assertEqual(config.profile, "claude-web")
        self.assertEqual(config.public_url, "https://bridge.example.com")
        self.assertEqual(config.tools, ("karox.git.status",))
        self.assertEqual(config.access_profile, AccessProfile.READ_ONLY)


class WebBridgeSupervisorTests(unittest.TestCase):
    def test_child_failure_stops_tunnel_and_revokes_temporary_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = Path(tmp)
            sessions = MagicMock()
            credentials = MagicMock()
            credentials.set.return_value = {"secret": "approval-secret"}
            tunnel = MagicMock()
            tunnel.public_url = "https://small-tree.trycloudflare.com"
            bridge = MagicMock()
            bridge.poll.return_value = 9
            with (
                patch(
                    "karox.web_bridge_launcher._port_is_available",
                    return_value=True,
                ),
                patch(
                    "karox.web_bridge_launcher.start_cloudflare_quick_tunnel",
                    return_value=tunnel,
                ),
                patch(
                    "karox.web_bridge_launcher.SessionStore",
                    return_value=sessions,
                ),
                patch(
                    "karox.web_bridge_launcher.BridgeCredentialStore",
                    return_value=credentials,
                ),
                patch(
                    "karox.web_bridge_launcher.subprocess.Popen",
                    return_value=bridge,
                ),
                patch("karox.web_bridge_launcher._wait_for_bridge"),
            ):
                with redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(
                        WebBridgeLaunchError,
                        "stopped unexpectedly with code 9",
                    ):
                        run_web_bridge(
                            WebBridgeConnectConfig(
                                profile="chatgpt-web",
                                repository=repository,
                            )
                        )
        session_id = sessions.create.call_args.kwargs["session_id"]
        credentials.delete.assert_called_once_with(session_id)
        sessions.revoke.assert_called_once_with(session_id)
        tunnel.stop.assert_called_once_with()

    def test_watchdog_records_the_tunnel_before_the_bridge_is_spawned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = root / "repo"
            repository.mkdir()
            tunnel = MagicMock()
            tunnel.public_url = "https://small-tree.trycloudflare.com"
            tunnel.process.pid = 4242
            credentials = MagicMock()
            credentials.set.return_value = {"secret": "approval-secret"}
            seen: dict[str, Any] = {}

            def popen(*args: object, **kwargs: object) -> None:
                # A kill landing here is the window the watchdog has to cover:
                # the tunnel is already public but the bridge does not exist yet.
                seen["records"] = [
                    json.loads(entry.read_text(encoding="utf-8"))
                    for entry in sorted((root / "web-bridge").glob("*.json"))
                ]
                raise OSError("cannot spawn")

            with (
                patch.dict(os.environ, {"KAROX_RUNTIME_DIR": str(root)}),
                patch(
                    "karox.web_bridge_launcher._port_is_available", return_value=True
                ),
                patch(
                    "karox.web_bridge_launcher.start_cloudflare_quick_tunnel",
                    return_value=tunnel,
                ),
                patch("karox.web_bridge_launcher.SessionStore", return_value=MagicMock()),
                patch(
                    "karox.web_bridge_launcher.BridgeCredentialStore",
                    return_value=credentials,
                ),
                patch("karox.web_bridge_launcher.subprocess.Popen", popen),
            ):
                with redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(
                        WebBridgeLaunchError, "cannot start KaroX bridge"
                    ):
                        run_web_bridge(
                            WebBridgeConnectConfig(
                                profile="chatgpt-web", repository=repository
                            )
                        )
        self.assertEqual(len(seen["records"]), 1)
        record = seen["records"][0]
        self.assertEqual(record["tunnel_pid"], 4242)
        self.assertIsNone(record["bridge_pid"])
        self.assertEqual(record["public_url"], "https://small-tree.trycloudflare.com")


class ParentDeathTests(unittest.TestCase):
    """A child must not outlive a hard kill of the launcher that owns it."""

    def test_child_options_carry_a_pdeathsig_hook_only_where_one_exists(self) -> None:
        options = _child_options()
        if sys.platform.startswith("linux"):
            self.assertTrue(callable(parent_death_hook()))
            self.assertIn("preexec_fn", options)
        else:
            # Windows uses the job object; macOS has no equivalent primitive, and
            # claiming one it does not have would be worse than the gap.
            self.assertIsNone(parent_death_hook())
            self.assertNotIn("preexec_fn", options)

    @unittest.skipUnless(
        sys.platform.startswith("linux"), "PR_SET_PDEATHSIG is Linux-only"
    )
    def test_a_hard_killed_launcher_takes_its_child_with_it(self) -> None:
        script = textwrap.dedent(
            f"""
            import os, subprocess, sys
            sys.path.insert(0, {str(SRC)!r})
            from karox.web_bridge_launcher import _child_options
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                **_child_options(),
            )
            print(child.pid, flush=True)
            os.kill(os.getpid(), 9)
            """
        )
        launcher = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=60,
        )
        pid = int(launcher.stdout.strip())
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        os.kill(pid, 9)
        self.fail("the child survived a hard kill of its launcher")


class EphemeralUrlWarningTests(unittest.TestCase):
    """A connector pasted with a throwaway URL breaks on the next restart.

    `chatgpt-web` and `claude-web` both declare `persistent_url=True` and nothing
    read that field, so KaroX printed a Quick Tunnel URL for them exactly as if it
    were permanent. When the bridge restarted the URL stopped existing and the
    connector failed on the user's side, with nothing here having warned them.
    """

    def test_a_web_profile_on_a_quick_tunnel_is_warned_about(self) -> None:
        for profile in ("chatgpt-web", "claude-web"):
            with self.subTest(profile=profile):
                note = ephemeral_url_warning(profile, None)
                self.assertIsNotNone(note)
                assert note is not None
                self.assertIn("temporary", note)
                self.assertIn("--public-url", note)

    def test_a_declared_origin_is_not_warned_about(self) -> None:
        """The note tells the user to do this, so it must go quiet once they have."""
        self.assertIsNone(
            ephemeral_url_warning("chatgpt-web", "https://mcp.example.com")
        )

    def test_a_profile_that_never_needed_a_stable_url_is_left_alone(self) -> None:
        for profile in ("promptql", "notion", "generic-streamable-http"):
            with self.subTest(profile=profile):
                self.assertIsNone(ephemeral_url_warning(profile, None))

    def test_an_unknown_profile_does_not_raise(self) -> None:
        self.assertIsNone(ephemeral_url_warning("not-a-profile", None))


if __name__ == "__main__":
    unittest.main()
