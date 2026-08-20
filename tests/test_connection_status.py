"""Tests for the typed live connection status model (B7).

Verifies that the status model correctly computes:
- stopped_ready_to_restart when profile+credential exist but bridge is dead
- not_configured only when profile is truly missing
- stale PID detection
- credential availability without revealing secrets
- client evidence recovery
- overall status transitions
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _support import SRC  # noqa: F401 - inserts src on sys.path

from karox.connection_status import (
    BridgeStatus,
    ChatGPTClientStatus,
    ConfigurationStatus,
    ConnectionLiveStatus,
    CredentialStatus,
    OAuthStatus,
    OverallStatus,
    ToolVerificationStatus,
    TunnelStatus,
    compute_live_status,
    write_client_evidence,
)


def _profile_data(
    saved_profile: str = "chatgpt-pc",
    session_id: str = "web-saved-test123",
    public_url: str = "https://example.ts.net",
    bridge_pid: int | None = None,
    tunnel_pid: int | None = None,
    started_at: float | None = None,
) -> dict:
    data = {
        "saved_profile": saved_profile,
        "profile": "chatgpt-web",
        "session_id": session_id,
        "public_url": public_url,
        "tunnel": "tailscale",
        "bridge_pid": bridge_pid,
        "tunnel_pid": tunnel_pid,
        "persistent_session": True,
    }
    if started_at is not None:
        data["started_at"] = started_at
    return data


class ConfigurationStatusTests(unittest.TestCase):
    def test_saved_profile_shows_saved_not_missing(self) -> None:
        status = compute_live_status(_profile_data(saved_profile="chatgpt-pc"))
        self.assertEqual(status.configuration, ConfigurationStatus.SAVED)

    def test_missing_profile_shows_missing(self) -> None:
        status = compute_live_status(_profile_data(saved_profile=""))
        self.assertEqual(status.configuration, ConfigurationStatus.MISSING)

    def test_missing_profile_overall_is_not_configured(self) -> None:
        status = compute_live_status(_profile_data(saved_profile=""))
        self.assertEqual(status.overall, OverallStatus.NOT_CONFIGURED)


class CredentialStatusTests(unittest.TestCase):
    def test_available_credential_shows_available(self) -> None:
        with patch("karox.connection_status._check_bridge_credential", return_value=(CredentialStatus.AVAILABLE, "sha256:abc123")):
            status = compute_live_status(_profile_data())
        self.assertEqual(status.credential, CredentialStatus.AVAILABLE)
        self.assertEqual(status.credential_fingerprint, "sha256:abc123")

    def test_missing_credential_shows_missing(self) -> None:
        with patch("karox.connection_status._check_bridge_credential", return_value=(CredentialStatus.MISSING, None)):
            status = compute_live_status(_profile_data())
        self.assertEqual(status.credential, CredentialStatus.MISSING)

    def test_status_never_contains_raw_secret(self) -> None:
        with patch("karox.connection_status._check_bridge_credential", return_value=(CredentialStatus.AVAILABLE, "sha256:abc123")):
            status = compute_live_status(_profile_data())
        d = status.to_dict()
        for key, value in d.items():
            if isinstance(value, str):
                self.assertNotIn("secret", value.lower())
                self.assertNotIn("password", value.lower())


class BridgeStatusTests(unittest.TestCase):
    def test_dead_pid_shows_stale(self) -> None:
        with patch("karox.connection_status._verify_pid_alive", return_value=False):
            status = compute_live_status(_profile_data(bridge_pid=99999))
        self.assertEqual(status.bridge, BridgeStatus.STALE)

    def test_alive_pid_shows_running(self) -> None:
        with patch("karox.connection_status._verify_pid_alive", return_value=True):
            status = compute_live_status(_profile_data(bridge_pid=12345, tunnel_pid=12346))
        self.assertEqual(status.bridge, BridgeStatus.RUNNING)
        self.assertTrue(status.bridge_identity_verified)

    def test_no_pid_shows_stopped(self) -> None:
        status = compute_live_status(_profile_data(bridge_pid=None))
        self.assertEqual(status.bridge, BridgeStatus.STOPPED)


class TunnelStatusTests(unittest.TestCase):
    def test_alive_tunnel_with_url_shows_ready(self) -> None:
        with patch("karox.connection_status._verify_pid_alive", return_value=True):
            status = compute_live_status(_profile_data(bridge_pid=12345, tunnel_pid=12346))
        self.assertEqual(status.tunnel, TunnelStatus.PUBLIC_URL_READY)

    def test_dead_tunnel_shows_stopped(self) -> None:
        with patch("karox.connection_status._verify_pid_alive", return_value=False):
            status = compute_live_status(_profile_data(bridge_pid=12345, tunnel_pid=12346))
        self.assertEqual(status.tunnel, TunnelStatus.STOPPED)


class OverallStatusTests(unittest.TestCase):
    def test_stopped_with_saved_profile_and_credential_shows_ready_to_restart(self) -> None:
        """The key B7 fix: don't show 'not_configured' when profile+credential exist."""
        with patch("karox.connection_status._check_bridge_credential", return_value=(CredentialStatus.AVAILABLE, "sha256:abc")):
            with patch("karox.connection_status._verify_pid_alive", return_value=False):
                status = compute_live_status(_profile_data(bridge_pid=99999))
        self.assertEqual(status.configuration, ConfigurationStatus.SAVED)
        self.assertEqual(status.credential, CredentialStatus.AVAILABLE)
        self.assertEqual(status.bridge, BridgeStatus.STALE)
        self.assertEqual(status.overall, OverallStatus.STOPPED_READY_TO_RESTART)

    def test_running_with_no_client_shows_ready_for_chatgpt_setup(self) -> None:
        with patch("karox.connection_status._check_bridge_credential", return_value=(CredentialStatus.AVAILABLE, "sha256:abc")):
            with patch("karox.connection_status._verify_pid_alive", return_value=True):
                with patch("karox.connection_status._read_client_evidence", return_value={}):
                    status = compute_live_status(_profile_data(bridge_pid=12345, tunnel_pid=12346))
        self.assertEqual(status.overall, OverallStatus.READY_FOR_CHATGPT_SETUP)

    def test_running_with_initialize_shows_waiting_for_chatgpt(self) -> None:
        with patch("karox.connection_status._check_bridge_credential", return_value=(CredentialStatus.AVAILABLE, "sha256:abc")):
            with patch("karox.connection_status._verify_pid_alive", return_value=True):
                with patch("karox.connection_status._read_client_evidence", return_value={"last_initialize_at": 1234567890}):
                    status = compute_live_status(_profile_data(bridge_pid=12345, tunnel_pid=12346))
        self.assertEqual(status.chatgpt_client, ChatGPTClientStatus.INITIALIZE_RECEIVED)
        self.assertEqual(status.overall, OverallStatus.WAITING_FOR_CHATGPT)

    def test_fully_verified_only_with_external_tool_call(self) -> None:
        with patch("karox.connection_status._check_bridge_credential", return_value=(CredentialStatus.AVAILABLE, "sha256:abc")):
            with patch("karox.connection_status._verify_pid_alive", return_value=True):
                with patch("karox.connection_status._read_client_evidence", return_value={
                    "last_initialize_at": 1234567890,
                    "last_tools_list_at": 1234567891,
                    "last_tool_call_at": 1234567892,
                    "last_tool_call_name": "karox.runtime.status",
                }):
                    status = compute_live_status(_profile_data(bridge_pid=12345, tunnel_pid=12346))
        self.assertEqual(status.tool_verification, ToolVerificationStatus.PASSED)
        self.assertEqual(status.overall, OverallStatus.FULLY_VERIFIED)

    def test_stopped_with_previous_evidence_shows_disconnected(self) -> None:
        with patch("karox.connection_status._check_bridge_credential", return_value=(CredentialStatus.AVAILABLE, "sha256:abc")):
            with patch("karox.connection_status._verify_pid_alive", return_value=False):
                with patch("karox.connection_status._read_client_evidence", return_value={
                    "last_initialize_at": 1234567890,
                    "last_tool_call_at": 1234567892,
                    "last_tool_call_name": "karox.runtime.status",
                }):
                    status = compute_live_status(_profile_data(bridge_pid=99999))
        self.assertEqual(status.chatgpt_client, ChatGPTClientStatus.DISCONNECTED)
        self.assertEqual(status.overall, OverallStatus.STOPPED_READY_TO_RESTART)


