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
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from mcp.types import CallToolResult, ImageContent, TextContent

from .artifacts import ArtifactStore
from .check_jobs import CheckJobError, CheckJobManager
from .browser_access import BrowserAccessPolicy, SecureBrowserSessionManager
from .extension_browser import ChromeExtensionBrowserSessionManager
from .browser_session import (
    BrowserError,
    BrowserSecurityError,
    _validate_local_url,
)
from .hot_worker import hot_worker_supervisor
from .hosted_bridge import (
    DEFAULT_HOSTED_DEADLINE_SECONDS,
    HOSTED_EXTRA_TOOL_NAMES,
    HostedBridgeAccessDenied,
)
from .models import AccessProfile, Capability, Origin
from .policy import CapabilityPolicy, PolicyDenied
from .process_launcher import resolve_process_argv as _resolve_process_argv
from .project_registry import ProjectRegistry, ProjectRegistryError
from .proxy import ProxyToolDescriptor
from .remote_tools import (
    ManagedProcessRecord,
    ManagedProcessStore,
    _kill_pid_tree,
    _pid_alive,
    _read_log,
)
from .security import child_process_environment, redact
from .service_supervisor import (
    PROTECTED_EXECUTABLES,
    ProcessIdentity,
    ServiceLease,
    ServiceLeaseError,
    verify_identity,
)
from .sessions import SessionStore
from .task_state import TaskStateStore


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


SERVER_MANIFEST_RELATIVE_PATH = Path(".karox") / "servers.json"
# The dot-directory form is the tidy one, but KaroX's own path guard refuses to
# write inside a hidden directory, so an agent cannot create it. The root-level
# name is therefore also accepted: a project must be able to declare how it is
# started without a human having to hand-create a file first.
SERVER_MANIFEST_FALLBACK_PATH = Path("karox.servers.json")

_MANIFEST_FORCED_ENV: dict[str, str] = {"HOST": "127.0.0.1"}
_MANIFEST_MAX_PROFILES = 20
_COMPOSITE_SCRIPT_MARKERS: tuple[str, ...] = (
    "&&",
    "||",
    ";",
    "|",
    "concurrently",
    "npm-run-all",
    "run-p ",
    "run-s ",
)
# Runners KaroX can start as a plain long-lived listener.  Anything else
# (docker, ssh, deploy wrappers, arbitrary shell) stays out of auto-discovery
# and must be declared in the in-repository manifest instead.
_SAFE_SCRIPT_RUNNERS: tuple[str, ...] = (
    "node ",
    "node--",
    "nodemon",
    "vite",
    "next dev",
    "nuxt dev",
    "astro dev",
    "http-server",
    "serve ",
    "python -m http.server",
    "uvicorn",
    "fastapi dev",
    "flask run",
)


def _is_composite_script(command: str) -> bool:
    normalized = f" {command.strip().lower()} "
    return any(marker in normalized for marker in _COMPOSITE_SCRIPT_MARKERS)


def _is_safe_script_runner(command: str) -> bool:
    normalized = command.strip().lower()
    if not normalized:
        return False
    return any(marker in normalized for marker in _SAFE_SCRIPT_RUNNERS)


def server_profiles_from_manifest(repository: Path) -> tuple[ManagedServerProfile, ...]:
    """Read the in-repository dev-server manifest, if the project ships one.

    Auto-discovery can only guess from ``package.json`` script names, so a
    project whose real launch recipe is anything else (a bare ``npm start`` that
    the project itself declares safe, a Python entry point, an extra argument)
    had no way to be startable by KaroX at all.  ``.karox/servers.json`` is that
    missing piece: the recipe lives in the repository, next to the code, under
    review, and the user approves it by committing it.

    The manifest never widens the security envelope beyond a loopback listener:
    ``HOST`` is force-set to ``127.0.0.1`` after the manifest's own ``env``, the
    ``host_hint`` must stay loopback (``ManagedServerProfile`` enforces that),
    and a malformed manifest yields no profiles rather than a partial allowlist.
    """

    payload = None
    for candidate in (SERVER_MANIFEST_RELATIVE_PATH, SERVER_MANIFEST_FALLBACK_PATH):
        try:
            payload = json.loads((repository / candidate).read_text(encoding="utf-8"))
            break
        except (OSError, ValueError):
            continue
    if payload is None:
        return ()
    entries = payload.get("servers") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        return ()
    profiles: list[ManagedServerProfile] = []
    for entry in entries[:_MANIFEST_MAX_PROFILES]:
        if not isinstance(entry, dict):
            continue
        argv = entry.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(item, str) and item for item in argv)
        ):
            continue
        raw_env = entry.get("env")
        env: dict[str, str] = {}
        if isinstance(raw_env, dict):
            for key, value in raw_env.items():
                if isinstance(key, str) and isinstance(value, str) and len(value) <= 1000:
                    env[key] = value
        env.update(_MANIFEST_FORCED_ENV)
        raw_allowlist = entry.get("env_allowlist")
        allowlist = frozenset(
            item.upper()
            for item in (raw_allowlist if isinstance(raw_allowlist, list) else [])
            if isinstance(item, str) and item
        )
        # A caller must never be able to override an env var the manifest
        # forces; that is the whole point of declaring it forced.
        allowlist = frozenset(allowlist.difference({key.upper() for key in env}))
        ready_url = entry.get("ready_url")
        if ready_url is not None and not isinstance(ready_url, str):
            ready_url = None
        try:
            profiles.append(
                ManagedServerProfile(
                    name=str(entry.get("name") or "manifest-server"),
                    argv=tuple(argv),
                    env=env,
                    env_allowlist=allowlist,
                    host_hint=str(entry.get("host_hint", "127.0.0.1")),
                    ready_url=ready_url,
                )
            )
        except (TypeError, ValueError):
            continue
    return tuple(profiles)


def server_profiles_for_repository(repository: Path) -> tuple[ManagedServerProfile, ...]:
    """Return loopback-only dev-server profiles that actually belong to *repository*.

    ``default_server_profiles`` predates multi-repository hosted clients and is
    intentionally kept for the Vacancy Control compatibility path.  Reusing it
    for every repository exposed a bogus ``npm run start:safe`` recipe to
    unrelated projects.  Hosted connectors now discover only scripts present in
    the selected repository, plus whatever the project declares for itself in
    ``.karox/servers.json``.
    """

    manifest_profiles = server_profiles_from_manifest(repository)
    package_json = repository / "package.json"
    try:
        payload = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _deduplicate_server_profiles(manifest_profiles)
    scripts_raw = payload.get("scripts") if isinstance(payload, dict) else None
    if not isinstance(scripts_raw, dict):
        return _deduplicate_server_profiles(manifest_profiles)
    scripts = {
        str(name): str(command)
        for name, command in scripts_raw.items()
        if isinstance(name, str) and isinstance(command, str)
    }

    profiles: list[ManagedServerProfile] = list(manifest_profiles)
    if "start:safe" in scripts:
        profiles.extend(default_server_profiles())

    # Prefer a client-only Vite script over a composite `dev` script.  Passing
    # extra argv through npm lets the final --host override any package default,
    # so the managed process remains loopback-only.  Composite runners (concurrently,
    # npm-run-all, shell pipelines) are deliberately not auto-approved.
    for script_name in ("client:dev", "dev"):
        command = scripts.get(script_name, "").strip()
        normalized = command.lower()
        if "vite" not in normalized or _is_composite_script(command):
            continue
        profiles.append(
            ManagedServerProfile(
                name=f"vite-{script_name.replace(':', '-')}-loopback",
                argv=("npm", "run", script_name, "--", "--host", "127.0.0.1"),
                env={"HOST": "127.0.0.1"},
                env_allowlist=frozenset({"PORT", "NODE_ENV", "CI"}),
                host_hint="127.0.0.1",
            )
        )
        break

    # A project that ships an explicit ``start:safe`` script has already told us
    # which recipe is the safe one, so its plain ``start``/``dev`` scripts stay
    # out of auto-discovery: on Vacancy Control the bare script is exactly the
    # one that turns live Facebook publication on.  Projects without that
    # distinction would otherwise have no startable server at all, which is why
    # a plain single-runner script is approved for them.
    if "start:safe" not in scripts:
        for script_name in ("start", "dev", "serve"):
            command = scripts.get(script_name, "").strip()
            if not command or _is_composite_script(command):
                continue
            if not _is_safe_script_runner(command):
                continue
            argv = ("npm", "start") if script_name == "start" else ("npm", "run", script_name)
            profiles.append(
                ManagedServerProfile(
                    name=f"npm-{script_name}-loopback",
                    env={"HOST": "127.0.0.1"},
                    argv=argv,
                    env_allowlist=frozenset({"PORT", "NODE_ENV", "CI", "DB_FILE"}),
                    host_hint="127.0.0.1",
                )
            )
            break
    return _deduplicate_server_profiles(tuple(profiles))


