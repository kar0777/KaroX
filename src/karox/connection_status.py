"""Typed live connection status model for B7 ChatGPT connection flow.

Separates the connection into typed status fields so the UI never shows
"not configured" when a saved profile and credential exist but the bridge
is stopped. Each status is computed from live data: saved profile existence,
credential reference resolvability, PID ownership verification, tunnel route
ownership, and persistent client-verification evidence.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .paths import runtime_dir


# ---------------------------------------------------------------------------
# Typed status constants
# ---------------------------------------------------------------------------

class ConfigurationStatus:
    MISSING = "missing"
    SAVED = "saved"
    INVALID = "invalid"


class CredentialStatus:
    MISSING = "missing"
    AVAILABLE = "available"
    REVOKED = "revoked"
    ERROR = "error"


class BridgeStatus:
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STALE = "stale"
    ERROR = "error"


class TunnelStatus:
    STOPPED = "stopped"
    STARTING = "starting"
    PUBLIC_URL_READY = "public_url_ready"
    ROUTE_CONFLICT = "route_conflict"
    ERROR = "error"


class OAuthStatus:
    UNAVAILABLE = "unavailable"
    READY = "ready"
    AUTHORIZATION_PENDING = "authorization_pending"
    AUTHORIZED = "authorized"
    REVOKED = "revoked"
    ERROR = "error"


class ChatGPTClientStatus:
    NOT_SEEN = "not_seen"
    INITIALIZE_RECEIVED = "initialize_received"
    TOOLS_LIST_RECEIVED = "tools_list_received"
    DISCONNECTED = "disconnected"
    CONNECTED = "connected"


class ToolVerificationStatus:
    NOT_TESTED = "not_tested"
    PASSED = "passed"
    FAILED = "failed"


class OverallStatus:
    NOT_CONFIGURED = "not_configured"
    STOPPED_READY_TO_RESTART = "stopped_ready_to_restart"
    STARTING = "starting"
    READY_FOR_CHATGPT_SETUP = "ready_for_chatgpt_setup"
    WAITING_FOR_CHATGPT = "waiting_for_chatgpt"
    CONNECTED_UNVERIFIED = "connected_unverified"
    FULLY_VERIFIED = "fully_verified"
    DEGRADED = "degraded"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Typed status dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConnectionLiveStatus:
    """Typed live connection status computed from live data.

    Never contains secrets — only references, fingerprints, PIDs, and status
    strings.
    """

    saved_profile: Optional[str] = None
    preset_id: Optional[str] = None
    session_id: Optional[str] = None

    configuration: str = ConfigurationStatus.MISSING
    credential: str = CredentialStatus.MISSING
    credential_fingerprint: Optional[str] = None
    bridge: str = BridgeStatus.STOPPED
    bridge_pid: Optional[int] = None
    bridge_identity_verified: bool = False
    tunnel: str = TunnelStatus.STOPPED
    tunnel_pid: Optional[int] = None
    public_url: Optional[str] = None
    oauth: str = OAuthStatus.UNAVAILABLE
    chatgpt_client: str = ChatGPTClientStatus.NOT_SEEN
    tool_verification: str = ToolVerificationStatus.NOT_TESTED
    overall: str = OverallStatus.NOT_CONFIGURED

    # Timestamps for evidence
    last_initialize_at: Optional[float] = None
    last_tools_list_at: Optional[float] = None
    last_tool_call_at: Optional[float] = None
    last_tool_call_name: Optional[str] = None
    discovered_tool_count: Optional[int] = None

    # Error detail
    error_detail: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "saved_profile": self.saved_profile,
            "preset_id": self.preset_id,
            "session_id": self.session_id,
            "configuration": self.configuration,
            "credential": self.credential,
            "credential_fingerprint": self.credential_fingerprint,
            "bridge": self.bridge,
            "bridge_pid": self.bridge_pid,
            "bridge_identity_verified": self.bridge_identity_verified,
            "tunnel": self.tunnel,
            "tunnel_pid": self.tunnel_pid,
            "public_url": self.public_url,
            "oauth": self.oauth,
            "chatgpt_client": self.chatgpt_client,
            "tool_verification": self.tool_verification,
            "overall": self.overall,
            "last_initialize_at": self.last_initialize_at,
            "last_tools_list_at": self.last_tools_list_at,
            "last_tool_call_at": self.last_tool_call_at,
            "last_tool_call_name": self.last_tool_call_name,
            "discovered_tool_count": self.discovered_tool_count,
            "error_detail": self.error_detail,
        }


# ---------------------------------------------------------------------------
# Live status computation
# ---------------------------------------------------------------------------

def _check_credential_reference(reference: str) -> tuple[str, Optional[str]]:
    """Check if a credential reference resolves without revealing the secret.

    Returns ``(status, fingerprint)`` where status is one of CredentialStatus.*
    and fingerprint is the sha256 fingerprint (never the secret itself).
    """
    if not reference:
        return CredentialStatus.MISSING, None
    from .credentials import CredentialError, CredentialStore
    from .connections import ConnectionCredentialStore

    store = ConnectionCredentialStore()
    try:
        secret = store.resolve(reference)
    except CredentialError:
        return CredentialStatus.MISSING, None
    except Exception:
        return CredentialStatus.ERROR, None
    if not secret:
        return CredentialStatus.MISSING, None
    return CredentialStatus.AVAILABLE, CredentialStore.fingerprint(secret)


def _check_bridge_credential(session_id: str) -> tuple[str, Optional[str]]:
    """Check if a bridge credential exists for a session without revealing it."""
    if not session_id:
        return CredentialStatus.MISSING, None
    from .credentials import CredentialError, CredentialStore
    from .bridge import BridgeCredentialMissing, BridgeCredentialStore

    ref = f"os-keyring:bridge/{session_id}"
    store = BridgeCredentialStore()
    try:
        secret = store.resolve(ref)
    except BridgeCredentialMissing:
        return CredentialStatus.MISSING, None
    except CredentialError:
        # The backend itself is unreadable. Reporting MISSING here would invite
        # the bootstrap path to mint a replacement token over a live identity,
        # so an unreadable store fails closed as ERROR instead.
        return CredentialStatus.ERROR, None
    except Exception:
        return CredentialStatus.ERROR, None
    if not secret:
        return CredentialStatus.MISSING, None
    return CredentialStatus.AVAILABLE, CredentialStore.fingerprint(secret)


def _verify_pid_alive(pid: Optional[int]) -> bool:
    """Verify a PID is alive using psutil (if available)."""
    if pid is None or pid <= 0:
        return False
    try:
        import psutil  # type: ignore[import-untyped]
        return psutil.pid_exists(pid)
    except ImportError:
        import os
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False


def _read_client_evidence(profile_name: str) -> dict[str, Any]:
    """Read persistent client-verification evidence for a saved profile.

    Returns a dict with last_initialize_at, last_tools_list_at, last_tool_call_at,
    last_tool_call_name, discovered_tool_count. Never contains secrets, tokens,
    or user content.
    """
    evidence_path = Path(runtime_dir()) / "vnext" / "connection-evidence" / f"{profile_name}.json"
    if not evidence_path.exists():
        return {}
    try:
        return json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_client_evidence(profile_name: str, evidence: dict[str, Any]) -> None:
    """Write persistent client-verification evidence for a saved profile.

    Safe fields only: timestamp, client kind, protocol version, tool count,
    tool name, success/failure. Never stores: OAuth tokens, authorization
    headers, tool arguments, file contents, user messages.
    """
    evidence_dir = Path(runtime_dir()) / "vnext" / "connection-evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = evidence_dir / f"{profile_name}.json"
    # Only safe fields
    safe: dict[str, Any] = {
        "saved_profile": profile_name,
        "last_initialize_at": evidence.get("last_initialize_at"),
        "last_tools_list_at": evidence.get("last_tools_list_at"),
        "last_tool_call_at": evidence.get("last_tool_call_at"),
        "last_tool_call_name": evidence.get("last_tool_call_name"),
        "discovered_tool_count": evidence.get("discovered_tool_count"),
        "client_kind": evidence.get("client_kind"),
        "protocol_version": evidence.get("protocol_version"),
        "success": evidence.get("success"),
        "error_code": evidence.get("error_code"),
        "updated_at": time.time(),
    }
    evidence_path.write_text(json.dumps(safe, indent=2, default=str), encoding="utf-8")


def compute_live_status(
    profile_data: dict[str, Any],
    *,
    preset_id: str = "chatgpt-web",
) -> ConnectionLiveStatus:
    """Compute typed live status from a discovered bridge profile.

    Reads the saved profile, checks credential resolvability, verifies PID
    ownership, reads persistent client evidence, and computes the overall
    status. Never reveals secrets.
    """
    saved_profile = str(profile_data.get("saved_profile") or "")
    session_id = str(profile_data.get("session_id") or "")
    public_url = str(profile_data.get("public_url") or "")
    bridge_pid = profile_data.get("bridge_pid")
    tunnel_pid = profile_data.get("tunnel_pid")
    bridge_started_at = profile_data.get("started_at")

    # Configuration: saved if profile_data has a saved_profile name
    if saved_profile:
        configuration = ConfigurationStatus.SAVED
    else:
        configuration = ConfigurationStatus.MISSING

    # Credential: check bridge credential for this session
    if session_id:
        cred_status, cred_fp = _check_bridge_credential(session_id)
    else:
        cred_status, cred_fp = CredentialStatus.MISSING, None

    # Bridge: alive if PID exists
    bridge_alive = _verify_pid_alive(bridge_pid)
    if bridge_pid and bridge_alive:
        bridge = BridgeStatus.RUNNING
    elif bridge_pid and not bridge_alive:
        bridge = BridgeStatus.STALE
    else:
        bridge = BridgeStatus.STOPPED

    # Tunnel: alive if PID exists and public_url is set
    tunnel_alive = _verify_pid_alive(tunnel_pid)
    if tunnel_pid and tunnel_alive and public_url:
        tunnel = TunnelStatus.PUBLIC_URL_READY
    elif tunnel_pid and not tunnel_alive:
        tunnel = TunnelStatus.STOPPED
    else:
        tunnel = TunnelStatus.STOPPED

    # OAuth: unavailable when bridge stopped, ready when bridge running
    if bridge == BridgeStatus.RUNNING:
        oauth = OAuthStatus.READY
    else:
        oauth = OAuthStatus.UNAVAILABLE

    # Client evidence from persistent store
    evidence = _read_client_evidence(saved_profile) if saved_profile else {}
    last_init = evidence.get("last_initialize_at")
    last_tools = evidence.get("last_tools_list_at")
    last_call = evidence.get("last_tool_call_at")
    last_call_name = evidence.get("last_tool_call_name")
    tool_count = evidence.get("discovered_tool_count")

    # After a restart, old evidence is "historical/previous": the current client
    # is disconnected until a NEW call comes in. Compare last_call_at with the
    # bridge's started_at: if the call predates the current bridge, it does not
    # count as a current connection.
    def _evidence_is_current(evidence_ts: Any) -> bool:
        """True if evidence_ts is at or after the current bridge start."""
        if not isinstance(evidence_ts, (int, float)):
            return True  # no bridge start time known: keep current behaviour
        if not isinstance(bridge_started_at, (int, float)):
            return True  # no bridge start time known: keep current behaviour
        return evidence_ts >= bridge_started_at

    if bridge == BridgeStatus.RUNNING:
        if last_call and last_call_name == "karox.runtime.status" and _evidence_is_current(last_call):
            chatgpt_client = ChatGPTClientStatus.CONNECTED
            tool_verification = ToolVerificationStatus.PASSED
        elif last_call and last_call_name == "karox.runtime.status" and not _evidence_is_current(last_call):
            # Evidence predates the current bridge start: historical, not current.
            chatgpt_client = ChatGPTClientStatus.NOT_SEEN
            tool_verification = ToolVerificationStatus.NOT_TESTED
        elif last_tools and _evidence_is_current(last_tools):
            chatgpt_client = ChatGPTClientStatus.TOOLS_LIST_RECEIVED
            tool_verification = ToolVerificationStatus.NOT_TESTED
        elif last_init and _evidence_is_current(last_init):
            chatgpt_client = ChatGPTClientStatus.INITIALIZE_RECEIVED
            tool_verification = ToolVerificationStatus.NOT_TESTED
        else:
            chatgpt_client = ChatGPTClientStatus.NOT_SEEN
            tool_verification = ToolVerificationStatus.NOT_TESTED
    else:
        # Bridge stopped: historical evidence is "previous", currently disconnected
        if last_call:
            chatgpt_client = ChatGPTClientStatus.DISCONNECTED
            tool_verification = ToolVerificationStatus.PASSED  # previous evidence
        elif last_init:
            chatgpt_client = ChatGPTClientStatus.DISCONNECTED
            tool_verification = ToolVerificationStatus.NOT_TESTED
        else:
            chatgpt_client = ChatGPTClientStatus.NOT_SEEN
            tool_verification = ToolVerificationStatus.NOT_TESTED

    # Overall status
    if configuration == ConfigurationStatus.MISSING:
        overall = OverallStatus.NOT_CONFIGURED
    elif bridge == BridgeStatus.RUNNING and tool_verification == ToolVerificationStatus.PASSED:
        overall = OverallStatus.FULLY_VERIFIED
    elif bridge == BridgeStatus.RUNNING and chatgpt_client in (
        ChatGPTClientStatus.TOOLS_LIST_RECEIVED,
        ChatGPTClientStatus.INITIALIZE_RECEIVED,
    ):
        overall = OverallStatus.WAITING_FOR_CHATGPT
    elif bridge == BridgeStatus.RUNNING and chatgpt_client == ChatGPTClientStatus.CONNECTED:
        overall = OverallStatus.CONNECTED_UNVERIFIED
    elif bridge == BridgeStatus.RUNNING:
        overall = OverallStatus.READY_FOR_CHATGPT_SETUP
    elif bridge == BridgeStatus.STALE:
        overall = OverallStatus.STOPPED_READY_TO_RESTART
    elif configuration == ConfigurationStatus.SAVED and cred_status == CredentialStatus.AVAILABLE:
        overall = OverallStatus.STOPPED_READY_TO_RESTART
    else:
        overall = OverallStatus.NOT_CONFIGURED

    return ConnectionLiveStatus(
        saved_profile=saved_profile or None,
        preset_id=preset_id,
        session_id=session_id or None,
        configuration=configuration,
        credential=cred_status,
        credential_fingerprint=cred_fp,
        bridge=bridge,
        bridge_pid=bridge_pid,
        bridge_identity_verified=bridge_alive and bridge == BridgeStatus.RUNNING,
        tunnel=tunnel,
        tunnel_pid=tunnel_pid,
        public_url=public_url or None,
        oauth=oauth,
        chatgpt_client=chatgpt_client,
        tool_verification=tool_verification,
        overall=overall,
        last_initialize_at=last_init,
        last_tools_list_at=last_tools,
        last_tool_call_at=last_call,
        last_tool_call_name=last_call_name,
        discovered_tool_count=tool_count,
    )
