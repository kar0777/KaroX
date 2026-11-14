"""Bridge wire/security helpers: hosts, tool naming, idempotency and safe errors."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import sys
import threading
from typing import Any, Iterable, Mapping, Optional, Sequence
from urllib.parse import urlsplit
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent
from starlette.responses import Response
from .core import CoreError
from .core_types import VerificationCommandNotApproved
from .command_guard import DeveloperCommandBlocked
from .hosted_core import (
    HostedBridgeError,
)
from .process_launcher import is_executable_resolution_error
from .sessions import SessionError


# The wire answers a third-party agent, so an error may only carry a code from
# this table. Raw exception text on this path has already leaked absolute paths
# -- and with them the user's home directory and OS account name -- to whoever
# holds the bearer token.
BRIDGE_ERROR_MESSAGES: dict[str, str] = {
    "tool_not_exposed": "the tool is not exposed by this bridge",
    # Still reachable on the OpenAPI wire, where the key travels in a header the
    # client controls. The MCP wire derives one instead, because its web clients
    # cannot send `_meta` at all.
    "idempotency_key_required": "mutating calls require a stable idempotency key",
    "idempotency_key_invalid": (
        "the supplied idempotency key must be a string of 1-256 characters"
    ),
    "denied": "the call was denied by the KaroX session policy",
    "not_found": "the requested repository path does not exist",
    "executable_not_found": (
        "the guarded command's executable could not be resolved "
        "(install the missing tool or use a command in the allowlist)"
    ),
    "invalid_request": "the call was rejected as invalid",
    "verification_not_approved": (
        "the check command is outside the user-approved verification set; "
        "use an approved checks.run command, or on Full developer access use "
        "karox.command.run/start for repository-scoped developer verification"
    ),
    "request_interrupted": "the call was interrupted before it finished",
    "internal": "the tool failed",
}

# ``tools/list`` advertises wire spellings only, but ``tools/call`` accepts the
# dotted internal spelling as a compatibility alias (see module comment above).
# The MCP SDK does not know about the alias, so its lowlevel server logs
# ``Tool 'karox.repo.read_file' not listed, no validation will be performed``
# for every dotted call on a perfectly normal flow.  That warning is precise
# noise: the call is routed, validated, and policy-checked by ``proxy.execute``.
#
# The filter below drops exactly that record and only for names this process
# currently routes as aliases.  A genuinely unknown tool name still warns, and
# nothing else about the ``mcp`` loggers is touched.
_DIAGNOSTICS_TOOL_NAME = "karox.bridge.diagnostics"
_ROUTABLE_ALIAS_NAMES: set[str] = {_DIAGNOSTICS_TOOL_NAME}

# Refusals whose text is fixed, server-authored and free of paths or secrets.
# Surfacing them turns a bare "invalid_request" into something a caller can
# act on (pass allow_secret_literal, re-read the file, fix the occurrence count)
# instead of guessing why a write was rejected.
_SAFE_INVALID_REQUEST_PREFIXES: tuple[str, ...] = (
    "write blocked by credential scanner",
    "file changed since it was read",
    "expected_sha256 must be",
    "repo.edit_file edits text files only",
    "repo.read_file reads text files only",
)

# Denials whose text is server-authored and names a transient, recoverable
# condition rather than a permission boundary. Without them a caller sees a bare
# "denied by the session policy" and wrongly concludes it lacks permission,
# when the repository is only busy with another live mutation (for example a
# long durable test run) and the right move is read-only work or a retry.
_SAFE_DENIED_PREFIXES: tuple[str, ...] = (
    "repository is busy with another live mutation",
    "mutation queue is briefly busy",
    "idempotency conflict, not a permission denial",
)

# Exceptions whose message KaroX itself wrote for the caller: a validation
# refusal ("request_id must be ...", "unknown job: ...", "session profile does
# not allow dev.command"). A bare "the call was rejected as invalid" sends a
# hosted model guessing, so their first line is passed on -- with anything that
# could describe the machine taken out. Library exceptions (a TypeError from a
# dependency, an OSError) never qualify: their text is not ours to vouch for.
_CALLER_AUTHORED_ERRORS: tuple[type[BaseException], ...] = (
    CoreError,
    HostedBridgeError,
    SessionError,
    DeveloperCommandBlocked,
)

ALLOWED_HOSTS_ENVIRONMENT = "KAROX_MCP_ALLOWED_HOSTS"

MODERN_MCP_PROTOCOL_VERSION = "2026-07-28"

LEGACY_MCP_PROTOCOL_VERSIONS = (
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)

_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

# `karox bridge connect` declares the tunnel host for the bridge it starts; a
# hand-run `karox bridge serve` behind someone else's tunnel has to be told, and
# the 421 body is the only place an operator will look.
HOST_REJECTION_HINT = (
    "invalid Host header; declare the public host in "
    f"{ALLOWED_HOSTS_ENVIRONMENT}"
)

# Process-local, content-free HTTP activity used only to defer a supervised
# MCP-child restart until the response that requested it has left the wire.
# This child serves one hosted bridge, so process scope is the correct boundary.
_TRANSPORT_ACTIVITY_LOCK = threading.Lock()

_TRANSPORT_ACTIVE_REQUESTS = 0

_TRANSPORT_RESPONSES_COMPLETED = 0

_LOGGER = logging.getLogger(__name__)

def _transport_activity_started() -> None:
    global _TRANSPORT_ACTIVE_REQUESTS
    with _TRANSPORT_ACTIVITY_LOCK:
        _TRANSPORT_ACTIVE_REQUESTS += 1

def _transport_activity_finished(*, completed: bool) -> None:
    global _TRANSPORT_ACTIVE_REQUESTS, _TRANSPORT_RESPONSES_COMPLETED
    with _TRANSPORT_ACTIVITY_LOCK:
        _TRANSPORT_ACTIVE_REQUESTS = max(0, _TRANSPORT_ACTIVE_REQUESTS - 1)
        if completed:
            _TRANSPORT_RESPONSES_COMPLETED += 1

def transport_activity_snapshot() -> dict[str, int]:
    """Return secret-free process-level transport liveness counters."""
    with _TRANSPORT_ACTIVITY_LOCK:
        return {
            "active_requests": _TRANSPORT_ACTIVE_REQUESTS,
            "responses_completed": _TRANSPORT_RESPONSES_COMPLETED,
        }

def derive_idempotency_key(tool_name: str, arguments: Mapping[str, object]) -> str:
    """Content-address a mutation whose caller cannot name its own retries.

    ChatGPT and Claude speak MCP without ``_meta``, so neither can carry a key of
    its own. Refusing those calls left ``--write`` advertising two tools that
    could never run: the connector listed ``repo.write_file``, the model called
    it, and every call came back as ``idempotency_key_required``.

    Hashing the call gives the property the key exists for. A retry after a lost
    response repeats the same tool and the same arguments, so it lands on the
    same key and returns the first outcome instead of writing twice -- which a
    per-attempt key would not have done.

    The cost is that a deliberate repeat of a byte-identical mutation is also
    treated as a retry. Both write tools are declarative, so that is close to
    free: ``repo.write_file`` states the whole content, and ``repo.edit_file``
    states an exact match plus the number of occurrences it expects, so a real
    repeat is a no-op or a loud mismatch either way. The remaining case -- the
    file changed underneath and the same content is written again -- is why the
    result carries ``idempotent_replay``, so a replay is never reported to the
    model as a write that just landed.
    """
    payload = json.dumps(
        {"tool": tool_name, "arguments": arguments},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"derived-{digest}"

# MCP itself puts no constraint on a tool's name, but the model APIs behind the
# clients that consume a remote MCP server do: an Anthropic tool definition must
# match ``^[a-zA-Z0-9_-]{1,64}$``, and OpenAI's function names are the same
# shape.  A name containing a dot therefore cannot be handed to the model at all.
#
# KaroX names its tools ``karox.repo.read_file``, and that spelling is an
# identifier throughout the codebase -- session allowlists, launch profiles,
# stored policy, and the hosted-bridge catalogue all key on it.  A client that
# turns our ``tools/list`` into model tools drops every one of them, and the
# symptom is the worst kind: the handshake succeeds, the connector shows as
# connected, and the model reports that it can see no tools.
#
# The dot is a wire-encoding problem, not an identity problem, so it is fixed at
# the wire and nowhere else.  Outbound names swap dots for underscores; inbound
# calls accept either spelling, so a client holding the old name keeps working
# and the internal identifier never changes.

def wire_tool_name(internal_name: str) -> str:
    """Return the ``tools/list`` spelling of an internal tool name."""
    return internal_name.replace(".", "_")

_NOT_LISTED_MESSAGE = re.compile(r"Tool '(?P<name>[^']+)' not listed")

class _AliasCatalogLogFilter(logging.Filter):
    """Suppress the SDK catalog warning for advertised-alias calls only."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        match = _NOT_LISTED_MESSAGE.search(message)
        if match is None:
            return True
        return match.group("name") not in _ROUTABLE_ALIAS_NAMES

