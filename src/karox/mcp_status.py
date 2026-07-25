"""One aggregated MCP state view.

Management of external MCP servers is spread over nine subcommands
(``mcp server add/remove/list/show/inspect/doctor``,
``mcp session select/deselect/permissions``,
``mcp credential set/show/delete/doctor``).  Answering the only questions a
user actually has -- what is connected, what is really alive, which tools may
run, where a secret is missing, what is marked ``ask`` and therefore blocked
-- required running several of them and joining the output by hand.

This module joins the three independent sources of truth into a single
payload:

* the registry (what is configured),
* the session selection (what is authorized),
* an optional liveness probe and credential probe (what is observable now).

Honesty rules encoded here on purpose:

* Authorization and reachability are DIFFERENT fields.  A tool that is allowed
  in the session is reported as allowed even when the server is unreachable,
  and a reachable server never implies an allowed tool.
* Liveness is never inferred.  Without a probe the state is ``not_probed``,
  not ``unknown`` and certainly not ``ok``.
* Credential expiry is not observable: the OS keyring stores a secret, not its
  lifetime.  We therefore report ``present``, ``missing`` or ``unreadable``
  and never invent an expiry date.  Servers whose secret is rejected by the
  remote end surface as an ``access`` liveness failure, which is the only
  honest evidence of an expired key that we actually have.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
from urllib.parse import urlsplit

from .mcp_client import McpServerRecord, mcp_registry_digest

STATUS_VERSION = 1

# Credential observation states.
CREDENTIAL_NOT_REQUIRED = "not_required"
CREDENTIAL_PRESENT = "present"
CREDENTIAL_MISSING = "missing"
CREDENTIAL_UNREADABLE = "unreadable"

# Liveness observation states.
LIVENESS_NOT_PROBED = "not_probed"
LIVENESS_LIVE = "live"
LIVENESS_FAILED = "failed"

# Session selection states.
SELECTION_NOT_SELECTED = "not_selected"
SELECTION_CURRENT = "current"
SELECTION_STALE = "stale"

# Per-tool authorization outcomes.
TOOL_ALLOWED = "allowed"
TOOL_BLOCKED_ASK = "blocked_ask"
TOOL_BLOCKED_DENY = "blocked_deny"
TOOL_BLOCKED_STALE = "blocked_stale"

_PERMISSIONS = {"allow": TOOL_ALLOWED, "ask": TOOL_BLOCKED_ASK, "deny": TOOL_BLOCKED_DENY}


@dataclass(frozen=True)
class McpLiveness:
    """Result of one deliberate probe against a server.

    ``tool_count`` is the number of tools the server advertised during the
    probe.  It is deliberately not merged with the number of authorized tools:
    a server may advertise ten tools while the session allows none.
    """

    state: str
    tool_count: Optional[int] = None
    failure_kind: Optional[str] = None
    detail: Optional[str] = None
    checked_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "tool_count": self.tool_count,
            "failure_kind": self.failure_kind,
            "detail": self.detail,
            "checked_at": self.checked_at,
        }


def _endpoint(record: McpServerRecord) -> str:
    """Human-readable endpoint.  Records never persist secrets (see registry)."""
    if record.transport == "stdio":
        return " ".join((record.command or "", *record.args)).strip()
    return record.url or ""


def _location(record: McpServerRecord) -> str:
    if record.transport == "stdio":
        return "local"
    host = (urlsplit(record.url or "").hostname or "").lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        return "local"
    return "remote"


def credential_state(
    record: McpServerRecord,
    resolve: Optional[Callable[[str], Any]] = None,
) -> dict[str, Any]:
    """Describe the secret a server needs without ever reading it out.

    ``resolve`` is called with the credential reference and may raise; a raise
    is reported as ``missing`` when the store says the reference does not
    exist and as ``unreadable`` for every other failure, because "the keyring
    could not be opened" is not the same fact as "the key was never set".
    """
    if record.credential_ref is None:
        return {
            "required": False,
            "reference": None,
            "target": None,
            "state": CREDENTIAL_NOT_REQUIRED,
            "detail": None,
        }
    state = CREDENTIAL_UNREADABLE if resolve is None else CREDENTIAL_PRESENT
    detail: Optional[str] = None if resolve is not None else "not checked"
    if resolve is not None:
        try:
            secret = resolve(record.credential_ref)
        except Exception as exc:  # noqa: BLE001 - store failures are data here
            message = str(exc)
            missing = "does not exist" in message
            state = CREDENTIAL_MISSING if missing else CREDENTIAL_UNREADABLE
            detail = type(exc).__name__
        else:
            if not isinstance(secret, str) or not secret:
                state = CREDENTIAL_MISSING
    return {
        "required": True,
        "reference": record.credential_ref,
        "target": record.credential_target,
        "state": state,
        "detail": detail,
    }


def _tools(selection: Optional[Mapping[str, Any]], stale: bool) -> list[dict[str, Any]]:
    if not isinstance(selection, Mapping):
        return []
    raw = selection.get("tools")
    if not isinstance(raw, Mapping):
        return []
    tools: list[dict[str, Any]] = []
    for remote_name, item in raw.items():
        entry = item if isinstance(item, Mapping) else {}
        permission = entry.get("permission")
        outcome = _PERMISSIONS.get(str(permission), TOOL_BLOCKED_DENY)
        if stale:
            outcome = TOOL_BLOCKED_STALE
        tools.append(
            {
                "remote_name": str(remote_name),
                "name": str(entry.get("name") or remote_name),
                "permission": str(permission) if permission is not None else "ask",
                "read_only": bool(entry.get("read_only")),
                "authorization": outcome,
            }
        )
    tools.sort(key=lambda value: value["remote_name"])
    return tools


def _selection_for(
    server_id: str, selections: Sequence[Any]
) -> Optional[Mapping[str, Any]]:
    for item in selections:
        if isinstance(item, Mapping) and item.get("server_id") == server_id:
            return item
    return None


def server_state(
    record: McpServerRecord,
    *,
    selection: Optional[Mapping[str, Any]] = None,
    credential: Optional[Mapping[str, Any]] = None,
    liveness: Optional[McpLiveness] = None,
) -> dict[str, Any]:
    """Join one server's configuration, authorization, and observations."""
    if selection is None:
        selection_state = SELECTION_NOT_SELECTED
        stale = False
    elif selection.get("registry_digest") != mcp_registry_digest(record):
        selection_state = SELECTION_STALE
        stale = True
    else:
        selection_state = SELECTION_CURRENT
        stale = False
    tools = _tools(selection, stale)
    counts = {
        "tools": len(tools),
        TOOL_ALLOWED: sum(1 for item in tools if item["authorization"] == TOOL_ALLOWED),
        TOOL_BLOCKED_ASK: sum(
            1 for item in tools if item["authorization"] == TOOL_BLOCKED_ASK
        ),
        TOOL_BLOCKED_DENY: sum(
            1 for item in tools if item["authorization"] == TOOL_BLOCKED_DENY
        ),
        TOOL_BLOCKED_STALE: sum(
            1 for item in tools if item["authorization"] == TOOL_BLOCKED_STALE
        ),
    }
    observed = liveness or McpLiveness(LIVENESS_NOT_PROBED)
    secret = dict(credential) if credential is not None else credential_state(record)
    attention: list[str] = []
    if selection_state == SELECTION_NOT_SELECTED:
        attention.append("not_selected")
    if selection_state == SELECTION_STALE:
        attention.append("selection_stale")
    if secret["state"] == CREDENTIAL_MISSING:
        attention.append("credential_missing")
    if secret["state"] == CREDENTIAL_UNREADABLE:
        attention.append("credential_unreadable")
    if observed.state == LIVENESS_FAILED:
        attention.append("unreachable")
    if tools and counts[TOOL_ALLOWED] == 0:
        attention.append("no_allowed_tools")
    if counts[TOOL_BLOCKED_ASK]:
        # ``ask`` is not an interactive prompt today: the call is refused until
        # the session is edited by hand.  Say so instead of implying a dialog.
        attention.append("ask_blocks_calls")
    return {
        "server_id": record.server_id,
        "namespace": record.namespace,
        "transport": record.transport,
        "location": _location(record),
        "endpoint": _endpoint(record),
        "timeout_seconds": record.timeout_seconds,
        "credential": secret,
        "liveness": observed.to_dict(),
        "selection": {"state": selection_state, "counts": counts},
        "tools": tools,
        "attention": attention,
    }