def _deduplicate_server_profiles(
    profiles: Sequence[ManagedServerProfile],
) -> tuple[ManagedServerProfile, ...]:
    """Keep the first recipe for each argv and each name.

    ``ManagedServerProfile`` holds a mapping, so it is not hashable and cannot
    go through ``dict.fromkeys``.  Duplicate argv would also make
    ``_resolve_profile`` order-dependent: the manifest is listed first so an
    in-repository declaration wins over the discovered default for the same
    command line.
    """

    seen_argv: set[tuple[str, ...]] = set()
    seen_names: set[str] = set()
    unique: list[ManagedServerProfile] = []
    for profile in profiles:
        if profile.argv in seen_argv or profile.name in seen_names:
            continue
        seen_argv.add(profile.argv)
        seen_names.add(profile.name)
        unique.append(profile)
    return tuple(unique)


# ---------------------------------------------------------------------------
# Tool catalogue
# ---------------------------------------------------------------------------

BROWSER_COMMAND = "karox.browser.command"
BROWSER_OPEN = "karox.browser.open"
BROWSER_TABS = "karox.browser.tabs"
BROWSER_NEW_TAB = "karox.browser.new_tab"
BROWSER_SWITCH_TAB = "karox.browser.switch_tab"
BROWSER_CLOSE_TAB = "karox.browser.close_tab"
BROWSER_SNAPSHOT = "karox.browser.snapshot"
BROWSER_CLICK = "karox.browser.click"
BROWSER_FILL = "karox.browser.fill"
BROWSER_FILL_CREDENTIAL = "karox.browser.fill_credential"
BROWSER_SELECT = "karox.browser.select"
BROWSER_PRESS = "karox.browser.press"
BROWSER_WAIT = "karox.browser.wait_for"
BROWSER_GET_TEXT = "karox.browser.get_text"
BROWSER_SCREENSHOT = "karox.browser.screenshot"
BROWSER_CONSOLE = "karox.browser.console"
BROWSER_NETWORK = "karox.browser.network_failures"
BROWSER_NETWORK_REQUESTS = "karox.browser.network_requests"
BROWSER_REQUEST_TAKEOVER = "karox.browser.request_user_takeover"
BROWSER_RESUME_TAKEOVER = "karox.browser.resume_after_user_takeover"
BROWSER_CLOSE = "karox.browser.close"
BROWSER_COMMAND_READ_ACTIONS = frozenset(
    {
        "tabs",
        "snapshot",
        "wait_for",
        "get_text",
        "screenshot",
        "console",
        "network_failures",
        "network_requests",
    }
)
DEV_SERVER_START = "karox.dev_server.start"
DEV_SERVER_STATUS = "karox.dev_server.status"
DEV_SERVER_LOGS = "karox.dev_server.logs"
DEV_SERVER_STOP = "karox.dev_server.stop"
DEV_SERVER_RESTART = "karox.dev_server.restart"
CHECKS_START = "karox.checks.start"
CHECKS_STATUS = "karox.checks.status"
CHECKS_LOGS = "karox.checks.logs"
CHECKS_CANCEL = "karox.checks.cancel"
RUNTIME_RESTART = "karox.runtime.restart"
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
    BROWSER_COMMAND: _ToolMeta(
        description=(
            "Stable hot-reload browser command. The schema remains fixed while "
            "new guarded actions can be added inside the worker without "
            "reconnecting the hosted client."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "payload": {"type": "object"},
            },
            "required": ["action", "payload"],
            "additionalProperties": False,
        },
        read_only=False,
        # Per-action authorization happens in _browser_command. The stable
        # descriptor itself is available to read-only sessions for tabs,
        # snapshot and other observation actions.
        capability=Capability.BROWSER_READ,
    ),
    BROWSER_OPEN: _ToolMeta(
        description=(
            "Open a URL in a browser context owned by this KaroX session. "
            "Legacy policies accept localhost only; an explicit external-browser "
            "policy also accepts public HTTPS after domain, DNS and private-address checks."
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
    BROWSER_TABS: _ToolMeta(
        description="List tabs owned by this KaroX browser session. Other sessions are never visible.",
        input_schema={**_OBJ, "additionalProperties": False},
        read_only=True,
        capability=Capability.BROWSER_READ,
    ),
    BROWSER_NEW_TAB: _ToolMeta(
        description="Open a new tab in this session-owned browser context, optionally navigating to an allowed URL.",
        input_schema={
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "additionalProperties": False,
        },
        read_only=False,
        capability=Capability.BROWSER_INPUT,
    ),
    BROWSER_SWITCH_TAB: _ToolMeta(
        description="Switch to a tab owned by this KaroX session.",
        input_schema={
            "type": "object",
            "properties": {"tab_id": {"type": "string"}},
            "required": ["tab_id"],
            "additionalProperties": False,
        },
        read_only=False,
        capability=Capability.BROWSER_INPUT,
    ),
    BROWSER_CLOSE_TAB: _ToolMeta(
        description="Close one tab owned by this KaroX session without touching other sessions or the last tab.",
        input_schema={
            "type": "object",
            "properties": {"tab_id": {"type": "string"}},
            "required": ["tab_id"],
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
        description="Fill a non-sensitive input/textarea by selector with the given value. Password/credential fields require local credential injection or user takeover.",
        input_schema={
            "type": "object",
            "properties": {"selector": {"type": "string"}, "value": {"type": "string"}},
            "required": ["selector", "value"],
            "additionalProperties": False,
        },
        read_only=False,
        capability=Capability.BROWSER_INPUT,
    ),
    BROWSER_FILL_CREDENTIAL: _ToolMeta(
        description=(
            "Fill a username/email or password field from an opaque browser credential "
            "reference stored only in the local OS keyring. The raw value is never "
            "accepted by or returned to the hosted client. Use only fake/test/non-important "
            "accounts in KaroX Browser; CAPTCHA, 2FA, OAuth consent and payment actions "
            "must use user takeover."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "selector": {"type": "string"},
                "reference": {"type": "string", "pattern": "^os-keyring:browser/"},
                "field": {"type": "string", "enum": ["username", "password"]},
            },
            "required": ["selector", "reference", "field"],
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
        description="Return failed network requests collected since open, with URLs and errors redacted.",
        input_schema={**_OBJ, "additionalProperties": False},
        read_only=True,
        capability=Capability.BROWSER_READ,
    ),
    BROWSER_NETWORK_REQUESTS: _ToolMeta(
        description=(
            "Return safe network metadata filtered by URL, method, resource type, and allowed JSON fields. "
            "Authorization, cookies, tokens, session IDs, card data, and high-entropy values are never returned."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url_contains": {"type": "string"},
                "method": {"type": "string"},
                "resource_type": {"type": "string"},
                "fields": {"type": "array", "items": {"type": "string"}},
            },
            "additionalProperties": False,
        },
        read_only=True,
        capability=Capability.BROWSER_READ,
    ),
    BROWSER_REQUEST_TAKEOVER: _ToolMeta(
        description=(
            "Pause agent browser input and hand the visible headed browser to the user for login, CAPTCHA, password, 2FA, consent, or payment review."
        ),
        input_schema={
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "additionalProperties": False,
        },
        read_only=False,
        capability=Capability.BROWSER_INPUT,
    ),
    BROWSER_RESUME_TAKEOVER: _ToolMeta(
        description="Resume agent browser input in the same tab and browser context after the user finishes takeover.",
        input_schema={**_OBJ, "additionalProperties": False},
        read_only=False,
        capability=Capability.BROWSER_INPUT,
    ),
    BROWSER_CLOSE: _ToolMeta(
        description=(
            "Destructively close the browser context and Chromium process owned by this KaroX session. "
            "Call this only after the user explicitly asks to end the browser session; never call it as automatic cleanup, after an answer, or while user takeover is active."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "user_confirmed": {"type": "boolean"},
                "reason": {"type": "string"},
            },
            "required": ["user_confirmed"],
            "additionalProperties": False,
        },
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
                "workstream_id": {"type": "string"},
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
            "properties": {
                "process_id": {"type": "string"},
                "workstream_id": {"type": "string"},
            },
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
            "properties": {
                "process_id": {"type": "string"},
                "workstream_id": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["process_id"],
            "additionalProperties": False,
        },
        read_only=True,
        capability=Capability.PROCESS_RUN,
    ),
    DEV_SERVER_STOP: _ToolMeta(
        description=(
            "Stop a managed project service started by this KaroX session only. "
            "The live PID must still match its recorded creation time, executable, "
            "and command identity; unverifiable or protected processes are refused."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "process_id": {"type": "string"},
                "workstream_id": {"type": "string"},
            },
            "required": ["process_id"],
            "additionalProperties": False,
        },
        read_only=False,
        capability=Capability.PROCESS_RUN,
    ),
    DEV_SERVER_RESTART: _ToolMeta(
        description=(
            "Atomically restart one managed project service: verify identity, acquire "
            "its mutation lease, stop and confirm exit, relaunch the same approved "
            "argv, then run the optional localhost readiness probe."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "process_id": {"type": "string"},
                "workstream_id": {"type": "string"},
                "env": {"type": "object"},
                "ready_url": {"type": "string"},
                "timeout_seconds": {"type": "number"},
            },
            "required": ["process_id"],
            "additionalProperties": False,
        },
        read_only=False,
        capability=Capability.PROCESS_RUN,
    ),
    CHECKS_START: _ToolMeta(
        description=(
            "Start a durable verification job and return immediately. The worker and "
            "its child process tree are isolated from the MCP request and bridge lifecycle."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["pytest", "check"]},
                "suite": {"type": "string", "enum": ["full", "focused", "split"]},
                "targets": {"type": "array", "items": {"type": "string"}},
                "split": {"type": "integer"},
                "part": {"type": "integer"},
                "argv": {"type": "array", "items": {"type": "string"}},
                "timeout_seconds": {"type": "number"},
            },
            "additionalProperties": False,
        },
        read_only=False,
        capability=Capability.CHECKS_RUN,
    ),
    CHECKS_STATUS: _ToolMeta(
        description="Return durable status and compact diagnostics for a managed check job.",
        input_schema={
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
            "additionalProperties": False,
        },
        read_only=True,
        capability=Capability.CHECKS_RUN,
    ),
    CHECKS_LOGS: _ToolMeta(
        description="Return a bounded redacted tail of a managed check job log.",
        input_schema={
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["job_id"],
            "additionalProperties": False,
        },
        read_only=True,
        capability=Capability.CHECKS_RUN,
    ),
    CHECKS_CANCEL: _ToolMeta(
        description=(
            "Idempotently request cancellation of an owned managed check job. "
            "Only the verified worker-owned child tree is terminated."
        ),
        input_schema={
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
            "additionalProperties": False,
        },
        read_only=False,
        capability=Capability.CHECKS_RUN,
    ),
    RUNTIME_RESTART: _ToolMeta(
        description=(
            "Safely recycle only this durable saved bridge's local MCP child after "
            "the current response is fully sent. The owner, public URL, durable "
            "session and bridge credential are preserved; an active managed-extension "
            "browser is preserved too, while an active Playwright browser or user takeover blocks restart."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
                    "description": (
                        "Unique for one intentional restart; keep the same value only when retrying that restart."
                    ),
                },
                "reason": {"type": "string", "maxLength": 500},
            },
            "required": ["request_id"],
            "additionalProperties": False,
        },
        read_only=False,
        capability=Capability.PROCESS_RUN,
    ),
    ARTIFACT_GET: _ToolMeta(
        description=(
            "Return metadata for a session-owned artifact, or selectively read a "
            "bounded line range, tail, regex match, first failure, JSON path, or section."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string"},
                "selector": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": [
                                "line_range",
                                "tail",
                                "regex",
                                "first_failure",
                                "json_path",
                                "section",
                            ],
                        },
                        "start": {"type": "integer"},
                        "count": {"type": "integer"},
                        "pattern": {"type": "string"},
                        "max_matches": {"type": "integer"},
                        "path": {"type": "string"},
                        "name": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
                "max_output_bytes": {"type": "integer"},
            },
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


