"""Saved web-bridge profiles, diagnostics, and Tailscale tunnel tests."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from _support import SRC  # noqa: F401 - inserts src on sys.path

from karox.cli import main
from karox.models import AccessProfile
from karox.sessions import SessionStore
from karox.tailscale import (
    TailscaleError,
    classify_funnel_failure,
    ensure_tailscale_ready,
    parse_tailscale_status,
    prepare_tailscale_funnel,
    query_funnel_ownership,
    query_tailscale_status,
)
from karox.tailscale_routes import TailscaleRoute
from karox.web_bridge_launcher import (
    WebBridgeConnectConfig,
    _legacy_saved_web_bridge_session_id,
    _session_id,
    WebBridgeLaunchError,
    delete_saved_web_bridge_identity,
    saved_web_bridge_session_id,
    start_tailscale_foreground_funnel,
    web_bridge_diagnostics,
)
from karox.web_bridge_profiles import (
    SavedWebBridgeProfile,
    WebBridgeProfileError,
    WebBridgeProfileStore,
)


def _completed(
    argv: list[str], returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


class SavedServerProfileValidationTests(unittest.TestCase):
    """Every malformed field has to leave as WebBridgeProfileError.

    The loader promises that shape, and the CLI relies on it to print an
    actionable message. A field whose type was assumed rather than checked broke
    the promise: a number where a list belonged reached ``frozenset(... for item
    in 5)`` and left as a bare TypeError, so a hand-edited profiles.json ended
    the process with a traceback instead of naming the field.
    """

    def _coerce(self, payload: dict[str, Any]) -> Any:
        from karox.web_bridge_profiles import _server_profile

        return _server_profile(payload)

    def _valid(self, **overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": "python-http",
            "argv": ["python", "-m", "http.server"],
            "env_keys": ["HOST"],
            "env_allowlist": ["HOST"],
            "host_hint": "127.0.0.1",
            "ready_url": None,
        }
        payload.update(overrides)
        return payload

    def test_a_valid_profile_round_trips(self) -> None:
        self.assertEqual(self._coerce(self._valid())["name"], "python-http")

    def test_a_non_list_env_allowlist_is_reported_not_raised_raw(self) -> None:
        with self.assertRaises(WebBridgeProfileError) as caught:
            self._coerce(self._valid(env_allowlist=5))
        self.assertIn("env_allowlist", str(caught.exception))

    def test_a_non_list_env_keys_is_ignored_rather_than_iterated(self) -> None:
        # env_keys carries names only and is rebuilt with empty values, so a
        # malformed one is dropped rather than fatal -- but it must never be
        # iterated as whatever type it happens to be.
        self.assertEqual(self._coerce(self._valid(env_keys="HOST"))["env_keys"], "HOST")

    def test_an_unknown_field_is_still_refused(self) -> None:
        with self.assertRaises(WebBridgeProfileError):
            self._coerce(self._valid(secret="nope"))


class SavedWebBridgeProfileTests(unittest.TestCase):
    def test_store_round_trip_is_secret_free(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profiles.json"
            store = WebBridgeProfileStore(path)
            profile = SavedWebBridgeProfile(
                name="full-dev",
                target_profile="chatgpt-web",
                repository=str(Path(tmp).resolve()),
                tools=("karox.repo.read_file", "karox.git.status"),
                tunnel="tailscale",
                language="ru",
                deadline_seconds=3600,
            )
            store.put(profile, replace_existing=False)
            self.assertEqual(store.get("full-dev"), profile)
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("password", raw.lower())
            self.assertNotIn("secret", raw.lower())
            self.assertNotIn("username", raw.lower())
            self.assertEqual(json.loads(raw)["version"], 1)
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_browser_credential_refs_round_trip_without_raw_login_values(self) -> None:
        profile = SavedWebBridgeProfile(
            name="browser-test",
            target_profile="chatgpt-web",
            tools=("karox.repo.read_file",),
            access_profile=AccessProfile.BROWSER_CONTROL,
            browser_external_https=True,
            browser_credential_refs=("os-keyring:browser/gmail-test",),
        )
        restored = SavedWebBridgeProfile.from_dict(profile.to_dict())
        self.assertEqual(
            restored.browser_credential_refs,
            ("os-keyring:browser/gmail-test",),
        )
        self.assertEqual(
            WebBridgeConnectConfig(
                profile="chatgpt-web",
                repository=Path.cwd(),
                tools=("karox.repo.read_file",),
                access_profile=AccessProfile.BROWSER_CONTROL,
                browser_external_https=True,
                browser_credential_refs=restored.browser_credential_refs,
            ).browser_credential_refs,
            ("os-keyring:browser/gmail-test",),
        )
        with self.assertRaisesRegex(WebBridgeProfileError, "credential reference"):
            SavedWebBridgeProfile(
                name="bad-browser-ref",
                target_profile="chatgpt-web",
                tools=("karox.repo.read_file",),
                access_profile=AccessProfile.BROWSER_CONTROL,
                browser_external_https=True,
                browser_credential_refs=("plaintext:browser/nope",),
            )

    def test_external_mcp_server_ids_round_trip_secret_free(self) -> None:
        profile = SavedWebBridgeProfile(
            name="with-notion",
            target_profile="chatgpt-web",
            tools=("karox.repo.read_file",),
            mcp_servers=("notion", "docs_mcp"),
        )
        restored = SavedWebBridgeProfile.from_dict(profile.to_dict())
        self.assertEqual(restored.mcp_servers, ("notion", "docs_mcp"))
        with self.assertRaisesRegex(WebBridgeProfileError, "MCP server IDs"):
            SavedWebBridgeProfile(
                name="bad-mcp",
                target_profile="chatgpt-web",
                tools=("karox.repo.read_file",),
                mcp_servers=("notion", "notion"),
            )

    def test_cli_create_and_edit_external_mcp_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root = Path(tmp) / "config"
            environment = {"KAROX_VNEXT_CONFIG_DIR": str(config_root)}
            output = io.StringIO()
            with patch.dict(os.environ, environment, clear=False), redirect_stdout(output):
                code = main(
                    (
                        "bridge", "saved", "create", "mcp-dev",
                        "--target-profile", "chatgpt-web",
                        "--mcp-server", "notion",
                        "--json",
                    )
                )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["profile"]["mcp_servers"], ["notion"])

            output = io.StringIO()
            with patch.dict(os.environ, environment, clear=False), redirect_stdout(output):
                code = main(
                    (
                        "bridge", "saved", "edit", "mcp-dev",
                        "--clear-mcp-servers",
                        "--json",
                    )
                )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["profile"]["mcp_servers"], [])

    def test_checks_run_requires_a_typed_allowlist(self) -> None:
        with self.assertRaisesRegex(WebBridgeProfileError, "approved verification"):
            SavedWebBridgeProfile(
                name="broken",
                target_profile="chatgpt-web",
                tools=("karox.checks.run",),
            )
        profile = SavedWebBridgeProfile(
            name="verified",
            target_profile="chatgpt-web",
            tools=("karox.checks.run",),
            verification_commands=(("python", "-m", "pytest"),),
        )
        self.assertEqual(
            profile.verification_commands,
            (("python", "-m", "pytest"),),
        )

    def test_store_crud_and_invalid_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profiles.json"
            store = WebBridgeProfileStore(path)
            first = SavedWebBridgeProfile(
                name="one",
                target_profile="chatgpt-web",
                tools=("karox.repo.read_file",),
            )
            second = SavedWebBridgeProfile(
                name="two",
                target_profile="claude-web",
                tools=("karox.git.status",),
            )
            store.put(first)
            store.put(second)
            self.assertEqual([item.name for item in store.list()], ["one", "two"])
            self.assertEqual(store.delete("one"), first)
            self.assertEqual([item.name for item in store.list()], ["two"])

            # Durable saved-profile deletion is fenced by the watchdog and then
            # reclaims both the repository-bound session and keyring account.
            repository = Path(tmp) / "repo"
            repository.mkdir()
            session_root = Path(tmp) / "sessions"
            watchdog_root = Path(tmp) / "watchdogs"
            sessions = SessionStore(session_root)
            session_id = saved_web_bridge_session_id("two")
            sessions.create(
                repository,
                "saved profile",
                AccessProfile.WORKSPACE_WRITE,
                session_id=session_id,
            )
            watchdog_root.mkdir()
            watchdog = watchdog_root / f"{session_id}.json"
            watchdog.write_text(
                json.dumps({"owner_pid": os.getpid(), "session_id": session_id}),
                encoding="utf-8",
            )
            credential_store = Mock()
            with patch(
                "karox.web_bridge_launcher.session_dir", return_value=session_root
            ), patch(
                "karox.web_bridge_launcher.watchdog_dir", return_value=watchdog_root
            ), patch(
                "karox.web_bridge_launcher.BridgeCredentialStore",
                return_value=credential_store,
            ):
                with self.assertRaisesRegex(WebBridgeLaunchError, "is running"):
                    delete_saved_web_bridge_identity("two")
                self.assertTrue(sessions.state_path(session_id).exists())
                watchdog.unlink()
                cleanup = delete_saved_web_bridge_identity("two")
            self.assertEqual(cleanup["session"], "deleted")
            self.assertEqual(cleanup["credential"], "deleted")
            self.assertFalse(sessions.session_dir(session_id).exists())
            credential_store.delete.assert_called_once_with(session_id)

            path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(WebBridgeProfileError, "unknown format"):
                store.list()

    def test_cli_create_and_short_saved_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = root / "repo"
            repository.mkdir()
            config_root = root / "config"
            environment = {"KAROX_VNEXT_CONFIG_DIR": str(config_root)}
            create_output = io.StringIO()
            with patch.dict(os.environ, environment, clear=False), redirect_stdout(
                create_output
            ):
                code = main(
                    (
                        "bridge",
                        "saved",
                        "create",
                        "full-dev",
                        "--repository",
                        str(repository),
                        "--write",
                        "--tool",
                        "karox.repo.read_file",
                        "--tool",
                        "karox.checks.run",
                        "--verification-command",
                        "[python,scripts/run_v5_preflight.py,--full]",
                        "--deadline-preset",
                        "full-suite",
                        "--tunnel",
                        "tailscale",
                        "--language",
                        "ru",
                        "--json",
                    )
                )
            self.assertEqual(code, 0)
            created = json.loads(create_output.getvalue())
            self.assertEqual(
                created["launch_command"],
                "karox bridge connect --saved full-dev",
            )
            self.assertEqual(
                created["profile"]["access_profile"],
                AccessProfile.WORKSPACE_WRITE.value,
            )
            self.assertEqual(created["profile"]["deadline_seconds"], 3600.0)

            diagnostics_output = io.StringIO()
            with patch.dict(os.environ, environment, clear=False), redirect_stdout(
                diagnostics_output
            ):
                code = main(
                    (
                        "bridge",
                        "connect",
                        "--saved",
                        "full-dev",
                        "--diagnostics-only",
                    )
                )
            self.assertEqual(code, 0)
            diagnostics = json.loads(diagnostics_output.getvalue())
            self.assertEqual(diagnostics["saved_profile"], "full-dev")
            self.assertEqual(diagnostics["tunnel"], "tailscale")
            self.assertEqual(
                diagnostics["verification_commands"][0],
                ["python", "scripts/run_v5_preflight.py", "--full"],
            )
            self.assertEqual(
                diagnostics["session_expiration"],
                "persists across managed launcher restarts",
            )

            rebound_repository = root / "rebound-repo"
            rebound_repository.mkdir()
            edit_error = io.StringIO()
            with patch.dict(os.environ, environment, clear=False), patch(
                "karox.cli.saved_web_bridge_identity_exists",
                return_value=True,
            ), redirect_stderr(edit_error):
                code = main(
                    (
                        "bridge",
                        "saved",
                        "edit",
                        "full-dev",
                        "--repository",
                        str(rebound_repository),
                        "--json",
                    )
                )
            self.assertEqual(code, 2)
            self.assertIn("requires --reset-identity", edit_error.getvalue())
            with patch.dict(os.environ, environment, clear=False):
                self.assertEqual(
                    WebBridgeProfileStore().get("full-dev").repository,
                    str(repository.resolve()),
                )

            reset_cleanup = {
                "profile_name": "full-dev",
                "session_id": saved_web_bridge_session_id("full-dev"),
                "session": "deleted",
                "credential": "deleted",
                "identities": [],
            }
            edit_output = io.StringIO()
            with patch.dict(os.environ, environment, clear=False), patch(
                "karox.cli.saved_web_bridge_identity_exists",
                return_value=True,
            ), patch(
                "karox.cli.delete_saved_web_bridge_identity",
                return_value=reset_cleanup,
            ) as reset_identity, redirect_stdout(edit_output):
                code = main(
                    (
                        "bridge",
                        "saved",
                        "edit",
                        "full-dev",
                        "--repository",
                        str(rebound_repository),
                        "--reset-identity",
                        "--json",
                    )
                )
            self.assertEqual(code, 0)
            edited = json.loads(edit_output.getvalue())
            self.assertEqual(edited["identity_reset"], reset_cleanup)
            self.assertTrue(edited["connector_reconfigure_required"])
            self.assertEqual(
                edited["profile"]["repository"],
                str(rebound_repository.resolve()),
            )
            reset_identity.assert_called_once_with("full-dev")

            cleanup = {
                "profile_name": "full-dev",
                "session_id": saved_web_bridge_session_id("full-dev"),
                "session": "deleted",
                "credential": "deleted",
            }
            delete_output = io.StringIO()
            with patch.dict(os.environ, environment, clear=False), patch(
                "karox.cli.delete_saved_web_bridge_identity",
                return_value=cleanup,
            ) as delete_identity, redirect_stdout(delete_output):
                code = main(
                    (
                        "bridge",
                        "saved",
                        "delete",
                        "full-dev",
                        "--json",
                    )
                )
            self.assertEqual(code, 0)
            deleted = json.loads(delete_output.getvalue())
            self.assertEqual(deleted["identity_cleanup"], cleanup)
            self.assertEqual(
                deleted["session_id"],
                saved_web_bridge_session_id("full-dev"),
            )
            delete_identity.assert_called_once_with("full-dev")
            with patch.dict(os.environ, environment, clear=False):
                self.assertEqual(WebBridgeProfileStore().list(), ())

            developer_output = io.StringIO()
            with patch.dict(os.environ, environment, clear=False), redirect_stdout(
                developer_output
            ):
                code = main(
                    (
                        "bridge",
                        "saved",
                        "ensure-developer",
                        "clickup-opus",
                        "--repository",
                        str(repository),
                        "--json",
                    )
                )
            self.assertEqual(code, 0)
            developer = json.loads(developer_output.getvalue())
            self.assertEqual(developer["status"], "created")
            self.assertEqual(developer["profile"]["tunnel"], "tailscale")
            self.assertEqual(
                developer["profile"]["access_profile"],
                AccessProfile.WORKSPACE_WRITE.value,
            )
            self.assertIn("karox.repo.write_file", developer["profile"]["tools"])
            self.assertIn("karox.repo.command", developer["profile"]["tools"])
            self.assertIn("karox.tests.run", developer["profile"]["tools"])
            self.assertIn("karox.checks.run", developer["profile"]["tools"])
            self.assertEqual(
                developer["profile"]["verification_commands"],
                [
                    ["python", "-m", "ruff", "check", "src", "tests", "scripts"],
                    ["python", "-m", "mypy", "src/karox"],
                    ["python", "-m", "build", "--wheel"],
                ],
            )

            repeated_output = io.StringIO()
            with patch.dict(os.environ, environment, clear=False), patch(
                "karox.cli.saved_web_bridge_identity_exists",
                return_value=False,
            ), redirect_stdout(repeated_output):
                code = main(
                    (
                        "bridge",
                        "saved",
                        "ensure-developer",
                        "clickup-opus",
                        "--repository",
                        str(repository),
                        "--json",
                    )
                )
            self.assertEqual(code, 0)
            repeated = json.loads(repeated_output.getvalue())
            self.assertEqual(repeated["status"], "updated")
            self.assertFalse(repeated["connector_reconfigure_required"])

    def test_saved_connect_overrides_reach_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            saved_repository = root / "saved"
            override_repository = root / "override"
            saved_repository.mkdir()
            override_repository.mkdir()
            config_root = root / "config"
            environment = {"KAROX_VNEXT_CONFIG_DIR": str(config_root)}
            with patch.dict(os.environ, environment, clear=False):
                WebBridgeProfileStore().put(
                    SavedWebBridgeProfile(
                        name="dev",
                        target_profile="claude-web",
                        repository=str(saved_repository),
                        tools=("karox.repo.read_file",),
                        tunnel="tailscale",
                        language="ru",
                    )
                )
                with patch("karox.cli.run_web_bridge", return_value=0) as launch:
                    code = main(
                        (
                            "bridge",
                            "connect",
                            "--saved",
                            "dev",
                            "--repository",
                            str(override_repository),
                            "--tunnel",
                            "cloudflare",
                            "--deadline-seconds",
                            "1200",
                            "--language",
                            "en",
                        )
                    )
            self.assertEqual(code, 0)
            config = launch.call_args.args[0]
            self.assertEqual(config.repository, override_repository.resolve())
            self.assertEqual(config.profile, "claude-web")
            self.assertEqual(config.tunnel, "cloudflare")
            self.assertEqual(config.deadline_seconds, 1200.0)
            self.assertEqual(config.language, "en")
            self.assertEqual(config.saved_profile_name, "dev")
            self.assertEqual(_session_id(config), saved_web_bridge_session_id("dev"))
            self.assertEqual(
                saved_web_bridge_session_id("dev"),
                saved_web_bridge_session_id("dev"),
            )
            self.assertNotEqual(
                saved_web_bridge_session_id("dev"),
                saved_web_bridge_session_id("other"),
            )

            # A profile launched by the first durable-ID implementation keeps
            # its existing session/secret even after the target profile changes.
            legacy_root = root / "legacy-sessions"
            legacy_watchdogs = root / "legacy-watchdogs"
            legacy_id = _legacy_saved_web_bridge_session_id(
                "dev", "chatgpt-web"
            )
            SessionStore(legacy_root).create(
                saved_repository,
                "legacy saved bridge",
                AccessProfile.READ_ONLY,
                session_id=legacy_id,
            )
            with patch(
                "karox.web_bridge_launcher.session_dir", return_value=legacy_root
            ), patch(
                "karox.web_bridge_launcher.watchdog_dir",
                return_value=legacy_watchdogs,
            ):
                self.assertEqual(_session_id(config), legacy_id)


class TailscaleAndDiagnosticsTests(unittest.TestCase):
    def test_status_parser_distinguishes_login_hostname_and_ready(self) -> None:
        cases: tuple[tuple[dict[str, Any], str], ...] = (
            ({"BackendState": "NeedsLogin"}, "login_required"),
            ({"BackendState": "NeedsMachineAuth"}, "machine_approval_required"),
            ({"BackendState": "Running", "Self": {}}, "hostname_unavailable"),
            (
                {
                    "BackendState": "Running",
                    "Self": {"DNSName": "workstation.tailnet.ts.net."},
                },
                "ready",
            ),
        )
        for payload, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(parse_tailscale_status(payload)["code"], expected)

        with patch("karox.tailscale.find_tailscale", return_value=None):
            self.assertEqual(query_tailscale_status()["code"], "not_installed")
        self.assertEqual(
            classify_funnel_failure("Funnel is not enabled for this tailnet")["code"],
            "funnel_policy_denied",
        )
        self.assertEqual(
            classify_funnel_failure("listen tcp :443: address already in use")["code"],
            "https_port_in_use",
        )

    def test_funnel_ownership_distinguishes_empty_and_active(self) -> None:
        responses = (
            (json.dumps({"TCP": {}}), False),
            (json.dumps({"TCP": {"443": {"HTTPS": True}}}), True),
        )
        for output, expected in responses:
            with self.subTest(active=expected):
                result = query_funnel_ownership(
                    "tailscale",
                    run=lambda argv, **kwargs: _completed(argv, stdout=output),
                )
                self.assertTrue(result["known"])
                self.assertEqual(result["active"], expected)

    def test_prepare_refuses_unknown_or_existing_routes(self) -> None:
        status = json.dumps(
            {
                "BackendState": "Running",
                "Self": {"DNSName": "workstation.tailnet.ts.net."},
            }
        )

        def active_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            del kwargs
            if argv[1:3] == ["status", "--json"]:
                return _completed(argv, stdout=status)
            return _completed(
                argv,
                stdout=json.dumps({"TCP": {"443": {"HTTPS": True}}}),
            )

        def unknown_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            del kwargs
            if argv[1:3] == ["status", "--json"]:
                return _completed(argv, stdout=status)
            return _completed(argv, returncode=1, stderr="unknown command")

        with patch("karox.tailscale.find_tailscale", return_value="tailscale"):
            with self.assertRaisesRegex(TailscaleError, "route_in_use"):
                prepare_tailscale_funnel(8765, run=active_run)
            with self.assertRaisesRegex(TailscaleError, "ownership_unknown"):
                prepare_tailscale_funnel(8765, run=unknown_run)

    def test_prepare_preserves_sibling_path_on_same_https_listener(self) -> None:
        status = json.dumps(
            {
                "BackendState": "Running",
                "Self": {"DNSName": "workstation.tailnet.ts.net."},
            }
        )
        root = TailscaleRoute(
            host="workstation.tailnet.ts.net",
            path="/",
            protocol="https",
            local_target="http://127.0.0.1:8765",
            mode="funnel",
            raw_key="workstation.tailnet.ts.net:443/",
        )
        notion = TailscaleRoute(
            host="workstation.tailnet.ts.net",
            path="/karox-notion",
            protocol="https",
            local_target="http://127.0.0.1:8767",
            mode="funnel",
            raw_key="workstation.tailnet.ts.net:443/karox-notion",
        )

        def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            del kwargs
            if argv[1:3] == ["status", "--json"]:
                return _completed(argv, stdout=status)
            if argv[1:4] == ["serve", "status", "--json"]:
                return _completed(argv, stdout=json.dumps({"TCP": {"443": {"HTTPS": True}}}))
            return _completed(argv)

        with (
            patch("karox.tailscale.find_tailscale", return_value="tailscale"),
            patch(
                "karox.tailscale_routes.inventory_tailscale_routes",
                side_effect=[[root, notion], [notion]],
            ),
        ):
            plan = prepare_tailscale_funnel(8765, run=run)

        self.assertEqual(plan.public_url, "https://workstation.tailnet.ts.net")
        self.assertEqual(plan.argv[-1], "http://127.0.0.1:8765")

    def test_prepare_returns_stable_foreground_plan(self) -> None:
        status = json.dumps(
            {
                "BackendState": "Running",
                "Self": {"DNSName": "workstation.tailnet.ts.net."},
            }
        )

        def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            del kwargs
            if argv[1:3] == ["status", "--json"]:
                return _completed(argv, stdout=status)
            return _completed(argv, stdout=json.dumps({"TCP": {}}))

        with patch("karox.tailscale.find_tailscale", return_value="tailscale"):
            plan = prepare_tailscale_funnel(9876, run=run)
        self.assertEqual(plan.public_url, "https://workstation.tailnet.ts.net")
        self.assertEqual(
            plan.argv,
            (
                "tailscale",
                "funnel",
                "--yes",
                "--https=443",
                "http://127.0.0.1:9876",
            ),
        )
        self.assertNotIn("--bg", plan.argv)
        self.assertNotIn("reset", plan.argv)

        confirmed_process = Mock()
        confirmed_process.stdout = io.StringIO(plan.public_url + "\n")
        confirmed_process.poll.return_value = None
        confirmed_process.wait.return_value = 0
        with patch(
            "karox.web_bridge_launcher.prepare_tailscale_funnel",
            return_value=plan,
        ):
            tunnel = start_tailscale_foreground_funnel(
                9876,
                timeout_seconds=1,
                popen=lambda *args, **kwargs: confirmed_process,
            )
        self.assertEqual(tunnel.public_url, plan.public_url)
        tunnel.stop()
        confirmed_process.terminate.assert_called_once()

        unconfirmed_process = Mock()
        unconfirmed_process.stdout = io.StringIO("Funnel starting without URL\n")
        unconfirmed_process.poll.return_value = None
        unconfirmed_process.wait.return_value = 0
        with patch(
            "karox.web_bridge_launcher.prepare_tailscale_funnel",
            return_value=plan,
        ):
            with self.assertRaisesRegex(WebBridgeLaunchError, "did not confirm"):
                start_tailscale_foreground_funnel(
                    9876,
                    timeout_seconds=1,
                    popen=lambda *args, **kwargs: unconfirmed_process,
                )
        unconfirmed_process.terminate.assert_called_once()

    def test_prepare_allows_notion_listener_beside_existing_443_funnel(self) -> None:
        from karox.tailscale_routes import TailscaleRoute

        existing_chatgpt = TailscaleRoute(
            host="workstation.tailnet.ts.net",
            path="/",
            protocol="https",
            local_target="http://127.0.0.1:8765",
            mode="funnel",
            raw_key="workstation.tailnet.ts.net:443/",
        )
        ready = {
            "ready": True,
            "executable": "tailscale",
            "public_url": "https://workstation.tailnet.ts.net",
        }
        with (
            patch("karox.tailscale.ensure_tailscale_ready", return_value=ready),
            patch(
                "karox.tailscale.query_funnel_ownership",
                return_value={"known": True, "active": True},
            ),
            patch(
                "karox.tailscale_routes.inventory_tailscale_routes",
                return_value=[existing_chatgpt],
            ),
        ):
            plan = prepare_tailscale_funnel(
                8767,
                https_port=8443,
                run=Mock(),
            )

        self.assertEqual(
            plan.public_url,
            "https://workstation.tailnet.ts.net:8443",
        )
        self.assertIn("--https=8443", plan.argv)
        self.assertIn("http://127.0.0.1:8767", plan.argv)

    def test_diagnostics_explain_tools_deadline_and_url_stability(self) -> None:
        unavailable = WebBridgeConnectConfig(
            profile="chatgpt-web",
            repository=Path.cwd(),
            tools=("karox.repo.read_file",),
            tunnel="cloudflare",
        )
        unavailable_report = web_bridge_diagnostics(unavailable)
        checks = next(
            item
            for item in unavailable_report["disabled_tools"]
            if item["name"] == "karox.checks.run"
        )
        self.assertIn("no approved", checks["reason"])
        self.assertEqual(unavailable_report["url_stability"], "ephemeral")

        full_suite = WebBridgeConnectConfig(
            profile="chatgpt-web",
            repository=Path.cwd(),
            tools=("karox.repo.read_file", "karox.checks.run"),
            verification_commands=(
                ("python", "scripts/run_v5_preflight.py", "--full"),
            ),
            deadline_seconds=120,
            tunnel="tailscale",
        )
        full_report = web_bridge_diagnostics(full_suite)
        self.assertEqual(full_report["expected_verification_seconds"], 900.0)
        self.assertIsNotNone(full_report["deadline_advisory"])
        self.assertEqual(full_report["url_stability"], "stable_device_hostname")
        self.assertEqual(
            full_report["session_expiration"],
            "when the managed launcher exits",
        )

    def test_hyperagent_full_diagnostics_advertise_unrestricted_dev_commands(self) -> None:
        config = WebBridgeConnectConfig(
            profile="hyperagent-web",
            repository=Path.cwd(),
            tools=("karox.repo.read_file", "karox.command.run"),
            access_profile=AccessProfile.ELEVATED,
            tunnel="tailscale",
        )
        report = web_bridge_diagnostics(config)
        self.assertEqual(
            report["mode_restrictions"],
            {
                "read_only": False,
                "no_git_push": True,
                "git_push_approval": "unavailable",
                "no_publish": True,
                "no_auth_commands": True,
                "no_deploy_release": True,
            },
        )

    @unittest.skipUnless(os.name == "nt", "Windows GUI-launch simulation contract")
    def test_ensure_ready_restarts_service_when_up_leaves_daemon_stuck(self) -> None:
        starting = json.dumps({"BackendState": "NoState", "Self": {"HostName": "x"}})
        ready = json.dumps(
            {
                "BackendState": "Running",
                "Self": {"DNSName": "monsterpc.tail.ts.net."},
            }
        )

        class _Run:
            def __init__(self, restarted: "list[bool]") -> None:
                self.restarted = restarted
                self.calls: list[list[str]] = []

            def __call__(self, argv, **kwargs):
                self.calls.append(list(argv))
                if argv[1:3] == ["status", "--json"]:
                    # Ready only once the service has been restarted; until then
                    # the daemon stays stuck in NoState even after `tailscale up`.
                    return _completed(
                        argv, stdout=ready if self.restarted[0] else starting
                    )
                # ``tailscale up`` itself: succeed (node already has a key).
                return _completed(argv)

        restarted = [False]
        run = _Run(restarted)
        elevated = Mock(return_value=_completed(["elevated"], returncode=0))

        def _elevated_side_effect(*args, **kwargs):
            restarted[0] = True
            return _completed(["elevated"], returncode=0)

        elevated.side_effect = _elevated_side_effect
        emit_lines: list[str] = []
        with patch("karox.tailscale._is_windows_admin", return_value=False):
            status = ensure_tailscale_ready(
                executable="tailscale",
                run=run,
                elevated_run=elevated,
                emit=emit_lines.append,
                restart_service=True,
                poll_attempts=3,
                poll_interval_seconds=0,
            )
        self.assertTrue(status["ready"])
        self.assertEqual(status["public_url"], "https://monsterpc.tail.ts.net")
        # The elevated restart was requested exactly once.
        elevated.assert_called_once()
        self.assertTrue(any("restart" in line.lower() for line in emit_lines))

    def test_ensure_ready_does_not_restart_when_disabled(self) -> None:
        starting = json.dumps({"BackendState": "NoState", "Self": {"HostName": "x"}})

        def run(argv, **kwargs):
            if argv[1:3] == ["status", "--json"]:
                return _completed(argv, stdout=starting)
            return _completed(argv)

        elevated = Mock()
        with patch.dict(os.environ, {"KAROX_TAILSCALE_SERVICE_RESTART": "0"}):
            status = ensure_tailscale_ready(
                executable="tailscale",
                run=run,
                elevated_run=elevated,
                restart_service=True,
                poll_attempts=2,
                poll_interval_seconds=0,
            )
        self.assertFalse(status["ready"])
        elevated.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows GUI-launch simulation contract")
    def test_ensure_ready_launches_gui_before_service_restart(self) -> None:
        starting = json.dumps({"BackendState": "NoState", "Self": {"HostName": "x"}})
        ready = json.dumps(
            {
                "BackendState": "Running",
                "Self": {"DNSName": "monsterpc.tail.ts.net."},
            }
        )

        class _Run:
            def __init__(self, gui_up: "list[bool]") -> None:
                self.gui_up = gui_up

            def __call__(self, argv, **kwargs):
                if argv[1:3] == ["status", "--json"]:
                    return _completed(
                        argv, stdout=ready if self.gui_up[0] else starting
                    )
                return _completed(argv)

        gui_up = [False]
        run = _Run(gui_up)
        popen = Mock()

        def _popen_side_effect(argv, **kwargs):
            gui_up[0] = True
            return Mock()

        popen.side_effect = _popen_side_effect
        elevated = Mock()
        emit_lines: list[str] = []
        # Pretend we are on Windows so the GUI launch path is exercised.
        with patch("karox.tailscale.os.name", "nt"), patch(
            "karox.tailscale.find_tailscale_gui", return_value="C:/Program Files/Tailscale/tailscale-ipn.exe"
        ):
            status = ensure_tailscale_ready(
                executable="tailscale",
                run=run,
                elevated_run=elevated,
                emit=emit_lines.append,
                restart_service=True,
                poll_attempts=2,
                poll_interval_seconds=0,
                popen=popen,
            )
        self.assertTrue(status["ready"])
        # The GUI was launched; the heavier service restart was never needed.
        popen.assert_called_once()
        elevated.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows GUI-launch simulation contract")
    def test_ensure_ready_launches_gui_when_status_initially_cannot_reach_daemon(self) -> None:
        ready = json.dumps(
            {
                "BackendState": "Running",
                "Self": {"DNSName": "monsterpc.tail.ts.net."},
            }
        )
        gui_up = [False]
        calls: list[list[str]] = []

        def run(argv, **kwargs):
            calls.append(list(argv))
            if argv[1:3] == ["status", "--json"]:
                if gui_up[0]:
                    return _completed(argv, stdout=ready)
                return _completed(
                    argv,
                    returncode=1,
                    stderr="failed to connect to local tailscaled",
                )
            return _completed(argv)

        popen = Mock()

        def launch_gui(argv, **kwargs):
            gui_up[0] = True
            return Mock()

        popen.side_effect = launch_gui
        with patch("karox.tailscale.os.name", "nt"), patch(
            "karox.tailscale.find_tailscale_gui",
            return_value="C:/Program Files/Tailscale/tailscale-ipn.exe",
        ):
            status = ensure_tailscale_ready(
                executable="tailscale",
                run=run,
                poll_attempts=2,
                poll_interval_seconds=0,
                popen=popen,
            )

        self.assertTrue(status["ready"])
        popen.assert_called_once()
        self.assertFalse(any(argv[1:] == ["up"] for argv in calls))

    def test_connect_command_defaults_to_chatgpt_and_tailscale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = Path(tmp)
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(
                    (
                        "connect",
                        "--repository",
                        str(repository),
                        "--diagnostics-only",
                    )
                )
            self.assertEqual(code, 0)
            report = json.loads(output.getvalue())
            self.assertEqual(report["target_profile"], "chatgpt-web")
            self.assertEqual(report["tunnel"], "tailscale")
            self.assertEqual(report["url_stability"], "stable_device_hostname")
            self.assertEqual(report["access_profile"], "read_only")

            # The short alias must be as complete as `bridge connect`, but a
            # repository with no approved local server recipe must not inherit
            # another project's start:safe profile. Write/check capabilities stay
            # enabled; managed server mutation is simply not advertised.
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(
                    (
                        "connect",
                        "chatgpt",
                        "--repository",
                        str(repository),
                        "--write",
                        "--verification-command",
                        '["python","-m","ruff","check","src","tests","scripts"]',
                        "--diagnostics-only",
                    )
                )
            self.assertEqual(code, 0)
            writable = json.loads(output.getvalue())
            self.assertEqual(writable["access_profile"], "workspace_write")
            self.assertTrue(writable["write_permission"])
            self.assertNotIn("karox.dev_server.start", writable["available_tools"])
            self.assertIn("karox.checks.run", writable["available_tools"])
            self.assertIn("karox.tests.run", writable["available_tools"])
            self.assertEqual(writable["server_profiles"], [])

    def test_connect_command_maps_short_connector_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = Path(tmp)
            for short, full in (
                ("chatgpt", "chatgpt-web"),
                ("claude", "claude-web"),
                ("hyperagent", "hyperagent-web"),
            ):
                output = io.StringIO()
                with redirect_stdout(output):
                    code = main(
                        (
                            "connect",
                            short,
                            "--repository",
                            str(repository),
                            "--diagnostics-only",
                        )
                    )
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(output.getvalue())["target_profile"], full)


if __name__ == "__main__":
    unittest.main()
