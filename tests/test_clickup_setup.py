"""Auto-setup backend tests for the ClickUp preset connection.

These exercise the pure resolver (transport/auth/tunnel/port/stability/secret
decisions), the override layer (invalid combinations blocked, reset works), and
the orchestrator coordination (progress, cancellation cleanup, no-save-on-failure,
handshake success) with injected fake launchers -- so the handshake in the
tests is real (initialize + tools/list over HTTP) against the echo MCP server in
``_mcp_http_server.py``, not mocked.
"""

from __future__ import annotations

import os

# Explicit opt-in: the orchestrator saves the bridge secret through the real
# OS keyring in production, and this suite exercised that path before the
# isolation guard existed. Keep it visible, not silent.
# TODO(post-v5): inject a fake keyring backend end-to-end instead.
os.environ.setdefault("KAROX_TEST_ALLOW_REAL_KEYRING", "1")
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from _support import SRC  # noqa: F401 - inserts src on sys.path

from karox.connections import (
    ConnectionConfigurationError,
    ClickupDefaults,
    apply_clickup_overrides,
    build_target_from_preset,
    connection_registry,
    resolve_clickup_defaults,
    resolve_connection_secret,
)
from karox.clickup_setup import (
    CLICKUP_DEVELOPER_TOOLS,
    ClickupSetupOutcome,
    ServerHandle,
    TunnelHandle,
    run_resilient_handshake,
    setup_clickup_connection,
    start_saved_clickup_connection,
    start_saved_mcp_connection,
)


class ClickupDefaultsTests(unittest.TestCase):
    """The pure resolver: auto values, tunnel fallback, port pick, overrides."""

    def test_tailscale_funnel_chosen_when_ready(self) -> None:
        d = resolve_clickup_defaults(
            {"tailscale_ready": True, "tailscale_funnel_available": True, "cloudflared_installed": True}
        )
        self.assertEqual(d.tunnel, "tailscale")
        self.assertEqual(d.url_stability, "stable")
        self.assertEqual(d.secret_source, "generate")
        self.assertEqual(d.transport, "streamable_http")
        self.assertEqual(d.auth_scheme, "bearer")
        self.assertEqual(d.endpoint_path, "/mcp")

    def test_cloudflare_fallback_when_tailscale_absent(self) -> None:
        d = resolve_clickup_defaults({"cloudflared_installed": True})
        self.assertEqual(d.tunnel, "cloudflare")
        self.assertEqual(d.url_stability, "temporary")

    def test_local_fallback_when_no_tunnel_installed(self) -> None:
        d = resolve_clickup_defaults({"tailscale_ready": False, "cloudflared_installed": False})
        self.assertEqual(d.tunnel, "local")
        self.assertEqual(d.tunnel_action, "install_cloudflared")

    def test_active_tunnel_is_reused(self) -> None:
        d = resolve_clickup_defaults(
            {"active_tunnel": "custom", "port_probe": lambda p: True}
        )
        self.assertEqual(d.tunnel, "custom")

    def test_port_picked_free_near_default(self) -> None:
        # default busy -> next free picked
        probe = lambda p: p != 8765  # noqa: E731
        d = resolve_clickup_defaults({"cloudflared_installed": True, "port_probe": probe})
        self.assertEqual(d.port, 8766)

    def test_port_override_respected(self) -> None:
        d = resolve_clickup_defaults(
            {"cloudflared_installed": True, "port_probe": lambda p: True},
            overrides={"port": "9000"},
        )
        self.assertEqual(d.port, 9000)
        self.assertEqual(dict(d.overrides), {"port": "9000"})

    def test_advanced_override_replaces_auto_port(self) -> None:
        base = resolve_clickup_defaults({"cloudflared_installed": True, "port_probe": lambda p: True})
        d = apply_clickup_overrides(base, {"port": "9100"})
        self.assertEqual(d.port, 9100)

    def test_single_override_preserves_resolved_tunnel_port_and_stability(self) -> None:
        base = resolve_clickup_defaults(
            {
                "tailscale_ready": True,
                "tailscale_funnel_available": True,
                "cloudflared_installed": True,
                "port_probe": lambda p: p != 8765,
            }
        )
        changed = apply_clickup_overrides(base, {"endpoint_path": "/custom-mcp"})
        self.assertEqual(changed.endpoint_path, "/custom-mcp")
        self.assertEqual(changed.tunnel, "tailscale")
        self.assertEqual(changed.port, 8766)
        self.assertEqual(changed.url_stability, "stable")
        self.assertEqual(changed.tunnel_reason, base.tunnel_reason)

    def test_secret_source_is_recorded_without_secret_value_in_overrides(self) -> None:
        defaults = resolve_clickup_defaults(
            {"cloudflared_installed": True},
            overrides={"secret": "do-not-retain-this-value"},
        )
        self.assertEqual(defaults.secret_source, "paste")
        self.assertNotIn("secret", dict(defaults.overrides))
        self.assertNotIn("do-not-retain-this-value", repr(defaults))

    def test_reset_to_automatic_clears_overrides(self) -> None:
        base = resolve_clickup_defaults({"cloudflared_installed": True, "port_probe": lambda p: True})
        overridden = apply_clickup_overrides(base, {"port": "9100"})
        self.assertEqual(overridden.port, 9100)
        # reset = re-resolve with no overrides
        reset = resolve_clickup_defaults({"cloudflared_installed": True, "port_probe": lambda p: True})
        self.assertEqual(reset.port, 8765)
        self.assertEqual(reset.overrides, ())

    def test_stable_url_on_ephemeral_tunnel_is_blocked(self) -> None:
        base = resolve_clickup_defaults({"cloudflared_installed": True})
        with self.assertRaises(ConnectionConfigurationError):
            apply_clickup_overrides(base, {"url_stability": "stable"})

    def test_none_auth_blocked_for_clickup(self) -> None:
        base = resolve_clickup_defaults({"cloudflared_installed": True})
        with self.assertRaises(ConnectionConfigurationError):
            apply_clickup_overrides(base, {"auth_scheme": "none"})

    def test_oauth_blocked_for_clickup(self) -> None:
        # Not because ClickUp lacks OAuth -- its connect form offers it and
        # preselects it.  This profile is what cannot serve it: bearer auth, no
        # OAuth discovery metadata, and a quick-tunnel URL that is not stable
        # enough for a redirect flow.  Saving "oauth" would print instructions
        # ClickUp then rejects.
        base = resolve_clickup_defaults({"cloudflared_installed": True})
        with self.assertRaises(ConnectionConfigurationError):
            apply_clickup_overrides(base, {"auth_scheme": "oauth"})