def _install_alias_catalog_log_filter() -> None:
    target = logging.getLogger("mcp.server.lowlevel.server")
    if not any(isinstance(item, _AliasCatalogLogFilter) for item in target.filters):
        target.addFilter(_AliasCatalogLogFilter())

def resolve_wire_tool_name(
    supplied: str, internal_names: Iterable[str]
) -> Optional[str]:
    """Return the internal tool name a ``tools/call`` name refers to.

    An exact internal name wins, so a caller that speaks the dotted spelling is
    unaffected.  Failing that, the supplied name is matched against the wire
    spelling of each candidate.  Returns ``None`` when nothing matches, which the
    caller reports as ``tool_not_exposed``.
    """
    candidates = list(internal_names)
    if supplied in candidates:
        return supplied
    for name in candidates:
        if wire_tool_name(name) == supplied:
            return name
    return None

def stale_catalog_hint(
    supplied: str,
    advertised_wire_names: Sequence[str],
    cached_wire_names: Optional[Sequence[str]] = None,
) -> str:
    """One short, actionable line for a client whose cached catalog is stale.

    This proxy answers stateless JSON (no long-lived stream), so the MCP
    ``notifications/tools/list_changed`` mechanism cannot reach a connected
    client and ``capabilities.tools.listChanged`` is honestly ``false``. A
    client that cached an old ``tools/list`` therefore keeps calling names
    outside the current catalog until it reconnects. When that happens, name
    exactly one fix: reconnect this client. Never propose a new physical
    bridge, a credential rotation, or a URL change; none of them refresh a
    client-side catalog cache and all of them are destructive.

    ``cached_wire_names`` is optional because this stateless wire usually does
    not know what a given client cached; a caller that does know (a session
    store, a test, a future stateful transport) gets the precise count of
    newly available tools.
    """
    advertised = set(advertised_wire_names)
    if cached_wire_names is not None:
        added = sorted(advertised - set(cached_wire_names))
        if added:
            noun = "tool is" if len(added) == 1 else "tools are"
            return (
                f"{len(added)} new {noun} available; reconnect this client to "
                "refresh its tool catalog (same bridge, same credential, "
                "same URL)"
            )
    return (
        f"the current catalog advertises {len(advertised)} tools; if this "
        "client connected before the last catalog upgrade its cached tool "
        "list is stale, and reconnecting this client refreshes it "
        "(same bridge, same credential, same URL)"
    )

