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

from karox.cli import _verification_command, main
from karox.models import AccessProfile
from karox.web_bridge_launcher import (
    DEFAULT_WEB_TOOLS,
    WRITE_WEB_TOOLS,
    WebBridgeConnectConfig,
    WebBridgeLaunchError,
    _bridge_argv,
    _child_options,
    _mirror_child_output,
    _wait_for_bridge,
    bundled_cloudflared,
    cloudflared_not_found_message,
    ephemeral_url_warning,
    find_cloudflared,
    parent_death_hook,
    run_web_bridge,
    start_cloudflare_quick_tunnel,
    web_bridge_connection_instructions,
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
        self.assertEqual(argv.count("--verification-command"), 1)
        index = argv.index("--verification-command")
        serialized = argv[index + 1]
        self.assertEqual(serialized, '["python","-m","pytest"]')
        self.assertEqual(_verification_command(serialized), ("python", "-m", "pytest"))


class WebBridgeCliTests(unittest.TestCase):
    def test_connect_builds_managed_cloudflare_config(self) -> None:
        expected_command = (
            "python",
            "scripts/run_v5_preflight.py",
            "--apply-reviewed-fixes",
            "--full",
            "--keep-going",
        )
        # Windows PowerShell 5.1 legacy native argv removes embedded JSON quotes
        # before Python receives this native-process argument.
        powershell_native_value = (
            "[python,scripts/run_v5_preflight.py,--apply-reviewed-fixes,"
            "--full,--keep-going]"
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
            self.assertIn(tool, config.tools)

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
            with patch.dict(os.environ, {"KAROX_RUNTIME_DIR": tmp}):
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
                self.assertIn("temporary", note)
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

    def test_a_profile_that_never_needed_a_stable_url_is_left_alone(self) -> None:
        for profile in ("promptql", "notion", "generic-streamable-http"):
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


if __name__ == "__main__":
    unittest.main()
