"""Tests for the one-command ChatGPT/Claude web bridge launcher."""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from _support import SRC  # noqa: F401 - inserts src on sys.path

from karox.cli import main
from karox.models import AccessProfile
from karox.web_bridge_launcher import (
    DEFAULT_WEB_TOOLS,
    WRITE_WEB_TOOLS,
    WebBridgeConnectConfig,
    WebBridgeLaunchError,
    _bridge_argv,
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


if __name__ == "__main__":
    unittest.main()