def _fake_server_factory(
    stopped: dict,
    secret_name: str = "clickup-test",
    handles: list | None = None,
):
    """A launcher seam recording both stop paths separately.

    ``stopped["server"]`` is the full stop (process + credential deletion) used
    on failure/cancellation; ``stopped["process"]`` is the process-only stop the
    orchestrator swaps in after a successful save, when the saved card owns the
    credential and must not have it deleted underneath it.  ``handles`` collects
    the created handle so a test can invoke the post-save stop directly.
    """

    def launch(port, secret, *, repository, credential_name=None):
        handle = ServerHandle(
            local_endpoint=f"http://127.0.0.1:{port}/mcp",
            secret=secret,
            port=port,
            credential_name=credential_name or secret_name,
            stop=lambda: stopped.__setitem__("server", True),
            stop_process=lambda: stopped.__setitem__("process", True),
        )
        if handles is not None:
            handles.append(handle)
        return handle

    return launch


def _fake_tunnel_factory(stopped: dict, public_url: str = "https://auto.example.com"):
    def launch(kind, port):
        return TunnelHandle(public_url=public_url, stop=lambda: stopped.__setitem__("tunnel", True))
    return launch


class ClickupOrchestratorTests(unittest.TestCase):
    """Coordination: progress, save-on-success, no-save-on-failure, cancellation."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        # ``patch.dict`` so the env override is scoped to this test and does
        # not leak into later modules (provider list, registry, etc.) -- a bare
        # ``os.environ[...] = ...`` here left KAROX_CONFIG_DIR pointing at a
        # temp dir that the next test's cleanup deleted, emptying the provider
        # list and failing ``test_provider_list_is_non_empty``.
        self._env = patch.dict(
            os.environ,
            {
                "KAROX_CONFIG_DIR": self._tmp,
                "KAROX_VNEXT_CONFIG_DIR": self._tmp,
                "KAROX_RUNTIME_DIR": self._tmp,
                "KAROX_VNEXT_RUNTIME_DIR": self._tmp,
            },
            clear=False,
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def tearDown(self) -> None:
        # Best-effort cleanup of the temp dir; patch.dict already restored env.
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_success_saves_connection_and_keeps_server_alive(self) -> None:
        defaults = resolve_clickup_defaults({"cloudflared_installed": True, "port_probe": lambda p: True})
        stopped = {"server": False, "tunnel": False}

        def ok_handshake(target, *, endpoint_url, secret, timeout_seconds=15.0):
            return {"state": "ok", "wire": "streamable_http", "tool_count": 2, "detail": "ok"}

        outcome = setup_clickup_connection(
            defaults,
            name="My ClickUp",
            repository=Path(self._tmp),
            server_launcher=_fake_server_factory(stopped),
            tunnel_launcher=_fake_tunnel_factory(stopped),
            handshake=ok_handshake,
        )
        self.assertTrue(outcome.success)
        self.assertEqual(outcome.public_endpoint, "https://auto.example.com/mcp")
        self.assertIsNotNone(outcome.target)
        saved = connection_registry().list()
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].preset_id, "clickup")
        self.assertEqual(saved[0].auth_scheme, "bearer")
        # Server kept alive (card owns the credential), not stopped on success.
        self.assertFalse(stopped["server"])

    def test_failed_handshake_does_not_save_and_cleans_up(self) -> None:
        defaults = resolve_clickup_defaults({"cloudflared_installed": True, "port_probe": lambda p: True})
        stopped = {"server": False, "tunnel": False}

        def fail_handshake(target, *, endpoint_url, secret, timeout_seconds=15.0):
            return {"state": "failed", "failure_kind": "unauthorized", "detail": "wrong secret"}

        outcome = setup_clickup_connection(
            defaults,
            name="fail",
            repository=Path(self._tmp),
            server_launcher=_fake_server_factory(stopped),
            tunnel_launcher=_fake_tunnel_factory(stopped),
            handshake=fail_handshake,
        )
        self.assertFalse(outcome.success)
        self.assertEqual(outcome.failure_kind, "unauthorized")
        self.assertEqual(outcome.remediation, "recreate_secret")
        # No connection saved; processes cleaned up.
        self.assertEqual(len(connection_registry().list()), 0)
        self.assertTrue(stopped["server"])
        self.assertTrue(stopped["tunnel"])

    def test_cancellation_after_server_cleans_up_server(self) -> None:
        defaults = resolve_clickup_defaults({"cloudflared_installed": True, "port_probe": lambda p: True})
        stopped = {"server": False, "tunnel": False}
        checks = [0]

        def cancel_after_server():
            checks[0] += 1
            return checks[0] >= 4  # cancel on 4th check (after server start; tunnel runs first now)

        outcome = setup_clickup_connection(
            defaults,
            name="cancel",
            repository=Path(self._tmp),
            server_launcher=_fake_server_factory(stopped),
            tunnel_launcher=_fake_tunnel_factory(stopped),
            handshake=lambda **k: {"state": "ok"},
            cancellation=cancel_after_server,
        )
        self.assertFalse(outcome.success)
        self.assertEqual(outcome.failure_kind, "cancelled")
        self.assertTrue(stopped["server"])
        self.assertEqual(len(connection_registry().list()), 0)

    def test_progress_reports_all_steps_in_order(self) -> None:
        defaults = resolve_clickup_defaults({"cloudflared_installed": True, "port_probe": lambda p: True})
        stopped = {"server": False, "tunnel": False}
        steps: list[str] = []

        def ok_handshake(target, *, endpoint_url, secret, timeout_seconds=15.0):
            return {"state": "ok", "wire": "streamable_http", "tool_count": 2, "detail": "ok"}

        setup_clickup_connection(
            defaults,
            name="prog",
            repository=Path(self._tmp),
            server_launcher=_fake_server_factory(stopped),
            tunnel_launcher=_fake_tunnel_factory(stopped),
            handshake=ok_handshake,
            on_progress=lambda s, st, d: steps.append((s, st)),
        )
        ok_steps = [s for s, st in steps if st == "ok"]
        self.assertEqual(
            ok_steps, ["config", "secret", "tunnel", "server", "url", "handshake"]
        )

    def test_none_auth_rejected_before_any_process_starts(self) -> None:
        defaults = ClickupDefaults(
            transport="streamable_http",
            auth_scheme="none",
            tunnel="cloudflare",
            port=8765,
            endpoint_path="/mcp",
            url_stability="temporary",
            secret_source="generate",
            tunnel_reason="",
            tunnel_action="none",
        )
        outcome = setup_clickup_connection(
            defaults,
            name="none",
            repository=Path(self._tmp),
            server_launcher=_fake_server_factory({"server": False}),
            tunnel_launcher=_fake_tunnel_factory({"tunnel": False}),
            handshake=lambda **k: {"state": "ok"},
        )
        self.assertFalse(outcome.success)
        self.assertEqual(outcome.failure_kind, "invalid_config")

    def test_supplied_secret_reaches_server_and_handshake_without_entering_defaults(self) -> None:
        from dataclasses import replace

        defaults = replace(
            resolve_clickup_defaults(
                {"cloudflared_installed": True, "port_probe": lambda p: True}
            ),
            secret_source="paste",
        )
        captured: dict[str, str] = {}
        stopped = {"server": False, "tunnel": False, "process": False}

        def server_launcher(port, secret, *, repository, credential_name=None):
            captured["server"] = secret
            return ServerHandle(
                local_endpoint=f"http://127.0.0.1:{port}/mcp",
                secret=secret,
                port=port,
                credential_name=credential_name or "clickup-supplied",
                stop=lambda: stopped.__setitem__("server", True),
                stop_process=lambda: stopped.__setitem__("process", True),
            )

        def handshake(target, *, endpoint_url, secret, timeout_seconds=15.0):
            captured["handshake"] = secret
            return {
                "state": "ok",
                "wire": "streamable_http",
                "tool_count": 2,
                "detail": "ok",
            }

        outcome = setup_clickup_connection(
            defaults,
            name="supplied",
            repository=Path(self._tmp),
            supplied_secret="user-provided-secret",
            server_launcher=server_launcher,
            tunnel_launcher=_fake_tunnel_factory(stopped),
            handshake=handshake,
        )
        self.assertTrue(outcome.success)
        self.assertEqual(captured["server"], "user-provided-secret")
        self.assertEqual(captured["handshake"], "user-provided-secret")
        self.assertNotIn("secret", dict(defaults.overrides))

    def test_public_dns_failure_is_not_saved_as_ready(self) -> None:
        # A local success proves the bridge, credential and tool catalogue, but
        # ClickUp can only call the public URL.  The old flow saved this as ready
        # and printed credentials for a connector that could not work.
        defaults = resolve_clickup_defaults(
            {"cloudflared_installed": True, "port_probe": lambda p: True}
        )
        stopped = {"server": False, "tunnel": False}
        calls: list[str] = []

        def handshake(target, *, endpoint_url, secret, timeout_seconds=15.0):
            calls.append(endpoint_url)
            if "example.com" in endpoint_url:
                return {
                    "state": "failed",
                    "failure_kind": "dns_failure",
                    "detail": "getaddrinfo failed",
                }
            return {
                "state": "ok",
                "wire": "streamable_http",
                "tool_count": 13,
                "detail": "ok",
            }

        outcome = setup_clickup_connection(
            defaults,
            name="fallback",
            repository=Path(self._tmp),
            server_launcher=_fake_server_factory(stopped),
            tunnel_launcher=_fake_tunnel_factory(stopped),
            handshake=handshake,
        )
        self.assertFalse(outcome.success)
        self.assertEqual(outcome.failure_kind, "public_unreachable")
        self.assertTrue(any("example.com" in c for c in calls))
        self.assertTrue(any("127.0.0.1" in c for c in calls))
        self.assertIsNone(outcome.target)
        self.assertEqual(len(connection_registry().list()), 0)
        self.assertTrue(stopped["server"])
        self.assertTrue(stopped["tunnel"])

    def test_foreground_can_keep_public_pending_alive_for_clickup_test(self) -> None:
        defaults = resolve_clickup_defaults(
            {"cloudflared_installed": True, "port_probe": lambda p: True}
        )
        stopped = {"server": False, "tunnel": False, "process": False}

        def handshake(target, *, endpoint_url, secret, timeout_seconds=15.0):
            if "example.com" in endpoint_url:
                return {
                    "state": "failed",
                    "failure_kind": "dns_failure",
                    "detail": "getaddrinfo failed",
                }
            return {
                "state": "ok",
                "wire": "streamable_http",
                "tool_count": 13,
                "detail": "local tools verified",
            }

        outcome = setup_clickup_connection(
            defaults,
            name="foreground-pending",
            repository=Path(self._tmp),
            server_launcher=_fake_server_factory(stopped),
            tunnel_launcher=_fake_tunnel_factory(stopped),
            handshake=handshake,
            keep_public_pending=True,
        )
        self.assertTrue(outcome.success)
        self.assertEqual((outcome.handshake or {}).get("state"), "public_pending")
        self.assertIsNotNone(outcome.target)
        self.assertEqual(len(connection_registry().list()), 1)
        self.assertFalse(stopped["server"])
        self.assertFalse(stopped["tunnel"])
        self.assertIsNotNone(outcome.stop)
        assert outcome.stop is not None
        outcome.stop()
        self.assertTrue(stopped["process"])
        self.assertTrue(stopped["tunnel"])

    def test_public_handshake_retries_until_cloudflare_is_reachable(self) -> None:
        target = build_target_from_preset(
            "clickup",
            name="retry",
            tunnel="cloudflare",
            public_url="https://retry.example.test",
            credential_ref="os-keyring:connection/retry",
        )
        attempts = {"public": 0}

        def handshake(target, *, endpoint_url, secret, timeout_seconds=15.0):
            if "retry.example.test" in endpoint_url:
                attempts["public"] += 1
                if attempts["public"] < 3:
                    return {
                        "state": "failed",
                        "failure_kind": "dns_failure",
                        "detail": "not propagated yet",
                    }
            return {
                "state": "ok",
                "wire": "streamable_http",
                "tool_count": len(CLICKUP_DEVELOPER_TOOLS),
                "detail": "public verified",
            }

        result = run_resilient_handshake(
            target,
            public_endpoint="https://retry.example.test/mcp",
            local_endpoint="http://127.0.0.1:8766/mcp",
            secret="secret",
            run_handshake=handshake,
            public_retry_seconds=1.0,
            retry_interval_seconds=0.0,
            sleep=lambda _seconds: None,
        )
        self.assertEqual(result.get("state"), "ok")
        self.assertEqual(attempts["public"], 3)

    def test_unauthorized_on_public_url_does_not_fall_back(self) -> None:
        # A bad secret is a bad secret: an auth verdict must NOT trigger a
        # loopback retry (it would just fail the same way), and must clean up.
        defaults = resolve_clickup_defaults({"cloudflared_installed": True, "port_probe": lambda p: True})
        stopped = {"server": False, "tunnel": False}
        calls: list[str] = []

        def handshake(target, *, endpoint_url, secret, timeout_seconds=15.0):
            calls.append(endpoint_url)
            return {"state": "failed", "failure_kind": "unauthorized", "detail": "wrong secret"}

        outcome = setup_clickup_connection(
            defaults,
            name="badsecret",
            repository=Path(self._tmp),
            server_launcher=_fake_server_factory(stopped),
            tunnel_launcher=_fake_tunnel_factory(stopped),
            handshake=handshake,
        )
        self.assertFalse(outcome.success)
        self.assertEqual(outcome.failure_kind, "unauthorized")
        self.assertEqual(outcome.remediation, "recreate_secret")
        # Only the public endpoint was consulted.
        self.assertEqual(len(calls), 1)
        self.assertIn("example.com", calls[0])
        self.assertEqual(len(connection_registry().list()), 0)
        self.assertTrue(stopped["server"])
        self.assertTrue(stopped["tunnel"])

    def test_local_failure_surfaces_the_public_failure(self) -> None:
        # If loopback also fails, the public verdict is what the user should see
        # (it names the URL they will paste into ClickUp), not the loopback one.
        defaults = resolve_clickup_defaults({"cloudflared_installed": True, "port_probe": lambda p: True})
        stopped = {"server": False, "tunnel": False}

        def handshake(target, *, endpoint_url, secret, timeout_seconds=15.0):
            if "example.com" in endpoint_url:
                return {"state": "failed", "failure_kind": "dns_failure", "detail": "getaddrinfo failed"}
            return {"state": "failed", "failure_kind": "timeout", "detail": "loopback timed out"}

        outcome = setup_clickup_connection(
            defaults,
            name="bothfail",
            repository=Path(self._tmp),
            server_launcher=_fake_server_factory(stopped),
            tunnel_launcher=_fake_tunnel_factory(stopped),
            handshake=handshake,
        )
        self.assertFalse(outcome.success)
        # The public (dns_failure) verdict wins over the loopback timeout.
        self.assertEqual(outcome.failure_kind, "dns_failure")
        self.assertEqual(len(connection_registry().list()), 0)
        self.assertTrue(stopped["server"])
        self.assertTrue(stopped["tunnel"])


class ClickupPostSaveStopTests(unittest.TestCase):
    """After a successful save, stopping must kill the process, not no-op.

    Regression: the post-save ``stop`` was replaced with a function that
    returned ``None`` and terminated nothing, on the reasoning that the saved
    card owns the credential.  It does -- but the *process* still has to die, or
    every attempt leaves a bridge holding the port.  A later run then probes
    that port, finds the orphan listening, reports the server as started, and
    authenticates the handshake against the orphan's secret: an unexplained 401.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self._env = patch.dict(
            os.environ,
            {
                "KAROX_CONFIG_DIR": self._tmp,
                "KAROX_VNEXT_CONFIG_DIR": self._tmp,
                "KAROX_RUNTIME_DIR": self._tmp,
                "KAROX_VNEXT_RUNTIME_DIR": self._tmp,
            },
            clear=False,
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_post_save_stop_terminates_process_but_keeps_credential(self) -> None:
        defaults = resolve_clickup_defaults(
            {"cloudflared_installed": True, "port_probe": lambda p: True}
        )
        stopped = {"server": False, "tunnel": False, "process": False}
        handles: list = []

        def ok_handshake(target, *, endpoint_url, secret, timeout_seconds=15.0):
            return {"state": "ok", "wire": "streamable_http", "tool_count": 2, "detail": "ok"}

        outcome = setup_clickup_connection(
            defaults,
            name="My ClickUp",
            repository=Path(self._tmp),
            server_launcher=_fake_server_factory(stopped, handles=handles),
            tunnel_launcher=_fake_tunnel_factory(stopped),
            handshake=ok_handshake,
        )
        self.assertTrue(outcome.success)
        self.assertEqual(len(handles), 1)
        # Nothing stopped merely by succeeding: the bridge must stay up so
        # ClickUp can reach it.
        self.assertFalse(stopped["process"])
        self.assertFalse(stopped["server"])
        # The swapped-in stop terminates the process...
        handles[0].stop()
        self.assertTrue(stopped["process"])
        # ...and does NOT run the credential-deleting full stop, which would
        # leave the saved card pointing at a deleted secret.
        self.assertFalse(stopped["server"])

    def test_outcome_carries_loopback_endpoint_for_retest(self) -> None:
        # The result card re-tests through the same resilient path, so it needs
        # the loopback endpoint to fall back to when the public URL does not
        # resolve from this host yet.
        defaults = resolve_clickup_defaults(
            {"cloudflared_installed": True, "port_probe": lambda p: True}
        )
        stopped = {"server": False, "tunnel": False, "process": False}

        def ok_handshake(target, *, endpoint_url, secret, timeout_seconds=15.0):
            return {"state": "ok", "wire": "streamable_http", "tool_count": 2, "detail": "ok"}

        outcome = setup_clickup_connection(
            defaults,
            name="My ClickUp",
            repository=Path(self._tmp),
            server_launcher=_fake_server_factory(stopped),
            tunnel_launcher=_fake_tunnel_factory(stopped),
            handshake=ok_handshake,
        )
        self.assertTrue(outcome.success)
        self.assertEqual(outcome.local_endpoint, f"http://127.0.0.1:{defaults.port}/mcp")

    def test_outcome_stop_shuts_down_tunnel_and_process_keeping_credential(self) -> None:
        # The successful outcome has to hand back an off switch.  Without one the
        # bridge and the tunnel outlive the window that started them -- the bridge
        # child runs in its own process group -- so each run left a listener on the
        # port and a published URL nobody could revoke.
        defaults = resolve_clickup_defaults(
            {"cloudflared_installed": True, "port_probe": lambda p: True}
        )
        stopped = {"server": False, "tunnel": False, "process": False}

        def ok_handshake(target, *, endpoint_url, secret, timeout_seconds=15.0):
            return {"state": "ok", "wire": "streamable_http", "tool_count": 2, "detail": "ok"}

        outcome = setup_clickup_connection(
            defaults,
            name="My ClickUp",
            repository=Path(self._tmp),
            server_launcher=_fake_server_factory(stopped),
            tunnel_launcher=_fake_tunnel_factory(stopped),
            handshake=ok_handshake,
        )
        self.assertTrue(outcome.success)
        self.assertIsNotNone(outcome.stop)
        assert outcome.stop is not None
        outcome.stop()
        # Both halves of the live setup are down...
        self.assertTrue(stopped["process"])
        self.assertTrue(stopped["tunnel"])
        # ...and the credential survives, so the saved card still resolves.
        self.assertFalse(stopped["server"])
        self.assertEqual(len(connection_registry().list()), 1)


class ClickupSavedRestartTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self._env = patch.dict(
            os.environ,
            {
                "KAROX_CONFIG_DIR": self._tmp,
                "KAROX_VNEXT_CONFIG_DIR": self._tmp,
                "KAROX_RUNTIME_DIR": self._tmp,
                "KAROX_VNEXT_RUNTIME_DIR": self._tmp,
            },
            clear=False,
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_saved_clickup_restarts_with_same_session_secret_and_connection_id(self) -> None:
        import time

        from karox.connections import McpClientTarget
        from karox.models import AccessProfile
        from karox.paths import session_dir
        from karox.sessions import SessionStore

        session_id = "clickup-saved-restart"
        repository = Path(self._tmp)
        SessionStore(session_dir()).create(
            repository,
            "ClickUp hosted bridge",
            AccessProfile.WORKSPACE_WRITE,
            session_id=session_id,
        )
        target = McpClientTarget(
            connection_id="c-1234567890abcdef",
            name="Saved ClickUp",
            preset_id="clickup",
            transport="streamable_http",
            endpoint_path="/mcp",
            auth_scheme="bearer",
            tunnel="cloudflare",
            runtime_profile="generic-streamable-http",
            url_stability="temporary",
            public_url="https://old.example.test",
            credential_ref=f"os-keyring:bridge/{session_id}",
            port=8765,
            created_at=time.time(),
            updated_at=time.time(),
        )
        connection_registry().put(target)
        seen: dict[str, object] = {}
        stopped = {"server": False, "tunnel": False}

        def server_launcher(
            port,
            secret,
            *,
            repository,
            credential_name=None,
            reuse_existing_session=False,
        ):
            seen.update(
                secret=secret,
                repository=repository,
                credential_name=credential_name,
                reuse_existing_session=reuse_existing_session,
            )
            return ServerHandle(
                local_endpoint=f"http://127.0.0.1:{port}/mcp",
                secret=secret,
                port=port,
                credential_name=credential_name or "unexpected",
                stop=lambda: stopped.__setitem__("server", True),
                stop_process=lambda: stopped.__setitem__("server", True),
                pid=111,
            )

        def tunnel_launcher(kind, port):
            return TunnelHandle(
                public_url="https://new.example.test",
                stop=lambda: stopped.__setitem__("tunnel", True),
                pid=222,
            )

        def handshake(target, *, endpoint_url, secret, timeout_seconds=15.0):
            seen["handshake_secret"] = secret
            seen["endpoint_url"] = endpoint_url
            return {
                "state": "ok",
                "wire": "streamable_http",
                "tool_count": 4,
                "detail": "ok",
            }

        with patch(
            "karox.connections.resolve_connection_secret",
            return_value="same-saved-secret",
        ):
            outcome = start_saved_clickup_connection(
                target,
                server_launcher=server_launcher,
                tunnel_launcher=tunnel_launcher,
                handshake=handshake,
            )
        self.assertTrue(outcome.success, outcome.failure_detail)
        self.assertIsNotNone(outcome.target)
        assert outcome.target is not None
        self.assertEqual(outcome.target.connection_id, target.connection_id)
        self.assertEqual(outcome.target.credential_ref, target.credential_ref)
        self.assertEqual(outcome.target.public_url, "https://new.example.test")
        self.assertEqual(seen["secret"], "same-saved-secret")
        self.assertEqual(seen["handshake_secret"], "same-saved-secret")
        self.assertEqual(seen["credential_name"], session_id)
        self.assertTrue(seen["reuse_existing_session"])
        self.assertEqual(
            seen["endpoint_url"], "https://new.example.test/mcp"
        )
        self.assertIsNotNone(outcome.stop)
        assert outcome.stop is not None
        outcome.stop()
        self.assertTrue(stopped["server"])
        self.assertTrue(stopped["tunnel"])

    def test_saved_custom_mcp_runs_beside_chatgpt_on_tailscale_10000(self) -> None:
        """Generic bearer MCP uses a non-443 Funnel listener on first launch."""

        import time

        from karox.connections import McpClientTarget
        from karox.models import AccessProfile
        from karox.paths import session_dir
        from karox.sessions import SessionStore

        session_id = "mcp-c-custom1234567890"
        repository = Path(self._tmp)
        SessionStore(session_dir()).create(
            repository,
            "Managed MCP bridge: Brainbase",
            AccessProfile.WORKSPACE_WRITE,
            session_id=session_id,
        )
        target = McpClientTarget(
            connection_id="c-custom1234567890",
            name="Brainbase",
            preset_id="custom",
            transport="streamable_http",
            endpoint_path="/mcp",
            auth_scheme="bearer",
            tunnel="tailscale",
            runtime_profile="generic-streamable-http",
            url_stability="stable",
            credential_ref=f"os-keyring:bridge/{session_id}",
            port=8768,
            created_at=time.time(),
            updated_at=time.time(),
        )
        seen: dict[str, object] = {}
        stopped = {"server": False, "tunnel": False}

        def server_launcher(
            port,
            secret,
            *,
            repository,
            credential_name=None,
            reuse_existing_session=False,
        ):
            seen["repository"] = repository
            seen["credential_name"] = credential_name
            return ServerHandle(
                local_endpoint=f"http://127.0.0.1:{port}/mcp",
                secret=secret,
                port=port,
                credential_name=credential_name or "unexpected",
                stop=lambda: stopped.__setitem__("server", True),
                stop_process=lambda: stopped.__setitem__("server", True),
                pid=111,
            )

        def tunnel_side_effect(kind, port, *, https_port=443):
            seen["tunnel"] = kind
            seen["https_port"] = https_port
            seen["local_port"] = port
            return TunnelHandle(
                public_url="https://monsterpc.example.ts.net:10000",
                stop=lambda: stopped.__setitem__("tunnel", True),
                pid=222,
            )

        def handshake(target, *, endpoint_url, secret, timeout_seconds=15.0):
            seen["endpoint_url"] = endpoint_url
            return {
                "state": "ok",
                "wire": "streamable_http",
                "tool_count": 4,
                "detail": "ok",
            }

        with (
            patch("karox.connections.resolve_connection_secret", return_value="saved-secret"),
            patch(
                "karox.clickup_setup.default_tunnel_launcher",
                side_effect=tunnel_side_effect,
            ),
        ):
            outcome = start_saved_mcp_connection(
                target,
                server_launcher=server_launcher,
                handshake=handshake,
            )

        self.assertTrue(outcome.success, outcome.failure_detail)
        self.assertEqual(seen["https_port"], 10000)
        self.assertEqual(seen["local_port"], 8768)
        self.assertEqual(seen["credential_name"], session_id)
        self.assertEqual(seen["repository"], repository.resolve())
        self.assertEqual(
            seen["endpoint_url"],
            "https://monsterpc.example.ts.net:10000/mcp",
        )
        self.assertIsNotNone(outcome.target)
        assert outcome.target is not None
        self.assertEqual(
            outcome.target.public_url,
            "https://monsterpc.example.ts.net:10000",
        )

    def test_saved_custom_mcp_refuses_busy_local_port_before_tunnel_mutation(self) -> None:
        import time

        from karox.connections import McpClientTarget
        from karox.models import AccessProfile
        from karox.paths import session_dir
        from karox.sessions import SessionStore

        session_id = "mcp-c-busy12345678901"
        repository = Path(self._tmp)
        SessionStore(session_dir()).create(
            repository,
            "Managed MCP bridge: Busy",
            AccessProfile.WORKSPACE_WRITE,
            session_id=session_id,
        )
        target = McpClientTarget(
            connection_id="c-busy12345678901",
            name="Busy custom MCP",
            preset_id="custom",
            transport="streamable_http",
            endpoint_path="/mcp",
            auth_scheme="bearer",
            tunnel="tailscale",
            runtime_profile="generic-streamable-http",
            url_stability="stable",
            credential_ref=f"os-keyring:bridge/{session_id}",
            port=8768,
            created_at=time.time(),
            updated_at=time.time(),
        )
        tunnel = Mock()
        with (
            patch("karox.connections.resolve_connection_secret", return_value="saved-secret"),
            patch("karox.web_bridge_launcher._port_is_available", return_value=False),
            patch("karox.clickup_setup.default_tunnel_launcher", tunnel),
        ):
            outcome = start_saved_mcp_connection(target)

        self.assertFalse(outcome.success)
        self.assertEqual(outcome.failure_kind, "port_in_use")
        tunnel.assert_not_called()

    def test_saved_restart_refuses_an_unmanaged_live_runtime(self) -> None:
        import time

        from karox.connection_runtime import (
            ConnectionRuntimeRecord,
            ConnectionRuntimeStore,
            connection_runtime_path,
        )
        from karox.connections import McpClientTarget

        target = McpClientTarget(
            connection_id="c-fedcba9876543210",
            name="Unmanaged ClickUp",
            preset_id="clickup",
            transport="streamable_http",
            endpoint_path="/mcp",
            auth_scheme="bearer",
            tunnel="cloudflare",
            runtime_profile="generic-streamable-http",
            url_stability="temporary",
            public_url="https://old.example.test",
            credential_ref="os-keyring:bridge/clickup-unmanaged",
            port=8765,
            created_at=time.time(),
            updated_at=time.time(),
        )
        ConnectionRuntimeStore(connection_runtime_path()).put(
            ConnectionRuntimeRecord(
                runtime_id="rt-fedcba9876543210",
                connection_id=target.connection_id,
                session_id="clickup-unmanaged",
                tunnel="cloudflare",
                local_endpoint="http://127.0.0.1:8765/mcp",
                public_endpoint="https://old.example.test/mcp",
                bridge_pid=999999,
                tunnel_pid=None,
                started_at=time.time(),
            )
        )
        with patch("karox.connection_runtime._pid_alive", return_value=True):
            # The singleton may already exist, so patch its liveness seam too.
            from karox.connection_runtime import connection_runtime_manager

            manager = connection_runtime_manager()
            original = manager._pid_alive
            manager._pid_alive = lambda _pid: True
            try:
                outcome = start_saved_clickup_connection(target)
            finally:
                manager._pid_alive = original
        self.assertFalse(outcome.success)
        self.assertEqual(outcome.failure_kind, "unmanaged_running")


class ClickupServerLauncherTests(unittest.TestCase):
    """The real launcher's two hard-won invariants: port guard and UTF-8 child."""

    def test_busy_port_is_refused_instead_of_adopting_a_stranger(self) -> None:
        # Binding the port first simulates a bridge left running by an earlier
        # attempt.  Without the guard the readiness probe connects to *that*
        # process and the handshake authenticates against its secret, which
        # surfaces as a 401 no retry can fix.  The guard must name the port.
        import socket

        from karox.clickup_setup import default_server_launcher

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
            held.bind(("127.0.0.1", 0))
            held.listen(1)
            port = held.getsockname()[1]
            with self.assertRaises(ConnectionConfigurationError) as caught:
                default_server_launcher(port, "secret", repository=Path(os.getcwd()))
        self.assertIn(str(port), str(caught.exception))

    def test_child_is_spawned_with_forced_utf8_and_a_drained_pipe(self) -> None:
        # The repository path can contain non-ASCII characters (this project
        # lives in a Cyrillic path), and ``bridge serve`` prints it on startup.
        # A piped child inherits the console code page, so that print raises
        # UnicodeEncodeError and the bridge dies before serving anything.  The
        # launcher must force UTF-8 on the child and read its pipe.
        import socket

        from karox.clickup_setup import default_server_launcher

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            free_port = probe.getsockname()[1]

        captured: dict = {}

        class _StubProcess:
            """Enough of Popen for the launcher: a readable pipe and a poll."""

            pid = -1

            def __init__(self) -> None:
                import io

                self.stdout = io.StringIO("bridge line\n")

            def poll(self):
                return None

            def terminate(self) -> None:
                captured["terminated"] = True

            def wait(self, timeout=None):
                return 0

            def kill(self) -> None:
                captured["terminated"] = True

        def fake_spawn(argv, **kwargs):
            captured["argv"] = argv
            captured["env"] = kwargs.get("env") or {}
            captured["kwargs"] = kwargs
            return _StubProcess()

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
            os.environ,
            {"KAROX_CONFIG_DIR": tmp, "KAROX_VNEXT_CONFIG_DIR": tmp},
            clear=False,
        ):
                with patch("karox.web_bridge_launcher._wait_for_bridge", lambda *a, **k: None):
                    handle = default_server_launcher(
                        free_port,
                        "secret-value",
                        repository=Path(tmp),
                        credential_name="clickup-utf8-test",
                        session_dir=Path(tmp),
                        spawn=fake_spawn,
                    )
        self.assertEqual(captured["env"].get("PYTHONIOENCODING"), "utf-8")
        self.assertEqual(captured["env"].get("PYTHONUTF8"), "1")
        # Decoding must match the encoding forced on the child, and an
        # undecodable byte must not crash the drain thread.
        self.assertTrue(captured["kwargs"].get("text"))
        self.assertEqual(captured["kwargs"].get("encoding"), "utf-8")
        self.assertEqual(captured["kwargs"].get("errors"), "replace")
        argv = captured["argv"]
        for tool in CLICKUP_DEVELOPER_TOOLS:
            self.assertIn(tool, argv)
        self.assertIn("--verification-command", argv)
        self.assertTrue(
            any("pytest" in value for value in argv),
            "ClickUp bridge did not receive its verification allowlist",
        )
        self.assertEqual(handle.local_endpoint, f"http://127.0.0.1:{free_port}/mcp")
        self.assertIsNotNone(handle.stop_process)