def _idna_normalize(host: str) -> str:
    """Return the punycode form of an internationalized host, or ``""``.

    Tailscale MagicDNS names are ASCII, but a user can publish the bridge behind
    an IDN origin, and the browser/TLS layer speaks punycode (``xn--...``) while
    the operator may paste the unicode form into ``--public-url``. Comparing the
    two as raw strings makes a perfectly good host look foreign and earns a 421.
    """
    if not host or host.isascii():
        return host
    try:
        return host.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return ""

def normalize_host(value: Optional[str]) -> str:
    """Return a lowercase hostname without port, or ``""`` when unusable."""
    raw = (value or "").strip().lower().rstrip(".")
    if not raw or any(
        char in raw for char in ("/", "\\", "@", "\x00", " ", "\t", "\r", "\n")
    ):
        return ""
    if raw.startswith("["):
        closing = raw.find("]")
        if closing < 0:
            return ""
        host = raw[1:closing]
        remainder = raw[closing + 1 :]
        if remainder and not (remainder.startswith(":") and remainder[1:].isdigit()):
            return ""
        return _idna_normalize(host)
    if raw.count(":") == 1:
        host, port = raw.rsplit(":", 1)
        if port.isdigit():
            return _idna_normalize(host)
    return _idna_normalize(raw)

