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

from karox.bridge import BridgeCredentialMissing
from karox.cli import _verification_command, main
from karox.credentials import CredentialError
from karox.mcp_client import McpServerRecord, McpToolDescriptor
from karox.models import AccessProfile
from karox.sessions import SessionStore
from karox.web_bridge_launcher import (
    DEFAULT_WEB_TOOLS,
    WRITE_WEB_TOOLS,
    TailscaleBackgroundFunnel,
    WebBridgeConnectConfig,
    WebBridgeLaunchError,
    _bridge_argv,
    _child_options,
    _consume_stop_request,
    _listener_belongs_to_process_tree,
    _mirror_child_output,
    _reclaim_orphaned_bridge_listener,
    _release_saved_bridge_owner_lock,
    _stop_request_path,
    _sync_external_mcp_selections,
    _try_acquire_saved_bridge_owner_lock,
    _wait_for_bridge,
    _write_stop_request,
    bundled_cloudflared,
    cloudflared_not_found_message,
    ephemeral_url_warning,
    find_cloudflared,
    parent_death_hook,
    run_web_bridge,
    start_cloudflare_quick_tunnel,
    web_bridge_connection_instructions,
    web_bridge_diagnostics,
    web_bridge_mcp_endpoint,
    web_bridge_mcp_path,
    windows_cloudflared_candidates,
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
    def test_stable_browser_command_is_read_capable_in_default_profile(self) -> None:
        config = WebBridgeConnectConfig(
            profile="chatgpt-web",
            repository=Path.cwd(),
        )
        self.assertIn("karox.browser.command", DEFAULT_WEB_TOOLS)
        self.assertNotIn("karox.browser.command", WRITE_WEB_TOOLS)
        diagnostics = web_bridge_diagnostics(config)
        self.assertTrue(diagnostics["browser_permission"]["read"])
        self.assertFalse(diagnostics["browser_permission"]["input"])

    def test_runtime_status_is_available_in_read_only_and_workspace_write_profiles(
        self,
    ) -> None:
        for access_profile in (
            AccessProfile.READ_ONLY,
            AccessProfile.WORKSPACE_WRITE,
        ):
            with self.subTest(access_profile=access_profile.value):
                diagnostics = web_bridge_diagnostics(
                    WebBridgeConnectConfig(
                        profile="chatgpt-web",
                        repository=Path.cwd(),
                        access_profile=access_profile,
                    )
                )
                self.assertIn("karox.runtime.status", diagnostics["available_tools"])
                disabled = {
                    item["name"] for item in diagnostics["disabled_tools"]
                }
                self.assertNotIn("karox.runtime.status", disabled)

    def test_chatgpt_diagnostics_advertise_stale_catalog_fallbacks(self) -> None:
        diagnostics = web_bridge_diagnostics(
            WebBridgeConnectConfig(
                profile="chatgpt-web",
                repository=Path.cwd(),
                access_profile=AccessProfile.ELEVATED,
            )
        )
        catalog = diagnostics["tool_catalog"]
        self.assertFalse(catalog["legacy_cached_catalog_compatible"])
        self.assertEqual(catalog["legacy_cached_catalog_mode"], "compatibility_fallbacks")
        self.assertTrue(catalog["catalog_refresh_required_for_new_tool_names"])
        self.assertIn("command.run", catalog["legacy_fallbacks"])
        self.assertIn("task.resume", catalog["legacy_fallbacks"])
        self.assertIn("browser.command", catalog["legacy_fallbacks"])

    def test_elevated_diagnostics_keep_global_remote_side_effect_blocks(self) -> None:
        diagnostics = web_bridge_diagnostics(
            WebBridgeConnectConfig(
                profile="chatgpt-web",
                repository=Path.cwd(),
                access_profile=AccessProfile.ELEVATED,
                tools=("karox.repo.read_file", "karox.command.run"),
            )
        )
        restrictions = diagnostics["mode_restrictions"]
        self.assertTrue(restrictions["no_git_push"])
        self.assertTrue(restrictions["no_publish"])
        self.assertTrue(restrictions["no_auth_commands"])
        self.assertTrue(restrictions["no_deploy_release"])

    def test_explicit_legacy_tool_families_keep_stable_worker_commands(self) -> None:
        config = WebBridgeConnectConfig(
            profile="chatgpt-web",
            repository=Path.cwd(),
            access_profile=AccessProfile.WORKSPACE_WRITE,
            tools=(
                "karox.repo.write_file",
                "karox.checks.run",
                "karox.browser.snapshot",
            ),
            verification_commands=((sys.executable, "-m", "pytest", "-q"),),
        )
        expected = {
            "karox.repo.command",
            "karox.tests.run",
            "karox.browser.command",
        }
        self.assertTrue(expected.issubset(config.tools))
        diagnostics = web_bridge_diagnostics(config)
        self.assertTrue(expected.issubset(diagnostics["available_tools"]))
        child_argv = _bridge_argv(
            config,
            session_id="web-stable-tools",
            public_url="https://bridge.example.com",
        )
        for name in expected:
            self.assertIn(name, child_argv)

    def test_read_only_browser_family_does_not_gain_workspace_commands(self) -> None:
        config = WebBridgeConnectConfig(
            profile="chatgpt-web",
            repository=Path.cwd(),
            tools=("karox.browser.snapshot",),
        )
        self.assertIn("karox.browser.command", config.tools)
        self.assertNotIn("karox.repo.command", config.tools)
        self.assertNotIn("karox.tests.run", config.tools)

    def test_a_saved_profile_drops_tools_the_profile_cannot_grant(self) -> None:
        """One impossible checkbox must not take the whole bridge down.

        A saved browser_control profile with dev-server tools used to crash
        the bridge at startup with "session profile does not allow
        process.run". The config now drops those names and diagnostics say
        exactly why they are not served.
        """

        config = WebBridgeConnectConfig(
            profile="chatgpt-web",
            repository=Path.cwd(),
            saved_profile_name="aura-browser",
            access_profile=AccessProfile.BROWSER_CONTROL,
            tools=(
                "karox.repo.read_file",
                "karox.browser.snapshot",
                "karox.dev_server.status",
                "karox.dev_server.logs",
            ),
        )
        self.assertIn("karox.repo.read_file", config.tools)
        self.assertIn("karox.browser.snapshot", config.tools)
        self.assertNotIn("karox.dev_server.status", config.tools)
        self.assertNotIn("karox.dev_server.logs", config.tools)
        self.assertIn("karox.dev_server.status", config.profile_denied_tools)
        diagnostics = web_bridge_diagnostics(config)
        disabled = {
            item["name"]: item["reason"] for item in diagnostics["disabled_tools"]
        }
        self.assertIn(
            "not allowed by the browser_control access profile",
            disabled["karox.dev_server.status"],
        )

    def test_an_explicit_cli_tool_list_is_not_silently_dropped(self) -> None:
        """Explicit `--tool` selection keeps the fail-closed startup error."""

        config = WebBridgeConnectConfig(
            profile="chatgpt-web",
            repository=Path.cwd(),
            access_profile=AccessProfile.READ_ONLY,
            tools=("karox.repo.read_file", "karox.repo.edit_file"),
        )
        self.assertIn("karox.repo.edit_file", config.tools)
        self.assertEqual(config.profile_denied_tools, {})

    def test_a_saved_profile_with_no_usable_tools_fails_at_config_time(self) -> None:
        with self.assertRaisesRegex(ValueError, "all incompatible"):
            WebBridgeConnectConfig(
                profile="chatgpt-web",
                repository=Path.cwd(),
                saved_profile_name="broken-profile",
                access_profile=AccessProfile.READ_ONLY,
                tools=("karox.repo.edit_file", "karox.dev_server.status"),
            )

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
            mcp_servers=("notion",),
            tunnel="custom",
            public_url="https://bridge.example.com",
            verification_commands=(("python", "-m", "pytest"),),
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
        self.assertEqual(argv.count("--server"), 1)
        self.assertEqual(argv[argv.index("--server") + 1], "notion")
        self.assertEqual(argv.count("--verification-command"), 1)
        index = argv.index("--verification-command")
        serialized = argv[index + 1]
        self.assertEqual(serialized, '["python","-m","pytest"]')
        self.assertEqual(_verification_command(serialized), ("python", "-m", "pytest"))

    def test_external_mcp_startup_sync_allows_only_read_only_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = root / "repo"
            repository.mkdir()
            sessions = SessionStore(root / "sessions")
            sessions.create(
                repository,
                "hosted bridge",
                AccessProfile.WORKSPACE_WRITE,
                session_id="web-mcp-sync",
            )
            server = McpServerRecord(
                server_id="notion",
                namespace="notion",
                transport="streamable_http",
                url="https://example.invalid/mcp",
            )
            tools = [
                McpToolDescriptor(
                    "notion", "notion", "search", "read", {"type": "object"}, "read-digest", True
                ),
                McpToolDescriptor(
                    "notion", "notion", "update", "write", {"type": "object"}, "write-digest", False
                ),
            ]
            registry = MagicMock()
            registry.get.return_value = server
            client = MagicMock()
            client.discover_record.return_value = tools
            config = WebBridgeConnectConfig(
                profile="chatgpt-web",
                repository=repository,
                tools=("karox.repo.read_file",),
                mcp_servers=("notion",),
            )
            with patch("karox.mcp_client.McpRegistry", return_value=registry), patch(
                "karox.mcp_client.McpClient", return_value=client
            ):
                _sync_external_mcp_selections(
                    config,
                    sessions,
                    "web-mcp-sync",
                    repository,
                )
            selection = sessions.load("web-mcp-sync").mcp_servers[0]
            self.assertEqual(selection["server_id"], "notion")
            self.assertEqual(selection["tools"]["search"]["permission"], "allow")
            self.assertEqual(selection["tools"]["update"]["permission"], "ask")


class WebBridgeCliTests(unittest.TestCase):
    def test_notion_bridge_argv_passes_oauth_public_url(self) -> None:
        config = WebBridgeConnectConfig(
            profile="notion",
            repository=Path("repo"),
            tools=("karox.repo.read_file",),
            tunnel="tailscale",
            port=8767,
        )
        argv = _bridge_argv(
            config,
            session_id="web-notion-test",
            public_url="https://notion.example.ts.net:8443",
        )
        self.assertIn("--public-url", argv)
        self.assertIn("https://notion.example.ts.net:8443", argv)
        self.assertIn("notion", argv)
        self.assertIn("8767", argv)

    def test_connect_builds_managed_cloudflare_config(self) -> None:
        expected_command = (
            "python",
            "scripts/run_v5_preflight.py",
            "--full",
            "--keep-going",
        )
        # Windows PowerShell 5.1 legacy native argv removes embedded JSON quotes
        # before Python receives this native-process argument.
        powershell_native_value = (
            "[python,scripts/run_v5_preflight.py,--full,--keep-going]"
        )
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
                        "--tunnel",
                        "cloudflare",
                        "--verification-command",
                        powershell_native_value,
                    )
                )
        self.assertEqual(code, 0)
        config = launch.call_args.args[0]
        self.assertEqual(config.profile, "chatgpt-web")
        self.assertEqual(config.tunnel, "cloudflare")
        self.assertEqual(config.access_profile, AccessProfile.WORKSPACE_WRITE)
        self.assertEqual(config.tools[: len(DEFAULT_WEB_TOOLS)], DEFAULT_WEB_TOOLS)
        self.assertEqual(config.verification_commands, (expected_command,))
        for tool in WRITE_WEB_TOOLS:
            if tool in {
                "karox.dev_server.start",
                "karox.dev_server.stop",
                "karox.dev_server.restart",
            }:
                self.assertNotIn(tool, config.tools)
            else:
                self.assertIn(tool, config.tools)
        self.assertEqual(config.server_profiles, ())

        child_argv = _bridge_argv(
            config,
            session_id="web-test",
            public_url="https://bridge.example.com",
        )
        command_index = child_argv.index("--verification-command")
        child_value = child_argv[command_index + 1]
        self.assertEqual(_verification_command(child_value), expected_command)
        self.assertEqual(json.loads(child_value), list(expected_command))
        escaped = r'[\"python\",\"-m\",\"pytest\"]'
        self.assertEqual(
            _verification_command(escaped), ("python", "-m", "pytest")
        )

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
            root = Path(tmp)
            repository = root / "repo"
            repository.mkdir()
            sessions = MagicMock()
            credentials = MagicMock()
            credentials.set.return_value = {"secret": "approval-secret"}
            tunnel = MagicMock()
            tunnel.public_url = "https://small-tree.trycloudflare.com"
            bridge = MagicMock()
            bridge.poll.return_value = 9
            with (
                # Without this the launcher reaps the developer's real watchdog
                # directory, and because BridgeCredentialStore is patched here the
                # reaper's own delete() lands on these mocks -- so an unrelated
                # leftover record made the single-call assertions below fail.
                patch.dict(
                    os.environ,
                    {
                        "KAROX_RUNTIME_DIR": str(root),
                        # paths.py prefers this spelling, so an inherited value
                        # would otherwise win over the line above.
                        "KAROX_VNEXT_RUNTIME_DIR": str(root),
                    },
                ),
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
                ) as popen,
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
                                access_profile=AccessProfile.BROWSER_CONTROL,
                                browser_external_https=True,
                                browser_headed=True,
                                browser_user_takeover=True,
                                tunnel="cloudflare",
                            )
                        )
        session_id = sessions.create.call_args.kwargs["session_id"]
        credentials.delete.assert_called_once_with(session_id)
        sessions.revoke.assert_called_once_with(session_id)
        tunnel.stop.assert_called_once_with()
        # Without these the child is handed a hidden Windows console and its
        # diagnostics are discarded, leaving the user only the exit code above.
        spawned = popen.call_args.kwargs
        self.assertEqual(spawned["stdout"], subprocess.PIPE)
        self.assertEqual(spawned["stderr"], subprocess.STDOUT)
        self.assertEqual(spawned["encoding"], "utf-8")
        self.assertEqual(spawned["env"]["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(spawned["env"]["KAROX_BROWSER_BACKEND"], "extension")

    def test_saved_bridge_keeps_local_service_when_initial_funnel_probe_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = root / "repo"
            repository.mkdir()
            sessions = MagicMock()
            sessions.state_path.return_value.exists.return_value = False
            credentials = MagicMock()
            credentials.resolve.return_value = "approval-secret"
            tunnel = MagicMock()
            tunnel.public_url = "https://stable.example.invalid"
            tunnel.process.poll.return_value = None
            bridge = MagicMock()
            bridge.pid = 9_999_101
            bridge.poll.return_value = None

            with (
                patch.dict(
                    os.environ,
                    {
                        "KAROX_RUNTIME_DIR": str(root),
                        "KAROX_VNEXT_RUNTIME_DIR": str(root),
                    },
                ),
                patch("karox.web_bridge_launcher._port_is_available", return_value=True),
                patch(
                    "karox.web_bridge_launcher.start_tailscale_background_funnel",
                    return_value=tunnel,
                ),
                patch("karox.web_bridge_launcher.SessionStore", return_value=sessions),
                patch(
                    "karox.web_bridge_launcher.BridgeCredentialStore",
                    return_value=credentials,
                ),
                patch("karox.web_bridge_launcher.subprocess.Popen", return_value=bridge),
                patch("karox.web_bridge_launcher._wait_for_bridge"),
                patch(
                    "karox.web_bridge_launcher._wait_for_public_mcp_route",
                    side_effect=WebBridgeLaunchError("Funnel is not ready"),
                ),
                patch("karox.web_bridge_launcher._consume_stop_request", return_value="stop"),
                patch(
                    "karox.saved_bridge_supervisor.ensure_saved_bridge_supervisor",
                    return_value=7777,
                ),
                patch(
                    "karox.saved_bridge_supervisor.set_saved_bridge_desired_running",
                ),
            ):
                with redirect_stdout(io.StringIO()):
                    code = run_web_bridge(
                        WebBridgeConnectConfig(
                            profile="chatgpt-web",
                            repository=repository,
                            saved_profile_name="chatgpt-durable",
                            tunnel="tailscale",
                        )
                    )

        self.assertEqual(code, 0)
        credentials.delete.assert_not_called()
        sessions.revoke.assert_not_called()

    def test_requested_restart_preserves_daemon_funnel_route(self) -> None:
        """Owner restart keeps the stable Tailscale route instead of tearing ingress down."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = root / "repo"
            repository.mkdir()
            sessions = MagicMock()
            sessions.state_path.return_value.exists.return_value = False
            credentials = MagicMock()
            credentials.resolve.return_value = "approval-secret"
            tunnel = TailscaleBackgroundFunnel(
                executable="tailscale",
                public_url="https://stable.example.invalid",
                port=8765,
            )
            bridge = MagicMock()
            bridge.pid = 9_999_102
            bridge.poll.return_value = None
            bridge.stdout = MagicMock()
            mirrored = MagicMock()
            mirrored.detail.return_value = ""
            mirrored.reader = MagicMock()

            with (
                patch.dict(
                    os.environ,
                    {
                        "KAROX_RUNTIME_DIR": str(root),
                        "KAROX_VNEXT_RUNTIME_DIR": str(root),
                    },
                ),
                patch("karox.web_bridge_launcher._port_is_available", return_value=True),
                patch(
                    "karox.web_bridge_launcher.start_tailscale_background_funnel",
                    return_value=tunnel,
                ),
                patch.object(TailscaleBackgroundFunnel, "stop") as stop_funnel,
                patch("karox.web_bridge_launcher.SessionStore", return_value=sessions),
                patch(
                    "karox.web_bridge_launcher.BridgeCredentialStore",
                    return_value=credentials,
                ),
                patch("karox.web_bridge_launcher.subprocess.Popen", return_value=bridge),
                patch(
                    "karox.web_bridge_launcher._mirror_child_output",
                    return_value=mirrored,
                ),
                patch("karox.web_bridge_launcher._wait_for_bridge"),
                patch("karox.web_bridge_launcher._wait_for_public_mcp_route"),
                patch(
                    "karox.web_bridge_launcher._consume_stop_request",
                    return_value="restart",
                ),
                patch(
                    "karox.saved_bridge_supervisor.ensure_saved_bridge_supervisor",
                    return_value=7777,
                ),
            ):
                with redirect_stdout(io.StringIO()):
                    code = run_web_bridge(
                        WebBridgeConnectConfig(
                            profile="chatgpt-web",
                            repository=repository,
                            saved_profile_name="chatgpt-durable",
                            tunnel="tailscale",
                        )
                    )

        self.assertEqual(code, 0)
        stop_funnel.assert_not_called()
        credentials.delete.assert_not_called()
        sessions.revoke.assert_not_called()

    def test_saved_bridge_recovers_local_child_without_rotating_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = root / "repo"
            repository.mkdir()
            sessions = MagicMock()
            sessions.state_path.return_value.exists.return_value = False
            credentials = MagicMock()
            credentials.resolve.return_value = "approval-secret"
            tunnel = MagicMock()
            tunnel.public_url = "https://stable.example.invalid"

            crashed = MagicMock()
            crashed.pid = 9_999_103
            crashed.poll.return_value = 9
            healthy = MagicMock()
            healthy.pid = 9_999_104
            healthy.poll.return_value = None

            with (
                patch.dict(
                    os.environ,
                    {
                        "KAROX_RUNTIME_DIR": str(root),
                        "KAROX_VNEXT_RUNTIME_DIR": str(root),
                    },
                ),
                patch("karox.web_bridge_launcher._port_is_available", return_value=True),
                patch(
                    "karox.web_bridge_launcher.start_cloudflare_quick_tunnel",
                    return_value=tunnel,
                ),
                patch("karox.web_bridge_launcher.SessionStore", return_value=sessions),
                patch(
                    "karox.web_bridge_launcher.BridgeCredentialStore",
                    return_value=credentials,
                ),
                patch(
                    "karox.web_bridge_launcher.subprocess.Popen",
                    side_effect=[crashed, healthy],
                ) as popen,
                patch("karox.web_bridge_launcher._wait_for_bridge") as wait_ready,
                patch(
                    "karox.web_bridge_launcher._consume_stop_request",
                    side_effect=[False, True],
                ),
                patch(
                    "karox.saved_bridge_supervisor.ensure_saved_bridge_supervisor",
                    return_value=7777,
                ) as ensure_supervisor,
                patch(
                    "karox.saved_bridge_supervisor.set_saved_bridge_desired_running",
                ) as desired_state,
            ):
                output = io.StringIO()
                with redirect_stdout(output):
                    code = run_web_bridge(
                        WebBridgeConnectConfig(
                            profile="hyperagent-web",
                            repository=repository,
                            saved_profile_name="hyperagent-durable",
                            tunnel="cloudflare",
                        )
                    )

        self.assertEqual(code, 0)
        self.assertEqual(popen.call_count, 2)
        self.assertEqual(wait_ready.call_count, 2)
        self.assertEqual(ensure_supervisor.call_count, 2)
        ensure_supervisor.assert_any_call("hyperagent-durable", desired_running=True)
        ensure_supervisor.assert_any_call("hyperagent-durable", desired_running=None)
        desired_state.assert_called_once_with("hyperagent-durable", False)
        self.assertIn("Local MCP child recovered (recovery #1)", output.getvalue())
        credentials.delete.assert_not_called()
        sessions.revoke.assert_not_called()
        tunnel.stop.assert_called_once_with()

    def test_saved_bridge_keeps_owner_alive_during_repeated_child_crashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = root / "repo"
            repository.mkdir()
            sessions = MagicMock()
            sessions.state_path.return_value.exists.return_value = False
            credentials = MagicMock()
            credentials.resolve.return_value = "approval-secret"
            tunnel = MagicMock()
            tunnel.public_url = "https://stable.example.invalid"

            crashed_children = []
            for pid in range(4201, 4207):
                child = MagicMock()
                child.pid = pid
                child.poll.return_value = 9
                crashed_children.append(child)
            healthy = MagicMock()
            healthy.pid = 9_999_105
            healthy.poll.return_value = None

            with (
                patch.dict(
                    os.environ,
                    {
                        "KAROX_RUNTIME_DIR": str(root),
                        "KAROX_VNEXT_RUNTIME_DIR": str(root),
                    },
                ),
                patch("karox.web_bridge_launcher._port_is_available", return_value=True),
                patch(
                    "karox.web_bridge_launcher.start_cloudflare_quick_tunnel",
                    return_value=tunnel,
                ),
                patch("karox.web_bridge_launcher.SessionStore", return_value=sessions),
                patch(
                    "karox.web_bridge_launcher.BridgeCredentialStore",
                    return_value=credentials,
                ),
                patch(
                    "karox.web_bridge_launcher.subprocess.Popen",
                    side_effect=[*crashed_children, healthy],
                ) as popen,
                patch("karox.web_bridge_launcher._wait_for_bridge"),
                patch(
                    "karox.web_bridge_launcher._consume_stop_request",
                    side_effect=[False] * 6 + [True],
                ),
                patch(
                    "karox.saved_bridge_supervisor.ensure_saved_bridge_supervisor",
                    return_value=7777,
                ),
                patch(
                    "karox.saved_bridge_supervisor.set_saved_bridge_desired_running",
                ),
            ):
                with redirect_stdout(io.StringIO()):
                    code = run_web_bridge(
                        WebBridgeConnectConfig(
                            profile="hyperagent-web",
                            repository=repository,
                            saved_profile_name="hyperagent-durable",
                            tunnel="cloudflare",
                        )
                    )

        self.assertEqual(code, 0)
        self.assertEqual(popen.call_count, 7)
        credentials.delete.assert_not_called()
        sessions.revoke.assert_not_called()

    def test_saved_bridge_survives_a_replacement_child_that_cannot_bind(self) -> None:
        """A replacement that fails to bind must not end the durable session.

        The dying child can still hold the port for a moment, so the replacement
        loses the bind. That used to raise out of the owner's loop: the public
        route, the OAuth identity and every MCP session went with it, and the
        sibling supervisor had to rebuild the whole lifecycle.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = root / "repo"
            repository.mkdir()
            sessions = MagicMock()
            sessions.state_path.return_value.exists.return_value = False
            credentials = MagicMock()
            credentials.resolve.return_value = "approval-secret"
            tunnel = MagicMock()
            tunnel.public_url = "https://stable.example.invalid"

            crashed = MagicMock()
            crashed.pid = 9_999_106
            crashed.poll.return_value = 9
            unbindable = MagicMock()
            unbindable.pid = 9_999_107
            unbindable.poll.return_value = 1
            healthy = MagicMock()
            healthy.pid = 9_999_108
            healthy.poll.return_value = None

            with (
                patch.dict(
                    os.environ,
                    {
                        "KAROX_RUNTIME_DIR": str(root),
                        "KAROX_VNEXT_RUNTIME_DIR": str(root),
                    },
                ),
                patch(
                    "karox.web_bridge_launcher._CHILD_RESPAWN_MIN_BACKOFF_SECONDS", 0.0
                ),
                patch("karox.web_bridge_launcher._port_is_available", return_value=True),
                patch(
                    "karox.web_bridge_launcher.start_cloudflare_quick_tunnel",
                    return_value=tunnel,
                ),
                patch("karox.web_bridge_launcher.SessionStore", return_value=sessions),
                patch(
                    "karox.web_bridge_launcher.BridgeCredentialStore",
                    return_value=credentials,
                ),
                patch(
                    "karox.web_bridge_launcher.subprocess.Popen",
                    side_effect=[crashed, unbindable, healthy],
                ) as popen,
                patch(
                    "karox.web_bridge_launcher._wait_for_bridge",
                    side_effect=[
                        None,
                        WebBridgeLaunchError("KaroX bridge did not open its local port"),
                        None,
                    ],
                ),
                patch(
                    "karox.web_bridge_launcher._consume_stop_request",
                    side_effect=[False, False, True],
                ),
                patch(
                    "karox.saved_bridge_supervisor.ensure_saved_bridge_supervisor",
                    return_value=7777,
                ),
                patch(
                    "karox.saved_bridge_supervisor.set_saved_bridge_desired_running",
                ),
            ):
                output = io.StringIO()
                with redirect_stdout(output):
                    code = run_web_bridge(
                        WebBridgeConnectConfig(
                            profile="hyperagent-web",
                            repository=repository,
                            saved_profile_name="hyperagent-durable",
                            tunnel="cloudflare",
                        )
                    )

        self.assertEqual(code, 0)
        self.assertEqual(popen.call_count, 3)
        self.assertIn(
            "Replacement MCP child did not become ready", output.getvalue()
        )
        self.assertIn("Local MCP child recovered", output.getvalue())
        credentials.delete.assert_not_called()
        sessions.revoke.assert_not_called()

    def test_owner_heartbeat_keeps_running_while_a_repair_blocks(self) -> None:
        """A blocking repair must not look like a hung owner.

        The supervisor force-kills an owner whose watchdog heartbeat is older than
        ``OWNER_HEARTBEAT_STALE_SECONDS``. While the heartbeat was written only at
        the top of the main loop, waiting on a respawned child or on Funnel
        propagation was indistinguishable from a hang.
        """
        observed: list[float] = []

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = root / "repo"
            repository.mkdir()
            sessions = MagicMock()
            sessions.state_path.return_value.exists.return_value = False
            credentials = MagicMock()
            credentials.resolve.return_value = "approval-secret"
            tunnel = MagicMock()
            tunnel.public_url = "https://stable.example.invalid"
            child = MagicMock()
            child.pid = 9_999_109
            child.poll.return_value = None

            def blocking_wait(*_args: Any, **_kwargs: Any) -> None:
                # Stand in for the real blocking repair work and sample the
                # heartbeat the owner publishes while it is busy.
                for _ in range(2):
                    time.sleep(1.3)
                    record = json.loads(
                        list((root / "web-bridge").glob("*.json"))[0].read_text(
                            encoding="utf-8"
                        )
                    )
                    observed.append(float(record["owner_heartbeat_at"]))

            with (
                patch.dict(
                    os.environ,
                    {
                        "KAROX_RUNTIME_DIR": str(root),
                        "KAROX_VNEXT_RUNTIME_DIR": str(root),
                    },
                ),
                patch("karox.web_bridge_launcher._port_is_available", return_value=True),
                patch(
                    "karox.web_bridge_launcher.start_cloudflare_quick_tunnel",
                    return_value=tunnel,
                ),
                patch("karox.web_bridge_launcher.SessionStore", return_value=sessions),
                patch(
                    "karox.web_bridge_launcher.BridgeCredentialStore",
                    return_value=credentials,
                ),
                patch(
                    "karox.web_bridge_launcher.subprocess.Popen", return_value=child
                ),
                patch(
                    "karox.web_bridge_launcher._wait_for_bridge",
                    side_effect=blocking_wait,
                ),
                patch(
                    "karox.web_bridge_launcher._consume_stop_request",
                    return_value=True,
                ),
                patch(
                    "karox.saved_bridge_supervisor.ensure_saved_bridge_supervisor",
                    return_value=7777,
                ),
                patch(
                    "karox.saved_bridge_supervisor.set_saved_bridge_desired_running",
                ),
            ):
                with redirect_stdout(io.StringIO()):
                    code = run_web_bridge(
                        WebBridgeConnectConfig(
                            profile="hyperagent-web",
                            repository=repository,
                            saved_profile_name="hyperagent-durable",
                            tunnel="cloudflare",
                        )
                    )

        self.assertEqual(code, 0)
        self.assertEqual(len(observed), 2)
        self.assertGreater(observed[1], observed[0])

    def test_an_owner_that_dies_records_the_reason_it_died(self) -> None:
        """A detached owner writes to DEVNULL, so the reason must reach disk."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = root / "repo"
            repository.mkdir()
            sessions = MagicMock()
            sessions.state_path.return_value.exists.return_value = False
            credentials = MagicMock()
            credentials.resolve.return_value = "approval-secret"
            tunnel = MagicMock()
            tunnel.public_url = "https://stable.example.invalid"
            child = MagicMock()
            child.pid = 9_999_110
            child.poll.return_value = None

            with (
                patch.dict(
                    os.environ,
                    {
                        "KAROX_RUNTIME_DIR": str(root),
                        "KAROX_VNEXT_RUNTIME_DIR": str(root),
                    },
                ),
                patch("karox.web_bridge_launcher._port_is_available", return_value=True),
                patch(
                    "karox.web_bridge_launcher.start_cloudflare_quick_tunnel",
                    return_value=tunnel,
                ),
                patch("karox.web_bridge_launcher.SessionStore", return_value=sessions),
                patch(
                    "karox.web_bridge_launcher.BridgeCredentialStore",
                    return_value=credentials,
                ),
                patch(
                    "karox.web_bridge_launcher.subprocess.Popen", return_value=child
                ),
                patch("karox.web_bridge_launcher._wait_for_bridge"),
                patch(
                    "karox.web_bridge_launcher._consume_stop_request",
                    side_effect=RuntimeError("watchdog store is unreadable"),
                ),
                patch(
                    "karox.saved_bridge_supervisor.ensure_saved_bridge_supervisor",
                    return_value=7777,
                ),
                patch(
                    "karox.saved_bridge_supervisor.set_saved_bridge_desired_running",
                ),
            ):
                with redirect_stdout(io.StringIO()):
                    with self.assertRaises(RuntimeError):
                        run_web_bridge(
                            WebBridgeConnectConfig(
                                profile="hyperagent-web",
                                repository=repository,
                                saved_profile_name="hyperagent-durable",
                                tunnel="cloudflare",
                            )
                        )

            records = list((root / "web-bridge").glob("*.last-exit.json"))
            self.assertEqual(len(records), 1)
            payload = json.loads(records[0].read_text(encoding="utf-8"))

        self.assertEqual(payload["reason"], "RuntimeError")
        self.assertIn("watchdog store is unreadable", payload["detail"])
        self.assertEqual(payload["saved_profile"], "hyperagent-durable")

    def test_watchdog_records_the_tunnel_before_the_bridge_is_spawned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = root / "repo"
            repository.mkdir()
            tunnel = MagicMock()
            tunnel.public_url = "https://small-tree.trycloudflare.com"
            tunnel.process.pid = 9_999_111
            credentials = MagicMock()
            # Absence, not an unreadable backend: the launcher may mint a first
            # token here, whereas a plain CredentialError must fail closed.
            credentials.resolve.side_effect = BridgeCredentialMissing("missing")
            credentials.set.return_value = {"secret": "approval-secret"}
            sessions = MagicMock()
            sessions.state_path.return_value.exists.return_value = False
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
                patch.dict(
                    os.environ,
                    {
                        "KAROX_RUNTIME_DIR": str(root),
                        # paths.py prefers this spelling, so an inherited value
                        # would otherwise win over the line above.
                        "KAROX_VNEXT_RUNTIME_DIR": str(root),
                    },
                ),
                patch(
                    "karox.web_bridge_launcher._port_is_available", return_value=True
                ),
                patch(
                    "karox.web_bridge_launcher.start_cloudflare_quick_tunnel",
                    return_value=tunnel,
                ),
                patch("karox.web_bridge_launcher.SessionStore", return_value=sessions),
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
                                profile="chatgpt-web",
                                repository=repository,
                                saved_profile_name="durable-dev",
                                tunnel="cloudflare",
                            )
                        )
        self.assertEqual(len(seen["records"]), 1)
        record = seen["records"][0]
        self.assertEqual(record["tunnel_pid"], 9_999_111)
        self.assertIsNone(record["bridge_pid"])
        self.assertEqual(record["public_url"], "https://small-tree.trycloudflare.com")
        self.assertTrue(record["persistent_session"])
        session_id = sessions.create.call_args.kwargs["session_id"]
        credentials.delete.assert_called_once_with(session_id)
        sessions.revoke.assert_not_called()


class OrphanedListenerReclaimTests(unittest.TestCase):
    """Reclaiming a port is proof-gated and narrowly targeted.

    The listener left behind by a dead owner is this profile's own bridge child.
    It may be stopped -- but only after the live process re-proves its identity,
    and only that one PID.
    """

    def _run(
        self,
        *,
        proven: str | None = "web-saved-test",
        pid: int = 4242,
        port_free: bool = True,
    ):
        import itertools

        # Alive when first asked (so it is stopped), gone afterwards.
        liveness = itertools.chain([True], itertools.repeat(False))
        with patch(
            "karox.port_ownership.prove_bridge_process_identity",
            return_value=proven,
        ), patch(
            "karox.web_bridge_launcher._process_is_alive",
            side_effect=lambda _pid: next(liveness),
        ), patch(
            "karox.web_bridge_launcher._port_is_available", return_value=port_free
        ), patch(
            "karox.web_bridge_launcher.subprocess.run"
        ) as run, patch(
            "karox.web_bridge_launcher.os.kill"
        ) as kill:
            result = _reclaim_orphaned_bridge_listener(
                pid, port=8765, session_id="web-saved-test"
            )
        return result, run, kill

    def test_proven_orphan_is_stopped_and_the_port_is_reported_free(self) -> None:
        result, run, kill = self._run()
        self.assertTrue(result)
        stopped = run.call_count + kill.call_count
        self.assertEqual(stopped, 1, "exactly one targeted stop is expected")
        if run.call_count:
            argv = [str(token) for token in run.call_args.args[0]]
            self.assertIn("4242", argv)
            self.assertNotIn("/T", argv, "a descendant tree kill is never used")
        else:
            self.assertEqual(kill.call_args.args[0], 4242)

    def test_a_holder_that_cannot_be_proven_is_never_stopped(self) -> None:
        result, run, kill = self._run(proven=None)
        self.assertFalse(result)
        run.assert_not_called()
        kill.assert_not_called()

    def test_the_calling_process_is_never_stopped(self) -> None:
        result, run, kill = self._run(pid=os.getpid())
        self.assertFalse(result)
        run.assert_not_called()
        kill.assert_not_called()

    def test_a_port_that_stays_busy_is_reported_as_not_reclaimed(self) -> None:
        with patch(
            "karox.port_ownership.prove_bridge_process_identity",
            return_value="web-saved-test",
        ), patch(
            "karox.web_bridge_launcher._process_is_alive", return_value=True
        ), patch(
            "karox.web_bridge_launcher._port_is_available", return_value=False
        ), patch(
            "karox.web_bridge_launcher._ORPHAN_RECLAIM_TIMEOUT_SECONDS", 0.05
        ), patch(
            "karox.web_bridge_launcher.subprocess.run"
        ), patch(
            "karox.web_bridge_launcher.os.kill"
        ):
            self.assertFalse(
                _reclaim_orphaned_bridge_listener(
                    4242, port=8765, session_id="web-saved-test"
                )
            )


class CloudflaredLookupTests(unittest.TestCase):
    """A missing cloudflared has to be diagnosable from the message alone."""

    def test_the_message_names_where_it_looked_and_a_remedy_that_exists_here(
        self,
    ) -> None:
        message = cloudflared_not_found_message()
        self.assertIn(str(bundled_cloudflared()), message)
        self.assertIn("PATH", message)
        # The old wording told every user to rerun the KaroX installer. There is
        # no KaroX installer to rerun on macOS or Linux, so the remedy has to be
        # the one that exists on the platform reading it.
        expected = {
            "win32": "winget",
            "darwin": "brew",
        }.get(sys.platform, "distribution")
        self.assertIn(expected, message)

    def test_a_wrong_explicit_path_is_reported_as_that_path(self) -> None:
        # Distinct from "nothing found anywhere": the user gave a path and it is
        # the path that is wrong, which the old single message never said.
        message = cloudflared_not_found_message(r"C:\typo\cloudflared.exe")
        self.assertIn(r"C:\typo\cloudflared.exe", message)
        self.assertIn("--cloudflared", message)
        self.assertNotIn("Searched PATH", message)

    def test_the_bundled_path_is_where_the_installer_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                os.environ,
                {"KAROX_RUNTIME_DIR": tmp, "KAROX_VNEXT_RUNTIME_DIR": tmp},
            ):
                bundled = bundled_cloudflared()
                self.assertEqual(bundled.parent.name, "bin")
                self.assertEqual(bundled.parent.parent, Path(tmp).resolve())
                self.assertIsNone(find_cloudflared(str(bundled)))
                bundled.parent.mkdir(parents=True)
                bundled.write_bytes(b"binary")
                self.assertEqual(find_cloudflared(str(bundled)), str(bundled))

    @unittest.skipUnless(sys.platform == "win32", "WinGet is Windows-only")
    def test_finds_cloudflared_inside_the_winget_package_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp)
            executable = (
                local
                / "Microsoft"
                / "WinGet"
                / "Packages"
                / "Cloudflare.cloudflared_Microsoft.Winget.Source_test"
                / "cloudflared.exe"
            )
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"binary")
            with (
                patch.dict(
                    os.environ,
                    {
                        "LOCALAPPDATA": tmp,
                        "KAROX_RUNTIME_DIR": str(local / "karox-runtime"),
                    },
                ),
                patch("karox.web_bridge_launcher.shutil.which", return_value=None),
            ):
                self.assertIn(executable, windows_cloudflared_candidates())
                self.assertEqual(find_cloudflared(), str(executable))


class BridgeDiagnosticsTests(unittest.TestCase):
    """A bridge that refuses to start has to say why, not just return a number."""

    def test_existing_listener_is_not_accepted_as_the_new_child(self) -> None:
        process = MagicMock()
        process.pid = 9_999_112
        process.poll.side_effect = [None, 3]
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.__exit__.return_value = False
        with (
            patch("karox.web_bridge_launcher.socket.create_connection", return_value=connection),
            patch(
                "karox.web_bridge_launcher._listener_belongs_to_process_tree",
                return_value=False,
            ),
        ):
            with self.assertRaisesRegex(WebBridgeLaunchError, "exited with code 3"):
                _wait_for_bridge(process, 8768, timeout_seconds=1.0)

    def test_stop_request_cannot_be_stolen_by_a_duplicate_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                "KAROX_RUNTIME_DIR": tmp,
                "KAROX_VNEXT_RUNTIME_DIR": tmp,
            },
        ):
            request = _write_stop_request("session-duplicate-owner", 111)
            self.assertIsNone(_consume_stop_request("session-duplicate-owner", 222))
            self.assertTrue(request.exists())
            self.assertEqual(
                _consume_stop_request("session-duplicate-owner", 111),
                "stop",
            )
            self.assertFalse(request.exists())

    def test_restart_stop_request_preserves_distinct_lifecycle_intent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                "KAROX_RUNTIME_DIR": tmp,
                "KAROX_VNEXT_RUNTIME_DIR": tmp,
            },
        ):
            request = _write_stop_request(
                "session-restart-owner",
                333,
                intent="restart",
            )
            payload = json.loads(request.read_text(encoding="utf-8"))
            self.assertEqual(payload["intent"], "restart")
            self.assertEqual(
                _consume_stop_request("session-restart-owner", 333),
                "restart",
            )
            self.assertFalse(request.exists())

    def test_legacy_stop_request_without_intent_fails_closed_to_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                "KAROX_RUNTIME_DIR": tmp,
                "KAROX_VNEXT_RUNTIME_DIR": tmp,
            },
        ):
            path = _stop_request_path("session-legacy-stop")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"owner_pid": 444}), encoding="utf-8")
            self.assertEqual(
                _consume_stop_request("session-legacy-stop", 444),
                "stop",
            )

    def test_saved_bridge_owner_lock_is_exclusive_and_released_with_handle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                "KAROX_RUNTIME_DIR": tmp,
                "KAROX_VNEXT_RUNTIME_DIR": tmp,
            },
        ):
            first = _try_acquire_saved_bridge_owner_lock("hyperagent-test")
            self.assertIsNotNone(first)
            try:
                second = _try_acquire_saved_bridge_owner_lock("hyperagent-test")
                self.assertIsNone(second)
            finally:
                _release_saved_bridge_owner_lock(first)
            third = _try_acquire_saved_bridge_owner_lock("hyperagent-test")
            self.assertIsNotNone(third)
            _release_saved_bridge_owner_lock(third)

    def test_listener_tree_probe_accepts_the_root_process_when_psutil_can_see_it(self) -> None:
        # The live branch is intentionally tiny: this guards the ownership helper
        # against accidentally rejecting the normal one-process uvicorn listener.
        with patch("psutil.net_connections") as connections:
            listener = MagicMock()
            listener.laddr.port = 8768
            listener.pid = os.getpid()
            connections.return_value = [listener]
            self.assertTrue(_listener_belongs_to_process_tree(8768, os.getpid()))

    def test_the_reason_a_bridge_child_exited_reaches_the_user(self) -> None:
        # A real child, because the defect is a real spawn: on Windows these are
        # started with CREATE_NO_WINDOW, and without redirected handles that gives
        # the child its own hidden console and throws away everything it printed.
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('OSError: address already in use\\n');"
                " sys.exit(2)",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            **_child_options(),
        )
        mirrored = io.StringIO()
        output = None
        try:
            with redirect_stdout(mirrored):
                output = _mirror_child_output(child, name="bridge")
                with self.assertRaisesRegex(
                    WebBridgeLaunchError,
                    r"exited with code 2: OSError: address already in use",
                ):
                    # Port 0 is never listening, so the wait can only end on the
                    # child's exit -- which is the path under test.
                    _wait_for_bridge(child, 0, timeout_seconds=10.0, output=output)
        finally:
            child.kill()
            child.wait(timeout=10)
            if output is not None:
                output.reader.join(timeout=5)
            assert child.stdout is not None
            child.stdout.close()
        self.assertIn(
            "[bridge] OSError: address already in use",
            mirrored.getvalue(),
        )

    def test_a_bridge_that_never_opens_its_port_is_not_waited_on_forever(self) -> None:
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            **_child_options(),
        )
        output = None
        try:
            output = _mirror_child_output(child, name="bridge")
            started = time.monotonic()
            with self.assertRaisesRegex(
                WebBridgeLaunchError, "did not open its local port"
            ):
                _wait_for_bridge(child, 0, timeout_seconds=0.5, output=output)
            # The drain thread cannot finish while the child lives, so joining it
            # here would add its own timeout to every failed start.
            self.assertLess(time.monotonic() - started, 2.0)
        finally:
            child.kill()
            child.wait(timeout=10)
            if output is not None:
                output.reader.join(timeout=5)
            assert child.stdout is not None
            child.stdout.close()


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
                self.assertTrue("temporary" in note or "временный" in note)
                self.assertIn("--public-url", note)

    def test_a_declared_origin_is_not_warned_about(self) -> None:
        """The note tells the user to do this, so it must go quiet once they have."""
        self.assertIsNone(
            ephemeral_url_warning("chatgpt-web", "https://mcp.example.com")
        )

    def test_russian_warning_explains_that_the_saved_url_must_be_updated(self) -> None:
        note = ephemeral_url_warning("chatgpt-web", None, language="ru")
        self.assertIsNotNone(note)
        assert note is not None
        self.assertIn("URL меняется", note)
        self.assertIn("нужно будет обновить", note)
        self.assertIn("--public-url", note)

    def test_notion_quick_tunnel_is_warned_about(self) -> None:
        note = ephemeral_url_warning("notion", None)
        self.assertIsNotNone(note)
        assert note is not None
        self.assertTrue("temporary" in note or "временный" in note)
        self.assertIn("--public-url", note)

    def test_a_profile_that_never_needed_a_stable_url_is_left_alone(self) -> None:
        for profile in ("promptql", "generic-streamable-http"):
            with self.subTest(profile=profile):
                self.assertIsNone(ephemeral_url_warning(profile, None))

    def test_an_unknown_profile_does_not_raise(self) -> None:
        self.assertIsNone(ephemeral_url_warning("not-a-profile", None))


class WebBridgeInstructionTests(unittest.TestCase):
    def test_connection_steps_name_current_ui_and_password_destination(self) -> None:
        for language, settings, create in (
            ("ru", "Настройки → Приложения", "Приложения → Создать"),
            ("en", "Settings → Apps", "Apps → Create"),
        ):
            with self.subTest(profile="chatgpt-web", language=language):
                text = "\n".join(
                    web_bridge_connection_instructions(
                        "chatgpt-web", language=language
                    )
                )
                self.assertIn(settings, text)
                self.assertIn(create, text)
                self.assertIn("MCP URL", text)
                self.assertNotIn("Plugins", text)
                self.assertNotIn("Плагины", text)

        russian = "\n".join(
            web_bridge_connection_instructions("chatgpt-web", language="ru")
        )
        self.assertIn("только на странице KaroX", russian)

        claude = "\n".join(
            web_bridge_connection_instructions("claude-web", language="en")
        )
        self.assertIn("Settings → Connectors → Add custom connector", claude)
        self.assertIn("MCP URL", claude)
        self.assertIn("only on the KaroX page", claude)

        notion = "\n".join(
            web_bridge_connection_instructions("notion", language="en")
        )
        self.assertIn("Notion Custom Agent", notion)
        self.assertIn("Tools & Access", notion)
        self.assertIn("Dynamic Client Registration", notion)
        self.assertIn("approval password only on the KaroX page", notion)
        self.assertIn("stable Tailscale", notion)
        self.assertNotIn("Claude", notion)

        notion_ru = "\n".join(
            web_bridge_connection_instructions("notion", language="ru")
        )
        self.assertIn("Notion Custom Agent", notion_ru)
        self.assertIn("OAuth", notion_ru)
        self.assertNotIn("Claude", notion_ru)


if __name__ == "__main__":
    unittest.main()
