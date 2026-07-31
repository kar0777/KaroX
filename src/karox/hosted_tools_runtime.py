"""Hosted-client bridge runtime for browser, dev-server and artifact tools.

The Core bridge (:mod:`karox.hosted_bridge`) exposes repository/git/check tools
through :class:`CoreToolRuntime`.  This module is the *other half* of a hosted
ChatGPT/Claude contract: capabilities that do not belong on the Core command
path because they do not touch the repository tree.  They are a separate
:class:`HostedToolRuntime` composed into the same :class:`CompositeHostedBridge`,
so a client sees one flat tool list.

Capabilities implemented here were already present in the codebase but only on
the remote (Ellipsis) path:

* Playwright browser automation exists in :mod:`karox.remote_tools`, but as a
  single shot ``browser.actions`` action list bound to the remote runtime.  Here
  it becomes a *stateful* session (open/snapshot/click/...) that a hosted client
  can drive one step at a time.
* Managed processes exist in :mod:`karox.remote_tools` (``ManagedProcessStore``,
  ``stop_session_processes``); here they back ``karox.dev_server.*`` behind a
  per-session server-profile allowlist (``start:safe`` yes, bare ``start`` no).
* Artifact storage with ``image/png`` MCP image content did not exist anywhere;
  :mod:`karox.artifacts` adds it.

All three share one principle: a tool owned by KaroX session A is unreachable
from session B, and the browser/process context dies with the session.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from mcp.types import CallToolResult, ImageContent, TextContent

from .artifacts import ArtifactStore
from .browser_session import (
    BrowserError,
    BrowserSecurityError,
    BrowserSessionManager,
    _validate_local_url,
)
from .hosted_bridge import (
    DEFAULT_HOSTED_DEADLINE_SECONDS,
    HOSTED_EXTRA_TOOL_NAMES,
    HostedBridgeAccessDenied,
)
from .models import AccessProfile, Capability, Origin
from .policy import CapabilityPolicy, PolicyDenied
from .process_launcher import resolve_process_argv as _resolve_process_argv
from .proxy import ProxyToolDescriptor
from .remote_tools import (
    ManagedProcessRecord,
    ManagedProcessStore,
    _kill_pid_tree,
    _pid_alive,
    _read_log,
)
from .security import child_process_environment, redact
from .sessions import SessionStore


# ---------------------------------------------------------------------------
# Server profiles
# ---------------------------------------------------------------------------

# The default safe server profile mirrors the Vacancy Control project's
# ``start:safe`` script: it forces ``FACEBOOK_LIVE_ENABLED=false`` and only
# allows the caller to override a harmless DB file path.  The bare ``npm start``
# script is *deliberately* absent because that script enables live publication
# by default -- exposing it would let a hosted client start real Facebook
# actions from a verification run.
SAFE_NPM = ("npm", "run", "start:safe")


@dataclass(frozen=True)
class ManagedServerProfile:
    """A user-approved dev-server launch recipe.

    ``argv`` is matched exactly against the argv a tool call requests, so a
    profile that permits ``npm run start:safe`` does not also permit ``npm
    start``.  ``env`` is forced into the child environment regardless of the
    caller; ``env_allowlist`` names the *additional* variables the caller may
    pass at start time.
    """

    name: str
    argv: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    env_allowlist: frozenset[str] = field(default_factory=frozenset)
    host_hint: str = "127.0.0.1"
    ready_url: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name or len(self.name) > 80:
            raise ValueError("server profile name must be a 1-80 character string")
        if not isinstance(self.argv, tuple) or not self.argv:
            raise ValueError("server profile argv must be a non-empty tuple")
        if any(not isinstance(item, str) or not item for item in self.argv):
            raise ValueError("server profile argv must contain only non-empty strings")
        if len(self.argv) > 100:
            raise ValueError("server profile argv is too long")
        if not isinstance(self.env, Mapping):
            raise ValueError("server profile env must be a mapping")
        if not isinstance(self.env_allowlist, frozenset):
            raise ValueError("server profile env_allowlist must be a frozenset")
        if self.host_hint not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("server profile host_hint must be a loopback address")

    def matches(self, argv: Sequence[str]) -> bool:
        target = tuple(argv)
        return target == self.argv

    def to_public_dict(self) -> dict[str, Any]:
        """A secret-free description for diagnostics and saved profiles."""
        return {
            "name": self.name,
            "argv": list(self.argv),
            "env_keys": sorted(self.env.keys()),
            "env_allowlist": sorted(self.env_allowlist),
            "host_hint": self.host_hint,
            "ready_url": self.ready_url,
        }


def default_server_profiles() -> tuple[ManagedServerProfile, ...]:
    return (
        ManagedServerProfile(
            name="vacancy-control-safe",
            argv=SAFE_NPM,
            # Both forced for defense-in-depth: ``FACEBOOK_LIVE_ENABLED=false``
            # keeps real Facebook publication off, and ``HOST=127.0.0.1`` pins
            # the listener to loopback even though the ``start:safe`` script
            # already defaults HOST to 127.0.0.1.  Neither name is in
            # ``env_allowlist``, so a hosted client cannot override either one.
            env={
                "FACEBOOK_LIVE_ENABLED": "false",
                "HOST": "127.0.0.1",
            },
            env_allowlist=frozenset({"DB_FILE", "PORT", "NODE_ENV", "CI"}),
            host_hint="127.0.0.1",
        ),
    )


# ---------------------------------------------------------------------------
# Tool catalogue
# ---------------------------------------------------------------------------

BROWSER_OPEN = "karox.browser.open"
BROWSER_SNAPSHOT = "karox.browser.snapshot"
BROWSER_CLICK = "karox.browser.click"
BROWSER_FILL = "karox.browser.fill"
BROWSER_SELECT = "karox.browser.select"
BROWSER_PRESS = "karox.browser.press"
BROWSER_WAIT = "karox.browser.wait_for"
BROWSER_GET_TEXT = "karox.browser.get_text"
BROWSER_SCREENSHOT = "karox.browser.screenshot"
BROWSER_CONSOLE = "karox.browser.console"
BROWSER_NETWORK = "karox.browser.network_failures"
BROWSER_CLOSE = "karox.browser.close"
DEV_SERVER_START = "karox.dev_server.start"
DEV_SERVER_STATUS = "karox.dev_server.status"
DEV_SERVER_LOGS = "karox.dev_server.logs"
DEV_SERVER_STOP = "karox.dev_server.stop"
ARTIFACT_GET = "karox.artifact.get"
ARTIFACT_READ_IMAGE = "karox.artifact.read_image"


@dataclass(frozen=True)
class _ToolMeta:
    description: str
    input_schema: dict[str, Any]
    read_only: bool
    capability: Optional[Capability]


_OBJ = {"type": "object"}


def _selector_schema(required: bool = True) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"selector": {"type": "string"}},
        "required": ["selector"] if required else [],
        "additionalProperties": False,
    }


_HOSTED_EXTRA_TOOLS: dict[str, _ToolMeta] = {
    BROWSER_OPEN: _ToolMeta(
        description=(
            "Open a localhost URL in a headless browser and start a stateful "
            "session bound to this KaroX session.  Only http(s) URLs on "
            "127.0.0.1/localhost are accepted; file: and data: are refused."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "width": {"type": "integer"},
                "height": {"type": "integer"},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        read_only=False,
        capability=Capability.BROWSER_INPUT,
    ),
    BROWSER_SNAPSHOT: _ToolMeta(
        description=(
            "Return a structured, secret-free accessibility/DOM snapshot of the "
            "current page: URL, title, viewport, focused element, headings, "
            "buttons, inputs (label/type/disabled only), links, dialogs, tabs, "
            "visible text, scroll dimensions, overflow, zero-height/off-viewport "
            "issues and computed display/visibility.  Password values are never "
            "returned."
        ),
        input_schema={**_OBJ, "additionalProperties": False},
        read_only=True,
        capability=Capability.BROWSER_READ,
    ),
    BROWSER_CLICK: _ToolMeta(
        description="Click an element by CSS selector, id, role+name, label, or text.",
        input_schema=_selector_schema(),
        read_only=False,
        capability=Capability.BROWSER_INPUT,
    ),
    BROWSER_FILL: _ToolMeta(
        description="Fill an input/textarea by selector with the given value.",
        input_schema={
            "type": "object",
            "properties": {"selector": {"type": "string"}, "value": {"type": "string"}},
            "required": ["selector", "value"],
            "additionalProperties": False,
        },
        read_only=False,
        capability=Capability.BROWSER_INPUT,
    ),
    BROWSER_SELECT: _ToolMeta(
        description="Select an option in a <select> by selector and value(s).",
        input_schema={
            "type": "object",
            "properties": {
                "selector": {"type": "string"},
                "value": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
            },
            "required": ["selector", "value"],
            "additionalProperties": False,
        },
        read_only=False,
        capability=Capability.BROWSER_INPUT,
    ),
    BROWSER_PRESS: _ToolMeta(
        description="Press a keyboard key on an element by selector.",
        input_schema={
            "type": "object",
            "properties": {"selector": {"type": "string"}, "key": {"type": "string"}},
            "required": ["selector", "key"],
            "additionalProperties": False,
        },
        read_only=False,
        capability=Capability.BROWSER_INPUT,
    ),
    BROWSER_WAIT: _ToolMeta(
        description=(
            "Wait for an element to reach a state (attached/detached/hidden/visible) "
            "or for a fixed number of milliseconds (<= 10000)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "selector": {"type": "string"},
                "state": {"type": "string", "enum": ["attached", "detached", "hidden", "visible"]},
                "milliseconds": {"type": "number"},
            },
            "additionalProperties": False,
        },
        read_only=True,
        capability=Capability.BROWSER_READ,
    ),
    BROWSER_GET_TEXT: _ToolMeta(
        description="Return the visible text of an element by selector.",
        input_schema=_selector_schema(),
        read_only=True,
        capability=Capability.BROWSER_READ,
    ),
    BROWSER_SCREENSHOT: _ToolMeta(
        description=(
            "Capture a PNG screenshot (viewport or full page) and return it as MCP "
            "image content (image/png) plus an artifact reference with SHA-256."
        ),
        input_schema={
            "type": "object",
            "properties": {"full_page": {"type": "boolean"}, "name": {"type": "string"}},
            "additionalProperties": False,
        },
        read_only=True,
        capability=Capability.BROWSER_READ,
    ),
    BROWSER_CONSOLE: _ToolMeta(
        description="Return redacted browser console messages collected since open.",
        input_schema={**_OBJ, "additionalProperties": False},
        read_only=True,
        capability=Capability.BROWSER_READ,
    ),
    BROWSER_NETWORK: _ToolMeta(
        description="Return failed network requests (status >= 400) collected since open.",
        input_schema={**_OBJ, "additionalProperties": False},
        read_only=True,
        capability=Capability.BROWSER_READ,
    ),
    BROWSER_CLOSE: _ToolMeta(
        description="Close the browser session and release the Chromium process.",
        input_schema={**_OBJ, "additionalProperties": False},
        read_only=False,
        capability=Capability.BROWSER_INPUT,
    ),
    DEV_SERVER_START: _ToolMeta(
        description=(
            "Start a managed dev server bound to this KaroX session from the "
            "approved server-profile allowlist.  argv must match an approved "
            "profile exactly (e.g. npm run start:safe).  Returns process_id, pid, "
            "url, port and the latest safe logs.  Idempotent for a live process_id."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "argv": {"type": "array", "items": {"type": "string"}},
                "process_id": {"type": "string"},
                "env": {"type": "object"},
                "ready_url": {"type": "string"},
                "timeout_seconds": {"type": "number"},
            },
            "required": ["argv"],
            "additionalProperties": False,
        },
        read_only=False,
        capability=Capability.PROCESS_RUN,
    ),
    DEV_SERVER_STATUS: _ToolMeta(
        description="Return the running state and tail of safe logs for a managed dev server.",
        input_schema={
            "type": "object",
            "properties": {"process_id": {"type": "string"}},
            "required": ["process_id"],
            "additionalProperties": False,
        },
        read_only=True,
        capability=Capability.PROCESS_RUN,
    ),
    DEV_SERVER_LOGS: _ToolMeta(
        description="Return redacted stdout/stderr logs for a managed dev server.",
        input_schema={
            "type": "object",
            "properties": {"process_id": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["process_id"],
            "additionalProperties": False,
        },
        read_only=True,
        capability=Capability.PROCESS_RUN,
    ),
    DEV_SERVER_STOP: _ToolMeta(
        description="Stop a managed dev server started by this KaroX session only.",
        input_schema={
            "type": "object",
            "properties": {"process_id": {"type": "string"}},
            "required": ["process_id"],
            "additionalProperties": False,
        },
        read_only=False,
        capability=Capability.PROCESS_RUN,
    ),
    ARTIFACT_GET: _ToolMeta(
        description="Return metadata for an artifact created by this KaroX session.",
        input_schema={
            "type": "object",
            "properties": {"artifact_id": {"type": "string"}},
            "required": ["artifact_id"],
            "additionalProperties": False,
        },
        read_only=True,
        capability=None,
    ),
    ARTIFACT_READ_IMAGE: _ToolMeta(
        description=(
            "Return a session-owned PNG artifact as MCP image content (image/png). "
            "Only artifacts created by this session are reachable."
        ),
        input_schema={
            "type": "object",
            "properties": {"artifact_id": {"type": "string"}},
            "required": ["artifact_id"],
            "additionalProperties": False,
        },
        read_only=True,
        capability=None,
    ),
}


# HOSTED_EXTRA_TOOL_NAMES is imported from karox.hosted_bridge (the single
# source for the hosted tool name universe) and is kept consistent with the
# keys of ``_HOSTED_EXTRA_TOOLS`` below by :func:`_assert_catalogue_consistent`.


def _assert_catalogue_consistent() -> None:
    if set(_HOSTED_EXTRA_TOOLS) != set(HOSTED_EXTRA_TOOL_NAMES):
        raise RuntimeError(
            "hosted tools catalogue is out of sync with HOSTED_EXTRA_TOOL_NAMES"
        )


_assert_catalogue_consistent()


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


def _image_call_result(payload: dict[str, Any], png_bytes: bytes) -> CallToolResult:
    """Wrap a tool payload alongside its PNG as MCP image content."""
    encoded = base64.b64encode(png_bytes).decode("ascii")
    return CallToolResult(
        content=[
            ImageContent(type="image", data=encoded, mimeType="image/png"),
            TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, sort_keys=True)),
        ],
        structuredContent=payload,
        isError=False,
    )


def _text_call_result(payload: dict[str, Any], *, is_error: bool = False) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, sort_keys=True))],
        structuredContent=payload,
        isError=is_error,
    )


class HostedToolsRuntime:
    """Expose browser, dev-server and artifact tools to one hosted session."""

    def __init__(
        self,
        repository: Path,
        sessions: SessionStore,
        session_id: str,
        allowed_tool_names: Sequence[str],
        *,
        access_profile: AccessProfile,
        hosted_origin: Origin,
        server_profiles: Sequence[ManagedServerProfile] = (),
        audit_path: Optional[Path] = None,
        artifact_store: Optional[ArtifactStore] = None,
        popen_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        if not allowed_tool_names:
            raise HostedBridgeAccessDenied("hosted tools allowlist must not be empty")
        unknown = set(allowed_tool_names).difference(_HOSTED_EXTRA_TOOLS)
        if unknown:
            raise HostedBridgeAccessDenied(
                f"unknown hosted bridge tools: {sorted(unknown)}"
            )
        if hosted_origin.kind.value != "hosted_client":
            raise HostedBridgeAccessDenied("hosted tools origin must be a hosted client")

        self.repository = repository.expanduser().resolve(strict=True)
        self.sessions = sessions
        self.session_id = session_id
        self.hosted_origin = hosted_origin
        self.audit_path = audit_path
        self._allowed = tuple(dict.fromkeys(allowed_tool_names))
        self._server_profiles = tuple(server_profiles)
        self._popen_factory = popen_factory

        record = sessions.load(session_id)
        sessions.validate_repository(record, self.repository)
        if record.revoked:
            raise HostedBridgeAccessDenied("session access has been revoked")

        self.policy = CapabilityPolicy(access_profile)
        grants: set[Capability] = set()
        for name in self._allowed:
            meta = _HOSTED_EXTRA_TOOLS[name]
            if meta.capability is not None:
                grants.add(meta.capability)
        self.policy.set_grants(hosted_origin, grants)
        for capability in grants:
            if not self.policy.decide(hosted_origin, capability).allowed:
                raise HostedBridgeAccessDenied(
                    f"session profile does not allow {capability.value}"
                )

        self._artifacts = artifact_store or ArtifactStore(session_id)
        self._browser = BrowserSessionManager(self._artifacts)
        self._process_store = ManagedProcessStore(session_id)

    # -- HostedToolRuntime protocol ----------------------------------------

    def descriptors(self) -> list[ProxyToolDescriptor]:
        self._assert_session_alive()
        return [
            ProxyToolDescriptor(
                name=name,
                description=meta.description,
                input_schema=meta.input_schema,
                read_only=meta.read_only,
            )
            for name, meta in ((n, _HOSTED_EXTRA_TOOLS[n]) for n in self._allowed)
        ]

    def session_info(self) -> dict[str, Any]:
        return {
            "browser_open": self._browser.is_open,
            "server_profiles": [p.to_public_dict() for p in self._server_profiles],
            "artifacts": [a.to_dict() for a in self._artifacts.list()],
        }

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: Optional[str] = None,
        deadline_seconds: float = DEFAULT_HOSTED_DEADLINE_SECONDS,
    ) -> dict[str, Any] | CallToolResult:
        self._assert_session_alive()
        if tool_name not in self._allowed:
            raise HostedBridgeAccessDenied(f"hosted tool is not exposed: {tool_name}")
        meta = _HOSTED_EXTRA_TOOLS[tool_name]
        if meta.capability is not None:
            try:
                self.policy.require(self.hosted_origin, meta.capability)
            except PolicyDenied as exc:
                raise HostedBridgeAccessDenied(str(exc)) from exc
        handler = self._dispatch.get(tool_name)
        if handler is None:
            raise HostedBridgeAccessDenied(f"hosted tool has no handler: {tool_name}")
        try:
            result = handler(self, arguments, deadline_seconds)
        except BrowserSecurityError as exc:
            return _text_call_result(
                {"ok": False, "error_code": "security", "error": str(exc)},
                is_error=True,
            )
        except BrowserError as exc:
            return _text_call_result(
                {"ok": False, "error_code": "browser", "error": str(exc)},
                is_error=True,
            )
        except HostedBridgeAccessDenied:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            return _text_call_result(
                {"ok": False, "error_code": "internal", "error": str(redact(str(exc)))},
                is_error=True,
            )
        # Screenshot/read_image handlers return a CallToolResult already;
        # everything else returns a dict, which is wrapped so the MCP wire gets
        # a consistent content/structuredContent shape and a correct isError.
        if isinstance(result, CallToolResult):
            return result
        return _text_call_result(result, is_error=not bool(result.get("ok", True)))

    # -- session bookkeeping ------------------------------------------------

    def _assert_session_alive(self) -> None:
        record = self.sessions.load(self.session_id)
        self.sessions.validate_repository(record, self.repository)
        if record.revoked:
            raise HostedBridgeAccessDenied("session access has been revoked")

    def cleanup_session(self) -> dict[str, Any]:
        """Tear down browser + stop every dev server this session started."""
        browser = self._browser.close()
        stopped: list[str] = []
        for record in self._process_store.list():
            if _pid_alive(record.pid):
                _kill_pid_tree(record.pid)
            if not _pid_alive(record.pid):
                stopped.append(record.process_id)
        return {"browser_closed": browser, "stopped_servers": stopped}

    # -- browser handlers ---------------------------------------------------

    def _browser_open(self, arguments: dict[str, Any], deadline_seconds: float) -> dict[str, Any]:
        return {"ok": True, **self._browser.open(arguments, deadline_seconds)}

    def _browser_snapshot(self, arguments: dict[str, Any], deadline_seconds: float) -> dict[str, Any]:
        return {"ok": True, **self._browser.snapshot(arguments, deadline_seconds)}

    def _browser_click(self, arguments: dict[str, Any], deadline_seconds: float) -> dict[str, Any]:
        return {"ok": True, **self._browser.click(arguments, deadline_seconds)}

    def _browser_fill(self, arguments: dict[str, Any], deadline_seconds: float) -> dict[str, Any]:
        return {"ok": True, **self._browser.fill(arguments, deadline_seconds)}

    def _browser_select(self, arguments: dict[str, Any], deadline_seconds: float) -> dict[str, Any]:
        return {"ok": True, **self._browser.select(arguments, deadline_seconds)}

    def _browser_press(self, arguments: dict[str, Any], deadline_seconds: float) -> dict[str, Any]:
        return {"ok": True, **self._browser.press(arguments, deadline_seconds)}

    def _browser_wait(self, arguments: dict[str, Any], deadline_seconds: float) -> dict[str, Any]:
        return {"ok": True, **self._browser.wait_for(arguments, deadline_seconds)}

    def _browser_get_text(self, arguments: dict[str, Any], deadline_seconds: float) -> dict[str, Any]:
        return {"ok": True, **self._browser.get_text(arguments, deadline_seconds)}

    def _browser_screenshot(self, arguments: dict[str, Any], deadline_seconds: float) -> CallToolResult:
        payload = {"ok": True, **self._browser.screenshot(arguments, deadline_seconds)}
        # Re-read the artifact bytes so the image content is exactly what was
        # stored; this also proves the artifact is reachable for read_image.
        png_bytes, _ = self._artifacts.read_image(payload["artifact_id"])
        return _image_call_result(payload, png_bytes)

    def _browser_console(self, arguments: dict[str, Any], deadline_seconds: float) -> dict[str, Any]:
        return {"ok": True, **self._browser.console(arguments, deadline_seconds)}

    def _browser_network(self, arguments: dict[str, Any], deadline_seconds: float) -> dict[str, Any]:
        return {"ok": True, **self._browser.network_failures(arguments, deadline_seconds)}

    def _browser_close(self, arguments: dict[str, Any], deadline_seconds: float) -> dict[str, Any]:
        return {"ok": True, **self._browser.close()}

    # -- dev server handlers ------------------------------------------------

    def _resolve_profile(self, argv: Sequence[str]) -> ManagedServerProfile:
        if not isinstance(argv, list) or not argv or len(argv) > 100:
            raise HostedBridgeAccessDenied("dev_server argv must be a 1-100 string array")
        if not all(isinstance(item, str) and item for item in argv):
            raise HostedBridgeAccessDenied("dev_server argv must contain non-empty strings")
        for profile in self._server_profiles:
            if profile.matches(argv):
                return profile
        raise HostedBridgeAccessDenied(
            "dev server argv is not in the user-approved server-profile allowlist"
        )

    def _dev_server_start(self, arguments: dict[str, Any], deadline_seconds: float) -> dict[str, Any]:
        argv = self._required(arguments, "argv", list)
        try:
            profile = self._resolve_profile(argv)
        except HostedBridgeAccessDenied as exc:
            return {"ok": False, "error_code": "denied", "error": str(exc)}
        process_id = arguments.get("process_id") or f"srv-{int(time.time()*1000)}"
        if not isinstance(process_id, str) or len(process_id) > 80:
            return {"ok": False, "error_code": "invalid_request", "error": "process_id must be a 1-80 character string"}
        if not all(ch.isalnum() or ch in "._-" for ch in process_id):
            return {"ok": False, "error_code": "invalid_request", "error": "process_id must use [A-Za-z0-9._-] only"}

        # Idempotency: a live process_id is returned as-is, never respawned.
        try:
            existing = self._process_store.get(process_id)
        except Exception:
            existing = None
        if existing is not None and _pid_alive(existing.pid):
            return {
                "ok": True,
                "process_id": existing.process_id,
                "pid": existing.pid,
                "running": True,
                "started_at": existing.started_at,
                "reused": True,
            }

        caller_env = arguments.get("env") or {}
        if not isinstance(caller_env, dict):
            return {"ok": False, "error_code": "invalid_request", "error": "env must be an object"}
        env = child_process_environment()
        # Profile env is forced; caller may only add allowlisted names on top.
        env.update(profile.env)
        for key, value in caller_env.items():
            if key.upper() not in profile.env_allowlist:
                return {"ok": False, "error_code": "denied", "error": f"env override is not in the profile allowlist: {key}"}
            if not isinstance(value, str) or len(value) > 1000:
                return {"ok": False, "error_code": "invalid_request", "error": f"env value for {key} is invalid"}
            env[key] = value

        stdout_path = self._process_store.root / f"{process_id}.stdout.log"
        stderr_path = self._process_store.root / f"{process_id}.stderr.log"
        stdout_handle = stdout_path.open("ab", buffering=0)
        stderr_handle = stderr_path.open("ab", buffering=0)
        try:
            # Resolve ``npm`` to ``npm.cmd`` (etc.) AFTER the profile allowlist
            # already matched on the logical argv.  Keeps shell=False, the
            # repo-scoped cwd, env, and the no-shell-string/no-injection rules.
            launch_argv = _resolve_process_argv(argv)
            if self._popen_factory is not None:
                process = self._popen_factory(
                    launch_argv,
                    cwd=str(self.repository),
                    env=env,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    shell=False,
                )
            else:
                import subprocess

                process = subprocess.Popen(
                    launch_argv,
                    cwd=str(self.repository),
                    env=env,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    shell=False,
                )
        except Exception:
            stdout_handle.close()
            stderr_handle.close()
            raise
        stdout_handle.close()
        stderr_handle.close()
        record = ManagedProcessRecord(
            process_id=process_id,
            pid=process.pid,
            session_id=self.session_id,
            argv=tuple(argv),
            started_at=time.time(),
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
        )
        self._process_store.put(record)

        ready_url = arguments.get("ready_url") or profile.ready_url
        url: Optional[str] = None
        ready = False
        ready_error: Optional[str] = None
        if ready_url is not None:
            ready_url = _validate_local_url(ready_url)
            url = ready_url
            ready, ready_error = self._poll_ready(ready_url, deadline_seconds)
        return {
            "ok": True,
            "process_id": process_id,
            "pid": process.pid,
            "running": _pid_alive(process.pid),
            "started_at": record.started_at,
            "url": url,
            "ready": ready,
            # Present only when readiness was checked and failed, so a caller can
            # distinguish "never polled" from "polled and here is what happened".
            "ready_error": str(redact(ready_error)) if ready_error else None,
            "host_hint": profile.host_hint,
            # Env values are deliberately never returned; only the names that were set.
            "env_keys": sorted(profile.env.keys()),
        }

    def _poll_ready(self, url: str, deadline_seconds: float) -> tuple[bool, Optional[str]]:
        """Poll a local readiness URL, returning why it never answered.

        The reason used to be collected into a local and then discarded, so a
        caller received ``ready: false`` with nothing to act on -- a dev server
        that crashed on startup, one that is merely slow, and a wrong port were
        all reported identically. The last transport error separates them.
        """
        import httpx

        timeout_ms = max(1000, min(int(deadline_seconds * 1000), 30_000))
        deadline = time.monotonic() + timeout_ms / 1000.0
        last_error: Optional[str] = None
        while time.monotonic() < deadline:
            try:
                with httpx.Client(timeout=2.0, follow_redirects=False, trust_env=False) as client:
                    response = client.get(url)
                if response.status_code < 500:
                    return True, None
                last_error = f"HTTP {response.status_code}"
            except httpx.HTTPError as exc:
                # The class name carries the distinction that matters here
                # (ConnectError vs ReadTimeout vs ConnectTimeout) and the message
                # carries the address; both go through redact before display.
                last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(0.25)
        if last_error is None:
            last_error = f"no response within {timeout_ms / 1000:g}s"
        return False, last_error

    def _dev_server_status(self, arguments: dict[str, Any], deadline_seconds: float) -> dict[str, Any]:
        process_id = self._required(arguments, "process_id", str)
        try:
            record = self._require_own(process_id)
        except HostedBridgeAccessDenied as exc:
            return {"ok": False, "error_code": "denied", "error": str(exc)}
        return {
            "ok": True,
            "process_id": record.process_id,
            "pid": record.pid,
            "running": _pid_alive(record.pid),
            "started_at": record.started_at,
            "argv": list(redact(record.argv)),
            "stdout": _read_log(Path(record.stdout_path)),
            "stderr": _read_log(Path(record.stderr_path)),
        }

    def _dev_server_logs(self, arguments: dict[str, Any], deadline_seconds: float) -> dict[str, Any]:
        process_id = self._required(arguments, "process_id", str)
        limit = arguments.get("limit", 100_000)
        if not isinstance(limit, int) or limit <= 0 or limit > 1_000_000:
            limit = 100_000
        try:
            record = self._require_own(process_id)
        except HostedBridgeAccessDenied as exc:
            return {"ok": False, "error_code": "denied", "error": str(exc)}
        return {
            "ok": True,
            "process_id": record.process_id,
            "stdout": _read_log(Path(record.stdout_path), limit=limit),
            "stderr": _read_log(Path(record.stderr_path), limit=limit),
        }

    def _dev_server_stop(self, arguments: dict[str, Any], deadline_seconds: float) -> dict[str, Any]:
        process_id = self._required(arguments, "process_id", str)
        try:
            record = self._require_own(process_id)
        except HostedBridgeAccessDenied as exc:
            return {"ok": False, "error_code": "denied", "error": str(exc)}
        was_running = _pid_alive(record.pid)
        if was_running:
            _kill_pid_tree(record.pid)
        return {
            "ok": True,
            "process_id": record.process_id,
            "pid": record.pid,
            "was_running": was_running,
            "running": _pid_alive(record.pid),
        }

    def _require_own(self, process_id: str) -> ManagedProcessRecord:
        try:
            record = self._process_store.get(process_id)
        except Exception as exc:
            raise HostedBridgeAccessDenied("managed dev server does not exist") from exc
        # The store is session-scoped, but defend against a record that was moved
        # or hand-edited to point at another session.
        if record.session_id != self.session_id:
            raise HostedBridgeAccessDenied("dev server belongs to a different KaroX session")
        return record

    # -- artifact handlers ---------------------------------------------------

    def _artifact_get(self, arguments: dict[str, Any], deadline_seconds: float) -> dict[str, Any]:
        artifact_id = self._required(arguments, "artifact_id", str)
        try:
            _data, record = self._artifacts.read(artifact_id)
        except FileNotFoundError as exc:
            return {"ok": False, "error_code": "not_found", "error": str(exc)}
        return {"ok": True, **record.to_dict(), "relative_path": f".karox/artifacts/{self.session_id}/{record.artifact_id}.bin"}

    def _artifact_read_image(self, arguments: dict[str, Any], deadline_seconds: float) -> CallToolResult:
        artifact_id = self._required(arguments, "artifact_id", str)
        try:
            png_bytes, mime = self._artifacts.read_image(artifact_id)
            _data, record = self._artifacts.read(artifact_id)
        except FileNotFoundError as exc:
            return _text_call_result({"ok": False, "error_code": "not_found", "error": str(exc)}, is_error=True)
        except ValueError as exc:
            return _text_call_result({"ok": False, "error_code": "not_image", "error": str(exc)}, is_error=True)
        payload = {"ok": True, **record.to_dict(), "mime": mime}
        return _image_call_result(payload, png_bytes)

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _required(arguments: dict[str, Any], name: str, kind: type) -> Any:
        value = arguments.get(name)
        if not isinstance(value, kind):
            raise HostedBridgeAccessDenied(f"{name} must be {kind.__name__}")
        return value

    # Dispatch table built after the handlers are defined.
    _dispatch: Mapping[str, Callable[..., Any]] = {}


# Bind the dispatch table once the class body is complete.  Keeping it as a
# class attribute lets tests introspect the handler map without an instance.
HostedToolsRuntime._dispatch = {
    BROWSER_OPEN: HostedToolsRuntime._browser_open,
    BROWSER_SNAPSHOT: HostedToolsRuntime._browser_snapshot,
    BROWSER_CLICK: HostedToolsRuntime._browser_click,
    BROWSER_FILL: HostedToolsRuntime._browser_fill,
    BROWSER_SELECT: HostedToolsRuntime._browser_select,
    BROWSER_PRESS: HostedToolsRuntime._browser_press,
    BROWSER_WAIT: HostedToolsRuntime._browser_wait,
    BROWSER_GET_TEXT: HostedToolsRuntime._browser_get_text,
    BROWSER_SCREENSHOT: HostedToolsRuntime._browser_screenshot,
    BROWSER_CONSOLE: HostedToolsRuntime._browser_console,
    BROWSER_NETWORK: HostedToolsRuntime._browser_network,
    BROWSER_CLOSE: HostedToolsRuntime._browser_close,
    DEV_SERVER_START: HostedToolsRuntime._dev_server_start,
    DEV_SERVER_STATUS: HostedToolsRuntime._dev_server_status,
    DEV_SERVER_LOGS: HostedToolsRuntime._dev_server_logs,
    DEV_SERVER_STOP: HostedToolsRuntime._dev_server_stop,
    ARTIFACT_GET: HostedToolsRuntime._artifact_get,
    ARTIFACT_READ_IMAGE: HostedToolsRuntime._artifact_read_image,
}
