"""Versioned client capability negotiation and durable session snapshots."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .sessions import SessionError, _atomic_json, _exclusive_file_lock

CLIENT_CAPABILITY_SCHEMA_VERSION = 1
_READ_PREFIXES = (
    "karox.repo.read",
    "karox.repo.search",
    "karox.repo.list",
    "karox.repo.inspect",
    "karox.git.",
    "karox.runtime.status",
    "karox.task.resume",
    "karox.task.status",
    "karox.task.workstreams",
    # Memory tools touch only the local memory store, never the repository or
    # guarded process state -- the same reasoning that keeps task.bootstrap
    # out of the write set. remember/forget mutate user-controlled local
    # knowledge, which must stay available on read-only connections.
    "karox.memory.",
)
# Tools whose selection means the connection can mutate the repository, the
# workspace, or guarded process state. ``karox.task.bootstrap`` is deliberately
# absent: it writes only session-scoped task state (the recovery journal), not
# the repository, and it ships in the read-only default bundle -- counting it
# here made a READ_ONLY launch negotiate itself as workspace_write.
# ``karox.git.commit`` is deliberately present: it is the most privileged Core
# mutation even though it touches no working-tree bytes.
_WRITE_TOOLS = frozenset(
    {
        "karox.repo.write_file",
        "karox.repo.edit_file",
        "karox.repo.command",
        "karox.git.commit",
        "karox.task.checkpoint",
        "karox.task.execute_plan",
        "karox.checks.run",
        "karox.checks.run_affected",
        "karox.checks.start",
        "karox.checks.cancel",
        "karox.command.start",
        "karox.command.cancel",
        "karox.tests.run",
        "karox.dev_server.start",
        "karox.dev_server.stop",
        "karox.dev_server.restart",
    }
)
# Browser tools that drive the browser (mutate its session state). Tab
# management belongs here with open/click/fill: switching or closing a tab is
# the same capability tier as navigating one. ``karox.browser.command`` is
# deliberately absent: its mutating actions are checked dynamically per call,
# so on a READ_ONLY session it is a genuinely read-only surface and must not
# make the bundle negotiate browser input support.
_BROWSER_INPUT_TOOLS = frozenset(
    {
        "karox.browser.open",
        "karox.browser.new_tab",
        "karox.browser.switch_tab",
        "karox.browser.close_tab",
        "karox.browser.click",
        "karox.browser.fill",
        "karox.browser.fill_credential",
        "karox.browser.select",
        "karox.browser.press",
        "karox.browser.close",
    }
)


def _checksum(payload: Mapping[str, Any]) -> str:
    value = dict(payload)
    value.pop("checksum", None)
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ClientCapabilitySnapshot:
    client_kind: str
    tool_schema_snapshot_version: int
    read_support: bool
    write_support: bool
    browser_support: bool
    browser_input_support: bool
    image_artifact_support: bool
    approval_behavior: str
    practical_output_size_limit: int
    reconnect_behavior: str
    effective_capability: str
    effective_reason: str
    available_tool_count: int
    available_tools_digest: str
    observed_at: float
    schema_version: int = CLIENT_CAPABILITY_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "client_kind": self.client_kind,
            "tool_schema_snapshot_version": self.tool_schema_snapshot_version,
            "read_support": self.read_support,
            "write_support": self.write_support,
            "browser_support": self.browser_support,
            "browser_input_support": self.browser_input_support,
            "image_artifact_support": self.image_artifact_support,
            "approval_behavior": self.approval_behavior,
            "practical_output_size_limit": self.practical_output_size_limit,
            "reconnect_behavior": self.reconnect_behavior,
            "effective_capability": self.effective_capability,
            "effective_reason": self.effective_reason,
            "available_tool_count": self.available_tool_count,
            "available_tools_digest": self.available_tools_digest,
            "observed_at": self.observed_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ClientCapabilitySnapshot":
        if int(payload.get("schema_version", 0)) != CLIENT_CAPABILITY_SCHEMA_VERSION:
            raise SessionError("unsupported client capability schema")
        return cls(
            client_kind=str(payload["client_kind"]),
            tool_schema_snapshot_version=int(payload["tool_schema_snapshot_version"]),
            read_support=bool(payload["read_support"]),
            write_support=bool(payload["write_support"]),
            browser_support=bool(payload["browser_support"]),
            browser_input_support=bool(payload["browser_input_support"]),
            image_artifact_support=bool(payload["image_artifact_support"]),
            approval_behavior=str(payload["approval_behavior"]),
            practical_output_size_limit=int(payload["practical_output_size_limit"]),
            reconnect_behavior=str(payload["reconnect_behavior"]),
            effective_capability=str(payload["effective_capability"]),
            effective_reason=str(payload["effective_reason"]),
            available_tool_count=int(payload["available_tool_count"]),
            available_tools_digest=str(payload["available_tools_digest"]),
            observed_at=float(payload["observed_at"]),
            schema_version=int(payload["schema_version"]),
        )


def negotiate_client_capabilities(
    *,
    client_kind: str,
    available_tools: Sequence[str],
    access_profile: str,
    disabled_tools: Sequence[Mapping[str, Any]] = (),
    practical_output_size_limit: int = 64 * 1024,
    persistent_session: bool = True,
    client_policy_write_blocked: bool = False,
) -> ClientCapabilitySnapshot:
    tools = tuple(sorted(set(str(item) for item in available_tools)))
    if not 4096 <= practical_output_size_limit <= 16 * 1024 * 1024:
        raise ValueError("practical output limit must be between 4096 and 16777216")
    read_support = any(
        name.startswith(_READ_PREFIXES) or name in {"karox.artifact.get", "karox.artifact.read_image"}
        for name in tools
    )
    write_support = any(name in _WRITE_TOOLS for name in tools)
    browser_support = any(name.startswith("karox.browser.") for name in tools)
    browser_input_support = any(name in _BROWSER_INPUT_TOOLS for name in tools)
    image_artifact_support = any(
        name in {"karox.browser.screenshot", "karox.artifact.read_image"}
        for name in tools
    )
    requested_write = access_profile in {"workspace_write", "elevated", "browser_control"}
    disabled_write_reasons = [
        str(item.get("reason", ""))
        for item in disabled_tools
        if str(item.get("name", "")) in _WRITE_TOOLS
    ]
    if client_policy_write_blocked:
        write_support = False
        browser_input_support = False
        effective_capability = "read_only" if read_support else "unavailable"
        reason = "client_policy"
    elif write_support:
        effective_capability = "workspace_write"
        reason = "configured_and_exposed"
    elif browser_input_support:
        # A session that can drive a browser but not the repository is
        # browser_control, whatever profile string launched it. Reporting it
        # as read_only understated the profile's real reach and made the
        # negotiation disagree with the session record and the UI.
        effective_capability = "browser_control"
        reason = "configured_and_exposed"
    elif requested_write:
        effective_capability = "read_only" if read_support else "unavailable"
        reason = (
            "client_policy"
            if any("client policy" in value.lower() for value in disabled_write_reasons)
            else "profile_configuration"
        )
    elif read_support:
        effective_capability = "read_only"
        reason = "access_profile"
    else:
        effective_capability = "unavailable"
        reason = "no_repository_tools_exposed"
    normalized_kind = client_kind.strip().lower() or "unknown-mcp-client"
    approval_behavior = (
        "explicit_user_gates"
        if normalized_kind in {"chatgpt-web", "chatgpt", "openai-chatgpt-web"}
        else "client_managed_with_server_tier3_gates"
    )
    reconnect_behavior = (
        "resume_persistent_session_and_revalidate_repository"
        if persistent_session
        else "new_session_required_after_launcher_exit"
    )
    digest = hashlib.sha256("\0".join(tools).encode("utf-8")).hexdigest()
    return ClientCapabilitySnapshot(
        client_kind=normalized_kind,
        tool_schema_snapshot_version=3,
        read_support=read_support,
        write_support=write_support,
        browser_support=browser_support,
        browser_input_support=browser_input_support,
        image_artifact_support=image_artifact_support,
        approval_behavior=approval_behavior,
        practical_output_size_limit=practical_output_size_limit,
        reconnect_behavior=reconnect_behavior,
        effective_capability=effective_capability,
        effective_reason=reason,
        available_tool_count=len(tools),
        available_tools_digest=digest,
        observed_at=time.time(),
    )


class ClientCapabilityStore:
    def __init__(self, session_directory: Path) -> None:
        self.path = session_directory / "client_capabilities.json"
        self.lock_path = session_directory / "client_capabilities.lock"

    def save(self, snapshot: ClientCapabilitySnapshot) -> None:
        payload = snapshot.to_dict()
        payload["checksum"] = _checksum(payload)
        with _exclusive_file_lock(self.lock_path):
            _atomic_json(self.path, payload)

    def load(self) -> ClientCapabilitySnapshot:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise SessionError("client capability snapshot does not exist") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise SessionError("client capability snapshot is unreadable") from exc
        if not isinstance(payload, dict):
            raise SessionError("client capability snapshot is malformed")
        checksum = payload.get("checksum")
        if not isinstance(checksum, str) or not hmac.compare_digest(checksum, _checksum(payload)):
            raise SessionError("client capability snapshot checksum mismatch")
        payload = dict(payload)
        payload.pop("checksum", None)
        return ClientCapabilitySnapshot.from_dict(payload)

    def load_optional(self) -> Optional[ClientCapabilitySnapshot]:
        try:
            return self.load()
        except SessionError as exc:
            if "does not exist" in str(exc):
                return None
            raise