class PIDReuseTests(unittest.TestCase):
    def test_stale_pid_not_treated_as_running(self) -> None:
        """A dead PID in the config must not be treated as a running bridge."""
        with patch("karox.connection_status._verify_pid_alive", return_value=False):
            status = compute_live_status(_profile_data(bridge_pid=25212))
        self.assertEqual(status.bridge, BridgeStatus.STALE)
        self.assertFalse(status.bridge_identity_verified)


class ClientEvidenceTests(unittest.TestCase):
    def test_write_and_read_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch("karox.connection_status.runtime_dir", return_value=Path(tmp)):
                write_client_evidence("test-profile", {
                    "last_initialize_at": 1234567890,
                    "last_tools_list_at": 1234567891,
                    "last_tool_call_at": 1234567892,
                    "last_tool_call_name": "karox.runtime.status",
                    "discovered_tool_count": 39,
                    "client_kind": "chatgpt-web",
                    "protocol_version": "2025-06-18",
                })
                # Re-read by computing status
                with patch("karox.connection_status._check_bridge_credential", return_value=(CredentialStatus.AVAILABLE, "sha256:abc")):
                    with patch("karox.connection_status._verify_pid_alive", return_value=True):
                        status = compute_live_status(_profile_data(saved_profile="test-profile", bridge_pid=12345, tunnel_pid=12346))
                self.assertEqual(status.discovered_tool_count, 39)
                self.assertEqual(status.last_tool_call_name, "karox.runtime.status")
                self.assertEqual(status.overall, OverallStatus.FULLY_VERIFIED)

    def test_evidence_file_never_contains_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch("karox.connection_status.runtime_dir", return_value=Path(tmp)):
                write_client_evidence("test-profile", {
                    "last_initialize_at": 1234567890,
                    "last_tool_call_name": "karox.runtime.status",
                    # These should NOT be stored:
                    "oauth_token": "should-not-be-stored",
                    "authorization_header": "Bearer should-not-be-stored",
                    "tool_arguments": {"arg": "should-not-be-stored"},
                    "user_message": "should-not-be-stored",
                })
                evidence_path = Path(tmp) / "vnext" / "connection-evidence" / "test-profile.json"
                content = evidence_path.read_text(encoding="utf-8")
                self.assertNotIn("should-not-be-stored", content)
                self.assertNotIn("oauth_token", content)
                self.assertNotIn("authorization_header", content)
                self.assertNotIn("tool_arguments", content)
                self.assertNotIn("user_message", content)

    def test_local_self_test_not_accepted_as_external(self) -> None:
        """A tool call from KaroX itself (not ChatGPT) must not set fully_verified."""
        # The evidence only records tool calls; the bridge must distinguish
        # external calls from local ones. This test verifies the status model
        # requires the tool_call_name to be 'karox.runtime.status' AND the
        # bridge to be running for fully_verified.
        with patch("karox.connection_status._check_bridge_credential", return_value=(CredentialStatus.AVAILABLE, "sha256:abc")):
            with patch("karox.connection_status._verify_pid_alive", return_value=False):
                with patch("karox.connection_status._read_client_evidence", return_value={
                    "last_tool_call_at": 1234567892,
                    "last_tool_call_name": "karox.runtime.status",
                }):
                    status = compute_live_status(_profile_data(bridge_pid=99999))
        # Bridge stopped: previous evidence is disconnected, not fully_verified
        self.assertEqual(status.chatgpt_client, ChatGPTClientStatus.DISCONNECTED)
        self.assertNotEqual(status.overall, OverallStatus.FULLY_VERIFIED)