# A hosted client may reconnect or cause the wire runtime to be rebuilt between
# two assistant turns while the managed bridge process itself remains alive.
# Keep one browser manager per durable KaroX session so the new runtime sees the
# same Chromium context, tabs, cookies and takeover state instead of silently
# creating a second isolated context.
_BROWSER_MANAGERS_LOCK = threading.RLock()
_BROWSER_MANAGERS: dict[
    str,
    tuple[BrowserAccessPolicy, SecureBrowserSessionManager | ChromeExtensionBrowserSessionManager],
] = {}


def _browser_manager_for_session(
    artifacts: ArtifactStore,
    policy: BrowserAccessPolicy,
) -> SecureBrowserSessionManager | ChromeExtensionBrowserSessionManager:
    with _BROWSER_MANAGERS_LOCK:
        current = _BROWSER_MANAGERS.get(policy.session_id)
        if current is not None:
            current_policy, manager = current
            if current_policy != policy:
                if manager.is_open:
                    raise HostedBridgeAccessDenied(
                        "an open browser session cannot be rebound to a different policy"
                    )
                manager.close(force=True)
                _BROWSER_MANAGERS.pop(policy.session_id, None)
            else:
                if manager._artifacts.root == artifacts.root:
                    return manager
                if manager.is_open:
                    raise HostedBridgeAccessDenied(
                        "an open browser session cannot move to a different artifact store"
                    )
                manager.close(force=True)
                _BROWSER_MANAGERS.pop(policy.session_id, None)
        manager = (
            ChromeExtensionBrowserSessionManager(artifacts, policy)
            if policy.backend == "extension"
            else SecureBrowserSessionManager(artifacts, policy)
        )
        _BROWSER_MANAGERS[policy.session_id] = (policy, manager)
        return manager