def forwarded_host(headers: Mapping[str, str]) -> str:
    """Extract the original host a trusted proxy forwarded, or ``""``.

    Only the *first* hop's value is taken: a chain like ``a, b`` is read left to
    right and the leftmost is the closest proxy, which is the one this bridge
    configured. Untrusted later hops are ignored so an external client cannot
    prepend its own host.
    """
    forwarded = headers.get("forwarded")
    if forwarded:
        for hop in forwarded.split(","):
            for pair in hop.split(";"):
                key, _, val = pair.strip().partition("=")
                if key.lower() == "host" and val:
                    return normalize_host(val.strip().strip('"'))
    xfh = headers.get("x-forwarded-host")
    if xfh:
        return normalize_host(xfh.split(",")[0].strip())
    return ""

def peer_is_loopback(scope: Mapping[str, Any]) -> bool:
    """True when the request reached the listener from this machine.

    The tunnel child connects to ``127.0.0.1:<port>``, so the only loopback peer
    in production is the tunnel itself. That is the trusted local proxy path
    permitted to set ``Forwarded``/``X-Forwarded-Host``; a remote client is not.
    """
    client = scope.get("client")
    if not isinstance(client, (list, tuple)) or len(client) < 1:
        return False
    return str(client[0]) in {"127.0.0.1", "::1", "localhost"}

def resolve_allowed_hosts(extra: Optional[Sequence[str]] = None) -> frozenset[str]:
    """Return the host names this bridge is willing to answer to.

    A public tunnel host name is not derivable from inside the listener, so it
    has to be declared -- by the launcher that created the tunnel or by the
    operator through ``KAROX_MCP_ALLOWED_HOSTS``. Everything else arriving at a
    loopback listener that is published over a tunnel is a name someone else
    pointed at this machine, which is exactly the DNS rebinding case.
    """
    hosts = set(_LOOPBACK_HOSTS)
    declared = list(extra or ())
    declared.extend(os.environ.get(ALLOWED_HOSTS_ENVIRONMENT, "").split(","))
    for item in declared:
        host = normalize_host(item)
        if host:
            hosts.add(host)
    return frozenset(hosts)

def origin_is_allowed(origin: Optional[str], allowed: frozenset[str]) -> bool:
    """Validate a browser ``Origin`` against the same host allowlist.

    A missing Origin is accepted because non-browser MCP clients never send one;
    a browser driving a rebound name always does, which is what this rejects.
    """
    if origin is None:
        return True
    parts = urlsplit(origin.strip())
    if parts.scheme not in {"http", "https"}:
        return False
    if parts.path or parts.query or parts.fragment:
        return False
    return normalize_host(parts.netloc) in allowed

def scope_headers(scope: Mapping[str, Any]) -> dict[str, str]:
    """Decode request headers without consuming the ASGI receive channel."""
    headers: dict[str, str] = {}
    for raw_name, raw_value in scope.get("headers", ()):
        name = raw_name.decode("latin-1").lower()
        value = raw_value.decode("latin-1").strip()
        headers[name] = f"{headers[name]}, {value}" if name in headers else value
    return headers

def transport_security_settings(
    allowed: frozenset[str],
) -> TransportSecuritySettings:
    """Express the same allowlist in the SDK's exact-match vocabulary."""
    hosts: list[str] = []
    origins: list[str] = []
    for host in sorted(allowed):
        literal = f"[{host}]" if ":" in host else host
        hosts.extend((literal, f"{literal}:*"))
        for scheme in ("http", "https"):
            origins.extend((f"{scheme}://{literal}", f"{scheme}://{literal}:*"))
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )

def bridge_error_result(code: str, *, detail: Optional[str] = None) -> CallToolResult:
    """Build the only tool-error shape this wire is allowed to send.

    ``detail`` must be server-generated, secret-free text (never raw exception
    content); it extends the fixed table message with an actionable hint such
    as the stale-catalog reconnect guidance.
    """
    # ``get`` with the internal fallback: an error handler that itself raises
    # KeyError replaces the intended tool error with a 500, which is how a
    # benign interrupt once escaped as a crash.
    message = BRIDGE_ERROR_MESSAGES.get(code, BRIDGE_ERROR_MESSAGES["internal"])
    if detail:
        message = f"{message}; {detail}"
    return CallToolResult(
        content=[TextContent(type="text", text=f"{code}: {message}")],
        structuredContent={"ok": False, "error_code": code, "error": message},
        isError=True,
    )

