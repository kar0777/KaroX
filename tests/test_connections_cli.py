"""Tests for ``karox connections`` -- the CLI half of the connections surface.

The saved MCP client connections used to be reachable only from the TUI's
Connections screens.  That hid the one value nobody can retype from memory -- an
auto-generated bridge secret -- behind a mask with no readable form, and hid a
stale connection's dead URL behind a card that looked identical to a live one.

Covers: the list showing which address each connection actually answers on, the
secret masked by default and printed only behind the explicit reveal flag, the
pasted header line carrying the same masking decision as the secret it contains,
a bridge-namespace secret being resolved at all (the TUI's old prefix test
skipped it), a failed handshake reporting a non-zero exit code, removal taking
the stored secret with it, and a masked secret surviving a console whose code
page has no bullet character.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _support import SRC  # noqa: F401 - inserts src on sys.path

from karox.bridge import BridgeCredentialStore
from karox.cli import _write_line, main
from karox.connection_runtime import connection_runtime_manager
from karox.connections import (
    ConnectionCredentialStore,
    McpClientTarget,
    connection_registry,
    mask_secret,
)

SECRET = "0f_KkTvmgSPS86RzxtneOrJKkjzA2rCJeCpJWOHfZ78"


class _FakeBackend:
    """An in-memory keyring so the tests never touch the real OS store."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def set(self, service: str, account: str, secret: str) -> None:
        self.store[(service, account)] = secret

    def get(self, service: str, account: str) -> str | None:
        return self.store.get((service, account))

    def delete(self, service: str, account: str) -> None:
        if (service, account) not in self.store:
            import keyring.errors  # type: ignore[import-not-found]

            raise keyring.errors.PasswordDeleteError(account)
        del self.store[(service, account)]


class ConnectionsCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(
            os.environ,
            {
                "KAROX_VNEXT_CONFIG_DIR": str(self.root / "config"),
                "KAROX_VNEXT_RUNTIME_DIR": str(self.root / "runtime"),
            },
        )
        self.environment.start()
        self.backend = _FakeBackend()
        # Both namespaces are backed by the same fake so a test can put a secret
        # in either one and see which store the CLI actually reads.
        self.stores = (
            patch(
                "karox.connections.ConnectionCredentialStore",
                lambda *a, **k: ConnectionCredentialStore(self.backend),
            ),
            patch(
                "karox.bridge.BridgeCredentialStore",
                lambda *a, **k: BridgeCredentialStore(self.backend),
            ),
        )
        for item in self.stores:
            item.start()

    def tearDown(self) -> None:
        for item in self.stores:
            item.stop()
        self.environment.stop()
        self.temporary.cleanup()

    def invoke(self, *arguments: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(arguments)
        return code, stdout.getvalue(), stderr.getvalue()

    def _save(
        self,
        *,
        connection_id: str = "c-0123456789abcdef",
        name: str = "ClickUp",
        credential_ref: str | None = "os-keyring:bridge/clickup-1-abc",
        public_url: str | None = "https://tunnel.example.com",
        tunnel: str = "cloudflare",
        secret: str | None = SECRET,
    ) -> McpClientTarget:
        target = McpClientTarget(
            connection_id=connection_id,
            name=name,
            preset_id="clickup",
            transport="streamable_http",
            endpoint_path="/mcp",
            auth_scheme="bearer",
            tunnel=tunnel,
            runtime_profile="generic-streamable-http",
            url_stability="temporary",
            public_url=public_url,
            credential_ref=credential_ref,
        )
        connection_registry().put(target)
        if secret is not None and credential_ref is not None:
            store: object
            if credential_ref.startswith("os-keyring:bridge/"):
                store = BridgeCredentialStore(self.backend)
                account = credential_ref[len("os-keyring:bridge/"):]
            else:
                store = ConnectionCredentialStore(self.backend)
                account = credential_ref[len("os-keyring:connection/"):]
            store.set(account, secret)  # type: ignore[attr-defined]
        return target

    def test_show_masks_the_secret_and_reveals_it_only_when_asked(self) -> None:
        """The masked default is safe to paste; the value needs an explicit flag.

        This is the defect the CLI exists for: the TUI card showed only
        ``••••••••fZ78`` and offered no way to read the value, on the one
        connection whose secret the user cannot retype because setup generated
        it.
        """
        self._save()
        code, stdout, stderr = self.invoke(
            "connections", "show", "c-0123456789abcdef", "--json"
        )
        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertNotIn(SECRET, stdout)
        self.assertEqual(payload["secret"], mask_secret(SECRET))
        self.assertFalse(payload["secret_revealed"])
        # The header line is the field that actually gets pasted, so it carries
        # the same masking decision rather than leaking around it.
        self.assertNotIn(SECRET, payload["header"])
        self.assertTrue(payload["header"].startswith("Authorization: Bearer "))

        code, stdout, stderr = self.invoke(
            "connections", "show", "c-0123456789abcdef", "--reveal-secret", "--json"
        )
        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["secret"], SECRET)
        self.assertTrue(payload["secret_revealed"])
        self.assertEqual(payload["header"], f"Authorization: Bearer {SECRET}")

    def test_show_reads_the_bridge_namespace_a_clickup_secret_lives_in(self) -> None:
        """A ClickUp secret is in ``KaroX/bridge``, not ``KaroX/connection``.

        The TUI's copy button tested for the ``os-keyring:connection/`` prefix and
        so reported "no secret" for exactly this connection.  The CLI dispatches
        on the namespace instead, and this asserts it against a bridge-namespace
        reference with nothing at all in the connection namespace.
        """
        self._save(credential_ref="os-keyring:bridge/clickup-1-abc")
        self.assertEqual(
            self.backend.store.get(("KaroX/bridge", "clickup-1-abc")), SECRET
        )
        self.assertIsNone(self.backend.store.get(("KaroX/connection", "clickup-1-abc")))
        code, stdout, stderr = self.invoke(
            "connections", "show", "c-0123456789abcdef", "--reveal-secret", "--json"
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["secret"], SECRET)

    def test_copy_auth_puts_bearer_on_clipboard_without_printing_secret(self) -> None:
        self._save()
        copied: list[str] = []
        cleared: list[int] = []
        with (
            patch(
                "karox.cli.clipboard.write_text",
                side_effect=lambda value: copied.append(value) or True,
            ),
            patch(
                "karox.cli.clipboard.schedule_clear",
                side_effect=lambda seconds=120: cleared.append(int(seconds)),
            ),
        ):
            code, stdout, stderr = self.invoke(
                "connections", "copy-auth", "c-0123456789abcdef", "--json"
            )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(copied, [f"Bearer {SECRET}"])
        self.assertEqual(cleared, [120])
        self.assertNotIn(SECRET, stdout + stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "copied")
        self.assertEqual(payload["auto_clear_seconds"], 120)
        self.assertTrue(payload["credential_fingerprint"])

    def test_copy_auth_clipboard_failure_never_prints_secret(self) -> None:
        self._save()
        with (
            patch("karox.cli.clipboard.write_text", return_value=False),
            patch("karox.cli.clipboard.schedule_clear") as clear,
        ):
            code, stdout, stderr = self.invoke(
                "connections", "copy-auth", "c-0123456789abcdef", "--json"
            )
        self.assertEqual(code, 1)
        self.assertNotIn(SECRET, stdout + stderr)
        self.assertEqual(json.loads(stdout)["status"], "clipboard_unavailable")
        clear.assert_not_called()

    def test_list_shows_every_saved_connection_with_its_url(self) -> None:
        """Two records with the same display name are only distinguishable here.

        The user had two ``ClickUp`` connections pointing at different tunnels
        and no way to see that from the card, which is how a stale URL survived.
        """
        self._save()
        self._save(
            connection_id="c-fedcba9876543210",
            public_url="https://second.example.com",
        )
        code, stdout, stderr = self.invoke("connections", "list", "--json")
        self.assertEqual(code, 0, stderr)
        rows = json.loads(stdout)
        self.assertEqual(
            {row["connection_id"] for row in rows},
            {"c-0123456789abcdef", "c-fedcba9876543210"},
        )
        self.assertEqual(
            {row["url"] for row in rows},
            {
                "https://tunnel.example.com/mcp",
                "https://second.example.com/mcp",
            },
        )
        # A list is never a place to print secrets, whatever the flags say.
        self.assertNotIn(SECRET, stdout)
        for row in rows:
            self.assertEqual(row["secret"], "")
            self.assertFalse(row["secret_revealed"])

        code, stdout, stderr = self.invoke("connections", "list")
        self.assertEqual(code, 0, stderr)
        self.assertIn("c-0123456789abcdef\tClickUp\tbearer\tcloudflare", stdout)
        self.assertNotIn(SECRET, stdout)

    def test_test_reaches_the_bridge_namespace_secret_and_the_known_url(self) -> None:
        """The handshake must carry the real secret to the address it advertises.

        Both halves used to be wrong for exactly this connection: the secret was
        read only from ``os-keyring:connection/``, so a bridge-namespace one went
        out empty and returned 401 from a healthy bridge, and the URL was used
        only when the tunnel was ``custom``.
        """
        self._save()
        seen: dict[str, object] = {}

        def fake_test(target, *, endpoint_url, secret, timeout_seconds):
            seen.update(
                endpoint_url=endpoint_url,
                secret=secret,
                timeout_seconds=timeout_seconds,
            )
            return {"state": "ok", "tool_count": 4, "wire": "streamable_http"}

        with patch("karox.connection_tests.test_mcp_client_target", fake_test):
            code, stdout, stderr = self.invoke(
                "connections", "test", "c-0123456789abcdef", "--json"
            )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(seen["secret"], SECRET)
        self.assertEqual(seen["endpoint_url"], "https://tunnel.example.com/mcp")
        payload = json.loads(stdout)
        self.assertEqual(payload["tool_count"], 4)
        self.assertEqual(payload["url"], "https://tunnel.example.com/mcp")
        # The secret travelled to the bridge, not to the terminal.
        self.assertNotIn(SECRET, stdout + stderr)

    def test_failed_test_reports_a_nonzero_exit_code(self) -> None:
        """A dead tunnel has to be visible to a script, not only to a reader."""
        self._save()
        failure = {
            "state": "failed",
            "failure_kind": "dns_failure",
            "detail": "getaddrinfo failed",
        }
        with patch(
            "karox.connection_tests.test_mcp_client_target", return_value=failure
        ):
            code, stdout, _ = self.invoke(
                "connections", "test", "c-0123456789abcdef", "--json"
            )
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout)["failure_kind"], "dns_failure")

    def test_status_and_stop_use_the_managed_runtime_registry(self) -> None:
        target = self._save()
        stopped: list[str] = []
        manager = connection_runtime_manager()
        manager.register(
            connection_id=target.connection_id,
            session_id="clickup-1-abc",
            tunnel="cloudflare",
            local_endpoint="http://127.0.0.1:8765/mcp",
            public_endpoint="https://tunnel.example.com/mcp",
            bridge_pid=None,
            tunnel_pid=None,
            stop=lambda: stopped.append("yes"),
        )

        code, stdout, stderr = self.invoke(
            "connections", "status", target.connection_id, "--json"
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["state"], "running")

        code, stdout, stderr = self.invoke(
            "connections", "launch-support", target.connection_id, "--json"
        )
        self.assertEqual(code, 0, stderr)
        support = json.loads(stdout)
        self.assertTrue(support["supported"])
        self.assertEqual(support["launcher_id"], "saved-clickup")

        # Start is idempotent for an already-running managed runtime and does
        # not spawn another bridge.
        code, stdout, stderr = self.invoke(
            "connections", "start", target.connection_id, "--json"
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["status"], "already_running")
        self.assertEqual(stopped, [])

        def fake_restart(saved_target):
            manager.register(
                connection_id=saved_target.connection_id,
                session_id="clickup-1-abc-restarted",
                tunnel="cloudflare",
                local_endpoint="http://127.0.0.1:8765/mcp",
                public_endpoint="https://tunnel.example.com/mcp",
                bridge_pid=None,
                tunnel_pid=None,
                stop=lambda: stopped.append("second"),
            )
            return {
                "success": True,
                "target": saved_target,
                "public_endpoint": "https://tunnel.example.com/mcp",
                "local_endpoint": "http://127.0.0.1:8765/mcp",
                "runtime_id": "rt-restarted",
            }

        with patch(
            "karox.clickup_setup.start_saved_clickup_connection", fake_restart
        ):
            code, stdout, stderr = self.invoke(
                "connections", "restart", target.connection_id, "--json"
            )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["status"], "restarted")
        self.assertEqual(stopped, ["yes"])

        code, stdout, stderr = self.invoke(
            "connections", "stop", target.connection_id, "--json"
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["state"], "stopped")
        self.assertEqual(stopped, ["yes", "second"])

    def test_remove_stops_a_managed_runtime_before_deleting_credentials(self) -> None:
        target = self._save()
        stopped: list[str] = []
        connection_runtime_manager().register(
            connection_id=target.connection_id,
            session_id="clickup-1-abc",
            tunnel="cloudflare",
            local_endpoint="http://127.0.0.1:8765/mcp",
            public_endpoint="https://tunnel.example.com/mcp",
            bridge_pid=None,
            tunnel_pid=None,
            stop=lambda: stopped.append("yes"),
        )
        code, stdout, stderr = self.invoke(
            "connections", "remove", target.connection_id, "--json"
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["status"], "removed")
        self.assertEqual(stopped, ["yes"])
        self.assertEqual(
            connection_runtime_manager().status(target.connection_id)["state"],
            "configured_not_running",
        )

    def test_remove_takes_the_bridge_namespace_secret_with_it(self) -> None:
        """Removal must not leave a live bearer token behind in the keyring."""
        self._save()
        key = ("KaroX/bridge", "clickup-1-abc")
        self.assertEqual(self.backend.store.get(key), SECRET)
        code, stdout, stderr = self.invoke(
            "connections", "remove", "c-0123456789abcdef", "--json"
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["status"], "removed")
        self.assertEqual(connection_registry().list(), [])
        self.assertIsNone(self.backend.store.get(key))

    def test_unknown_connection_fails_without_a_traceback(self) -> None:
        code, _, stderr = self.invoke("connections", "show", "c-nope")
        self.assertEqual(code, 2)
        self.assertTrue(stderr.startswith("karox: "), stderr)

    def test_connect_clickup_diagnostics_use_independent_cloudflare_defaults(self) -> None:
        """The one-line launcher must not reuse the running ChatGPT bridge slot."""
        code, stdout, stderr = self.invoke(
            "connect",
            "clickup",
            "--repository",
            str(self.root),
            "--diagnostics-only",
        )
        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["connector"], "clickup")
        self.assertEqual(payload["tunnel"], "cloudflare")
        self.assertEqual(payload["port"], 8766)
        self.assertEqual(payload["auth_scheme"], "bearer")
        self.assertEqual(
            payload["authentication_method"], "Authorization header"
        )
        self.assertTrue(payload["independent_process"])
        self.assertTrue(payload["public_verification_required"])
        self.assertTrue(payload["temporary_connection"])
        self.assertIn("karox.repo.command", payload["tools"])
        self.assertIn("karox.tests.run", payload["tools"])
        self.assertIn("karox.runtime.status", payload["tools"])
        self.assertEqual(payload["verification_commands"][0][-2:], ["pytest", "-q"])

    def test_connect_clickup_ctrl_c_stops_and_removes_only_its_temporary_record(self) -> None:
        from karox.clickup_setup import ClickupSetupOutcome

        target = self._save(connection_id="c-temp-clickup-0001")
        stopped: list[str] = []
        outcome = ClickupSetupOutcome(
            success=True,
            target=target,
            public_endpoint="https://fresh.example.test/mcp",
            local_endpoint="http://127.0.0.1:8766/mcp",
            handshake={"state": "public_pending", "tool_count": 13},
            stop=lambda: stopped.append("yes"),
        )
        with (
            patch(
                "karox.clickup_setup.setup_clickup_connection",
                return_value=outcome,
            ) as setup,
            patch(
                "karox.connections.resolve_connection_secret",
                return_value=SECRET,
            ),
            patch("karox.connections.remove_connection") as remove,
            patch("karox.cli.time.sleep", side_effect=KeyboardInterrupt),
        ):
            code, stdout, stderr = self.invoke(
                "connect",
                "clickup",
                "--repository",
                str(self.root),
            )
        self.assertEqual(code, 0, stderr)
        self.assertTrue(setup.call_args.kwargs["keep_public_pending"])
        self.assertEqual(stopped, ["yes"])
        remove.assert_called_once_with(target.connection_id)
        self.assertIn("locally verified and kept online", stdout)
        self.assertIn("authoritative external verification", stdout)
        self.assertNotIn("publicly verified and ready", stdout)
        self.assertIn("temporary KaroX connection was removed", stdout)

    def test_masked_output_survives_a_console_that_cannot_encode_the_mask(self) -> None:
        """A display command must not die on a decorative character.

        ``mask_secret`` renders ``••••••••abcd``, and a Windows stdout on an OEM
        code page (``cp866``) cannot encode ``•``: printing it raised
        ``UnicodeEncodeError``, which the CLI reported as exit code 2 with the
        line half-written.  The identifying tail is what the reader needs, so the
        line degrades instead of failing.
        """

        class _Cp866Stream(io.StringIO):
            encoding = "cp866"

            def write(self, text: str) -> int:
                text.encode("cp866")  # raises UnicodeEncodeError on the mask
                return super().write(text)

        stream = _Cp866Stream()
        with patch("sys.stdout", stream):
            _write_line(mask_secret(SECRET))
        self.assertIn("fZ78", stream.getvalue())
        self.assertNotIn("•", stream.getvalue())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