def _release_browser_manager(
    session_id: str,
    manager: SecureBrowserSessionManager | ChromeExtensionBrowserSessionManager,
) -> dict[str, Any]:
    with _BROWSER_MANAGERS_LOCK:
        current = _BROWSER_MANAGERS.get(session_id)
        if current is not None and current[1] is manager:
            _BROWSER_MANAGERS.pop(session_id, None)
    if isinstance(manager, ChromeExtensionBrowserSessionManager):
        return manager.detach()
    return manager.close(force=True)


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
        browser_policy: Optional[BrowserAccessPolicy] = None,
        audit_path: Optional[Path] = None,
        artifact_store: Optional[ArtifactStore] = None,
        popen_factory: Optional[Callable[..., Any]] = None,
        verification_commands: Sequence[Sequence[str]] = (),
        saved_profile_name: Optional[str] = None,
        project_registry: Optional[ProjectRegistry] = None,
        project_registry_loader: Optional[Callable[[], ProjectRegistry]] = None,
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
        if saved_profile_name is not None:
            if (
                not isinstance(saved_profile_name, str)
                or not 1 <= len(saved_profile_name) <= 128
                or not saved_profile_name[0].isalnum()
                or not all(ch.isalnum() or ch in "._-" for ch in saved_profile_name)
            ):
                raise HostedBridgeAccessDenied("saved profile name is invalid")
        self._saved_profile_name = saved_profile_name

        record = sessions.load(session_id)
        sessions.validate_repository(record, self.repository)
        if record.revoked:
            raise HostedBridgeAccessDenied("session access has been revoked")
        # Stable repository identity used by managed-process scope records. The
        # canonical path alone is not enough after a repo is moved/recreated;
        # SessionStore already maintains a fingerprint for this exact purpose.
        self._repo_fingerprint = record.repo_fingerprint

        self._access_profile = access_profile
        self.policy = CapabilityPolicy(access_profile)
        grants: set[Capability] = set()
        for name in self._allowed:
            meta = _HOSTED_EXTRA_TOOLS[name]
            if meta.capability is not None:
                grants.add(meta.capability)
        if BROWSER_COMMAND in self._allowed and access_profile in {
            AccessProfile.BROWSER_CONTROL,
            AccessProfile.WORKSPACE_WRITE,
            AccessProfile.ELEVATED,
        }:
            # The command descriptor is read-capable, while mutating actions are
            # checked dynamically. Grant input only to profiles that already
            # carry it; READ_ONLY keeps a genuinely read-only command surface.
            grants.add(Capability.BROWSER_INPUT)
        self.policy.set_grants(hosted_origin, grants)
        for capability in grants:
            if not self.policy.decide(hosted_origin, capability).allowed:
                raise HostedBridgeAccessDenied(
                    f"session profile does not allow {capability.value}"
                )

        self._artifacts = artifact_store or ArtifactStore(session_id)
        self._browser_policy = browser_policy or BrowserAccessPolicy(session_id=session_id)
        if self._browser_policy.session_id != session_id:
            raise HostedBridgeAccessDenied("browser policy belongs to a different KaroX session")
        self._browser = _browser_manager_for_session(
            self._artifacts,
            self._browser_policy,
        )
        self._process_store = ManagedProcessStore(session_id)
        self._check_jobs = CheckJobManager(
            self.repository,
            session_id,
            verification_commands,
        )
        # Managed servers are per project, not per bridge.  The anchor
        # repository is only the session's identity; a hosted client that works
        # across several approved projects must be able to start each project's
        # own server in that project's own directory.
        try:
            self._project_registry = project_registry or ProjectRegistry.single(self.repository)
            anchor_entry = self._project_registry.entry_for_path(self.repository)
        except ProjectRegistryError as exc:
            raise HostedBridgeAccessDenied(f"invalid project registry: {exc}") from exc
        if anchor_entry is None:
            raise HostedBridgeAccessDenied(
                "managed-server session anchor must be an approved project"
            )
        self._anchor_project_id = anchor_entry.project_id
        self._project_registry_loader = project_registry_loader
        self._task_states = TaskStateStore(sessions)
        self._project_profile_cache: dict[str, tuple[ManagedServerProfile, ...]] = {}
        self._project_profile_lock = threading.RLock()

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
            "session_id": self.session_id,
            "repository": str(self.repository),
            "saved_profile": self._saved_profile_name,
            "access_profile": self._access_profile.value,
            "browser_open": self._browser.is_open,
            "browser_takeover_active": self._browser.takeover_active,
            "browser_context_id": self._browser.context_id,
            "browser_engine": getattr(self._browser, "engine", "playwright_chromium"),
            "browser_profile_persistent": isinstance(
                self._browser, ChromeExtensionBrowserSessionManager
            ),
            "browser_persistent_across_calls": True,
            "browser_survives_bridge_restart": isinstance(
                self._browser, ChromeExtensionBrowserSessionManager
            ),
            "browser_local_credential_injection": True,
            "browser_permission": self._browser_policy.to_diagnostics(),
            "browser_isolation": {
                "context_per_session": True,
                "cross_session_control": False,
                "session_id": self.session_id,
            },
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
            if tool_name in {CHECKS_START, RUNTIME_RESTART}:
                arguments = dict(arguments)
                arguments["_idempotency_key"] = idempotency_key
            if (
                tool_name.startswith("karox.browser.")
                and self._browser.is_open
                and not self._browser.takeover_active
                and tool_name
                not in {
                    BROWSER_COMMAND,
                    BROWSER_OPEN,
                    BROWSER_CLOSE,
                    BROWSER_REQUEST_TAKEOVER,
                    BROWSER_RESUME_TAKEOVER,
                }
            ):
                self._browser.recover_if_blank(deadline_seconds)
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
        except CheckJobError as exc:
            return _text_call_result(
                {"ok": False, "error_code": "check_job", "error": str(redact(str(exc)))},
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
        """Tear down browser and only still-proven owned dev servers.

        Cleanup is not a privileged bypass. A stale process record may point at
        a PID the OS already reused, so bridge teardown goes through the same
        fail-closed identity path as an explicit ``dev_server.stop``. Anything
        that cannot still be proven ours is left untouched and reported.
        """
        browser = _release_browser_manager(self.session_id, self._browser)
        stopped: list[str] = []
        refused: list[str] = []
        for record in self._process_store.list():
            scope = self._load_process_scope(record) or {}
            stop_arguments: dict[str, Any] = {"process_id": record.process_id}
            stored_workstream = scope.get("workstream_id")
            if isinstance(stored_workstream, str):
                stop_arguments["workstream_id"] = stored_workstream
            result = self._dev_server_stop(
                stop_arguments,
                DEFAULT_HOSTED_DEADLINE_SECONDS,
            )
            if bool(result.get("ok")) and not bool(result.get("running")):
                stopped.append(record.process_id)
            elif not bool(result.get("ok")):
                refused.append(record.process_id)
        return {
            "browser_closed": browser,
            "stopped_servers": stopped,
            "refused_servers": refused,
        }

    # -- browser handlers ---------------------------------------------------

    def _browser_command(
        self,
        arguments: dict[str, Any],
        deadline_seconds: float,
    ) -> dict[str, Any] | CallToolResult:
        action = arguments.get("action")
        required_capability = (
            Capability.BROWSER_READ
            if action in BROWSER_COMMAND_READ_ACTIONS
            else Capability.BROWSER_INPUT
        )
        try:
            self.policy.require(self.hosted_origin, required_capability)
        except PolicyDenied as exc:
            raise HostedBridgeAccessDenied(str(exc)) from exc
        payload = hot_worker_supervisor().execute_browser(
            self,
            arguments,
            deadline_seconds,
        )
        result = {"ok": True, **payload}
        if arguments.get("action") == "screenshot":
            artifact_id = result.get("artifact_id")
            if not isinstance(artifact_id, str):
                raise BrowserError("browser screenshot returned no artifact_id")
            png_bytes, _mime = self._artifacts.read_image(artifact_id)
            return _image_call_result(result, png_bytes)
        return result

    def _browser_open(self, arguments: dict[str, Any], deadline_seconds: float) -> dict[str, Any]:
        return {"ok": True, **self._browser.open(arguments, deadline_seconds)}

    def _browser_tabs(self, arguments: dict[str, Any], deadline_seconds: float) -> dict[str, Any]:
        return {"ok": True, **self._browser.tabs(arguments, deadline_seconds)}

    def _browser_new_tab(self, arguments: dict[str, Any], deadline_seconds: float) -> dict[str, Any]:
        return {"ok": True, **self._browser.new_tab(arguments, deadline_seconds)}

    def _browser_switch_tab(self, arguments: dict[str, Any], deadline_seconds: float) -> dict[str, Any]:
        return {"ok": True, **self._browser.switch_tab(arguments, deadline_seconds)}

    def _browser_close_tab(self, arguments: dict[str, Any], deadline_seconds: float) -> dict[str, Any]:
        return {"ok": True, **self._browser.close_tab(arguments, deadline_seconds)}

    def _browser_snapshot(self, arguments: dict[str, Any], deadline_seconds: float) -> dict[str, Any]:
        return {"ok": True, **self._browser.snapshot(arguments, deadline_seconds)}

    def _browser_click(self, arguments: dict[str, Any], deadline_seconds: float) -> dict[str, Any]:
        return {"ok": True, **self._browser.click(arguments, deadline_seconds)}

    def _browser_fill(self, arguments: dict[str, Any], deadline_seconds: float) -> dict[str, Any]:
        return {"ok": True, **self._browser.fill(arguments, deadline_seconds)}

    def _browser_fill_credential(
        self, arguments: dict[str, Any], deadline_seconds: float
    ) -> dict[str, Any]:
        return {"ok": True, **self._browser.fill_credential(arguments, deadline_seconds)}

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

    def _browser_network_requests(self, arguments: dict[str, Any], deadline_seconds: float) -> dict[str, Any]:
        return {"ok": True, **self._browser.network_requests(arguments, deadline_seconds)}

    def _browser_request_takeover(self, arguments: dict[str, Any], deadline_seconds: float) -> dict[str, Any]:
        return {"ok": True, **self._browser.request_user_takeover(arguments, deadline_seconds)}

    def _browser_resume_takeover(self, arguments: dict[str, Any], deadline_seconds: float) -> dict[str, Any]:
        return {"ok": True, **self._browser.resume_after_user_takeover(arguments, deadline_seconds)}

    def _browser_close(self, arguments: dict[str, Any], deadline_seconds: float) -> dict[str, Any]:
        if arguments.get("user_confirmed") is not True:
            raise BrowserSecurityError(
                "browser.close requires explicit user confirmation and is never automatic cleanup"
            )
        reason = arguments.get("reason", "")
        if reason is not None and (not isinstance(reason, str) or len(reason) > 500):
            raise BrowserError("browser close reason must be a string up to 500 characters")
        return {"ok": True, **self._browser.close()}

    # -- dev server handlers ------------------------------------------------

    def _current_project_registry(self) -> ProjectRegistry:
        loader = self._project_registry_loader
        if loader is None:
            return self._project_registry
        try:
            registry = loader()
            anchor = registry.entry_for_path(self.repository)
        except (ProjectRegistryError, OSError, RuntimeError, ValueError) as exc:
            raise HostedBridgeAccessDenied(
                f"cannot refresh saved project registry: {exc}"
            ) from exc
        if anchor is None:
            raise HostedBridgeAccessDenied(
                "saved project registry no longer contains the durable session anchor"
            )
        self._project_registry = registry
        self._anchor_project_id = anchor.project_id
        return registry

    def _project_for_workstream(self, workstream_id: str) -> tuple[str, Path]:
        """Resolve which approved project a managed-server call belongs to.

        The binding comes from the workstream's own durable task state, the same
        record every repository tool routes through, so a server always starts
        in the directory whose code the agent is editing.  A workstream that was
        never bootstrapped (or predates project routing) keeps the historical
        behaviour and resolves to the session anchor.
        """

        registry = self._current_project_registry()
        project_id = self._anchor_project_id
        state = self._task_states.load_optional(
            self.session_id,
            workstream_id=workstream_id if workstream_id != "default" else None,
        )
        if state is not None:
            stored = state.facts.get("project_id")
            if stored is not None:
                project_id = str(stored.value)
        try:
            entry = registry.get(project_id)
            return entry.project_id, Path(registry.resolve(entry.project_id))
        except ProjectRegistryError as exc:
            raise HostedBridgeAccessDenied(
                f"managed server project is not approved: {exc}"
            ) from exc

    def _profiles_for_project(self, project_id: str, path: Path) -> tuple[ManagedServerProfile, ...]:
        """Server recipes this one project approves, discovered from its own tree.

        Sharing one flat allowlist across projects would let a recipe discovered
        in project A launch inside project B, where nobody approved it.  The
        explicitly configured profiles stay bound to the session anchor, which
        is the project they were configured for.
        """

        with self._project_profile_lock:
            cached = self._project_profile_cache.get(project_id)
            if cached is not None:
                return cached
            discovered = list(server_profiles_for_repository(path))
            if project_id == self._anchor_project_id:
                discovered = [*self._server_profiles, *discovered]
            profiles = _deduplicate_server_profiles(discovered)
            self._project_profile_cache[project_id] = profiles
            return profiles

    def _resolve_profile(
        self, argv: Sequence[str], profiles: Sequence[ManagedServerProfile]
    ) -> ManagedServerProfile:
        if not isinstance(argv, list) or not argv or len(argv) > 100:
            raise HostedBridgeAccessDenied("dev_server argv must be a 1-100 string array")
        if not all(isinstance(item, str) and item for item in argv):
            raise HostedBridgeAccessDenied("dev_server argv must contain non-empty strings")
        for profile in profiles:
            if profile.matches(argv):
                return profile
        approved = ", ".join(" ".join(item.argv) for item in profiles) or "none"
        raise HostedBridgeAccessDenied(
            "dev server argv is not in the user-approved server-profile allowlist "
            f"for this project (approved: {approved})"
        )

    def _spawn_dev_server_locked(
        self,
        *,
        profile: ManagedServerProfile,
        argv: Sequence[str],
        process_id: str,
        caller_env: Any,
        ready_url: Optional[str],
        deadline_seconds: float,
        workstream_id: str,
        project_id: Optional[str] = None,
        cwd: Optional[Path] = None,
    ) -> dict[str, Any]:
        """Spawn one approved profile while the caller owns its service lease."""

        project_id = project_id or self._anchor_project_id
        working_directory = str(cwd or self.repository)

        if not isinstance(caller_env, dict):
            return {
                "ok": False,
                "error_code": "invalid_request",
                "error": "env must be an object",
            }
        env = child_process_environment()
        env.update(profile.env)
        for key, value in caller_env.items():
            if not isinstance(key, str) or key.upper() not in profile.env_allowlist:
                return {
                    "ok": False,
                    "error_code": "denied",
                    "error": f"env override is not in the profile allowlist: {key}",
                }
            if not isinstance(value, str) or len(value) > 1000:
                return {
                    "ok": False,
                    "error_code": "invalid_request",
                    "error": f"env value for {key} is invalid",
                }
            env[key] = value

        stdout_path = self._process_store.root / f"{process_id}.stdout.log"
        stderr_path = self._process_store.root / f"{process_id}.stderr.log"
        stdout_handle = stdout_path.open("ab", buffering=0)
        stderr_handle = stderr_path.open("ab", buffering=0)
        try:
            launch_argv = _resolve_process_argv(argv)
            if self._popen_factory is not None:
                process = self._popen_factory(
                    launch_argv,
                    cwd=working_directory,
                    env=env,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    shell=False,
                )
            else:
                import subprocess

                process = subprocess.Popen(
                    launch_argv,
                    cwd=working_directory,
                    env=env,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    shell=False,
                )
        except Exception as exc:
            return {
                "ok": False,
                "error_code": "launch_failed",
                "error": str(redact(f"managed service launch failed: {exc}")),
            }
        finally:
            stdout_handle.close()
            stderr_handle.close()

        identity = ProcessIdentity.capture(process.pid)
        if (
            identity.created_at is None
            and identity.executable is None
            and identity.cmdline_digest is None
        ):
            # We still own the freshly-created child here, so this one cleanup is
            # safe. Do not persist an unmanageable process that future calls
            # would have to guess about.
            _kill_pid_tree(process.pid)
            return {
                "ok": False,
                "error_code": "identity_capture_failed",
                "error": "managed service started but its process identity could not be captured",
            }

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
        identity_saved = self._write_process_identity(process_id, identity)
        scope_saved = self._write_process_scope(
            record, workstream_id=workstream_id, project_id=project_id
        )
        if not identity_saved or not scope_saved:
            # The child was created by this exact call, so cleanup is safe here.
            # Never leave behind a process whose future ownership cannot be
            # proven after the bridge or OS restarts.
            _kill_pid_tree(process.pid)
            return {
                "ok": False,
                "error_code": "ownership_record_failed",
                "error": "managed service ownership metadata could not be persisted",
                "process_id": process_id,
                "pid": process.pid,
                "running": _pid_alive(process.pid),
            }

        # Give immediate-exit failures a short chance to become observable
        # before reporting a successful long-lived service.
        time.sleep(0.03)
        running = _pid_alive(process.pid)
        if not running:
            return {
                "ok": False,
                "error_code": "startup_exit",
                "error": "managed service exited immediately after launch",
                "process_id": process_id,
                "pid": process.pid,
                "running": False,
                "started_at": record.started_at,
                "stdout": _read_log(stdout_path),
                "stderr": _read_log(stderr_path),
            }

        effective_ready_url = ready_url or profile.ready_url
        url: Optional[str] = None
        ready = False
        ready_error: Optional[str] = None
        if effective_ready_url is not None:
            effective_ready_url = _validate_local_url(effective_ready_url)
            url = effective_ready_url
            ready, ready_error = self._poll_ready(effective_ready_url, deadline_seconds)
        ok = running and (ready if url is not None else True)
        return {
            "ok": ok,
            "error_code": None if ok else "readiness_failed",
            "error": str(redact(ready_error)) if ready_error else None,
            "process_id": process_id,
            "pid": process.pid,
            "running": running,
            "started_at": record.started_at,
            "url": url,
            "ready": ready,
            "ready_error": str(redact(ready_error)) if ready_error else None,
            "host_hint": profile.host_hint,
            # Values are never returned; names are safe diagnostics.
            "env_keys": sorted({*profile.env.keys(), *caller_env.keys()}),
        }

    def _dev_server_start(
        self, arguments: dict[str, Any], deadline_seconds: float
    ) -> dict[str, Any]:
        try:
            workstream_id = self._requested_workstream(arguments)
        except HostedBridgeAccessDenied as exc:
            return {"ok": False, "error_code": "denied", "error": str(exc)}
        argv = self._required(arguments, "argv", list)
        try:
            project_id, project_path = self._project_for_workstream(workstream_id)
            profile = self._resolve_profile(
                argv, self._profiles_for_project(project_id, project_path)
            )
        except HostedBridgeAccessDenied as exc:
            return {"ok": False, "error_code": "denied", "error": str(exc)}
        process_id = arguments.get("process_id") or f"srv-{int(time.time() * 1000)}"
        if not isinstance(process_id, str) or not 1 <= len(process_id) <= 80:
            return {
                "ok": False,
                "error_code": "invalid_request",
                "error": "process_id must be a 1-80 character string",
            }
        if not all(ch.isalnum() or ch in "._-" for ch in process_id):
            return {
                "ok": False,
                "error_code": "invalid_request",
                "error": "process_id must use [A-Za-z0-9._-] only",
            }

        lease = ServiceLease(
            self._process_store.root,
            process_id,
            owner=f"{self.session_id}:{os.getpid()}",
        )
        try:
            with lease:
                try:
                    existing = self._process_store.get(process_id)
                except Exception:
                    existing = None
                if existing is not None and _pid_alive(existing.pid):
                    scope_ok, scope_reason = self._verify_process_scope(
                        existing, workstream_id=workstream_id, project_id=project_id
                    )
                    if not scope_ok:
                        return {
                            "ok": False,
                            "error_code": "ownership_scope_mismatch",
                            "error": f"refusing to reuse pid {existing.pid}: {scope_reason}",
                            "process_id": existing.process_id,
                            "pid": existing.pid,
                            "running": False,
                            "pid_alive": True,
                        }
                    proven, state, reason, _live = self._prove_live_record(existing)
                    if not proven:
                        return {
                            "ok": False,
                            "error_code": state,
                            "error": f"refusing to reuse pid {existing.pid}: {reason}",
                            "process_id": existing.process_id,
                            "pid": existing.pid,
                            "running": False,
                            "pid_alive": True,
                        }
                    return {
                        "ok": True,
                        "process_id": existing.process_id,
                        "pid": existing.pid,
                        "running": True,
                        "started_at": existing.started_at,
                        "identity": state,
                        "reused": True,
                    }
                return self._spawn_dev_server_locked(
                    profile=profile,
                    argv=argv,
                    process_id=process_id,
                    caller_env=arguments.get("env") or {},
                    ready_url=arguments.get("ready_url"),
                    deadline_seconds=deadline_seconds,
                    workstream_id=workstream_id,
                    project_id=project_id,
                    cwd=project_path,
                )
        except ServiceLeaseError as exc:
            return {
                "ok": False,
                "error_code": "service_busy",
                "error": str(redact(str(exc))),
                "process_id": process_id,
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
        del deadline_seconds
        process_id = self._required(arguments, "process_id", str)
        try:
            workstream_id = self._requested_workstream(arguments)
            record = self._require_own(process_id)
            project_id, _project_path = self._project_for_workstream(workstream_id)
        except HostedBridgeAccessDenied as exc:
            return {"ok": False, "error_code": "denied", "error": str(exc)}
        scope_ok, scope_reason = self._verify_process_scope(
            record, workstream_id=workstream_id, project_id=project_id
        )
        if not scope_ok:
            return {
                "ok": False,
                "error_code": "ownership_scope_mismatch",
                "error": f"refusing to inspect pid {record.pid}: {scope_reason}",
                "process_id": record.process_id,
                "pid": record.pid,
                "running": False,
                "pid_alive": _pid_alive(record.pid),
            }
        alive = _pid_alive(record.pid)
        identity = "not_running"
        if alive:
            proven, identity, reason, _live = self._prove_live_record(record)
            if not proven:
                return {
                    "ok": False,
                    "error_code": identity,
                    "error": f"managed service identity is not trusted: {reason}",
                    "process_id": record.process_id,
                    "pid": record.pid,
                    "running": False,
                    "pid_alive": True,
                    "started_at": record.started_at,
                    "argv": list(redact(record.argv)),
                    "stdout": _read_log(Path(record.stdout_path)),
                    "stderr": _read_log(Path(record.stderr_path)),
                }
        return {
            "ok": True,
            "process_id": record.process_id,
            "pid": record.pid,
            "running": alive,
            "identity": identity,
            "started_at": record.started_at,
            "argv": list(redact(record.argv)),
            "stdout": _read_log(Path(record.stdout_path)),
            "stderr": _read_log(Path(record.stderr_path)),
        }

    def _dev_server_logs(self, arguments: dict[str, Any], deadline_seconds: float) -> dict[str, Any]:
        del deadline_seconds
        process_id = self._required(arguments, "process_id", str)
        limit = arguments.get("limit", 100_000)
        if not isinstance(limit, int) or limit <= 0 or limit > 1_000_000:
            limit = 100_000
        try:
            workstream_id = self._requested_workstream(arguments)
            record = self._require_own(process_id)
            project_id, _project_path = self._project_for_workstream(workstream_id)
        except HostedBridgeAccessDenied as exc:
            return {"ok": False, "error_code": "denied", "error": str(exc)}
        scope_ok, scope_reason = self._verify_process_scope(
            record, workstream_id=workstream_id, project_id=project_id
        )
        if not scope_ok:
            return {
                "ok": False,
                "error_code": "ownership_scope_mismatch",
                "error": f"refusing to read logs for pid {record.pid}: {scope_reason}",
                "process_id": record.process_id,
                "pid": record.pid,
            }
        return {
            "ok": True,
            "process_id": record.process_id,
            "stdout": _read_log(Path(record.stdout_path), limit=limit),
            "stderr": _read_log(Path(record.stderr_path), limit=limit),
        }

    def _identity_path(self, process_id: str) -> Any:
        return self._process_store.root / f"{process_id}.identity.json"

    def _scope_path(self, process_id: str) -> Any:
        return self._process_store.root / f"{process_id}.scope.json"

    @staticmethod
    def _requested_workstream(arguments: Mapping[str, Any]) -> str:
        raw = arguments.get("workstream_id")
        if raw is None:
            return "default"
        if not isinstance(raw, str):
            raise HostedBridgeAccessDenied("workstream_id must be a string")
        value = raw.strip()
        if not 1 <= len(value) <= 64 or not all(
            ch.isalnum() or ch in "._-" for ch in value
        ):
            raise HostedBridgeAccessDenied(
                "workstream_id must be 1-64 characters from [A-Za-z0-9._-]"
            )
        return value

    def _write_process_identity(
        self, process_id: str, identity: ProcessIdentity
    ) -> bool:
        """Persist identity facts; mutation must fail closed if this fails."""

        import dataclasses as _dc

        try:
            self._identity_path(process_id).write_text(
                json.dumps(_dc.asdict(identity), sort_keys=True),
                encoding="utf-8",
            )
            return True
        except OSError:
            return False

    def _write_process_scope(
        self,
        record: ManagedProcessRecord,
        *,
        workstream_id: str,
        project_id: Optional[str] = None,
    ) -> bool:
        """Persist non-model-visible ownership proof for a managed service."""

        import secrets
        import tempfile

        path = self._scope_path(record.process_id)
        payload = {
            "schema_version": 1,
            "session_id": self.session_id,
            "repository": str(self.repository),
            "repo_fingerprint": self._repo_fingerprint,
            "workstream_id": workstream_id,
            "project_id": project_id or self._anchor_project_id,
            "pid": record.pid,
            # Local ownership proof only. Never returned in status/logs/MCP.
            "ownership_token": secrets.token_hex(24),
        }
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=path.parent
        )
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            return True
        except OSError:
            try:
                os.close(descriptor)
            except OSError:
                pass
            return False
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _load_process_scope(self, record: ManagedProcessRecord) -> Optional[dict[str, Any]]:
        try:
            payload = json.loads(
                self._scope_path(record.process_id).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def _verify_process_scope(
        self,
        record: ManagedProcessRecord,
        *,
        workstream_id: str,
        project_id: Optional[str] = None,
    ) -> tuple[bool, str]:
        scope = self._load_process_scope(record)
        if scope is None:
            return False, "managed service has no ownership scope proof"
        token = scope.get("ownership_token")
        expected = {
            "schema_version": 1,
            "session_id": self.session_id,
            "repository": str(self.repository),
            "repo_fingerprint": self._repo_fingerprint,
            "workstream_id": workstream_id,
            "pid": record.pid,
        }
        # Records written before managed servers were project-aware carry no
        # project at all; they belong to the anchor by construction, so they
        # keep verifying instead of stranding a running service.
        if project_id is not None and "project_id" in scope:
            expected["project_id"] = project_id
        for key, value in expected.items():
            if scope.get(key) != value:
                return False, f"managed service ownership scope mismatch: {key}"
        if not isinstance(token, str) or len(token) < 32:
            return False, "managed service ownership token is missing or malformed"
        return True, "scope verified"

    def _load_process_identity(
        self, record: ManagedProcessRecord
    ) -> ProcessIdentity:
        try:
            payload = json.loads(
                self._identity_path(record.process_id).read_text(encoding="utf-8")
            )
            return ProcessIdentity(
                pid=int(payload.get("pid") or record.pid),
                created_at=(
                    float(payload["created_at"])
                    if payload.get("created_at") is not None
                    else None
                ),
                executable=(
                    str(payload["executable"]) if payload.get("executable") else None
                ),
                cmdline_digest=(
                    str(payload["cmdline_digest"])
                    if payload.get("cmdline_digest")
                    else None
                ),
            )
        except (OSError, ValueError, TypeError, KeyError):
            # Legacy record without a sidecar: the only stored time fact is
            # started_at, recorded moments after the spawn.
            return ProcessIdentity(
                pid=record.pid,
                created_at=None,
                executable=None,
                cmdline_digest=None,
            )

    @staticmethod
    def _protected_process_reason(
        record: ManagedProcessRecord, live: ProcessIdentity
    ) -> Optional[str]:
        """Why this recorded process must never be signalled, if any."""

        if record.pid == os.getpid():
            return "refusing to stop the KaroX control plane itself"
        # A managed record must never be allowed to target an ancestor of the
        # bridge either: killing its tree would also kill KaroX even when the
        # record's PID differs from os.getpid().
        try:
            import psutil  # type: ignore[import-untyped]

            if any(parent.pid == record.pid for parent in psutil.Process(os.getpid()).parents()):
                return "refusing to stop an ancestor of the KaroX control plane"
        except Exception:
            pass
        logical = [str(item).replace("\\", "/").lower() for item in record.argv]
        if logical:
            first = logical[0].rsplit("/", 1)[-1]
            if first in {"karox", "karox.exe", "karox-vnext", "karox-vnext.exe"}:
                return "refusing to stop a KaroX launcher"
            if len(logical) >= 3 and logical[1] == "-m" and logical[2].startswith("karox"):
                return "refusing to stop a KaroX Python module"
        executable = (live.executable or "").replace("\\", "/")
        basename = executable.rsplit("/", 1)[-1].lower()
        for suffix in (".exe", ".cmd", ".bat", ".com"):
            if basename.endswith(suffix):
                basename = basename[: -len(suffix)]
                break
        if basename in PROTECTED_EXECUTABLES:
            return f"refusing to stop protected executable {basename!r}"
        return None

    def _prove_live_record(
        self, record: ManagedProcessRecord
    ) -> tuple[bool, str, str, ProcessIdentity]:
        """Prove that a live PID is still the process this record started."""

        if not _pid_alive(record.pid):
            return False, "not_running", "process is not running", ProcessIdentity.capture(record.pid)
        live = ProcessIdentity.capture(record.pid)
        protected = self._protected_process_reason(record, live)
        if protected is not None:
            return False, "protected_process", protected, live
        stored = self._load_process_identity(record)
        has_stored_facts = (
            stored.created_at is not None
            or stored.executable is not None
            or stored.cmdline_digest is not None
        )
        has_live_facts = (
            live.created_at is not None
            or live.executable is not None
            or live.cmdline_digest is not None
        )
        if not has_stored_facts or not has_live_facts:
            return (
                False,
                "identity_unverifiable",
                (
                    f"process identity for pid {record.pid} cannot be proven from "
                    "both the stored record and the live OS"
                ),
                live,
            )
        verdict = verify_identity(stored, live, alive=True)
        if not verdict.ok:
            return False, "identity_mismatch", verdict.reason, live
        return True, "verified", "identity verified", live

    def _verified_stop_locked(
        self,
        record: ManagedProcessRecord,
        *,
        workstream_id: str,
        project_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Stop one managed process after the caller owns its mutation lease."""

        scope_ok, scope_reason = self._verify_process_scope(
            record, workstream_id=workstream_id, project_id=project_id
        )
        if not scope_ok:
            return {
                "ok": False,
                "error_code": "ownership_scope_mismatch",
                "error": f"refusing to manage pid {record.pid}: {scope_reason}",
                "process_id": record.process_id,
                "pid": record.pid,
                "running": False,
                "pid_alive": _pid_alive(record.pid),
                "exit_confirmed": False,
            }

        was_running = _pid_alive(record.pid)
        if not was_running:
            return {
                "ok": True,
                "process_id": record.process_id,
                "pid": record.pid,
                "was_running": False,
                "running": False,
                "identity": "not_running",
                "exit_confirmed": True,
            }

        proven, state, reason, _live = self._prove_live_record(record)
        if not proven:
            return {
                "ok": False,
                "error_code": state,
                "error": f"refusing to stop pid {record.pid}: {reason}",
                "process_id": record.process_id,
                "pid": record.pid,
                "was_running": True,
                "running": True,
                "exit_confirmed": False,
            }

        _kill_pid_tree(record.pid)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and _pid_alive(record.pid):
            time.sleep(0.05)
        running = _pid_alive(record.pid)
        return {
            "ok": not running,
            "error_code": None if not running else "stop_timeout",
            "error": None if not running else "managed process did not exit within 10 seconds",
            "process_id": record.process_id,
            "pid": record.pid,
            "was_running": True,
            "running": running,
            "identity": state,
            "exit_confirmed": not running,
        }

    def _dev_server_stop(
        self, arguments: dict[str, Any], deadline_seconds: float
    ) -> dict[str, Any]:
        del deadline_seconds
        process_id = self._required(arguments, "process_id", str)
        try:
            workstream_id = self._requested_workstream(arguments)
            record = self._require_own(process_id)
            project_id, _project_path = self._project_for_workstream(workstream_id)
        except HostedBridgeAccessDenied as exc:
            return {"ok": False, "error_code": "denied", "error": str(exc)}
        lease = ServiceLease(
            self._process_store.root,
            process_id,
            owner=f"{self.session_id}:{os.getpid()}",
        )
        try:
            with lease:
                return self._verified_stop_locked(
                    record, workstream_id=workstream_id, project_id=project_id
                )
        except ServiceLeaseError as exc:
            return {
                "ok": False,
                "error_code": "service_busy",
                "error": str(redact(str(exc))),
                "process_id": process_id,
                "pid": record.pid,
                "running": _pid_alive(record.pid),
            }

    def _dev_server_restart(
        self, arguments: dict[str, Any], deadline_seconds: float
    ) -> dict[str, Any]:
        """Identity-checked stop -> confirmed exit -> launch -> readiness."""

        process_id = self._required(arguments, "process_id", str)
        try:
            workstream_id = self._requested_workstream(arguments)
            record = self._require_own(process_id)
            project_id, project_path = self._project_for_workstream(workstream_id)
            profile = self._resolve_profile(
                list(record.argv), self._profiles_for_project(project_id, project_path)
            )
        except HostedBridgeAccessDenied as exc:
            return {"ok": False, "error_code": "denied", "error": str(exc)}
        lease = ServiceLease(
            self._process_store.root,
            process_id,
            owner=f"{self.session_id}:{os.getpid()}",
        )
        try:
            with lease:
                stopped = self._verified_stop_locked(
                    record, workstream_id=workstream_id, project_id=project_id
                )
                if not bool(stopped.get("ok")):
                    return {"action": "restart", **stopped}
                launched = self._spawn_dev_server_locked(
                    profile=profile,
                    argv=record.argv,
                    process_id=process_id,
                    caller_env=arguments.get("env") or {},
                    ready_url=arguments.get("ready_url"),
                    deadline_seconds=deadline_seconds,
                    workstream_id=workstream_id,
                    project_id=project_id,
                    cwd=project_path,
                )
                return {
                    "action": "restart",
                    "old_pid": record.pid,
                    "exit_confirmed": True,
                    "restarted": bool(launched.get("ok")),
                    **launched,
                }
        except ServiceLeaseError as exc:
            return {
                "ok": False,
                "action": "restart",
                "error_code": "service_busy",
                "error": str(redact(str(exc))),
                "process_id": process_id,
                "pid": record.pid,
                "running": _pid_alive(record.pid),
            }

    def _checks_start(
        self, arguments: dict[str, Any], deadline_seconds: float
    ) -> dict[str, Any]:
        del deadline_seconds
        idempotency_key = arguments.pop("_idempotency_key", None)
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise CheckJobError("checks.start requires an idempotency key")
        return {
            "ok": True,
            **self._check_jobs.start(
                arguments,
                idempotency_key=idempotency_key,
                bridge_pid=os.getpid(),
            ),
        }

    def _checks_status(
        self, arguments: dict[str, Any], deadline_seconds: float
    ) -> dict[str, Any]:
        del deadline_seconds
        job_id = self._required(arguments, "job_id", str)
        return {"ok": True, **self._check_jobs.status(job_id)}

    def _checks_logs(
        self, arguments: dict[str, Any], deadline_seconds: float
    ) -> dict[str, Any]:
        del deadline_seconds
        job_id = self._required(arguments, "job_id", str)
        limit = arguments.get("limit", 64 * 1024)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise CheckJobError("limit must be an integer")
        return {"ok": True, **self._check_jobs.logs(job_id, limit=limit)}

    def _checks_cancel(
        self, arguments: dict[str, Any], deadline_seconds: float
    ) -> dict[str, Any]:
        del deadline_seconds
        job_id = self._required(arguments, "job_id", str)
        return {"ok": True, **self._check_jobs.cancel(job_id)}

    def _runtime_restart(
        self, arguments: dict[str, Any], deadline_seconds: float
    ) -> dict[str, Any]:
        del deadline_seconds
        if not self._saved_profile_name:
            raise HostedBridgeAccessDenied(
                "runtime restart is available only for a durable saved bridge"
            )
        idempotency_key = arguments.pop("_idempotency_key", None)
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise HostedBridgeAccessDenied("runtime restart requires an idempotency key")
        request_id = arguments.get("request_id")
        if (
            not isinstance(request_id, str)
            or not 1 <= len(request_id) <= 128
            or not request_id[0].isalnum()
            or not all(ch.isalnum() or ch in "._-" for ch in request_id)
        ):
            raise HostedBridgeAccessDenied("runtime restart requires a safe request_id")
        reason = arguments.get("reason", "")
        if self._browser.takeover_active:
            raise HostedBridgeAccessDenied(
                "runtime restart is blocked while browser user takeover is active"
            )
        browser_open = self._browser.is_open
        browser_backend = self._browser_policy.backend
        if browser_open and browser_backend != "extension":
            raise HostedBridgeAccessDenied(
                "runtime restart would discard the active Playwright browser; "
                "finish that browser session or use the managed extension backend"
            )
        from .runtime_restart import RuntimeRestartError, schedule_saved_bridge_child_restart

        try:
            return schedule_saved_bridge_child_restart(
                session_id=self.session_id,
                saved_profile=self._saved_profile_name,
                idempotency_key=idempotency_key,
                reason=reason,
                browser_backend=browser_backend,
                browser_process_preserved=browser_open and browser_backend == "extension",
            )
        except RuntimeRestartError as exc:
            raise HostedBridgeAccessDenied(str(exc)) from exc

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
        selector = arguments.get("selector")
        if selector is not None and not isinstance(selector, dict):
            return {
                "ok": False,
                "error_code": "invalid_request",
                "error": "selector must be an object",
            }
        max_output_bytes = arguments.get("max_output_bytes", 64 * 1024)
        if (
            not isinstance(max_output_bytes, int)
            or isinstance(max_output_bytes, bool)
            or not 1024 <= max_output_bytes <= 1024 * 1024
        ):
            return {
                "ok": False,
                "error_code": "invalid_request",
                "error": "max_output_bytes must be between 1024 and 1048576",
            }
        try:
            _data, record = self._artifacts.read(artifact_id)
            if selector is not None:
                selection = self._artifacts.read_selection(
                    artifact_id,
                    selector,
                    max_output_bytes=max_output_bytes,
                )
                return {"ok": True, **record.to_dict(), "selection": selection}
        except FileNotFoundError as exc:
            return {"ok": False, "error_code": "not_found", "error": str(exc)}
        except (KeyError, ValueError) as exc:
            return {"ok": False, "error_code": "invalid_request", "error": str(exc)}
        return {
            "ok": True,
            **record.to_dict(),
            "relative_path": f".karox/artifacts/{self.session_id}/{record.artifact_id}.bin",
        }

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
    BROWSER_COMMAND: HostedToolsRuntime._browser_command,
    BROWSER_OPEN: HostedToolsRuntime._browser_open,
    BROWSER_TABS: HostedToolsRuntime._browser_tabs,
    BROWSER_NEW_TAB: HostedToolsRuntime._browser_new_tab,
    BROWSER_SWITCH_TAB: HostedToolsRuntime._browser_switch_tab,
    BROWSER_CLOSE_TAB: HostedToolsRuntime._browser_close_tab,
    BROWSER_SNAPSHOT: HostedToolsRuntime._browser_snapshot,
    BROWSER_CLICK: HostedToolsRuntime._browser_click,
    BROWSER_FILL: HostedToolsRuntime._browser_fill,
    BROWSER_FILL_CREDENTIAL: HostedToolsRuntime._browser_fill_credential,
    BROWSER_SELECT: HostedToolsRuntime._browser_select,
    BROWSER_PRESS: HostedToolsRuntime._browser_press,
    BROWSER_WAIT: HostedToolsRuntime._browser_wait,
    BROWSER_GET_TEXT: HostedToolsRuntime._browser_get_text,
    BROWSER_SCREENSHOT: HostedToolsRuntime._browser_screenshot,
    BROWSER_CONSOLE: HostedToolsRuntime._browser_console,
    BROWSER_NETWORK: HostedToolsRuntime._browser_network,
    BROWSER_NETWORK_REQUESTS: HostedToolsRuntime._browser_network_requests,
    BROWSER_REQUEST_TAKEOVER: HostedToolsRuntime._browser_request_takeover,
    BROWSER_RESUME_TAKEOVER: HostedToolsRuntime._browser_resume_takeover,
    BROWSER_CLOSE: HostedToolsRuntime._browser_close,
    DEV_SERVER_START: HostedToolsRuntime._dev_server_start,
    DEV_SERVER_STATUS: HostedToolsRuntime._dev_server_status,
    DEV_SERVER_LOGS: HostedToolsRuntime._dev_server_logs,
    DEV_SERVER_STOP: HostedToolsRuntime._dev_server_stop,
    DEV_SERVER_RESTART: HostedToolsRuntime._dev_server_restart,
    CHECKS_START: HostedToolsRuntime._checks_start,
    CHECKS_STATUS: HostedToolsRuntime._checks_status,
    CHECKS_LOGS: HostedToolsRuntime._checks_logs,
    CHECKS_CANCEL: HostedToolsRuntime._checks_cancel,
    RUNTIME_RESTART: HostedToolsRuntime._runtime_restart,
    ARTIFACT_GET: HostedToolsRuntime._artifact_get,
    ARTIFACT_READ_IMAGE: HostedToolsRuntime._artifact_read_image,
}