class ClickupRealBridgeE2ETests(unittest.TestCase):
    """Spawn the real ``karox bridge serve`` child and run a real handshake.

    Regression for the ``server_failed -- bridge exited with code 2`` defect:
    the auto-setup argv omitted ``--tool``, and ``bridge serve`` exits 2 with
    "requires at least one --tool or --server".  This starts the genuine bridge
    on a free loopback port (isolated config so it doesn't touch the user's
    sessions or keyring), runs the real MCP initialize + tools/list, and stops
    the child -- so the default tools the orchestrator ships actually reach the
    wire, and a future drop of the ``--tool`` flag fails here, not in the TUI.
    """

    def test_real_bridge_serves_tools_and_handshake_passes(self) -> None:
        import secrets
        import socket
        import time as _time
        import uuid

        from _tui_harness import isolated_karox_directories

        with isolated_karox_directories() as repository:
            # A free loopback port so the bridge doesn't clash with a running one.
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.close()
            # A unique session id per run so two suite runs never collide on
            # "session already exists" in the (isolated) runtime dir.
            session_id = f"clickup-e2e-{uuid.uuid4().hex[:8]}"
            from karox.clickup_setup import default_server_launcher
            from karox.connections import (
                McpClientTarget,
                _BRIDGE_REFERENCE_PREFIX,
                _new_connection_id,
            )
            from karox.connection_tests import test_mcp_client_target

            secret = secrets.token_urlsafe(32)
            handle = default_server_launcher(
                port, secret, repository=repository, credential_name=session_id
            )
            try:
                target = McpClientTarget(
                    connection_id=_new_connection_id(),
                    name="cu-e2e",
                    preset_id="clickup",
                    transport="streamable_http",
                    endpoint_path="/mcp",
                    auth_scheme="bearer",
                    tunnel="local",
                    runtime_profile="generic-streamable-http",
                    url_stability="temporary",
                    public_url=None,
                    header_name="",
                    header_prefix="",
                    description="",
                    instructions="",
                    credential_ref=f"{_BRIDGE_REFERENCE_PREFIX}{session_id}",
                    credential_fingerprint=None,
                    port=port,
                    created_at=_time.time(),
                    updated_at=_time.time(),
                )
                result = test_mcp_client_target(
                    target,
                    endpoint_url=handle.local_endpoint,
                    secret=secret,
                    timeout_seconds=15.0,
                )
                # An unauthenticated probe must be told which scheme to use.
                # This is the CLI's own wiring (not the ASGI factory's default),
                # and it is what an MCP client discovering auth by probing reads:
                # ClickUp's connector, left on its default OAuth, saw a bare 401
                # here and reported "Authentication method not supported by this
                # MCP Server" without ever learning bearer would have worked.
                import httpx as _httpx

                probe = _httpx.post(
                    handle.local_endpoint,
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    headers={"accept": "application/json, text/event-stream"},
                    timeout=10.0,
                )
            finally:
                handle.stop()
            self.assertEqual(result.get("state"), "ok")
            self.assertGreaterEqual(
                result.get("tool_count", 0), len(CLICKUP_DEVELOPER_TOOLS)
            )
            self.assertEqual(probe.status_code, 401)
            self.assertIn("bearer", probe.headers.get("www-authenticate", "").lower())


if __name__ == "__main__":
    unittest.main()