def utf8_call_result(
    result: dict[str, Any] | CallToolResult,
) -> tuple[list[TextContent], dict[str, Any]] | CallToolResult:
    """Hand a plain tool result to the SDK with its text already UTF-8.

    The low-level SDK turns a ``dict`` into text with ``json.dumps(indent=2)``,
    which escapes every non-ASCII character: a Cyrillic letter became six ASCII
    characters in the text block most clients hand the model. Returning the
    SDK's own ``(content, structured)`` pair keeps everything else as it was:
    the structured copy, the indentation, ``isError`` and output-schema
    validation; a value ``json`` cannot encode still fails inside the handler.
    """
    if isinstance(result, CallToolResult):
        return result
    text = json.dumps(result, indent=2, ensure_ascii=False)
    return [TextContent(type="text", text=text)], result

_SAFE_OCCURRENCE_DETAIL = re.compile(
    r"expected \d+ occurrence\(s\) of old_string but found \d+"
)

_SAFE_DENIED_OPERATION = re.compile(r"\(([A-Za-z0-9_.:-]{1,64})\)")

def bridge_denied_detail(exc: BaseException) -> Optional[str]:
    """Return a caller-safe detail for a transient ``denied`` refusal, if any."""
    text = str(exc).splitlines()[0].strip() if str(exc) else ""
    for prefix in _SAFE_DENIED_PREFIXES:
        if not text.startswith(prefix):
            continue
        if prefix.startswith("repository is busy"):
            match = _SAFE_DENIED_OPERATION.search(text)
            operation = match.group(1) if match else "mutation"
            return (
                f"repository is busy with another live mutation ({operation}); "
                "this is not a permission denial: continue read-only work or retry "
                "after it finishes"
            )
        return prefix
    return caller_safe_detail(exc)

def bridge_error_detail(exc: BaseException) -> Optional[str]:
    """Return a caller-safe detail for an invalid_request refusal, if any."""
    text = str(exc).splitlines()[0].strip() if str(exc) else ""
    if _SAFE_OCCURRENCE_DETAIL.fullmatch(text):
        return text
    for prefix in _SAFE_INVALID_REQUEST_PREFIXES:
        if text.startswith(prefix):
            return text[:300]
    return caller_safe_detail(exc)

_DETAIL_LIMIT = 400

_URL_TEXT = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"<>]+")

_UNC_PATH_TEXT = re.compile(r"\\\\[^\s'\";,()<>]+")

_WINDOWS_PATH_TEXT = re.compile(r"(?<![\w])[A-Za-z]:[\\/][^\s'\";,()<>]*")

_HOME_PATH_TEXT = re.compile(r"(?<![\w])~[\\/][^\s'\";,()<>]*")

_POSIX_PATH_TEXT = re.compile(r"(?<![\w.~:/\\-])/(?=[^\s/])[^\s'\";,()<>]*")

def caller_safe_detail(exc: BaseException) -> Optional[str]:
    """First line of a KaroX-authored refusal, without paths, URLs or secrets."""
    from .security import redact

    if not isinstance(exc, _CALLER_AUTHORED_ERRORS):
        return None
    lines = str(exc).strip().splitlines()
    if not lines:
        return None
    text = lines[0].strip()
    try:
        home = os.path.expanduser("~")
    except Exception:  # noqa: BLE001 - a missing home only skips a replacement
        home = ""
    if home and len(home) > 3:
        text = text.replace(home, "~")
    text = _URL_TEXT.sub("<url>", text)
    for pattern in (_UNC_PATH_TEXT, _WINDOWS_PATH_TEXT, _HOME_PATH_TEXT, _POSIX_PATH_TEXT):
        text = pattern.sub("<path>", text)
    text = str(redact(text)).strip()
    if not text:
        return None
    if len(text) > _DETAIL_LIMIT:
        text = text[: _DETAIL_LIMIT - 3].rstrip() + "..."
    return text