def build_mcp_status(
    servers: Iterable[McpServerRecord],
    *,
    session_id: Optional[str] = None,
    selections: Sequence[Any] = (),
    resolve_credential: Optional[Callable[[str], Any]] = None,
    liveness: Optional[Mapping[str, McpLiveness]] = None,
) -> dict[str, Any]:
    """Build the whole MCP state screen payload.

    Nothing here contacts a server or a keyring by itself: probes are passed
    in.  That keeps the screen renderable offline and keeps the expensive,
    failure-prone work under the caller's explicit control.
    """
    probes = dict(liveness or {})
    entries = [
        server_state(
            record,
            selection=_selection_for(record.server_id, selections),
            credential=credential_state(record, resolve_credential),
            liveness=probes.get(record.server_id),
        )
        for record in sorted(servers, key=lambda item: item.server_id)
    ]
    totals = {
        "servers": len(entries),
        "selected": sum(
            1
            for item in entries
            if item["selection"]["state"] != SELECTION_NOT_SELECTED
        ),
        "probed": sum(
            1 for item in entries if item["liveness"]["state"] != LIVENESS_NOT_PROBED
        ),
        "live": sum(
            1 for item in entries if item["liveness"]["state"] == LIVENESS_LIVE
        ),
        "allowed_tools": sum(
            item["selection"]["counts"][TOOL_ALLOWED] for item in entries
        ),
        "blocked_tools": sum(
            item["selection"]["counts"][TOOL_BLOCKED_ASK]
            + item["selection"]["counts"][TOOL_BLOCKED_DENY]
            + item["selection"]["counts"][TOOL_BLOCKED_STALE]
            for item in entries
        ),
        "needs_attention": sum(1 for item in entries if item["attention"]),
    }
    return {
        "status_version": STATUS_VERSION,
        "session_id": session_id,
        "totals": totals,
        "servers": entries,
    }