class HistoricalEvidenceTests(unittest.TestCase):
    """After a restart, old evidence is 'historical/previous', not current."""

    def test_evidence_before_bridge_start_is_not_current(self) -> None:
        """Evidence timestamp before the bridge's started_at is historical."""
        with patch("karox.connection_status._check_bridge_credential", return_value=(CredentialStatus.AVAILABLE, "sha256:abc")):
            with patch("karox.connection_status._verify_pid_alive", return_value=True):
                with patch("karox.connection_status._read_client_evidence", return_value={
                    "last_tool_call_at": 1000.0,  # before bridge start
                    "last_tool_call_name": "karox.runtime.status",
                }):
                    status = compute_live_status(_profile_data(bridge_pid=12345, tunnel_pid=12346, started_at=2000.0))
        self.assertEqual(status.chatgpt_client, ChatGPTClientStatus.NOT_SEEN)
        self.assertEqual(status.tool_verification, ToolVerificationStatus.NOT_TESTED)
        self.assertNotEqual(status.overall, OverallStatus.FULLY_VERIFIED)

    def test_evidence_after_bridge_start_is_current(self) -> None:
        """Evidence timestamp at or after the bridge's started_at is current."""
        with patch("karox.connection_status._check_bridge_credential", return_value=(CredentialStatus.AVAILABLE, "sha256:abc")):
            with patch("karox.connection_status._verify_pid_alive", return_value=True):
                with patch("karox.connection_status._read_client_evidence", return_value={
                    "last_tool_call_at": 3000.0,  # after bridge start
                    "last_tool_call_name": "karox.runtime.status",
                }):
                    status = compute_live_status(_profile_data(bridge_pid=12345, tunnel_pid=12346, started_at=2000.0))
        self.assertEqual(status.chatgpt_client, ChatGPTClientStatus.CONNECTED)
        self.assertEqual(status.tool_verification, ToolVerificationStatus.PASSED)
        self.assertEqual(status.overall, OverallStatus.FULLY_VERIFIED)

    def test_no_started_at_keeps_current_behavior(self) -> None:
        """Without started_at, evidence is treated as current (backward compat)."""
        with patch("karox.connection_status._check_bridge_credential", return_value=(CredentialStatus.AVAILABLE, "sha256:abc")):
            with patch("karox.connection_status._verify_pid_alive", return_value=True):
                with patch("karox.connection_status._read_client_evidence", return_value={
                    "last_tool_call_at": 1234567892,
                    "last_tool_call_name": "karox.runtime.status",
                }):
                    status = compute_live_status(_profile_data(bridge_pid=12345, tunnel_pid=12346))
        self.assertEqual(status.chatgpt_client, ChatGPTClientStatus.CONNECTED)
        self.assertEqual(status.overall, OverallStatus.FULLY_VERIFIED)


if __name__ == "__main__":
    unittest.main()
