"""Focused tests for the B7 saved-bridge service API.

Tests the canonical ``start_saved_bridge`` / ``stop_saved_bridge`` functions in
``karox.web_bridge_launcher`` that the TUI ServiceConnectScreen S and X keys
call. These verify:

* idempotency: a proven live bridge is reused, not relaunched;
* foreign process on the port is never touched;
* missing profile / credential / repository produce a clear error;
* stop only acts on a proven owned PID;
* stop never touches credentials, OAuth, ClickUp, or Chrome;
* the result never contains a Bearer secret, approval password, OAuth token,
  Authorization header, tool arguments, or file contents.

No test here launches a real process or reads a real keyring: the ownership
check, credential check, endpoint verification, and detached-launch are all
mocked so the contract is pinned without side effects.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from _support import SRC  # noqa: F401 - inserts src on sys.path

from karox.port_ownership import (
    OWNERSHIP_FREE,
    OWNERSHIP_REUSE_SAME,
    OWNERSHIP_STALE_OWNED,
    OWNERSHIP_UNRELATED,
    OwnershipMetadata,
    OwnershipVerdict,
)
from karox.connection_status import CredentialStatus
from karox.models import AccessProfile
from karox.sessions import SessionStore
from karox.web_bridge_launcher import (
    _overall_status,
    _sync_saved_bridge_session_access_profile,
    restart_saved_bridge,
    saved_web_bridge_session_id,
    start_saved_bridge,
    stop_saved_bridge,
)


# ---------------------------------------------------------------------------
# Fake saved profile
# ---------------------------------------------------------------------------


def _fake_profile(
    *,
    name: str = "chatgpt-pc",
    port: int = 8765,
    repository: str = "/repo",
) -> MagicMock:
    profile = MagicMock()
    profile.name = name
    profile.port = port
    profile.repository = repository
    return profile


def _existing_repo() -> MagicMock:
    """A mock path whose is_dir() returns True, for tests that pass the repo check."""
    repo = MagicMock()
    repo.is_dir.return_value = True
    return repo


def _empty_metadata() -> OwnershipMetadata:
    return OwnershipMetadata(
        profile=None,
        credential_reference=None,
        session_id=None,
        pid=None,
        process_start_time_ns=None,
        executable_path=None,
        repository=None,
        local_host=None,
        local_port=None,
        public_url=None,
        tunnel_type=None,
        route_identity=None,
        config_digest=None,
        creation_timestamp=None,
        watchdog_path=None,
        pid_proven=False,
    )


def _verdict(
    code: str,
    *,
    pid: int | None = None,
    pid_proven: bool = False,
    public_url: str = "",
    unrelated_pid: int | None = None,
    owned_orphan_pid: int | None = None,
    live_unrecorded_owner_pid: int | None = None,
) -> OwnershipVerdict:
    metadata = OwnershipMetadata(
        profile="chatgpt-pc",
        credential_reference="os-keyring:bridge/web-saved-abc" if pid else None,
        session_id="web-saved-abc" if pid else None,
        pid=pid,
        process_start_time_ns=1234567890 if pid_proven else None,
        executable_path=None,
        repository=None,
        local_host="127.0.0.1",
        local_port=8765,
        public_url=public_url or None,
        tunnel_type="tailscale",
        route_identity=None,
        config_digest=None,
        creation_timestamp=1000000.0,
        watchdog_path=None,
        pid_proven=pid_proven,
    )
    return OwnershipVerdict(
        verdict=code,
        reason=f"test verdict: {code}",
        metadata=metadata,
        unrelated_pid=unrelated_pid,
        owned_orphan_pid=owned_orphan_pid,
        live_unrecorded_owner_pid=live_unrecorded_owner_pid,
    )


_SECRET_KEYS = (
    "secret",
    "token",
    "authorization",
    "bearer",
    "password",
    "api_key",
    "apikey",
)


def _assert_no_secrets(testcase: unittest.TestCase, result: dict) -> None:
    """Assert no key in the result dict looks like a secret field."""
    for key in result:
        lower = str(key).lower()
        for forbidden in _SECRET_KEYS:
            testcase.assertNotIn(
                forbidden,
                lower,
                f"result key '{key}' looks like a secret field",
            )
    # The credential_fingerprint is allowed (sha256:..., not the secret).
    if "credential_fingerprint" in result:
        fp = str(result["credential_fingerprint"] or "")
        testcase.assertTrue(
            fp.startswith("sha256:") or fp == "",
            f"fingerprint must be sha256-prefixed or empty, got: {fp!r}",
        )


# ---------------------------------------------------------------------------
# start_saved_bridge
# ---------------------------------------------------------------------------


class StartSavedBridgeIdempotencyTests(unittest.TestCase):
    """A proven live bridge is reused, never relaunched."""

    @patch("karox.web_bridge_launcher._saved_bridge_repository", return_value=_existing_repo())
    @patch("karox.bridge.BridgeCredentialStore")
    @patch("karox.web_bridge_launcher._verify_bridge_endpoint")
    @patch("karox.connection_status._check_bridge_credential")
    @patch("karox.port_ownership.check_port_ownership")
    @patch("karox.web_bridge_profiles.WebBridgeProfileStore")
    def test_reuse_same_profile_returns_reused_without_launch(
        self,
        mock_store_cls: MagicMock,
        mock_ownership: MagicMock,
        mock_cred: MagicMock,
        mock_verify: MagicMock,
        mock_cred_store_cls: MagicMock,
        mock_repo: MagicMock,
    ) -> None:
        mock_store_cls.return_value.get.return_value = _fake_profile()
        mock_ownership.return_value = _verdict(
            OWNERSHIP_REUSE_SAME,
            pid=99999,
            pid_proven=True,
            public_url="https://example.ts.net",
        )
        mock_cred.return_value = (CredentialStatus.AVAILABLE, "sha256:abc123")
        mock_cred_store_cls.return_value.resolve.return_value = "test-secret"
        mock_verify.return_value = {
            "unauth_401": True,
            "auth_initialized": True,
            "tools_list_ok": True,
            "tool_count": 37,
            "endpoint": "https://example.ts.net/mcp",
        }

        with patch("karox.web_bridge_launcher.subprocess.Popen") as mock_popen:
            result = start_saved_bridge("chatgpt-pc")

        # No second bridge was launched.
        mock_popen.assert_not_called()
        self.assertEqual(result["action"], "reused")
        self.assertEqual(result["bridge_pid"], 99999)
        self.assertEqual(result["public_url"], "https://example.ts.net")
        self.assertTrue(result["unauth_401"])
        self.assertTrue(result["auth_initialized"])
        self.assertTrue(result["tools_list_ok"])
        self.assertEqual(result["tool_count"], 37)
        _assert_no_secrets(self, result)

    @patch("karox.web_bridge_launcher._saved_bridge_repository", return_value=_existing_repo())
    @patch("karox.web_bridge_launcher._verify_bridge_endpoint")
    @patch("karox.bridge.BridgeCredentialStore")
    @patch("karox.connection_status._check_bridge_credential")
    @patch("karox.port_ownership.check_port_ownership")
    @patch("karox.web_bridge_profiles.WebBridgeProfileStore")
    def test_reuse_same_profile_resolves_secret_internally_only(
        self,
        mock_store_cls: MagicMock,
        mock_ownership: MagicMock,
        mock_cred: MagicMock,
        mock_cred_store_cls: MagicMock,
        mock_verify: MagicMock,
        mock_repo: MagicMock,
    ) -> None:
        """The secret is resolved inside the service for verification and never returned."""
        mock_store_cls.return_value.get.return_value = _fake_profile()
        mock_ownership.return_value = _verdict(
            OWNERSHIP_REUSE_SAME,
            pid=99999,
            pid_proven=True,
            public_url="https://example.ts.net",
        )
        mock_cred.return_value = (CredentialStatus.AVAILABLE, "sha256:abc123")
        mock_cred_store_cls.return_value.resolve.return_value = "super-secret-value"
        mock_verify.return_value = {
            "unauth_401": True,
            "auth_initialized": True,
            "tools_list_ok": True,
            "tool_count": 5,
            "endpoint": "https://example.ts.net/mcp",
        }

        result = start_saved_bridge("chatgpt-pc")

        # The secret was resolved for verification...
        mock_cred_store_cls.return_value.resolve.assert_called_once()
        # ...but the result does not contain it.
        self.assertNotIn(str(result), "super-secret-value")
        _assert_no_secrets(self, result)


class StartSavedBridgeForeignProcessTests(unittest.TestCase):
    """A foreign process on the port is never touched."""

    @patch("karox.web_bridge_launcher._saved_bridge_repository", return_value=_existing_repo())
    @patch("karox.connection_status._check_bridge_credential")
    @patch("karox.port_ownership.check_port_ownership")
    @patch("karox.web_bridge_profiles.WebBridgeProfileStore")
    def test_foreign_process_returns_error_without_launch(
        self,
        mock_store_cls: MagicMock,
        mock_ownership: MagicMock,
        mock_cred: MagicMock,
        mock_repo: MagicMock,
    ) -> None:
        mock_store_cls.return_value.get.return_value = _fake_profile()
        mock_ownership.return_value = _verdict(
            OWNERSHIP_UNRELATED,
            unrelated_pid=12345,
        )
        mock_cred.return_value = (CredentialStatus.AVAILABLE, "sha256:abc123")

        with patch("karox.web_bridge_launcher.subprocess.Popen") as mock_popen:
            result = start_saved_bridge("chatgpt-pc")

        mock_popen.assert_not_called()
        self.assertEqual(result["action"], "error")
        self.assertIn("unrelated", result["error"].lower())
        self.assertEqual(result.get("unrelated_pid"), 12345)
        _assert_no_secrets(self, result)


class StartSavedBridgeOrphanedListenerTests(unittest.TestCase):
    """An owner can die and leave its own ``bridge serve`` child on the port.

    That child is provably ours, so recovery must reclaim the port instead of
    reporting a foreign holder and deadlocking every future restart.
    """

    @patch("karox.web_bridge_launcher._saved_bridge_repository", return_value=_existing_repo())
    @patch("karox.web_bridge_launcher._reclaim_orphaned_bridge_listener")
    @patch("karox.connection_status._check_bridge_credential")
    @patch("karox.port_ownership.check_port_ownership")
    @patch("karox.web_bridge_profiles.WebBridgeProfileStore")
    def test_orphaned_owned_listener_is_reclaimed_before_launch(
        self,
        mock_store_cls: MagicMock,
        mock_ownership: MagicMock,
        mock_cred: MagicMock,
        mock_reclaim: MagicMock,
        mock_repo: MagicMock,
    ) -> None:
        mock_store_cls.return_value.get.return_value = _fake_profile()
        mock_cred.return_value = (CredentialStatus.AVAILABLE, "sha256:abc123")
        mock_ownership.return_value = _verdict(
            OWNERSHIP_STALE_OWNED,
            owned_orphan_pid=4242,
        )
        mock_reclaim.return_value = True

        with patch("karox.web_bridge_launcher.subprocess.Popen") as mock_popen, \
             patch("karox.saved_bridge_supervisor.set_saved_bridge_desired_running"):
            mock_popen.return_value.pid = 5555
            mock_popen.return_value.poll.return_value = None
            start_saved_bridge("chatgpt-pc", timeout_seconds=0.1)

        self.assertEqual(mock_reclaim.call_args.args[0], 4242)
        self.assertEqual(mock_reclaim.call_args.kwargs.get("port"), 8765)
        mock_popen.assert_called_once()

    @patch("karox.web_bridge_launcher._saved_bridge_repository", return_value=_existing_repo())
    @patch("karox.web_bridge_launcher._reclaim_orphaned_bridge_listener")
    @patch("karox.connection_status._check_bridge_credential")
    @patch("karox.port_ownership.check_port_ownership")
    @patch("karox.web_bridge_profiles.WebBridgeProfileStore")
    def test_launch_is_refused_when_the_orphan_cannot_be_reclaimed(
        self,
        mock_store_cls: MagicMock,
        mock_ownership: MagicMock,
        mock_cred: MagicMock,
        mock_reclaim: MagicMock,
        mock_repo: MagicMock,
    ) -> None:
        mock_store_cls.return_value.get.return_value = _fake_profile()
        mock_cred.return_value = (CredentialStatus.AVAILABLE, "sha256:abc123")
        mock_ownership.return_value = _verdict(
            OWNERSHIP_STALE_OWNED,
            owned_orphan_pid=4242,
        )
        mock_reclaim.return_value = False

        with patch("karox.web_bridge_launcher.subprocess.Popen") as mock_popen:
            result = start_saved_bridge("chatgpt-pc", timeout_seconds=0.1)

        mock_popen.assert_not_called()
        self.assertEqual(result["action"], "error")
        self.assertIn("reclaim", result["error"].lower())
        _assert_no_secrets(self, result)

    @patch("karox.web_bridge_launcher._saved_bridge_repository", return_value=_existing_repo())
    @patch("karox.web_bridge_launcher._reclaim_orphaned_bridge_listener")
    @patch("karox.connection_status._check_bridge_credential")
    @patch("karox.port_ownership.check_port_ownership")
    @patch("karox.web_bridge_profiles.WebBridgeProfileStore")
    def test_stale_owner_without_an_orphan_pid_reclaims_nothing(
        self,
        mock_store_cls: MagicMock,
        mock_ownership: MagicMock,
        mock_cred: MagicMock,
        mock_reclaim: MagicMock,
        mock_repo: MagicMock,
    ) -> None:
        mock_store_cls.return_value.get.return_value = _fake_profile()
        mock_cred.return_value = (CredentialStatus.AVAILABLE, "sha256:abc123")
        # A live-but-unverifiable owner: there is no proven PID to terminate.
        mock_ownership.return_value = _verdict(OWNERSHIP_STALE_OWNED, pid=999)

        with patch("karox.web_bridge_launcher.subprocess.Popen") as mock_popen, \
             patch("karox.saved_bridge_supervisor.set_saved_bridge_desired_running"):
            mock_popen.return_value.pid = 5555
            mock_popen.return_value.poll.return_value = None
            start_saved_bridge("chatgpt-pc", timeout_seconds=0.1)

        mock_reclaim.assert_not_called()


class StartSavedBridgeLiveUnrecordedOwnerTests(unittest.TestCase):
    """A live owner that lost its watchdog record must be recycled, not fought.

    Reclaiming only its listener is futile: the live owner respawns the child and
    every freshly spawned owner then dies binding the port, which is exactly the
    "detached bridge process exited before becoming ready" loop users hit.
    """

    @patch("karox.web_bridge_launcher._saved_bridge_repository", return_value=_existing_repo())
    @patch("karox.web_bridge_launcher._recycle_unrecorded_saved_bridge_owner")
    @patch("karox.web_bridge_launcher._reclaim_orphaned_bridge_listener")
    @patch("karox.connection_status._check_bridge_credential")
    @patch("karox.port_ownership.check_port_ownership")
    @patch("karox.web_bridge_profiles.WebBridgeProfileStore")
    def test_the_proven_live_owner_is_recycled_instead_of_its_child(
        self,
        mock_store_cls: MagicMock,
        mock_ownership: MagicMock,
        mock_cred: MagicMock,
        mock_reclaim: MagicMock,
        mock_recycle: MagicMock,
        mock_repo: MagicMock,
    ) -> None:
        mock_store_cls.return_value.get.return_value = _fake_profile()
        mock_cred.return_value = (CredentialStatus.AVAILABLE, "sha256:abc123")
        mock_ownership.return_value = _verdict(
            OWNERSHIP_STALE_OWNED,
            owned_orphan_pid=4242,
            live_unrecorded_owner_pid=909,
        )
        mock_recycle.return_value = True

        with patch("karox.web_bridge_launcher.subprocess.Popen") as mock_popen, \
             patch("karox.saved_bridge_supervisor.set_saved_bridge_desired_running"):
            mock_popen.return_value.pid = 5555
            mock_popen.return_value.poll.return_value = None
            start_saved_bridge("chatgpt-pc", timeout_seconds=0.1)

        self.assertEqual(mock_recycle.call_args.args[0], 909)
        self.assertEqual(mock_recycle.call_args.kwargs.get("profile_name"), "chatgpt-pc")
        mock_reclaim.assert_not_called()
        mock_popen.assert_called_once()

    @patch("karox.web_bridge_launcher._saved_bridge_repository", return_value=_existing_repo())
    @patch("karox.web_bridge_launcher._recycle_unrecorded_saved_bridge_owner")
    @patch("karox.connection_status._check_bridge_credential")
    @patch("karox.port_ownership.check_port_ownership")
    @patch("karox.web_bridge_profiles.WebBridgeProfileStore")
    def test_a_launch_is_refused_when_that_owner_cannot_be_recycled(
        self,
        mock_store_cls: MagicMock,
        mock_ownership: MagicMock,
        mock_cred: MagicMock,
        mock_recycle: MagicMock,
        mock_repo: MagicMock,
    ) -> None:
        mock_store_cls.return_value.get.return_value = _fake_profile()
        mock_cred.return_value = (CredentialStatus.AVAILABLE, "sha256:abc123")
        mock_ownership.return_value = _verdict(
            OWNERSHIP_STALE_OWNED,
            owned_orphan_pid=4242,
            live_unrecorded_owner_pid=909,
        )
        mock_recycle.return_value = False

        with patch("karox.web_bridge_launcher.subprocess.Popen") as mock_popen:
            result = start_saved_bridge("chatgpt-pc", timeout_seconds=0.1)

        mock_popen.assert_not_called()
        self.assertEqual(result["action"], "error")
        self.assertIn("watchdog record", result["error"])
        _assert_no_secrets(self, result)


class StartSavedBridgeEarlyExitDiagnosticsTests(unittest.TestCase):
    """An early child exit must explain itself, and only about *this* child.

    The parent's console is DEVNULL, so the owner's redacted exit record is the
    only evidence. A record left by a previous owner must never be presented as
    the reason this launch failed.
    """

    def _run_with_exit_record(self, payload: dict | None) -> dict:
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record_path = root / "exit.json"
            if payload is not None:
                record_path.write_text(json.dumps(payload), encoding="utf-8")

            with patch(
                "karox.web_bridge_profiles.WebBridgeProfileStore"
            ) as mock_store_cls, patch(
                "karox.connection_status._check_bridge_credential"
            ) as mock_cred, patch(
                "karox.port_ownership.check_port_ownership"
            ) as mock_ownership, patch(
                "karox.web_bridge_launcher._saved_bridge_repository",
                return_value=_existing_repo(),
            ), patch(
                "karox.web_bridge_launcher.owner_exit_record_path",
                return_value=record_path,
            ), patch(
                "karox.web_bridge_launcher.watchdog_dir", return_value=root
            ), patch(
                "karox.saved_bridge_supervisor.set_saved_bridge_desired_running"
            ), patch(
                "karox.web_bridge_launcher.subprocess.Popen"
            ) as mock_popen:
                mock_store_cls.return_value.get.return_value = _fake_profile()
                mock_cred.return_value = (CredentialStatus.AVAILABLE, "sha256:abc123")
                mock_ownership.return_value = _verdict(OWNERSHIP_FREE)
                mock_popen.return_value.pid = 5555
                # The child is already gone on the first poll.
                mock_popen.return_value.poll.return_value = 3
                return start_saved_bridge("chatgpt-pc", timeout_seconds=5.0)

    def test_a_fresh_exit_record_becomes_the_reported_reason(self) -> None:
        result = self._run_with_exit_record(
            {
                "reason": "WebBridgeLaunchError",
                "detail": "KaroX bridge exited with code 3: bind on 127.0.0.1:8765 failed",
                "recorded_at": time.time(),
            }
        )
        self.assertEqual(result["action"], "error")
        self.assertIn("exit reason: WebBridgeLaunchError", result["error"])
        self.assertIn("bind", result["error"])
        _assert_no_secrets(self, result)

    def test_a_previous_owners_record_is_not_blamed_on_this_launch(self) -> None:
        result = self._run_with_exit_record(
            {
                "reason": "StaleReasonFromAnOlderOwner",
                "detail": "this happened long before the current spawn",
                "recorded_at": time.time() - 3600.0,
            }
        )
        self.assertEqual(result["action"], "error")
        self.assertNotIn("StaleReasonFromAnOlderOwner", result["error"])
        self.assertIn("exited before becoming ready", result["error"])

    def test_no_record_still_reports_the_generic_early_exit(self) -> None:
        result = self._run_with_exit_record(None)
        self.assertEqual(result["action"], "error")
        self.assertIn("exited before becoming ready", result["error"])


class StartSavedBridgeMissingProfileTests(unittest.TestCase):
    """A missing or invalid profile produces a clear error."""

    @patch("karox.web_bridge_profiles.WebBridgeProfileStore")
    def test_invalid_profile_name_returns_error(self, mock_store_cls: MagicMock) -> None:
        from karox.web_bridge_profiles import WebBridgeProfileError

        mock_store_cls.return_value.get.side_effect = WebBridgeProfileError("not found")

        result = start_saved_bridge("nonexistent")

        self.assertEqual(result["action"], "error")
        self.assertIn("not found", result["error"])
        _assert_no_secrets(self, result)


class StartSavedBridgeMissingCredentialTests(unittest.TestCase):
    """A first-run missing credential must not deadlock saved-profile bootstrap."""

    @patch("karox.web_bridge_launcher._saved_bridge_repository", return_value=_existing_repo())
    @patch("karox.connection_status._check_bridge_credential")
    @patch("karox.port_ownership.check_port_ownership")
    @patch("karox.web_bridge_profiles.WebBridgeProfileStore")
    def test_missing_credential_reaches_ownership_instead_of_short_circuiting(
        self,
        mock_store_cls: MagicMock,
        mock_ownership: MagicMock,
        mock_cred: MagicMock,
        mock_repo: MagicMock,
    ) -> None:
        mock_store_cls.return_value.get.return_value = _fake_profile()
        mock_cred.return_value = (CredentialStatus.MISSING, None)
        # Use an unrelated port holder to stop after the bootstrap gate without
        # spawning a child. If missing credentials still short-circuit, this
        # specific ownership error can never be reached.
        mock_ownership.return_value = _verdict(
            OWNERSHIP_UNRELATED,
            unrelated_pid=12345,
        )

        with patch("karox.web_bridge_launcher.subprocess.Popen") as mock_popen:
            result = start_saved_bridge("chatgpt-pc")

        mock_popen.assert_not_called()
        self.assertEqual(result["action"], "error")
        self.assertIn("unrelated", result["error"].lower())
        self.assertNotIn("credential is missing", result["error"].lower())
        _assert_no_secrets(self, result)


class StartSavedBridgeMissingRepositoryTests(unittest.TestCase):
    """A non-existent repository produces a clear error before any launch."""

    @patch("karox.connection_status._check_bridge_credential")
    @patch("karox.web_bridge_profiles.WebBridgeProfileStore")
    def test_missing_repository_returns_error_without_launch(
        self,
        mock_store_cls: MagicMock,
        mock_cred: MagicMock,
    ) -> None:
        profile = _fake_profile(repository="/nonexistent/path/that/does/not/exist")
        mock_store_cls.return_value.get.return_value = profile
        mock_cred.return_value = (CredentialStatus.AVAILABLE, "sha256:abc123")

        with patch("karox.web_bridge_launcher.subprocess.Popen") as mock_popen:
            result = start_saved_bridge("chatgpt-pc")

        mock_popen.assert_not_called()
        self.assertEqual(result["action"], "error")
        self.assertIn("repository", result["error"].lower())
        _assert_no_secrets(self, result)


class StartSavedBridgeDetachedLaunchTests(unittest.TestCase):
    """When the port is free, a detached production bridge is launched."""

    @patch("karox.web_bridge_launcher._saved_bridge_repository", return_value=_existing_repo())
    @patch("karox.web_bridge_launcher._verify_bridge_endpoint")
    @patch("karox.bridge.BridgeCredentialStore")
    @patch("karox.connection_status._check_bridge_credential")
    @patch("karox.port_ownership.check_port_ownership")
    @patch("karox.web_bridge_profiles.WebBridgeProfileStore")
    def test_free_port_launches_detached_cli_and_verifies(
        self,
        mock_store_cls: MagicMock,
        mock_ownership: MagicMock,
        mock_cred: MagicMock,
        mock_cred_store_cls: MagicMock,
        mock_verify: MagicMock,
        mock_repo: MagicMock,
    ) -> None:
        import karox.web_bridge_launcher as launcher

        mock_store_cls.return_value.get.return_value = _fake_profile()
        # Port is free → launch path.
        mock_ownership.return_value = _verdict(OWNERSHIP_FREE)
        mock_cred.return_value = (CredentialStatus.AVAILABLE, "sha256:abc123")
        mock_cred_store_cls.return_value.resolve.return_value = "test-secret"

        # Watchdog polling: simulate the watchdog file appearing with a ready bridge.
        watchdog_record = {
            "session_id": "web-saved-test",
            "owner_pid": 55555,
            "bridge_pid": 55556,
            "tunnel_pid": 55557,
            "public_url": "https://example.ts.net",
            "port": 8765,
            "tunnel": "tailscale",
        }

        mock_verify.return_value = {
            "unauth_401": True,
            "auth_initialized": True,
            "tools_list_ok": True,
            "tool_count": 37,
            "endpoint": "https://example.ts.net/mcp",
        }

        # Patch the watchdog path to exist and return our record.
        with patch.object(launcher, "watchdog_dir", return_value=Path("/fake/watchdog")):
            with patch("pathlib.Path.exists", return_value=True):
                with patch("pathlib.Path.read_text", return_value=json.dumps(watchdog_record)):
                    with patch.object(launcher, "_process_is_alive", return_value=True):
                        with patch("karox.web_bridge_launcher.subprocess.Popen") as mock_popen:
                            mock_popen.return_value.poll.return_value = None
                            result = start_saved_bridge("chatgpt-pc")

        # A detached process was launched with the CLI connect --saved command.
        mock_popen.assert_called_once()
        launch_argv = mock_popen.call_args[0][0]
        self.assertIn("bridge", launch_argv)
        self.assertIn("connect", launch_argv)
        self.assertIn("--saved", launch_argv)
        self.assertIn("chatgpt-pc", launch_argv)
        if os.name == "nt":
            flags = mock_popen.call_args.kwargs["creationflags"]
            self.assertTrue(flags & subprocess.DETACHED_PROCESS)
            self.assertTrue(flags & subprocess.CREATE_NO_WINDOW)
            self.assertTrue(flags & subprocess.CREATE_BREAKAWAY_FROM_JOB)

        self.assertEqual(result["action"], "started")
        self.assertEqual(result["bridge_pid"], 55556)
        self.assertEqual(result["public_url"], "https://example.ts.net")
        self.assertTrue(result["unauth_401"])
        self.assertTrue(result["auth_initialized"])
        self.assertEqual(result["tool_count"], 37)
        _assert_no_secrets(self, result)

    @patch("karox.web_bridge_launcher._saved_bridge_repository", return_value=_existing_repo())
    @patch("karox.web_bridge_launcher._verify_bridge_endpoint")
    @patch("karox.bridge.BridgeCredentialStore")
    @patch("karox.connection_status._check_bridge_credential")
    @patch("karox.port_ownership.check_port_ownership")
    @patch("karox.web_bridge_profiles.WebBridgeProfileStore")
    def test_free_port_persists_running_intent_before_detached_launch(
        self,
        mock_store_cls: MagicMock,
        mock_ownership: MagicMock,
        mock_cred: MagicMock,
        mock_cred_store_cls: MagicMock,
        mock_verify: MagicMock,
        mock_repo: MagicMock,
    ) -> None:
        import karox.web_bridge_launcher as launcher

        mock_store_cls.return_value.get.return_value = _fake_profile()
        mock_ownership.return_value = _verdict(OWNERSHIP_FREE)
        mock_cred.return_value = (CredentialStatus.AVAILABLE, "sha256:abc123")
        mock_cred_store_cls.return_value.resolve.return_value = "test-secret"
        mock_verify.return_value = {
            "unauth_401": True,
            "auth_initialized": True,
            "tools_list_ok": True,
            "tool_count": 37,
            "endpoint": "https://example.ts.net/mcp",
        }
        watchdog_record = {
            "session_id": "web-saved-test",
            "owner_pid": 55555,
            "bridge_pid": 55556,
            "tunnel_pid": None,
            "public_url": "https://example.ts.net",
            "port": 8765,
            "tunnel": "tailscale",
        }
        events: list[str] = []

        with (
            patch.object(launcher, "watchdog_dir", return_value=Path("/fake/watchdog")),
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.read_text", return_value=json.dumps(watchdog_record)),
            patch.object(launcher, "_process_is_alive", return_value=True),
            patch(
                "karox.saved_bridge_supervisor.set_saved_bridge_desired_running",
                side_effect=lambda *_: events.append("intent"),
            ),
            patch("karox.web_bridge_launcher.subprocess.Popen") as mock_popen,
        ):
            mock_popen.side_effect = lambda *_args, **_kwargs: (
                events.append("launch") or MagicMock(poll=MagicMock(return_value=None))
            )
            result = start_saved_bridge("chatgpt-pc")

        self.assertEqual(result["action"], "started")
        self.assertEqual(events[:2], ["intent", "launch"])

    @patch("karox.web_bridge_launcher._saved_bridge_repository", return_value=_existing_repo())
    @patch("karox.web_bridge_launcher._verify_bridge_endpoint")
    @patch("karox.bridge.BridgeCredentialStore")
    @patch("karox.connection_status._check_bridge_credential")
    @patch("karox.port_ownership.check_port_ownership")
    @patch("karox.web_bridge_profiles.WebBridgeProfileStore")
    def test_a_bridge_that_dies_after_readiness_is_not_reported_started(
        self,
        mock_store_cls: MagicMock,
        mock_ownership: MagicMock,
        mock_cred: MagicMock,
        mock_cred_store_cls: MagicMock,
        mock_verify: MagicMock,
        mock_repo: MagicMock,
    ) -> None:
        """A crash between the watchdog write and the return must read as an error.

        This was the restore defect: the TUI said "Bridge запущен" while the
        process had already exited, and the status line still said "KaroX не
        запущен". The result must be honest so the caller can retry or repair.
        """

        import karox.web_bridge_launcher as launcher

        mock_store_cls.return_value.get.return_value = _fake_profile()
        mock_ownership.return_value = _verdict(OWNERSHIP_FREE)
        mock_cred.return_value = (CredentialStatus.AVAILABLE, "sha256:abc123")
        mock_cred_store_cls.return_value.resolve.return_value = "test-secret"
        mock_verify.return_value = {
            "unauth_401": False,
            "auth_initialized": False,
            "tools_list_ok": False,
            "tool_count": 0,
            "endpoint": "https://example.ts.net/mcp",
        }
        watchdog_record = {
            "session_id": "web-saved-test",
            "owner_pid": 55555,
            "bridge_pid": 55556,
            "tunnel_pid": 55557,
            "public_url": "https://example.ts.net",
            "port": 8765,
            "tunnel": "tailscale",
        }
        with patch.object(launcher, "watchdog_dir", return_value=Path("/fake/watchdog")):
            with patch("pathlib.Path.exists", return_value=True):
                with patch("pathlib.Path.read_text", return_value=json.dumps(watchdog_record)):
                    # Alive for the readiness loop, dead for the final check.
                    with patch.object(
                        launcher, "_process_is_alive", side_effect=[True, False]
                    ):
                        with patch("karox.web_bridge_launcher.subprocess.Popen") as mock_popen:
                            mock_popen.return_value.poll.return_value = None
                            result = start_saved_bridge("chatgpt-pc")

        self.assertEqual(result["action"], "error")
        self.assertIn("exited right after", result["error"])
        self.assertFalse(result.get("unauth_401", True))
        _assert_no_secrets(self, result)


class OverallStatusTests(unittest.TestCase):
    """The _overall_status helper maps verification booleans to a redacted status."""

    def test_fully_verified(self) -> None:
        self.assertEqual(
            _overall_status(auth_initialized=True, tools_list_ok=True, tool_count=37),
            "ready_for_chatgpt_setup",
        )

    def test_initialized_only(self) -> None:
        self.assertEqual(
            _overall_status(auth_initialized=True, tools_list_ok=False, tool_count=0),
            "waiting_for_chatgpt",
        )

    def test_not_initialized(self) -> None:
        self.assertEqual(
            _overall_status(auth_initialized=False, tools_list_ok=False, tool_count=0),
            "bridge_started_unverified",
        )


# ---------------------------------------------------------------------------
# restart_saved_bridge / stop_saved_bridge
# ---------------------------------------------------------------------------


class RestartSavedBridgeTests(unittest.TestCase):
    def test_access_profile_switch_updates_durable_session_without_rotating_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "mini app"
            repository.mkdir()
            sessions_root = root / "sessions"
            profile_name = "hyperagent-mini-app"
            session_id = saved_web_bridge_session_id(profile_name)
            sessions = SessionStore(sessions_root)
            original = sessions.create(
                repository,
                "hyperagent saved bridge",
                AccessProfile.WORKSPACE_WRITE,
                session_id=session_id,
            )
            profile = MagicMock()
            profile.repository = str(repository)
            profile.access_profile = AccessProfile.ELEVATED

            with patch("karox.web_bridge_launcher.session_dir", return_value=sessions_root):
                changed = _sync_saved_bridge_session_access_profile(profile_name, profile)

            self.assertTrue(changed)
            updated = sessions.load(session_id)
            self.assertEqual(updated.session_id, original.session_id)
            self.assertEqual(updated.repository, original.repository)
            self.assertEqual(updated.created_at, original.created_at)
            self.assertEqual(updated.access_profile, AccessProfile.ELEVATED.value)

            profile.access_profile = AccessProfile.WORKSPACE_WRITE
            with patch("karox.web_bridge_launcher.session_dir", return_value=sessions_root):
                changed_back = _sync_saved_bridge_session_access_profile(profile_name, profile)
            self.assertTrue(changed_back)
            self.assertEqual(
                sessions.load(session_id).access_profile,
                AccessProfile.WORKSPACE_WRITE.value,
            )

    @patch("karox.web_bridge_launcher.start_saved_bridge")
    @patch("karox.web_bridge_launcher.stop_saved_bridge")
    @patch("karox.web_bridge_launcher._port_is_available", return_value=True)
    @patch("karox.web_bridge_profiles.WebBridgeProfileStore")
    def test_restart_stops_waits_then_starts_without_rotating_identity(
        self,
        mock_store_cls: MagicMock,
        mock_port_free: MagicMock,
        mock_stop: MagicMock,
        mock_start: MagicMock,
    ) -> None:
        mock_store_cls.return_value.get.return_value = _fake_profile()
        mock_stop.return_value = {
            "saved_profile": "chatgpt-pc",
            "action": "stopped",
            "verdict": OWNERSHIP_REUSE_SAME,
            "error": None,
        }
        mock_start.return_value = {
            "saved_profile": "chatgpt-pc",
            "action": "started",
            "public_url": "https://example.ts.net",
            "tool_count": 37,
            "error": None,
        }

        result = restart_saved_bridge("chatgpt-pc")

        mock_stop.assert_called_once_with(
            "chatgpt-pc",
            allow_legacy_migration=False,
        )
        mock_port_free.assert_called_with(8765)
        mock_start.assert_called_once_with("chatgpt-pc", timeout_seconds=120.0)
        self.assertEqual(result["action"], "restarted")
        self.assertEqual(result["public_url"], "https://example.ts.net")
        self.assertEqual(result["tool_count"], 37)
        _assert_no_secrets(self, result)

    @patch("karox.web_bridge_launcher.start_saved_bridge")
    @patch("karox.web_bridge_launcher.stop_saved_bridge")
    @patch("karox.web_bridge_profiles.WebBridgeProfileStore")
    def test_restart_does_not_start_when_safe_stop_failed(
        self,
        mock_store_cls: MagicMock,
        mock_stop: MagicMock,
        mock_start: MagicMock,
    ) -> None:
        mock_store_cls.return_value.get.return_value = _fake_profile()
        mock_stop.return_value = {
            "saved_profile": "chatgpt-pc",
            "action": "error",
            "error": "owner could not be stopped safely",
        }

        result = restart_saved_bridge("chatgpt-pc")

        mock_start.assert_not_called()
        self.assertEqual(result["action"], "error")
        self.assertEqual(result["phase"], "stop")
        _assert_no_secrets(self, result)


class StopSavedBridgeTests(unittest.TestCase):
    """Stop only acts on a proven owned PID and never touches credentials."""

    @patch("karox.port_ownership.check_port_ownership")
    @patch("karox.web_bridge_profiles.WebBridgeProfileStore")
    def test_stop_proven_owned_bridge(self, mock_store_cls: MagicMock, mock_ownership: MagicMock) -> None:
        mock_store_cls.return_value.get.return_value = _fake_profile()
        mock_ownership.return_value = _verdict(
            OWNERSHIP_REUSE_SAME,
            pid=99999,
            pid_proven=True,
            public_url="https://example.ts.net",
        )

        request_path = MagicMock()
        with (
            patch("karox.web_bridge_launcher._watchdog_supports_stop_request", return_value=True),
            patch("karox.web_bridge_launcher._write_stop_request", return_value=request_path) as request_stop,
            patch("karox.web_bridge_launcher._process_is_alive", return_value=False),
            patch("karox.web_bridge_launcher._port_is_available", return_value=True),
            patch(
                "karox.saved_bridge_supervisor.saved_bridge_supervisor_status",
                side_effect=[{"supervisor_alive": True}, {"supervisor_alive": False}],
            ) as supervisor_status,
            patch("karox.web_bridge_launcher.subprocess.run") as mock_run,
        ):
            result = stop_saved_bridge("chatgpt-pc")

        # Stop is cooperative: never kill the whole descendant tree. That is what
        # lets an MCP-triggered restart worker survive long enough to start again.
        request_stop.assert_called_once_with("web-saved-abc", 99999)
        mock_run.assert_not_called()
        request_path.unlink.assert_called_once_with()
        self.assertEqual(supervisor_status.call_count, 2)
        self.assertEqual(result["action"], "stopped")
        self.assertEqual(result["pid"], 99999)
        _assert_no_secrets(self, result)

    @patch("karox.port_ownership.check_port_ownership")
    @patch("karox.web_bridge_profiles.WebBridgeProfileStore")
    def test_legacy_bridge_can_migrate_when_tui_explicitly_allows_it(
        self,
        mock_store_cls: MagicMock,
        mock_ownership: MagicMock,
    ) -> None:
        mock_store_cls.return_value.get.return_value = _fake_profile()
        metadata = _verdict(
            OWNERSHIP_REUSE_SAME,
            pid=99999,
            pid_proven=True,
            public_url="https://example.ts.net",
        )
        mock_ownership.return_value = metadata
        completed = MagicMock(returncode=0)
        with (
            patch("karox.web_bridge_launcher._watchdog_supports_stop_request", return_value=False),
            patch("karox.web_bridge_launcher._current_process_is_descendant_of", return_value=None),
            patch("karox.web_bridge_launcher._process_is_alive", return_value=False),
            patch("karox.web_bridge_launcher._port_is_available", return_value=True),
            patch("karox.web_bridge_launcher.subprocess.run", return_value=completed) as run,
        ):
            result = stop_saved_bridge(
                "chatgpt-pc",
                allow_legacy_migration=True,
            )

        run.assert_called_once()
        self.assertEqual(result["action"], "stopped")
        self.assertEqual(result["pid"], 99999)
        _assert_no_secrets(self, result)

    @patch("karox.port_ownership.check_port_ownership")
    @patch("karox.web_bridge_profiles.WebBridgeProfileStore")
    def test_legacy_bridge_fails_closed_without_tui_migration_flag(
        self,
        mock_store_cls: MagicMock,
        mock_ownership: MagicMock,
    ) -> None:
        mock_store_cls.return_value.get.return_value = _fake_profile()
        mock_ownership.return_value = _verdict(
            OWNERSHIP_REUSE_SAME,
            pid=99999,
            pid_proven=True,
            public_url="https://example.ts.net",
        )
        with (
            patch("karox.web_bridge_launcher._watchdog_supports_stop_request", return_value=False),
            patch("karox.web_bridge_launcher._current_process_is_descendant_of", return_value=None),
            patch("karox.web_bridge_launcher.subprocess.run") as run,
        ):
            result = stop_saved_bridge("chatgpt-pc")

        run.assert_not_called()
        self.assertEqual(result["action"], "error")
        self.assertEqual(result["error"], "legacy bridge restart requires KaroX UI")
        _assert_no_secrets(self, result)

    @patch("karox.port_ownership.check_port_ownership")
    @patch("karox.web_bridge_profiles.WebBridgeProfileStore")
    def test_stop_foreign_process_returns_no_action(
        self, mock_store_cls: MagicMock, mock_ownership: MagicMock
    ) -> None:
        mock_store_cls.return_value.get.return_value = _fake_profile()
        mock_ownership.return_value = _verdict(OWNERSHIP_UNRELATED, unrelated_pid=12345)

        with patch("karox.web_bridge_launcher.subprocess.run") as mock_run:
            result = stop_saved_bridge("chatgpt-pc")

        mock_run.assert_not_called()
        self.assertEqual(result["action"], "no_action")
        self.assertEqual(result["verdict"], OWNERSHIP_UNRELATED)
        _assert_no_secrets(self, result)

    @patch("karox.port_ownership.check_port_ownership")
    @patch("karox.web_bridge_profiles.WebBridgeProfileStore")
    def test_stop_free_port_returns_no_action(
        self, mock_store_cls: MagicMock, mock_ownership: MagicMock
    ) -> None:
        mock_store_cls.return_value.get.return_value = _fake_profile()
        mock_ownership.return_value = _verdict(OWNERSHIP_FREE)

        with patch("karox.web_bridge_launcher.subprocess.run") as mock_run:
            result = stop_saved_bridge("chatgpt-pc")

        mock_run.assert_not_called()
        self.assertEqual(result["action"], "no_action")
        self.assertEqual(result["verdict"], OWNERSHIP_FREE)
        _assert_no_secrets(self, result)

    @patch("karox.port_ownership.check_port_ownership")
    @patch("karox.web_bridge_profiles.WebBridgeProfileStore")
    def test_stop_unproven_pid_refuses_to_terminate(
        self, mock_store_cls: MagicMock, mock_ownership: MagicMock
    ) -> None:
        mock_store_cls.return_value.get.return_value = _fake_profile()
        mock_ownership.return_value = _verdict(
            OWNERSHIP_REUSE_SAME,
            pid=99999,
            pid_proven=False,  # PID reused or unverifiable
        )

        with patch("karox.web_bridge_launcher.subprocess.run") as mock_run:
            result = stop_saved_bridge("chatgpt-pc")

        mock_run.assert_not_called()
        self.assertEqual(result["action"], "no_action")
        self.assertIn("proven", result["reason"].lower())
        _assert_no_secrets(self, result)

    @patch("karox.web_bridge_profiles.WebBridgeProfileStore")
    def test_stop_invalid_profile_returns_error(self, mock_store_cls: MagicMock) -> None:
        from karox.web_bridge_profiles import WebBridgeProfileError

        mock_store_cls.return_value.get.side_effect = WebBridgeProfileError("not found")

        result = stop_saved_bridge("nonexistent")

        self.assertEqual(result["action"], "error")
        _assert_no_secrets(self, result)


# ---------------------------------------------------------------------------
# No-secrets invariant across all results
# ---------------------------------------------------------------------------


class NoSecretsInvariantTests(unittest.TestCase):
    """Every result from start/stop must be free of secret-shaped fields."""

    @patch("karox.web_bridge_launcher._saved_bridge_repository", return_value=_existing_repo())
    @patch("karox.bridge.BridgeCredentialStore")
    @patch("karox.web_bridge_launcher._verify_bridge_endpoint")
    @patch("karox.connection_status._check_bridge_credential")
    @patch("karox.port_ownership.check_port_ownership")
    @patch("karox.web_bridge_profiles.WebBridgeProfileStore")
    def test_start_reuse_result_has_no_secrets(
        self,
        mock_store_cls: MagicMock,
        mock_ownership: MagicMock,
        mock_cred: MagicMock,
        mock_verify: MagicMock,
        mock_cred_store_cls: MagicMock,
        mock_repo: MagicMock,
    ) -> None:
        mock_store_cls.return_value.get.return_value = _fake_profile()
        mock_ownership.return_value = _verdict(
            OWNERSHIP_REUSE_SAME,
            pid=99999,
            pid_proven=True,
            public_url="https://example.ts.net",
        )
        mock_cred.return_value = (CredentialStatus.AVAILABLE, "sha256:fingerprint123")
        mock_cred_store_cls.return_value.resolve.return_value = "test-secret"
        mock_verify.return_value = {
            "unauth_401": True,
            "auth_initialized": True,
            "tools_list_ok": True,
            "tool_count": 37,
            "endpoint": "https://example.ts.net/mcp",
        }

        result = start_saved_bridge("chatgpt-pc")
        _assert_no_secrets(self, result)
        # The fingerprint is the sha256-prefixed fingerprint, not the secret.
        self.assertEqual(result["credential_fingerprint"], "sha256:fingerprint123")

    @patch("karox.web_bridge_launcher._saved_bridge_repository", return_value=_existing_repo())
    @patch("karox.connection_status._check_bridge_credential")
    @patch("karox.port_ownership.check_port_ownership")
    @patch("karox.web_bridge_profiles.WebBridgeProfileStore")
    def test_start_foreign_result_has_no_secrets(
        self,
        mock_store_cls: MagicMock,
        mock_ownership: MagicMock,
        mock_cred: MagicMock,
        mock_repo: MagicMock,
    ) -> None:
        mock_store_cls.return_value.get.return_value = _fake_profile()
        mock_ownership.return_value = _verdict(OWNERSHIP_UNRELATED, unrelated_pid=12345)
        mock_cred.return_value = (CredentialStatus.AVAILABLE, "sha256:abc")

        with patch("karox.web_bridge_launcher.subprocess.Popen"):
            result = start_saved_bridge("chatgpt-pc")
        _assert_no_secrets(self, result)


if __name__ == "__main__":
    unittest.main()