def bridge_error_code(exc: BaseException) -> str:
    """Classify a handler failure into one of the codes callers may be told."""
    # A missing executable (npm.cmd not on PATH) raises ExecutableResolutionError,
    # a FileNotFoundError subclass.  It must NOT be collapsed with a genuine
    # missing-repository FileNotFoundError into ``not_found`` ("the requested
    # repository path does not exist"), which previously misdiagnosed a WinError 2
    # as a repo-path problem even though the repo was readable.  Check it first.
    if is_executable_resolution_error(exc):
        return "executable_not_found"
    if isinstance(exc, FileNotFoundError):
        return "not_found"
    if isinstance(exc, VerificationCommandNotApproved):
        return "verification_not_approved"
    if isinstance(exc, (PermissionError, SessionError)):
        return "denied"
    if isinstance(exc, (CoreError, HostedBridgeError, TypeError, ValueError)):
        return "invalid_request"
    return "internal"

def _maybe_record_client_evidence(tool_name: str, *, success: bool) -> None:
    """Record client-verification evidence when an external client calls runtime.status.

    This hook fires inside the bridge serve child's ``call_tool`` handler, so it
    is triggered ONLY by a real MCP tool call from an external client. Evidence
    fields are safe only: timestamp, tool name, success/failure. Never stores an
    OAuth token, Authorization header, tool arguments, file contents, or user
    messages.

    The release runtime keeps no connection-status ledger, so this is a no-op
    seam. It stays in the call path (rather than being deleted) so a future
    durable evidence store can be attached without touching tool semantics.
    """
    if tool_name != "runtime.status":
        return
    return None

def _log_rebinding_421(
    scope: Mapping[str, Any],
    headers: Mapping[str, str],
    *,
    rejected_host: str,
    reason: str,
    allowed: frozenset[str],
    expected_host: Optional[str] = None,
) -> None:
    """Print one redacted line explaining a 421, to the launcher console.

    The 421 used to be a bare ``invalid Host header`` on an open endpoint, so the
    only way to learn which header a real client actually sent was to reproduce it
    with a packet capture. This prints what the guard saw -- nothing it must not.

    Nothing sensitive is logged: the Authorization header, Cookie, any token, the
    approval password, and the bridge credential are all absent from ``scope``'s
    path. ``scope["path"]`` carries no query string in ASGI (``query_string`` is
    separate), and even that is not printed, so a ``code``/``token`` leaking into
    a redirect's query is not a concern here.
    """
    method = scope.get("method", "GET").upper()
    path = scope.get("path", "")
    request_id = secrets.token_hex(8)
    forwarded = headers.get("forwarded") or headers.get("x-forwarded-host")
    expected = expected_host or ",".join(sorted(allowed))
    print(
        f"[karox-rebind] 421 {method} {path} request_id={request_id} "
        f"host={rejected_host or '<none>'} "
        f"x-forwarded-host={'yes' if forwarded else 'no'} "
        f"x-forwarded-proto={'yes' if headers.get('x-forwarded-proto') else 'no'} "
        f"forwarded={'yes' if headers.get('forwarded') else 'no'} "
        f"expected_origin={expected} reason={reason}",
        file=sys.stderr,
        flush=True,
    )

def rebinding_rejection(
    scope: Mapping[str, Any], allowed: frozenset[str]
) -> Optional[Response]:
    """Reject a request whose Host or Origin was not declared for this bridge.

    Every wire published through the same tunnel shares this guard: a name
    someone else pointed at a loopback listener is the DNS rebinding case no
    matter which path it lands on.
    """
    headers = scope_headers(scope)
    host = normalize_host(headers.get("host"))
    if host not in allowed:
        # A trusted local tunnel/proxy connects from loopback and may rewrite the
        # Host to its loopback target while preserving the public name it received
        # in Forwarded/X-Forwarded-Host. An external client is not a loopback peer,
        # so it cannot use this path to smuggle in a host of its own choosing.
        if peer_is_loopback(scope):
            forwarded = forwarded_host(headers)
            if forwarded and forwarded in allowed:
                host = forwarded
        if host not in allowed:
            _log_rebinding_421(
                scope,
                headers,
                rejected_host=normalize_host(headers.get("host")),
                reason="host_not_allowed",
                allowed=allowed,
            )
            return Response(HOST_REJECTION_HINT, status_code=421)
    if not origin_is_allowed(headers.get("origin"), allowed):
        _log_rebinding_421(
            scope,
            headers,
            rejected_host=host,
            reason="origin_not_allowed",
            allowed=allowed,
        )
        return Response("invalid Origin header", status_code=421)
    return None