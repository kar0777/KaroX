"""One-command lifecycle manager for ChatGPT/Claude web MCP bridges."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Optional, Sequence
from urllib.parse import urlsplit

from .bridge import (
    BridgeCredentialMissing,
    BridgeCredentialStore,
    known_bridge_profiles,
)
from .browser_access import BrowserAccessPolicy
from .client_capabilities import negotiate_client_capabilities
from .tool_catalog import catalog_groups
from .credentials import CredentialError
from .detached_process import spawn_detached
from .hosted_bridge import (
    DEFAULT_HOSTED_DEADLINE_SECONDS,
    KNOWN_HOSTED_TOOL_NAMES,
)
from .hosted_tools_runtime import (
    ManagedServerProfile,
)
from .models import AccessProfile, Capability
from .process_launcher import resolve_executable as _resolve_executable
from .route_health import RouteHealthTracker, public_mcp_route_healthy
from .tailscale import (
    TailscaleError,
    classify_funnel_failure,
    prepare_tailscale_funnel,
)
from .paths import runtime_dir, session_dir
from .project_registry import ProjectRegistry, ProjectRegistryError
from .proxy_server import ALLOWED_HOSTS_ENVIRONMENT
from .sessions import SessionError, SessionStore


WEB_BRIDGE_PROFILES = (
    "chatgpt-web",
    "claude-web",
    "notion",
    "hyperagent-web",
    "adapt",
)
SELF_RESTART_WEB_PROFILES = frozenset({"chatgpt-web", "claude-web", "hyperagent-web"})
# The redirect hosts a strict profile will send authorization codes to. ``None``
# means "any HTTPS host" -- the permissive default chatgpt-web/claude-web and
# library callers rely on. hyperagent-web pins the one verified canonical host.
# The exact callback path is deliberately not hardcoded: the documented
# endpoint has moved (``/api/mcp`` today, ``/api/mcp-serve`` in one screenshot
# that no source corroborates), so the host is pinned and any path on it is
# accepted. Arbitrary subdomains, wildcards, HTTP, and non-web schemes are
# rejected by the structural check in ``_redirect_uri`` before the host is ever
# compared.
NOTION_OAUTH_CLIENT_HOSTS = frozenset(
    {"notion.so", "www.notion.so", "notion.com", "www.notion.com", "app.notion.com"}
)

PROFILE_REDIRECT_HOSTS: dict[str, Optional[frozenset[str]]] = {
    "notion": NOTION_OAUTH_CLIENT_HOSTS,
    "hyperagent-web": frozenset({"hyperagent.com"}),
}


def profile_redirect_hosts(profile: str) -> Optional[frozenset[str]]:
    """Return the pinned redirect-host allowlist for a strict profile, or None."""
    return PROFILE_REDIRECT_HOSTS.get(profile)


def profile_tailscale_https_port(profile: str) -> int:
    """Give parallel hosted clients distinct Funnel listeners on one device."""
    return {"notion": 8443, "hyperagent-web": 10000, "adapt": 10001}.get(profile, 443)


def web_bridge_mcp_path(profile: str) -> str:
    """Return the canonical public MCP resource path for a hosted client."""
    return "/mcp"


def web_bridge_mcp_endpoint(public_url: str, profile: str) -> str:
    return f"{public_url.rstrip('/')}{web_bridge_mcp_path(profile)}"


DEFAULT_WEB_TOOLS = (
    "karox.repo.read_file",
    "karox.repo.read_lines",
    "karox.repo.list_files",
    "karox.repo.search",
    "karox.repo.inspect",
    "karox.git.status",
    "karox.git.diff",
    "karox.git.log",
    "karox.runtime.status",
    "karox.task.bootstrap",
    "karox.task.resume",
    "karox.task.status",
    "karox.task.workstreams",
    # Universal memory ships in the default bundle: cross-client memory only
    # works when every connected client can remember and recall. These tools
    # write local memory files only, never the repository.
    "karox.memory.remember",
    "karox.memory.recall",
    "karox.memory.context",
    "karox.memory.list",
    "karox.memory.forget",
    "karox.browser.command",
    # Read-only browser/artifact/dev-server tools a hosted client needs to see
    # the interface without mutating it. Tab enumeration is session-scoped and
    # reveals no cookies or storage. Mutating browser/server tools are added by
    # WRITE_WEB_TOOLS below.
    "karox.browser.tabs",
    "karox.browser.snapshot",
    "karox.browser.wait_for",
    "karox.browser.get_text",
    "karox.browser.console",
    "karox.browser.network_failures",
    "karox.browser.screenshot",
    "karox.artifact.get",
    "karox.artifact.read_image",
    "karox.dev_server.status",
    "karox.dev_server.logs",
    "karox.checks.status",
    "karox.checks.logs",
)
WRITE_WEB_TOOLS = (
    "karox.task.checkpoint",
    "karox.task.execute_plan",
    "karox.checks.run_affected",
    "karox.repo.edit_file",
    "karox.repo.write_file",
    "karox.repo.command",
    "karox.tests.run",
    # Stateful browser and dev-server control. These drive a UI or start a
    # process, so legacy profiles still gate them behind --write. The explicit
    # external-browser mode uses the narrower browser_control access profile.
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
    "karox.dev_server.start",
    "karox.dev_server.stop",
    "karox.checks.start",
    "karox.checks.cancel",
)
# Single source of truth for "this tool mutates the repository, workspace, or
# guarded process state" -- i.e. selecting any of these means the connection
# must not launch as READ_ONLY. The TUI wizard, the capability negotiation in
# :mod:`karox.client_capabilities`, and the write-tier gating above all key off
# this one set so they can never drift back into the state where the wizard
# launched a READ_ONLY session for a tool that could then never run.
MUTATING_WEB_TOOLS: frozenset[str] = frozenset(
    {
        *WRITE_WEB_TOOLS,
        "karox.checks.run",
        "karox.command.run",
        "karox.git.commit",
        "karox.runtime.restart",
    }
)
# Canonical browser tool-name groups.  These are the single source of truth for
# the browser read/input split: ``__post_init__`` normalizes the tool bundle
# with them (browser.input implies browser.read) and ``web_bridge_diagnostics``
# derives ``browser_permission`` from them, so the two never drift into the
# contradictory ``read:false, input:true`` state.  ``open``/``close`` are input
# tools because they mutate browser session state (navigate/tear down), the
# same capability tier as click/fill/select/press.
BROWSER_READ_TOOL_NAMES: tuple[str, ...] = (
    "karox.browser.command",
    "karox.browser.tabs",
    "karox.browser.snapshot",
    "karox.browser.wait_for",
    "karox.browser.get_text",
    "karox.browser.console",
    "karox.browser.network_failures",
    "karox.browser.network_requests",
    "karox.browser.screenshot",
)
BROWSER_INPUT_TOOL_NAMES: tuple[str, ...] = (
    "karox.browser.open",
    "karox.browser.new_tab",
    "karox.browser.switch_tab",
    "karox.browser.close_tab",
    "karox.browser.click",
    "karox.browser.fill",
    "karox.browser.fill_credential",
    "karox.browser.select",
    "karox.browser.press",
    "karox.browser.request_user_takeover",
    "karox.browser.resume_after_user_takeover",
    "karox.browser.close",
)
EXTERNAL_BROWSER_TOOL_NAMES: tuple[str, ...] = tuple(
    dict.fromkeys((*BROWSER_READ_TOOL_NAMES, *BROWSER_INPUT_TOOL_NAMES))
)
# checks.run is read-only in effect (it runs an approved verification command)
# but is only useful with a verification allowlist, so it is surfaced by the
# diagnostics as available when one is configured rather than forced into the
# default bundle.
CHECKS_RUN_TOOL = "karox.checks.run"

# Stable, hot-reload command surfaces must not disappear merely because a
# launcher supplied an explicit legacy ``--tool`` list. The TUI historically
# selected concrete operations (write_file, checks.run, browser.snapshot, ...),
# and argparse treats an explicit list as a replacement for DEFAULT_WEB_TOOLS.
# That left a newly connected hosted agent with only the old calls even though
# the guarded worker commands were implemented and available.
#
# These aliases do not grant a new capability tier: they are included only when
# the corresponding capability family was already selected, and workspace
# execution aliases still require a workspace-write/elevated session.
_WORKSPACE_WRITE_PROFILES = {
    AccessProfile.WORKSPACE_WRITE,
    AccessProfile.ELEVATED,
}
_REPOSITORY_WRITE_TOOL_NAMES = {
    "karox.repo.edit_file",
    "karox.repo.write_file",
}


def _include_stable_worker_commands(
    tools: tuple[str, ...],
    *,
    access_profile: AccessProfile,
    verification_commands: tuple[tuple[str, ...], ...],
) -> tuple[str, ...]:
    """Merge stable worker commands implied by an already-selected tool family."""
    ordered = list(tools)
    selected = set(ordered)

    def include(name: str) -> None:
        if name not in selected:
            ordered.append(name)
            selected.add(name)

    if access_profile in _WORKSPACE_WRITE_PROFILES:
        if selected.intersection(_REPOSITORY_WRITE_TOOL_NAMES):
            include("karox.repo.command")
        # Local Git commits are an explicit ELEVATED capability. Publish the
        # corresponding tool automatically for an elevated saved bridge so the
        # client does not end up in the contradictory state where policy grants
        # GIT_COMMIT but the tool catalogue makes it impossible to use. Remote
        # Git, publish, and auth remain separately blocked by policy.
        if access_profile == AccessProfile.ELEVATED:
            include("karox.git.commit")
        # Supplying a verification allowlist is the explicit approval needed by
        # checks.run. Hiding the tool after accepting that allowlist produced a
        # contradictory bridge: diagnostics listed approved commands while the
        # client received tool_not_exposed. Publish both guarded verification
        # surfaces whenever a write-capable profile carries that approval.
        if verification_commands:
            include(CHECKS_RUN_TOOL)
            include("karox.tests.run")
            include("karox.checks.start")
            include("karox.checks.status")
            include("karox.checks.logs")
            include("karox.checks.cancel")

    browser_family = set(BROWSER_READ_TOOL_NAMES) | set(BROWSER_INPUT_TOOL_NAMES)
    if selected.intersection(browser_family):
        include("karox.browser.command")

    return tuple(ordered)


# Capabilities the Core tools require, as a static projection of the
# ``ToolDefinition`` tables in :mod:`karox.core` / :mod:`karox.core_tools`.
# The hosted-extra tools read theirs from the authoritative
# ``_HOSTED_EXTRA_TOOLS`` catalogue; Core definitions are instance-built and
# cannot be imported without a repository, so this table is pinned by
# ``test_saved_bridge_service`` against the real runtimes instead.
_CORE_TOOL_CAPABILITIES: dict[str, frozenset[Capability]] = {
    "karox.repo.read_file": frozenset({Capability.REPO_READ}),
    "karox.repo.read_lines": frozenset({Capability.REPO_READ}),
    "karox.repo.list_files": frozenset({Capability.REPO_READ}),
    "karox.repo.search": frozenset({Capability.REPO_READ}),
    "karox.repo.inspect": frozenset({Capability.REPO_READ}),
    "karox.runtime.status": frozenset({Capability.REPO_READ}),
    "karox.git.status": frozenset({Capability.GIT_READ}),
    "karox.git.diff": frozenset({Capability.GIT_READ}),
    "karox.git.log": frozenset({Capability.GIT_READ}),
    "karox.repo.write_file": frozenset({Capability.REPO_WRITE}),
    "karox.repo.edit_file": frozenset({Capability.REPO_WRITE}),
    "karox.repo.command": frozenset({Capability.REPO_WRITE}),
    "karox.command.run": frozenset({Capability.DEV_COMMAND, Capability.PROCESS_RUN}),
    "karox.git.commit": frozenset({Capability.GIT_COMMIT}),
    "karox.checks.run": frozenset(
        {Capability.CHECKS_RUN, Capability.PROCESS_RUN}
    ),
    "karox.tests.run": frozenset(
        {Capability.CHECKS_RUN, Capability.PROCESS_RUN}
    ),
}


def profile_incompatible_tools(
    tools: Sequence[str],
    access_profile: AccessProfile,
) -> dict[str, str]:
    """Tools the access profile cannot ever grant, each with a human reason.

    The runtimes fail closed when an allowed tool needs a capability the session
    profile does not include, and that raise is pinned by tests. A saved profile
    edited by an older UI can still hold such a tool, and letting one impossible
    checkbox kill the *whole bridge at startup* turned a fixable configuration
    into a dead ChatGPT connection. Saved-profile configs therefore drop those
    names -- they could never run anyway -- and ``web_bridge_diagnostics``
    reports every drop with this reason instead of leaving the user to read it
    out of a crash message.
    """
    from .autonomy_runtime import _TOOLS as _AUTONOMY_TOOLS
    from .hosted_tools_runtime import _HOSTED_EXTRA_TOOLS
    from .policy import _PROFILE_CAPABILITIES

    allowed = _PROFILE_CAPABILITIES.get(access_profile, frozenset())
    reason = f"not allowed by the {access_profile.value} access profile"
    denied: dict[str, str] = {}
    for name in dict.fromkeys(tools):
        meta = _HOSTED_EXTRA_TOOLS.get(name)
        autonomy_meta = _AUTONOMY_TOOLS.get(name)
        if meta is not None:
            required = frozenset({meta.capability}) if meta.capability is not None else frozenset()
        elif autonomy_meta is not None:
            required = frozenset(
                ({autonomy_meta.capability} if autonomy_meta.capability else set())
            ) | frozenset(autonomy_meta.additional_capabilities)
        else:
            required = _CORE_TOOL_CAPABILITIES.get(name, frozenset())
        if required - allowed:
            denied[name] = reason
    return denied


_QUICK_TUNNEL_URL = re.compile(
    r"https://[A-Za-z0-9-]+\.trycloudflare\.com(?=$|[\s/])"
)
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x0800
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_PR_SET_PDEATHSIG = 1
# A watchdog rename loses to any concurrent reader on Windows. Retry across a
# window shorter than OWNER_HEARTBEAT_STALE_SECONDS so a contended write never
# escalates into the supervisor replacing a perfectly healthy owner.
_WATCHDOG_REPLACE_TIMEOUT_SECONDS = 5.0
_WATCHDOG_REPLACE_INITIAL_DELAY_SECONDS = 0.02
_WATCHDOG_REPLACE_MAX_DELAY_SECONDS = 0.25
# A durable owner recycles its own MCP child. The replacement needs the port the
# dead one is still releasing, and a child that keeps failing to bind must be
# retried on a backoff instead of ending the session that owns the public URL.
_CHILD_PORT_RELEASE_TIMEOUT_SECONDS = 10.0
_CHILD_RESPAWN_MIN_BACKOFF_SECONDS = 1.0
_CHILD_RESPAWN_MAX_BACKOFF_SECONDS = 30.0


class WebBridgeLaunchError(RuntimeError):
    """A managed web bridge could not be prepared or kept alive."""


class SavedBridgeRestartRequired(RuntimeError):
    """A saved-profile edit is safe only after explicit live-restart consent."""


@dataclass(frozen=True)
class WebBridgeConnectConfig:
    profile: str
    repository: Path
    projects: tuple[dict[str, str], ...] = ()
    default_project_id: Optional[str] = None
    port: int = 8765
    tools: tuple[str, ...] = DEFAULT_WEB_TOOLS
    mcp_servers: tuple[str, ...] = ()
    session_id: Optional[str] = None
    # The dangerous profile must be asked for. A caller that forgets to pass one
    # publishes a repository to a third-party agent, so the omission defaults to
    # the profile that cannot write.
    access_profile: AccessProfile = AccessProfile.READ_ONLY
    tunnel: str = "tailscale"
    public_url: Optional[str] = None
    cloudflared: Optional[str] = None
    tailscale: Optional[str] = None
    tunnel_timeout_seconds: float = 30.0
    deadline_seconds: float = DEFAULT_HOSTED_DEADLINE_SECONDS
    verification_commands: tuple[tuple[str, ...], ...] = ()
    # The dev-server recipes a hosted client may start through karox.dev_server.*.
    # Defaults include the safe Vacancy Control profile (start:safe with
    # FACEBOOK_LIVE_ENABLED=false).  Profiles carry no secrets, only argv, an
    # env key set and an env allowlist, so they are safe to persist alongside a
    # saved bridge profile.
    server_profiles: tuple[ManagedServerProfile, ...] = ()
    browser_external_https: bool = False
    browser_allowed_domains: tuple[str, ...] = ()
    browser_denied_domains: tuple[str, ...] = ()
    browser_headed: bool = False
    browser_user_takeover: bool = False
    browser_network_inspection: bool = False
    browser_payment_confirmation: bool = False
    browser_allowed_emails: tuple[str, ...] = ()
    browser_credential_refs: tuple[str, ...] = ()
    language: str = "en"
    saved_profile_name: Optional[str] = None
    # Tools dropped at launch because the access profile cannot grant their
    # capability. Kept as name -> reason so diagnostics can say exactly why a
    # tool a saved profile selected is not served, instead of the bridge
    # crashing at startup with no explanation at all.
    profile_denied_tools: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.profile not in WEB_BRIDGE_PROFILES:
            raise ValueError(
                "web bridge profile must be chatgpt-web, claude-web, notion, "
                "hyperagent-web, or adapt"
            )
        repository_exists = self.repository.expanduser().exists()
        if self.projects or self.default_project_id is not None or repository_exists:
            try:
                project_registry = ProjectRegistry.from_profile(
                    repository=str(self.repository),
                    projects=self.projects,
                    default_project_id=self.default_project_id,
                )
            except ProjectRegistryError as exc:
                raise ValueError(f"web bridge project registry is invalid: {exc}") from exc
            if project_registry.default is None:
                raise ValueError("web bridge requires a default project")
            anchor = project_registry.entry_for_path(self.repository)
            if anchor is None:
                raise ValueError("web bridge session anchor is not an approved project")
            object.__setattr__(self, "projects", tuple(project_registry.to_payload()))
            object.__setattr__(self, "default_project_id", project_registry.default_project_id)
            object.__setattr__(self, "repository", Path(anchor.path))
        if not 1 <= self.port <= 65_535:
            raise ValueError("web bridge port must be between 1 and 65535")
        if self.tunnel not in {"cloudflare", "tailscale", "custom"}:
            raise ValueError(
                "web bridge tunnel must be cloudflare, tailscale, or custom"
            )
        if self.tunnel == "custom" and not self.public_url:
            raise ValueError("custom web bridge tunnel requires --public-url")
        if self.tunnel != "custom" and self.public_url:
            raise ValueError(
                "--public-url is only valid with --tunnel custom"
            )
        if self.public_url:
            parsed = urlsplit(self.public_url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "--public-url must be an HTTPS origin without a path, query, "
                    "fragment, or user information"
                )
        if not self.tools or len(set(self.tools)) != len(self.tools):
            raise ValueError("web bridge tools must be non-empty and unique")
        if len(set(self.mcp_servers)) != len(self.mcp_servers) or not all(
            isinstance(item, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", item)
            for item in self.mcp_servers
        ):
            raise ValueError("web bridge MCP server IDs must be unique safe identifiers")
        object.__setattr__(
            self,
            "tools",
            _include_stable_worker_commands(
                tuple(self.tools),
                access_profile=self.access_profile,
                verification_commands=tuple(self.verification_commands),
            ),
        )
        if (
            self.saved_profile_name is not None
            and self.profile in SELF_RESTART_WEB_PROFILES
            and self.access_profile in _WORKSPACE_WRITE_PROFILES
            and "karox.runtime.restart" not in self.tools
        ):
            # A durable owner can safely recycle only its local MCP child while
            # keeping the public URL, session/keyring identity and managed browser.
            # Ad-hoc/read-only/browser-only bridges do not advertise this power.
            object.__setattr__(self, "tools", (*self.tools, "karox.runtime.restart"))
        if self.browser_external_https:
            if self.access_profile == AccessProfile.READ_ONLY:
                raise ValueError(
                    "external browser mode requires the browser_control, workspace_write, or elevated access profile"
                )
            optional_tools = {
                "karox.browser.network_requests",
                "karox.browser.request_user_takeover",
                "karox.browser.resume_after_user_takeover",
            }
            selected_external = [
                name for name in EXTERNAL_BROWSER_TOOL_NAMES if name not in optional_tools
            ]
            if self.browser_network_inspection:
                selected_external.append("karox.browser.network_requests")
            if self.browser_user_takeover:
                selected_external.extend(
                    (
                        "karox.browser.request_user_takeover",
                        "karox.browser.resume_after_user_takeover",
                    )
                )
            ordered_tools = list(self.tools)
            ordered_tools.extend(name for name in selected_external if name not in ordered_tools)
            object.__setattr__(self, "tools", tuple(ordered_tools))
        # Validate and canonicalize the secret-free browser policy here, before
        # the launcher persists it or passes it to the bridge child.
        browser_policy = BrowserAccessPolicy(
            session_id=self.session_id or "pending-web-bridge-session",
            localhost=True,
            external_https=self.browser_external_https,
            allowed_domains=tuple(self.browser_allowed_domains),
            denied_domains=tuple(self.browser_denied_domains),
            headed=self.browser_headed,
            user_takeover=self.browser_user_takeover,
            network_inspection=self.browser_network_inspection,
            payment_confirmation=self.browser_payment_confirmation,
            allowed_emails=tuple(self.browser_allowed_emails),
            allowed_credential_refs=tuple(self.browser_credential_refs),
            saved_profile_id=self.saved_profile_name or "ad-hoc",
        )
        object.__setattr__(self, "browser_allowed_domains", browser_policy.allowed_domains)
        object.__setattr__(self, "browser_denied_domains", browser_policy.denied_domains)
        object.__setattr__(self, "browser_allowed_emails", browser_policy.allowed_emails)
        object.__setattr__(
            self,
            "browser_credential_refs",
            browser_policy.allowed_credential_refs,
        )
        if "karox.browser.network_requests" in self.tools and not self.browser_network_inspection:
            raise ValueError(
                "karox.browser.network_requests requires --browser-network-inspection"
            )
        takeover_tools = {
            "karox.browser.request_user_takeover",
            "karox.browser.resume_after_user_takeover",
        }
        if takeover_tools.intersection(self.tools) and not self.browser_user_takeover:
            raise ValueError("browser takeover tools require --browser-user-takeover")
        if self.browser_network_inspection and "karox.browser.network_requests" not in self.tools:
            raise ValueError("browser network inspection requires karox.browser.network_requests")
        if self.browser_user_takeover and not {
            "karox.browser.request_user_takeover",
            "karox.browser.resume_after_user_takeover",
        }.issubset(self.tools):
            raise ValueError("browser user takeover tools are not exposed")
        # The tool universe is the union of Core tools and the hosted browser/
        # dev-server/artifact tools.  Both halves are validated against the same
        # ``KNOWN_HOSTED_TOOL_NAMES`` set so a launch cannot select a name that
        # no runtime knows how to serve.
        unknown_tools = sorted(set(self.tools) - set(KNOWN_HOSTED_TOOL_NAMES))
        if unknown_tools:
            raise ValueError(
                "web bridge contains unknown tools: " + ", ".join(unknown_tools)
            )
        # browser.input implies browser.read.  Selecting any input tool (open/
        # click/fill/select/press/close) without the read tools (snapshot/
        # get_text/console/network_failures/screenshot) is a contradictory state:
        # a hosted client could click a button but never snapshot the result, and
        # ``web_bridge_diagnostics`` would report ``read:false, input:true``.  We
        # normalize the bundle here so the read tools are always present whenever
        # an input tool is, which makes the granted capability set (derived from
        # this same bundle in ``CoreToolBridge``) and the diagnostics (derived
        # from this bundle in ``web_bridge_diagnostics``) agree by construction.
        tool_set = set(self.tools)
        if tool_set.intersection(BROWSER_INPUT_TOOL_NAMES):
            missing_read = set(BROWSER_READ_TOOL_NAMES) - tool_set
            if missing_read:
                # Preserve caller order, then append the auto-included read tools
                # in their canonical order so the bundle is deterministic.
                ordered = list(self.tools)
                ordered.extend(
                    name
                    for name in BROWSER_READ_TOOL_NAMES
                    if name in missing_read
                    and (
                        name != "karox.browser.network_requests"
                        or self.browser_network_inspection
                    )
                )
                object.__setattr__(self, "tools", tuple(ordered))
        if any(
            not command
            or len(command) > 100
            or not all(isinstance(item, str) and item for item in command)
            for command in self.verification_commands
        ):
            raise ValueError(
                "web bridge verification commands must contain 1-100 non-empty strings"
            )
        if "karox.checks.run" in self.tools and not self.verification_commands:
            raise ValueError(
                "karox.checks.run requires at least one approved verification command"
            )
        # ``karox.dev_server.start`` can only ever reject without an approved
        # profile, so it is not allowed in the bundle: catch that early with a
        # message that names the fix instead of a runtime denial.  status/logs
        # are read-only and harmless without profiles (they report "not found"),
        # so they are left alone here.
        if "karox.dev_server.start" in self.tools and not self.server_profiles:
            raise ValueError(
                "karox.dev_server.start requires at least one approved server profile"
            )
        seen_profiles: set[str] = set()
        for profile in self.server_profiles:
            if not isinstance(profile, ManagedServerProfile):
                raise ValueError("server profiles must be ManagedServerProfile instances")
            if profile.name in seen_profiles:
                raise ValueError(f"duplicate server profile: {profile.name}")
            seen_profiles.add(profile.name)
        if self.language not in {"en", "ru"}:
            raise ValueError("web bridge language must be en or ru")
        if not 1.0 <= float(self.tunnel_timeout_seconds) <= 300.0:
            raise ValueError("tunnel timeout must be between 1 and 300 seconds")
        if not 0.1 <= float(self.deadline_seconds) <= 3600.0:
            raise ValueError("bridge deadline must be between 0.1 and 3600 seconds")
        # Drop tools the access profile cannot grant -- but only for saved
        # profiles. A saved profile edited by an older UI can hold an
        # impossible combination (dev-server tools on a browser_control
        # profile, write tools on read_only), and one of them used to take the
        # whole bridge down at startup: the detached process dies silently,
        # the TUI reports "started", and the user's ChatGPT connection is dead.
        # The names are kept on the config so diagnostics can report the drop.
        #
        # An explicit command line (`bridge connect --tool ...`) does not get
        # this: the user typed the tool list just now, so the honest answer is
        # the existing fail-closed startup error, not a quiet drop.
        denied = profile_incompatible_tools(self.tools, self.access_profile)
        if denied and self.saved_profile_name is not None:
            object.__setattr__(
                self,
                "profile_denied_tools",
                {**self.profile_denied_tools, **denied},
            )
            # Hyperagent caches a Custom MCP tool catalogue aggressively.  Keep
            # one stable advertised universe for that client and let the runtime
            # policy reject Full-only calls while Project access is active.  A
            # later Full toggle therefore changes permissions, not tools/list.
            # Other hosted clients retain the historical fail-closed projection.
            if self.profile != "hyperagent-web":
                object.__setattr__(
                    self,
                    "tools",
                    tuple(name for name in self.tools if name not in denied),
                )
                if not self.tools:
                    raise ValueError(
                        "web bridge tools are all incompatible with the "
                        f"{self.access_profile.value} access profile: "
                        + ", ".join(sorted(denied))
                    )


@dataclass
class CloudflareQuickTunnel:
    process: subprocess.Popen[str]
    public_url: str
    output_tail: deque[str]
    reader: threading.Thread

    def stop(self) -> None:
        _stop_process(self.process)
        self.reader.join(timeout=2)


@dataclass
class TailscaleForegroundFunnel:
    """A foreground Funnel route owned only by this launcher process."""

    process: subprocess.Popen[str]
    public_url: str
    output_tail: deque[str]
    reader: threading.Thread

    def stop(self) -> None:
        # Never use a global Funnel reset; foreground mode owns this route.
        _stop_process(self.process)
        self.reader.join(timeout=2)


@dataclass
class TailscaleBackgroundFunnel:
    """A daemon-managed Funnel route with explicit KaroX route ownership."""

    executable: str
    public_url: str
    port: int
    https_port: int = 443

    def stop(self) -> None:
        # Background Funnel survives the launcher process by design, so normal
        # shutdown removes only the exact route still proven to point at this
        # KaroX bridge port. A hard-kill leaves a harmless stale route pointing
        # at a closed local port; the next launch classifies and replaces it.
        stop_tailscale_background_funnel(self)


def bundled_cloudflared() -> Path:
    """Where the KaroX installer puts cloudflared when the user accepts it."""
    executable = "cloudflared.exe" if os.name == "nt" else "cloudflared"
    return runtime_dir() / "bin" / executable


def windows_cloudflared_candidates() -> tuple[Path, ...]:
    """Return cloudflared locations used by Windows installers.

    WinGet normally exposes package executables through ``WinGet\\Links``, but
    that directory is not guaranteed to be present in PATH (and on some WinGet
    versions the link is not created at all).  The package itself is still
    installed and executable, so include its stable package directory as a
    fallback instead of telling the user to install an already installed tool.
    """
    if sys.platform != "win32":
        return ()

    candidates: list[Path] = []
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        local = Path(local_app_data)
        candidates.extend(
            (
                local / "Microsoft" / "WinGet" / "Links" / "cloudflared.exe",
                local / "Microsoft" / "WindowsApps" / "cloudflared.exe",
            )
        )
        packages = local / "Microsoft" / "WinGet" / "Packages"
        candidates.extend(
            sorted(packages.glob("Cloudflare.cloudflared_*\\cloudflared.exe"))
        )

    program_files = os.environ.get("ProgramFiles", "").strip()
    if program_files:
        candidates.append(Path(program_files) / "Cloudflare" / "cloudflared.exe")
    return tuple(candidates)


def find_cloudflared(explicit: Optional[str] = None) -> Optional[str]:
    """Resolve cloudflared from explicit, PATH, KaroX, or installer locations."""
    if explicit:
        candidate = Path(explicit).expanduser().resolve()
        return str(candidate) if candidate.is_file() else None
    discovered = shutil.which("cloudflared")
    if discovered:
        return discovered
    bundled = bundled_cloudflared()
    if bundled.is_file():
        return str(bundled)
    for candidate in windows_cloudflared_candidates():
        if candidate.is_file():
            return str(candidate)
    return None


def cloudflared_install_hint() -> str:
    """The way to get cloudflared that exists on *this* platform."""
    if sys.platform == "win32":
        return (
            "install it with `winget install --id Cloudflare.cloudflared` or rerun "
            "the KaroX installer"
        )
    if sys.platform == "darwin":
        return "install it with `brew install cloudflared`"
    return (
        "install your distribution's cloudflared package, or take the binary from "
        "github.com/cloudflare/cloudflared/releases"
    )


def cloudflared_not_found_message(explicit: Optional[str] = None) -> str:
    """Say what was searched, because "not found" alone is not diagnosable.

    The old wording named neither the paths tried nor a remedy that exists off
    Windows: it told every user to rerun an installer, and on macOS and Linux
    there is no KaroX installer to rerun. A report of this failure could not be
    acted on without reproducing it.
    """
    if explicit:
        return (
            "cloudflared was not found at the path given with --cloudflared: "
            f"{explicit}"
        )
    return (
        "cloudflared was not found. Searched PATH and "
        f"{bundled_cloudflared()}. To fix it, {cloudflared_install_hint()}, "
        "or pass --cloudflared PATH."
    )


def _creation_flags() -> int:
    if os.name != "nt":
        return 0
    # The bridge child must not share the launcher's console-control process
    # group. A CTRL_C/CTRL_BREAK used to cancel a hosted request must never be
    # interpreted as bridge shutdown.
    return int(
        getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    )


def parent_death_hook() -> Optional[Callable[[], None]]:
    """Return a pre-exec hook that has the kernel kill the child with us.

    Windows uses a job object instead (see :func:`_create_child_job`). On Linux
    ``PR_SET_PDEATHSIG`` is the equivalent: a hard kill of the launcher never
    runs the cleanup path, and an orphaned cloudflared keeps a public
    ``*.trycloudflare.com`` URL pointed at an authenticated bridge.

    Linux is the only POSIX target where KaroX both has such a primitive and can
    verify it here. macOS has none that a parent can set on a child it does not
    control, and the FreeBSD analogue (``procctl PROC_PDEATHSIG_CTL``) is
    untested, so those platforms fall back to the watchdog record and the orphan
    reaper rather than pretending to hold the child down.
    """
    if os.name == "nt" or not sys.platform.startswith("linux"):
        return None
    import ctypes

    try:
        # Resolved in the parent: loading a library between fork and exec in a
        # process that has threads can deadlock, and this one always does.
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
    except OSError:
        return None

    def hook() -> None:
        libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)

    return hook


def _child_options() -> dict[str, Any]:
    """Popen options that put a child in its own reapable process group."""
    options: dict[str, Any] = {
        "creationflags": _creation_flags(),
        # POSIX only: the group leader is the child itself, so the whole tree it
        # spawns can be signalled by one killpg instead of leaking behind it.
        "start_new_session": True,
    }
    hook = parent_death_hook()
    if hook is not None:
        # subprocess runs this after its own setsid, and the kernel keeps the
        # setting across the following execve.
        options["preexec_fn"] = hook
    return options


def _pid_of(process: Optional[object]) -> Optional[int]:
    pid = getattr(process, "pid", None)
    return pid if isinstance(pid, int) and pid > 0 else None


def _create_child_job() -> Optional[int]:
    """Open a Windows job whose closure kills everything assigned to it.

    A hard kill of the launcher (``taskkill /F``, a closed console) never runs
    the cleanup path, and an orphaned cloudflared keeps a public
    ``*.trycloudflare.com`` URL pointed at an authenticated bridge. The job
    handle dies with this process and Windows then terminates the children,
    which is the only kill-on-parent-death primitive available here.
    """
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    limits = _ExtendedLimits()
    # The bridge and tunnel remain kill-on-close children. The dedicated KaroX
    # Chrome profile is the one intentional exception: extension_browser starts
    # it with CREATE_BREAKAWAY_FROM_JOB so a bridge restart does not destroy the
    # user's open browser work. Nothing else escapes unless it explicitly asks.
    limits.BasicLimitInformation.LimitFlags = (
        _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | _JOB_OBJECT_LIMIT_BREAKAWAY_OK
    )
    ok = kernel32.SetInformationJobObject(
        wintypes.HANDLE(job),
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    )
    if not ok:
        kernel32.CloseHandle(wintypes.HANDLE(job))
        return None
    return int(job)


def _adopt_child(job: Optional[int], process: Optional[object]) -> bool:
    """Assign a child to the kill-on-close job; POSIX relies on its group."""
    pid = _pid_of(process)
    if job is None or pid is None or os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(
        _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid
    )
    if not handle:
        return False
    try:
        return bool(
            kernel32.AssignProcessToJobObject(
                wintypes.HANDLE(job), wintypes.HANDLE(handle)
            )
        )
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(handle))


def _close_job(job: Optional[int]) -> None:
    if job is None or os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(wintypes.HANDLE(job))


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill on Windows terminates the target even for signal 0, so
        # liveness has to be asked for rather than probed with a signal.
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(
                wintypes.HANDLE(handle), ctypes.byref(code)
            )
            return bool(ok) and code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(handle))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _signal_process_group(process: Optional[object], *, hard: bool) -> None:
    pid = _pid_of(process)
    if pid is None or os.name == "nt":
        return
    try:
        os.killpg(
            os.getpgid(pid),
            signal.SIGKILL if hard else signal.SIGTERM,
        )
    except (OSError, AttributeError):
        pass


def _close_child_pipes(process: subprocess.Popen[str]) -> None:
    """Release a stopped child's piped handles instead of leaving them to GC.

    ``stdout``/``stderr`` normally belong to the drain thread, which closes them
    at EOF; closing here as well is idempotent and covers children that were
    never mirrored. ``stdin`` has no reader thread, so this is its only owner.
    """
    # ``getattr`` rather than attribute access: launch tests exercise these
    # paths with minimal fake processes that only model the pieces they need.
    for stream in (
        getattr(process, "stdin", None),
        getattr(process, "stdout", None),
        getattr(process, "stderr", None),
    ):
        if stream is None:
            continue
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _stop_process(process: Optional[subprocess.Popen[str]]) -> None:
    if process is None:
        return
    if process.poll() is not None:
        _close_child_pipes(process)
        return
    try:
        _signal_process_group(process, hard=False)
        process.terminate()
        process.wait(timeout=5)
    except (OSError, subprocess.SubprocessError):
        try:
            _signal_process_group(process, hard=True)
            process.kill()
            process.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass
    finally:
        _close_child_pipes(process)


def _tail_detail(output_tail: deque[str]) -> str:
    """Format the last thing a child said as a suffix for an error message."""
    detail = next((line for line in reversed(output_tail) if line), "")
    return f": {detail}" if detail else ""


@dataclass(frozen=True)
class MirroredChildOutput:
    """A child's recent output and the thread still draining it."""

    tail: deque[str]
    reader: threading.Thread

    def detail(self) -> str:
        """The last line the child printed, as a suffix for an error message.

        The drain thread is joined first. A child that fails on startup exits
        while its final lines are still in the pipe, so reading the tail straight
        away reports the reason as absent in exactly the case it is needed.
        """
        self.reader.join(timeout=2.0)
        return _tail_detail(self.tail)


def _mirror_child_output(
    process: subprocess.Popen[str],
    *,
    name: str,
) -> MirroredChildOutput:
    """Echo a child's merged output to this console and keep its last lines.

    Windows starts these children with ``CREATE_NO_WINDOW``, and a child started
    that way without redirected handles is given its own hidden console: whatever
    it prints goes to a window nobody can see. So the traceback explaining that a
    port was taken, or a dependency was missing, was discarded and the only thing
    reaching the user was ``exited with code 2``. Draining the pipe is what turns
    that number back into a reason.
    """
    output_tail: deque[str] = deque(maxlen=30)

    def drain() -> None:
        stream = process.stdout
        if stream is None:
            return
        # The reader owns the read end: it closes the wrapper at EOF so a
        # stopped child never leaves an unclosed TextIOWrapper behind for the
        # garbage collector to warn about. A concurrent _stop_process() may
        # close the stream first; that reads as EOF/ValueError here and both
        # are a normal shutdown, not an error.
        try:
            for raw_line in stream:
                line = raw_line.rstrip()
                output_tail.append(line)
                print(f"[{name}] {line}", flush=True)
        except ValueError:
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    reader = threading.Thread(
        target=drain,
        name=f"karox-{name}-output",
        daemon=True,
    )
    reader.start()
    return MirroredChildOutput(output_tail, reader)


def start_cloudflare_quick_tunnel(
    port: int,
    *,
    executable: Optional[str] = None,
    timeout_seconds: float = 30.0,
    popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    job: Optional[int] = None,
) -> CloudflareQuickTunnel:
    """Start cloudflared, parse its assigned HTTPS origin, and keep draining logs."""
    resolved = find_cloudflared(executable)
    if resolved is None:
        raise WebBridgeLaunchError(cloudflared_not_found_message(executable))
    try:
        process = popen(
            [
                resolved,
                "tunnel",
                "--url",
                f"http://127.0.0.1:{port}",
                "--no-autoupdate",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            **_child_options(),
        )
    except OSError as exc:
        raise WebBridgeLaunchError(
            f"cannot start cloudflared: {type(exc).__name__}"
        ) from exc
    _adopt_child(job, process)

    ready = threading.Event()
    state: dict[str, Optional[str]] = {"public_url": None}
    output_tail: deque[str] = deque(maxlen=30)

    def drain() -> None:
        stream = process.stdout
        if stream is None:
            ready.set()
            return
        # Reader owns the read end: close it at EOF so the stopped tunnel
        # child never leaves an unclosed TextIOWrapper for the GC to report.
        try:
            for raw_line in stream:
                line = raw_line.rstrip()
                output_tail.append(line)
                match = _QUICK_TUNNEL_URL.search(line)
                if match and state["public_url"] is None:
                    state["public_url"] = match.group(0)
                    ready.set()
        except ValueError:
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass
            ready.set()

    reader = threading.Thread(
        target=drain,
        name="karox-cloudflared-output",
        daemon=True,
    )
    reader.start()
    ready.wait(timeout_seconds)
    public_url = state["public_url"]
    if public_url is None:
        code = process.poll()
        _stop_process(process)
        reader.join(timeout=2)
        suffix = _tail_detail(output_tail)
        if code is None:
            raise WebBridgeLaunchError(
                f"cloudflared did not provide a public URL within {timeout_seconds:g}s"
                f"{suffix}"
            )
        raise WebBridgeLaunchError(f"cloudflared exited with code {code}{suffix}")
    return CloudflareQuickTunnel(process, public_url, output_tail, reader)


def start_tailscale_foreground_funnel(
    port: int,
    *,
    executable: Optional[str] = None,
    timeout_seconds: float = 30.0,
    popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    job: Optional[int] = None,
    emit: Optional[Callable[[str], None]] = None,
    restart_service: bool = False,
    elevated_run: Optional[Callable[..., subprocess.CompletedProcess[str]]] = None,
) -> TailscaleForegroundFunnel:
    """Start a stable Funnel without replacing or globally resetting routes.

    ``emit`` receives human-readable progress while Tailscale is brought online
    (see :func:`karox.tailscale.ensure_tailscale_ready`); the bridge launcher
    passes a flushed ``print`` so a user who confirmed ``bridge connect`` sees
    that the daemon is being started rather than a silent hang. ``restart_service``
    is deliberately off by default because the Windows Tailscale service is shared
    by every local Funnel: one bridge must never drop unrelated live bridges while
    repairing itself. A global service restart remains an explicit user action.
    """
    try:
        plan = prepare_tailscale_funnel(
            port,
            executable=executable,
            emit=emit,
            restart_service=restart_service,
            elevated_run=elevated_run,
        )
    except TailscaleError as exc:
        raise WebBridgeLaunchError(str(exc)) from exc
    try:
        process = popen(
            list(plan.argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            **_child_options(),
        )
    except OSError as exc:
        raise WebBridgeLaunchError(
            f"cannot start Tailscale Funnel: {type(exc).__name__}"
        ) from exc
    _adopt_child(job, process)
    ready = threading.Event()
    confirmed = {"public_url": False}
    output_tail: deque[str] = deque(maxlen=30)

    def drain() -> None:
        stream = process.stdout
        if stream is None:
            ready.set()
            return
        # Reader owns the read end: close it at EOF so a stopped funnel child
        # never leaves an unclosed TextIOWrapper for the GC to report.
        try:
            for raw_line in stream:
                line = raw_line.rstrip()
                output_tail.append(line)
                print(f"[tailscale] {line}", flush=True)
                if plan.public_url in line or ".ts.net" in line:
                    confirmed["public_url"] = True
                    ready.set()
        except ValueError:
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass
            ready.set()

    reader = threading.Thread(
        target=drain,
        name="karox-tailscale-funnel-output",
        daemon=True,
    )
    reader.start()
    # The stable hostname comes from authenticated status, but publication is
    # ready only after the foreground command echoes the public URL.
    ready.wait(float(timeout_seconds))
    code = process.poll()
    if code is not None:
        reader.join(timeout=2)
        detail = "\n".join(output_tail)
        failure = classify_funnel_failure(detail)
        raise WebBridgeLaunchError(f"{failure['code']}: {failure['detail']}")
    if not confirmed["public_url"]:
        _stop_process(process)
        reader.join(timeout=2)
        raise WebBridgeLaunchError(
            "Tailscale Funnel did not confirm its stable public URL within "
            f"{timeout_seconds:g}s{_tail_detail(output_tail)}"
        )
    return TailscaleForegroundFunnel(
        process=process,
        public_url=plan.public_url,
        output_tail=output_tail,
        reader=reader,
    )


def recover_tailscale_foreground_funnel(
    tunnel: TailscaleForegroundFunnel,
    *,
    port: int,
    expected_public_url: str,
    executable: Optional[str] = None,
    timeout_seconds: float = 30.0,
    job: Optional[int] = None,
    emit: Optional[Callable[[str], None]] = None,
) -> TailscaleForegroundFunnel:
    """Recycle only the foreground Funnel route owned by this launcher.

    A live ``tailscale funnel`` process is not sufficient evidence that its
    public ingress is still reachable. Recovery deliberately keeps the local
    bridge, OAuth credential, and repository session alive: only the owned
    foreground Funnel child is replaced. The replacement must resolve to the
    same stable Tailscale hostname or the recovery is refused.

    Service restart is disabled here. A background health loop must not trigger
    an unexpected UAC prompt; the explicit ``bridge connect`` startup path keeps
    the stronger service-repair behavior.
    """
    tunnel.stop()
    replacement = start_tailscale_foreground_funnel(
        port,
        executable=executable,
        timeout_seconds=timeout_seconds,
        job=job,
        emit=emit,
        restart_service=False,
    )
    if replacement.public_url.rstrip("/") != expected_public_url.rstrip("/"):
        replacement.stop()
        raise WebBridgeLaunchError(
            "Tailscale route recovery changed the public hostname; refusing "
            "to keep the existing OAuth bridge on a different origin"
        )
    return replacement


def _background_funnel_argv(
    executable: str, port: int, *, https_port: int = 443
) -> list[str]:
    return [
        executable,
        "funnel",
        "--bg",
        "--yes",
        f"--https={https_port}",
        f"http://127.0.0.1:{port}",
    ]


def _matching_background_funnel_routes(
    executable: str,
    *,
    public_url: str,
    port: int,
    https_port: int = 443,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[list[Any], list[Any]]:
    from .tailscale_routes import inventory_tailscale_routes

    host = urlsplit(public_url).hostname or ""
    routes = inventory_tailscale_routes(executable, run=run)
    same_listener = [
        route
        for route in routes
        if route.public_port == https_port and (not route.host or route.host == host)
    ]
    owned = [route for route in same_listener if route.local_port == port]
    foreign = [route for route in same_listener if route.local_port != port]
    return owned, foreign


def _apply_tailscale_background_funnel(
    executable: str,
    *,
    public_url: str,
    port: int,
    https_port: int = 443,
    timeout_seconds: float,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    kwargs: dict[str, Any] = {
        "check": False,
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": max(1.0, float(timeout_seconds)),
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = run(
            _background_funnel_argv(executable, port, https_port=https_port),
            **kwargs,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WebBridgeLaunchError(
            f"cannot configure background Tailscale Funnel: {type(exc).__name__}"
        ) from exc
    detail = "\n".join(
        value.strip()
        for value in (result.stdout or "", result.stderr or "")
        if value and value.strip()
    )
    if result.returncode != 0:
        failure = classify_funnel_failure(detail)
        raise WebBridgeLaunchError(f"{failure['code']}: {failure['detail']}")

    # `--bg` stores configuration in tailscaled. Do not trust the CLI exit code
    # alone: verify that the daemon reports the exact host/port route KaroX owns.
    deadline = time.monotonic() + min(max(float(timeout_seconds), 1.0), 15.0)
    while time.monotonic() < deadline:
        owned, foreign = _matching_background_funnel_routes(
            executable,
            public_url=public_url,
            port=port,
            https_port=https_port,
            run=run,
        )
        if foreign:
            raise WebBridgeLaunchError(
                "background Funnel configuration introduced or encountered a foreign "
                "Tailscale route; refusing to claim ownership"
            )
        if owned:
            return
        time.sleep(0.2)
    raise WebBridgeLaunchError(
        "background Tailscale Funnel was accepted but the owned route did not "
        "appear in Tailscale status"
    )


def start_tailscale_background_funnel(
    port: int,
    *,
    https_port: int = 443,
    executable: Optional[str] = None,
    timeout_seconds: float = 30.0,
    emit: Optional[Callable[[str], None]] = None,
    restart_service: bool = False,
    elevated_run: Optional[Callable[..., subprocess.CompletedProcess[str]]] = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> TailscaleBackgroundFunnel:
    """Create a daemon-managed, persistent Funnel with proven route ownership."""
    try:
        plan = prepare_tailscale_funnel(
            port,
            https_port=https_port,
            executable=executable,
            run=run,
            emit=emit,
            restart_service=restart_service,
            elevated_run=elevated_run,
        )
    except TailscaleError as exc:
        raise WebBridgeLaunchError(str(exc)) from exc
    _apply_tailscale_background_funnel(
        plan.executable,
        public_url=plan.public_url,
        port=port,
        https_port=https_port,
        timeout_seconds=timeout_seconds,
        run=run,
    )
    if emit is not None:
        emit(f"Background Funnel is ready: {plan.public_url}")
    return TailscaleBackgroundFunnel(
        plan.executable, plan.public_url, port, https_port=https_port
    )


def refresh_tailscale_background_funnel(
    tunnel: TailscaleBackgroundFunnel,
    *,
    timeout_seconds: float = 30.0,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> TailscaleBackgroundFunnel:
    """Reapply only the daemon route already proven to belong to this bridge."""
    owned, foreign = _matching_background_funnel_routes(
        tunnel.executable,
        public_url=tunnel.public_url,
        port=tunnel.port,
        https_port=tunnel.https_port,
        run=run,
    )
    if foreign:
        raise WebBridgeLaunchError(
            "Tailscale route recovery found a foreign route; refusing to mutate Funnel"
        )
    # If the route disappeared entirely, reapplying is also safe: its hostname and
    # local target are fixed by the already-proven Tailscale identity and bridge port.
    _apply_tailscale_background_funnel(
        tunnel.executable,
        public_url=tunnel.public_url,
        port=tunnel.port,
        https_port=tunnel.https_port,
        timeout_seconds=timeout_seconds,
        run=run,
    )
    return tunnel


def stop_tailscale_background_funnel(
    tunnel: TailscaleBackgroundFunnel,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> bool:
    """Turn off only the exact background Funnel route still owned by KaroX."""
    owned, foreign = _matching_background_funnel_routes(
        tunnel.executable,
        public_url=tunnel.public_url,
        port=tunnel.port,
        https_port=tunnel.https_port,
        run=run,
    )
    if not owned:
        return True
    if foreign:
        # Fail closed during cleanup. Leaving a stale proxy to a closed localhost
        # port is safer than deleting another program's Tailscale route.
        return False
    kwargs: dict[str, Any] = {
        "check": False,
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": 15,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = run(
            [
                tunnel.executable,
                "funnel",
                f"--https={tunnel.https_port}",
                f"http://127.0.0.1:{tunnel.port}",
                "off",
            ],
            **kwargs,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    remaining, _foreign = _matching_background_funnel_routes(
        tunnel.executable,
        public_url=tunnel.public_url,
        port=tunnel.port,
        https_port=tunnel.https_port,
        run=run,
    )
    return not remaining


def _port_is_available(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", port))
    except OSError:
        return False
    return True


def _listener_belongs_to_process_tree(port: int, root_pid: int) -> Optional[bool]:
    """Best-effort proof that the local listener belongs to ``root_pid``.

    ``None`` means the platform cannot prove ownership (for example psutil is not
    installed or access is denied).  ``False`` is meaningful: a different live
    process owns the port, so readiness must not accept that listener as proof
    that the child we just spawned is healthy.
    """
    try:
        import psutil  # type: ignore[import-untyped]
    except ImportError:
        return None
    try:
        holder_pid: Optional[int] = None
        for connection in psutil.net_connections(kind="tcp"):
            if connection.laddr and connection.laddr.port == port:
                holder_pid = connection.pid
                break
        if not isinstance(holder_pid, int) or holder_pid <= 0:
            return None
        if holder_pid == root_pid:
            return True
        process = psutil.Process(holder_pid)
        return any(parent.pid == root_pid for parent in process.parents())
    except Exception:
        return None


def _wait_for_local_port_release(
    port: int,
    *,
    timeout_seconds: float = _CHILD_PORT_RELEASE_TIMEOUT_SECONDS,
) -> bool:
    """Wait until nothing accepts on ``port`` again, bounded.

    A child that has just exited can still hold its listener for a moment. The
    replacement then loses the bind and dies immediately, which used to look like
    a crash loop. Returning ``False`` means the port never came free; the caller
    still tries, because a stale probe must not block a recovery outright.
    """
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while time.monotonic() < deadline:
        if _port_is_available(port):
            return True
        time.sleep(0.1)
    return _port_is_available(port)


def _wait_for_bridge(
    process: subprocess.Popen[str],
    port: int,
    *,
    timeout_seconds: float = 15.0,
    output: Optional[MirroredChildOutput] = None,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        code = process.poll()
        if code is not None:
            suffix = output.detail() if output is not None else ""
            raise WebBridgeLaunchError(
                f"KaroX bridge exited with code {code}{suffix}"
            )
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                ownership = _listener_belongs_to_process_tree(port, process.pid)
                if ownership is False:
                    # A pre-existing listener can make a freshly spawned bridge
                    # look ready even though that child is about to fail bind()
                    # with WinError 10048. Keep waiting for *this* process to own
                    # the port (or to exit) instead of blessing the wrong PID.
                    time.sleep(0.05)
                    continue
                # When ownership cannot be proven, give a fast bind failure one
                # scheduling turn to surface before accepting the connected port.
                time.sleep(0.1)
                code = process.poll()
                if code is not None:
                    suffix = output.detail() if output is not None else ""
                    raise WebBridgeLaunchError(
                        f"KaroX bridge exited with code {code}{suffix}"
                    )
                return
        except OSError:
            time.sleep(0.05)
    suffix = _tail_detail(output.tail) if output is not None else ""
    raise WebBridgeLaunchError(f"KaroX bridge did not open its local port{suffix}")


def _wait_for_public_mcp_route(
    process: subprocess.Popen[str],
    public_url: str,
    *,
    path: str = "/mcp",
    timeout_seconds: float = 15.0,
    output: Optional[MirroredChildOutput] = None,
) -> None:
    """Wait until the public MCP ingress reaches the local bridge auth layer."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        code = process.poll()
        if code is not None:
            suffix = output.detail() if output is not None else ""
            raise WebBridgeLaunchError(
                f"KaroX bridge exited with code {code}{suffix}"
            )
        remaining = max(0.1, deadline - time.monotonic())
        if public_mcp_route_healthy(
            public_url,
            path=path,
            timeout_seconds=min(1.0, remaining),
        ):
            return
        time.sleep(0.2)
    suffix = _tail_detail(output.tail) if output is not None else ""
    raise WebBridgeLaunchError(
        "Tailscale Funnel did not make the public MCP route reachable before "
        f"the bridge readiness deadline{suffix}"
    )


def watchdog_dir() -> Path:
    return runtime_dir() / "web-bridge"


def _saved_bridge_owner_lock_path(profile_name: str) -> Path:
    digest = hashlib.sha256(profile_name.encode("utf-8")).hexdigest()[:24]
    return watchdog_dir() / "owner-locks" / f"{digest}.lock"


def _saved_bridge_identity_is_exclusive(profile_name: Optional[str]) -> bool:
    """True when no *other* live process claims ``profile_name`` as its owner.

    Used before discarding a durable credential. The answer must be conservative:
    an unprovable platform (no psutil) returns ``False`` so the shared connector
    identity is kept rather than destroyed on a guess.
    """
    if not isinstance(profile_name, str) or not profile_name:
        return False
    try:
        import psutil  # type: ignore[import-untyped]

        from .port_ownership import prove_saved_bridge_owner_identity
    except ImportError:
        return False
    own_pid = os.getpid()
    try:
        pids = list(psutil.pids())
    except Exception:
        return False
    for pid in pids:
        if not isinstance(pid, int) or pid == own_pid:
            continue
        try:
            if prove_saved_bridge_owner_identity(pid, profile_name):
                return False
        except Exception:
            # An unreadable process is not proof of absence.
            return False
    return True


def _try_acquire_saved_bridge_owner_lock(profile_name: str) -> Optional[Any]:
    """Acquire a lifetime single-owner lock for one saved bridge profile.

    The lock is held by the durable owner process itself, not by the TUI or the
    short-lived start/repair caller. OS file locks disappear automatically when
    the owner dies, so a crash cannot permanently brick the profile. Returning
    ``None`` means another owner already holds the lock.
    """
    path = _saved_bridge_owner_lock_path(profile_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def _release_saved_bridge_owner_lock(handle: Optional[Any]) -> None:
    if handle is None:
        return
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        try:
            handle.close()
        except OSError:
            pass


def _stop_request_path(session_id: str) -> Path:
    return watchdog_dir() / f"{session_id}.stop.json"


def _write_stop_request(session_id: str, owner_pid: int) -> Path:
    """Ask the saved-bridge supervisor to exit through its own finally-block."""

    root = watchdog_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = _stop_request_path(session_id)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            {"owner_pid": owner_pid, "requested_at": time.time()},
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def _consume_stop_request(session_id: str, owner_pid: int) -> bool:
    """Consume only a stop request addressed to this exact supervisor PID."""

    path = _stop_request_path(session_id)
    if not path.exists():
        return False
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = None
    addressed = isinstance(raw, dict) and raw.get("owner_pid") == owner_pid
    if not addressed:
        # Multiple owners should never exist, but older builds could race two
        # saved-bridge launchers. A stop request is addressed to one exact owner;
        # a different owner must not steal/delete it before the target sees it.
        # Malformed requests are the only safe exception because nobody can ever
        # consume them successfully.
        if raw is None:
            try:
                path.unlink()
            except OSError:
                pass
        return False
    try:
        path.unlink()
    except OSError:
        pass
    return True


def _current_process_is_descendant_of(ancestor_pid: int) -> Optional[bool]:
    """Return whether this caller lives inside ``ancestor_pid``'s process tree.

    ``None`` means the relationship cannot be proven. Callers use that as the
    fail-closed answer: a broad Windows tree kill is allowed only when we can
    prove the restart caller is *outside* the bridge tree.
    """

    if ancestor_pid <= 0:
        return None
    if os.getpid() == ancestor_pid:
        return True
    try:
        import psutil  # type: ignore[import-untyped]
    except ImportError:
        return None
    try:
        process = psutil.Process(os.getpid())
        seen: set[int] = set()
        while True:
            parent_pid = int(process.ppid())
            if parent_pid == ancestor_pid:
                return True
            if parent_pid <= 0 or parent_pid in seen:
                return False
            seen.add(parent_pid)
            process = psutil.Process(parent_pid)
    except (psutil.Error, OSError, ValueError):
        return None


def _watchdog_supports_stop_request(path_value: Optional[str]) -> bool:
    if not path_value:
        return False
    try:
        raw = json.loads(Path(path_value).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(raw, dict) and raw.get("stop_protocol") == "request-v1"


# How long the port may take to come back after the orphaned listener exits.
_ORPHAN_RECLAIM_TIMEOUT_SECONDS = 10.0

# How long a proven live owner is given to honour a cooperative stop request
# before its proven tree is force-stopped. Short on purpose: this path only runs
# for an owner that has already lost the record that made it manageable.
_UNRECORDED_OWNER_GRACEFUL_SECONDS = 6.0


def _reclaim_orphaned_bridge_listener(pid: int, *, port: int, session_id: str) -> bool:
    """Free ``port`` from this profile's own orphaned ``bridge serve`` child.

    An owner that dies can leave its local MCP child listening. The child is
    ours, so the port may be reclaimed -- but only after the process proves, from
    its own command line, that it is still that child. Re-proving here (rather
    than trusting the earlier verdict) closes the PID-reuse window between the
    ownership check and this call, and a foreign holder can never reach the
    terminate step. Only the proven PID is stopped: no descendant tree kill.

    Returns True only when the port is actually free again.
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    if pid == os.getpid():
        # Never terminate the caller; a bridge asked to reclaim itself is a bug.
        return False
    from .port_ownership import prove_bridge_process_identity

    if prove_bridge_process_identity(pid, (session_id,)) is None:
        return False
    if not _process_is_alive(pid):
        return _port_is_available(port)
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                capture_output=True,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            os.kill(pid, signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        return False
    deadline = time.monotonic() + _ORPHAN_RECLAIM_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not _process_is_alive(pid) and _port_is_available(port):
            return True
        time.sleep(0.2)
    return False


def _recycle_unrecorded_saved_bridge_owner(
    owner_pid: int, *, profile_name: str, port: int
) -> bool:
    """Stop a proven live owner of ``profile_name`` that lost its watchdog record.

    Such an owner cannot be managed: nothing on disk names its port, URL or
    child, ``stop`` reports ``no_action``, and its self-repair loop keeps
    rebinding the port so every newly spawned owner dies on the bind. Recycling
    it is the only way back to a manageable bridge.

    Safety: the PID must prove from its own argv that it is
    ``bridge connect --saved <profile_name>`` before anything is signalled, and
    only that proven tree is stopped -- never a broad or name-based kill. Session
    identity, keyring credential, OAuth grants and public hostname all derive
    from the saved profile name, so recycling the process preserves them.

    Returns True only when the port is actually free again.
    """
    if not isinstance(owner_pid, int) or owner_pid <= 0:
        return False
    if owner_pid == os.getpid():
        return False
    from .port_ownership import prove_saved_bridge_owner_identity

    if not prove_saved_bridge_owner_identity(owner_pid, profile_name):
        return False
    if not _process_is_alive(owner_pid):
        return _port_is_available(port)
    # Ask first: a cooperative owner runs its own cleanup (tunnel, watchdog,
    # child) instead of leaving another orphan behind. The request is addressed
    # to this exact proven owner, and every session id this profile can legally
    # serve is offered because a lost record also hides which one it chose.
    requested: list[str] = []
    for candidate in saved_web_bridge_session_candidates(profile_name):
        try:
            _write_stop_request(candidate, owner_pid)
            requested.append(candidate)
        except OSError:
            pass
    try:
        graceful_deadline = time.monotonic() + _UNRECORDED_OWNER_GRACEFUL_SECONDS
        while time.monotonic() < graceful_deadline:
            if not _process_is_alive(owner_pid) and _port_is_available(port):
                return True
            time.sleep(0.2)
    finally:
        # An unconsumed request must not outlive this attempt: the next owner
        # would read a stop addressed to a PID that no longer exists.
        for candidate in requested:
            try:
                _stop_request_path(candidate).unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
    try:
        if os.name == "nt":
            # /T covers the owner's own proven bridge child; the tree root is
            # the process that just proved it owns this saved profile.
            subprocess.run(
                ["taskkill", "/PID", str(owner_pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            os.kill(owner_pid, signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        return False
    deadline = time.monotonic() + _ORPHAN_RECLAIM_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not _process_is_alive(owner_pid) and _port_is_available(port):
            return True
        time.sleep(0.2)
    return False


def discover_saved_bridge_profiles() -> list[dict[str, Any]]:
    """List all saved bridge profile configs with their target profile.

    Reads every ``web-saved-*.json`` in the bridge dir and returns a list of
    dicts with ``saved_profile``, ``profile`` (target), ``session_id``,
    ``public_url``, ``tunnel``, and ``bridge_pid``. Used by the Connection Hub
    to discover existing ChatGPT Web / Claude Web connections that were
    launched but never registered in ``connections.json``.

    Secrets are never included in the returned dicts.
    """
    bridge_dir = watchdog_dir()
    results: list[dict[str, Any]] = []
    if bridge_dir.exists():
        for path in bridge_dir.glob("web-saved-*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            entry: dict[str, Any] = {
                "saved_profile": str(data.get("saved_profile") or ""),
                "profile": str(data.get("profile") or ""),
                "session_id": str(data.get("session_id") or ""),
                "repository": str(data.get("repository") or ""),
                "public_url": str(data.get("public_url") or ""),
                "tunnel": str(data.get("tunnel") or ""),
                "bridge_pid": data.get("bridge_pid"),
                "tunnel_pid": data.get("tunnel_pid"),
                "persistent_session": bool(data.get("persistent_session")),
                "started_at": data.get("started_at"),
                "config_path": str(path),
            }
            # Never include secrets.
            for k in ("credential", "token", "secret", "bridge_credential"):
                entry.pop(k, None)
            results.append(entry)

    # Active-process fallback is intentionally scoped to KaroX's canonical
    # runtime directory.  Callers that explicitly redirect ``watchdog_dir``
    # (tests, migration probes, alternate stores) are asking for discovery in
    # that directory only; mixing the machine's live saved profiles into that
    # result breaks isolation and can leak unrelated connection metadata.
    try:
        canonical_bridge_dir = (runtime_dir() / "web-bridge").resolve(strict=False)
        selected_bridge_dir = bridge_dir.resolve(strict=False)
    except OSError:
        canonical_bridge_dir = runtime_dir() / "web-bridge"
        selected_bridge_dir = bridge_dir
    if selected_bridge_dir != canonical_bridge_dir:
        return results

    # Also discover running bridges whose canonical watchdog file might be
    # missing while the saved profile and proven listening process still exist.
    known_sessions = {p["session_id"] for p in results if p.get("session_id")}
    try:
        from .web_bridge_profiles import WebBridgeProfileStore
        from .port_ownership import (
            _port_has_listener,
            _port_owning_pid,
            prove_bridge_process_identity,
            extract_bridge_process_info,
        )

        for saved in WebBridgeProfileStore().list():
            candidates = tuple(saved_web_bridge_session_candidates(saved.name))
            if any(c in known_sessions for c in candidates):
                continue
            if _port_has_listener(saved.port):
                holder_pid = _port_owning_pid(saved.port)
                if isinstance(holder_pid, int) and holder_pid > 0:
                    matched_session = prove_bridge_process_identity(holder_pid, candidates)
                    if matched_session is not None:
                        proc_info = extract_bridge_process_info(holder_pid) or {}
                        pub_url = proc_info.get("public_url") or saved.public_url or ""
                        results.append(
                            {
                                "saved_profile": saved.name,
                                "profile": saved.target_profile,
                                "session_id": matched_session,
                                "repository": str(saved.repository or ""),
                                "public_url": pub_url,
                                "tunnel": saved.tunnel,
                                "bridge_pid": holder_pid,
                                "tunnel_pid": None,
                                "persistent_session": True,
                                "started_at": None,
                                "config_path": "",
                            }
                        )
                        known_sessions.add(matched_session)
    except Exception:
        pass

    return results


def discover_saved_bridge_profiles_for_preset(preset_id: str) -> list[dict[str, Any]]:
    """Find saved bridge profiles whose target profile matches a preset.

    For ChatGPT Web (``preset_id="chatgpt-web"``), this finds bridge configs
    where ``profile == "chatgpt-web"`` — even if the saved profile name is
    ``clickup-opus`` and no connection record exists in the registry.
    """
    # Map preset_id to the bridge config ``profile`` value.
    profile_map = {
        "chatgpt-web": "chatgpt-web",
        "claude-web": "claude-web",
        "hyperagent-web": "hyperagent-web",
        "clickup": "generic-streamable-http",
        "notion": "notion",
        "promptql": "promptql",
    }
    target_profile = profile_map.get(preset_id, preset_id)
    return [
        p for p in discover_saved_bridge_profiles()
        if p.get("profile") == target_profile
    ]


def write_watchdog(path: Path, payload: dict[str, Any]) -> None:
    """Atomically record bridge ownership/health without ever exposing secrets."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        temporary.write_text(rendered, encoding="utf-8")
        # Windows denies the rename while another reader still holds the record
        # open (a peer owner, the supervisor, an indexer, or antivirus). That is
        # transient contention, not a broken bridge, so retry briefly instead of
        # letting the owner die over a bookkeeping write.
        deadline = time.monotonic() + _WATCHDOG_REPLACE_TIMEOUT_SECONDS
        delay = _WATCHDOG_REPLACE_INITIAL_DELAY_SECONDS
        while True:
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 2.0, _WATCHDOG_REPLACE_MAX_DELAY_SECONDS)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


_OWNER_EXIT_RECORD_SUFFIX = ".last-exit.json"


def owner_exit_record_path(session_id: str) -> Path:
    """Where the last stop/crash reason of one durable owner is kept."""
    return watchdog_dir() / f"{session_id}{_OWNER_EXIT_RECORD_SUFFIX}"


def _record_owner_exit(
    session_id: str,
    *,
    saved_profile: Optional[str],
    reason: str,
    detail: str = "",
) -> None:
    """Persist why an owner stopped, outside the record it deletes on the way out.

    Best effort by construction: a bridge that is already going down must not
    fail harder because its post-mortem could not be written.
    """
    payload = {
        "session_id": session_id,
        "saved_profile": saved_profile,
        "reason": str(reason)[:120],
        "detail": str(detail)[:500],
        "owner_pid": os.getpid(),
        "recorded_at": time.time(),
    }
    try:
        path = owner_exit_record_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except Exception:
        pass


def ephemeral_url_warning(
    profile_name: str,
    public_url: Optional[str],
    language: Optional[str] = None,
    tunnel: str = "cloudflare",
) -> Optional[str]:
    """Warn when a profile that needs a stable URL is published on a throwaway one.

    ``chatgpt-web`` and ``claude-web`` both declare ``persistent_url=True``, and
    nothing read that field. A Cloudflare Quick Tunnel hands out a fresh
    ``*.trycloudflare.com`` name on every start, so the URL a user has just pasted
    into their connector stops existing the moment the bridge restarts -- and the
    connector then fails on their side with nothing here to explain why.

    Returns ``None`` when the user supplied their own origin, which is exactly the
    case the note would be telling them to move to.
    """
    if tunnel != "cloudflare" or public_url:
        return None
    profile = next(
        (item for item in known_bridge_profiles() if item.name == profile_name), None
    )
    if profile is None or not profile.persistent_url:
        return None
    if (language or os.environ.get("KAROX_UI_LANGUAGE", "en")).lower() == "ru":
        return (
            "\nВажно: это временный Cloudflare Quick Tunnel. После каждого "
            "перезапуска KaroX URL меняется, поэтому сохранённое в клиенте "
            "подключение перестанет работать и его URL нужно будет обновить. "
            "Для постоянного подключения опубликуйте стабильный HTTPS-адрес и "
            "используйте --tunnel custom --public-url."
        )
    return (
        "\nNote: this is a Cloudflare Quick Tunnel, so the URL above is temporary. "
        "It changes every time the bridge restarts, and the connector you paste it "
        "into will stop working when it does. Your authorization does survive a "
        "restart -- the URL is what does not. For something you keep, publish a "
        "stable HTTPS origin and pass --tunnel custom --public-url."
    )


def web_bridge_connection_instructions(
    profile: str, language: Optional[str] = None
) -> tuple[str, ...]:
    """Human steps printed after a web bridge becomes reachable."""
    selected = (language or os.environ.get("KAROX_UI_LANGUAGE", "en")).lower()
    if selected == "ru":
        if profile == "chatgpt-web":
            return (
                "Как подключить мост к ChatGPT:",
                "  1. Нужен ChatGPT Web с доступом к developer mode и custom MCP apps.",
                "  2. В ChatGPT откройте Настройки → Приложения и включите developer mode в расширенных настройках, если он доступен.",
                "  3. Создайте custom app через Приложения → Создать и вставьте MCP URL из строки выше.",
                "  4. Нажмите сканирование инструментов и завершите OAuth через KaroX.",
                "  5. На странице KaroX вставьте пароль подтверждения (скопируйте его командой из строки выше) и нажмите «Разрешить».",
                "Важно: пароль вводится только на странице KaroX, не в настройках приложения.",
            )
        if profile == "notion":
            return (
                "Как подключить мост к Notion Custom Agent:",
                "  1. В workspace должен быть разрешён Custom MCP server.",
                "  2. Откройте Custom Agent → Settings → Tools & Access → Add connection → Custom MCP server.",
                "  3. Вставьте стабильный MCP URL из строки выше.",
                "  4. Выберите OAuth, если Notion показывает выбор; KaroX публикует OAuth discovery и поддерживает Dynamic Client Registration, поэтому Client ID, Client Secret и Bearer token не нужны.",
                "  5. На странице KaroX завершите подтверждение, затем вернитесь в Notion и сохраните подключение.",
                "Важно: пароль подтверждения вводится только на странице KaroX, а не в форме Notion.",
            )
        if profile == "hyperagent-web":
            return (
                "Как подключить мост к Hyperagent:",
                "  1. В Hyperagent откройте Settings → Integrations и добавьте Custom MCP server.",
                "  2. Name: KaroX.",
                "  3. URL: стабильный MCP URL из строки выше (заканчивается на /mcp).",
                "  4. Используйте автоматический OAuth сервера, если Hyperagent предлагает способ авторизации: KaroX публикует OAuth discovery и поддерживает Dynamic Client Registration, поэтому Client ID и Client Secret KaroX вручную не нужны.",
                "  5. Начните подключение и завершите подтверждение на открывшейся странице KaroX.",
                "Важно: пароль подтверждения вводится только на странице KaroX, а не в настройках Hyperagent.",
            )
        return (
            "Как подключить мост к Claude:",
            "  1. Нужен тариф Claude с поддержкой custom connectors.",
            "  2. Откройте Claude: Settings → Connectors → Add custom connector.",
            "  3. Вставьте MCP URL из строки выше и добавьте connector.",
            "  4. На странице KaroX вставьте пароль подтверждения (скопируйте его командой из строки выше) и нажмите «Разрешить».",
            "Важно: пароль вводится только на странице KaroX, не в настройках коннектора.",
        )
    if profile == "chatgpt-web":
        return (
            "How to connect the bridge to ChatGPT:",
            "  1. Use ChatGPT Web with access to developer mode and custom MCP apps.",
            "  2. Open Settings → Apps and enable developer mode in Advanced Settings when available.",
            "  3. Create a custom app from Apps → Create and paste the MCP URL shown above.",
            "  4. Scan tools and complete the OAuth prompt through KaroX.",
            "  5. On the KaroX page, paste the approval password (copy it with the command shown above) and click Authorize.",
            "Important: enter the password only on the KaroX page, not in the app settings.",
        )
    if profile == "notion":
        return (
            "How to connect the bridge to a Notion Custom Agent:",
            "  1. Custom MCP servers must be enabled for the workspace.",
            "  2. Open Custom Agent → Settings → Tools & Access → Add connection → Custom MCP server.",
            "  3. Paste the stable MCP URL shown above.",
            "  4. Choose OAuth if Notion shows an auth selector. KaroX publishes OAuth discovery and supports Dynamic Client Registration, so no Client ID, Client Secret, or bearer token is entered in Notion.",
            "  5. Complete the KaroX approval page, then return to Notion and save the connection.",
            "Important: use a stable Tailscale/custom HTTPS address, and enter the approval password only on the KaroX page, never in the Notion form.",
        )
    if profile == "hyperagent-web":
        return (
            "How to connect the bridge to Hyperagent:",
            "  1. In Hyperagent, open Settings → Integrations and add a Custom MCP server.",
            "  2. Name: KaroX.",
            "  3. URL: the stable MCP URL shown above (it ends in /mcp).",
            "  4. Use the server's automatic OAuth flow if Hyperagent offers an authentication choice: KaroX publishes OAuth discovery metadata and supports Dynamic Client Registration, so no KaroX Client ID or Client Secret is entered by hand.",
            "  5. Start connecting and complete approval on the KaroX page that opens.",
            "Important: enter the approval password only on the KaroX page, never in Hyperagent settings.",
        )
    return (
        "How to connect the bridge to Claude:",
        "  1. Open Claude Settings → Connectors → Add custom connector.",
        "  2. Paste the MCP URL shown above and start connecting.",
        "  3. On the KaroX page, paste the approval password (copy it with the command shown above) and click Authorize.",
        "Important: enter the password only on the KaroX page, not in connector settings.",
    )


def _write_console_utf8(line: str) -> None:
    """Write one user-facing line under both real consoles and test streams."""
    binary = getattr(sys.stdout, "buffer", None)
    if binary is not None:
        binary.write((line + "\n").encode("utf-8", errors="replace"))
        binary.flush()
        return
    print(line, flush=True)


def claim_watchdog(path: Path, payload: dict[str, Any]) -> None:
    """Create a session's watchdog record, refusing to take over another's.

    The record used to be written unconditionally, before the session store --
    the only thing enforcing session-id uniqueness -- had been consulted, and the
    launcher's ``finally`` then deleted it just as unconditionally. Two
    ``bridge connect`` runs sharing a ``--session-id`` therefore had the second
    overwrite the first's record with its own pid and delete it on the way out,
    leaving a live bridge holding a public tunnel that nothing on disk could find.
    That is the exact failure the record exists to prevent.

    Creating it exclusively makes the record the thing that decides ownership, so
    the loser never touches what it does not own. An existing record is not
    overwritten even when its owner is dead: a stale one is what
    ``karox bridge doctor`` needs in order to revoke the orphan's credential and
    session, and silently replacing it would strand them.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    try:
        descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if not _stale_durable_record_is_reclaimable(path, payload):
            raise WebBridgeLaunchError(_watchdog_conflict(path)) from None
        # Same durable identity, previous owner provably dead: taking the record
        # over strands nothing, because a saved profile's credential and session
        # are kept by design rather than revoked from the stale record. Refusing
        # here is what left every successor unable to register, so the supervisor
        # read the profile as "not running" and respawned it without end.
        try:
            os.replace(_write_reclaim_temporary(path, body), path)
        except OSError:
            raise WebBridgeLaunchError(_watchdog_conflict(path)) from None
        return
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(body)


def _release_own_watchdog(path: Path) -> bool:
    """Delete ``path`` only while it still records this process as the owner.

    Returns ``True`` when the record was ours and is now gone. A record naming a
    different owner belongs to a successor that reclaimed it and is left alone.
    """
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Unreadable or already gone: nothing this owner can safely delete.
        try:
            path.unlink()
        except OSError:
            return False
        return True
    owner = record.get("owner_pid") if isinstance(record, dict) else None
    if isinstance(owner, int) and owner != os.getpid():
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def _write_reclaim_temporary(path: Path, body: str) -> str:
    """Stage a reclaimed record beside its target for an atomic replace."""
    temporary = path.with_name(f"{path.name}.{os.getpid()}.reclaim")
    temporary.write_text(body, encoding="utf-8", newline="\n")
    return str(temporary)


def _stale_durable_record_is_reclaimable(path: Path, payload: dict[str, Any]) -> bool:
    """True when an existing record is this same durable identity, owner dead.

    Deliberately narrow. Every one of these must hold, or the record stays:
    the claim and the record must be persistent saved-profile records for the
    *same* saved profile and session id, and the recorded owner must be readable
    and provably not running. An unreadable or live record is never taken over.
    """
    if not payload.get("persistent_session"):
        return False
    claimed_session = payload.get("session_id")
    claimed_profile = payload.get("saved_profile")
    if not isinstance(claimed_session, str) or not claimed_session:
        return False
    if not isinstance(claimed_profile, str) or not claimed_profile:
        return False
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(existing, dict):
        return False
    if existing.get("session_id") != claimed_session:
        return False
    if existing.get("saved_profile") != claimed_profile:
        return False
    owner = existing.get("owner_pid")
    if not isinstance(owner, int) or owner <= 0:
        return False
    if owner == os.getpid():
        return False
    return not _process_is_alive(owner)


def _watchdog_conflict(path: Path) -> str:
    """Explain a refused claim in terms of what the user should do next."""
    owner: Any = None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        record = None
    if isinstance(record, dict):
        owner = record.get("owner_pid")
    if isinstance(owner, int) and _process_is_alive(owner):
        return (
            f"a web bridge is already serving this session in process {owner}; "
            "stop it, or start this one with a different --session-id"
        )
    return (
        f"a previous web bridge left a record at {path} and it has not been "
        "cleaned up; run `karox bridge doctor` to revoke it, then start again"
    )


def reap_orphaned_web_bridges() -> tuple[str, ...]:
    """Revoke bridges whose launcher died before it could clean up.

    On Windows the job object has already terminated the children, so the work
    left is the credential, the session, and the stale watchdog file. On POSIX
    the recorded process groups are signalled first: a reused pid is very
    unlikely to also be a group leader, which is why the group is signalled
    rather than the bare pid.
    """
    reaped: list[str] = []
    try:
        entries = sorted(
            entry
            for entry in watchdog_dir().glob("*.json")
            # The post-mortem artifact lives in this directory under the same
            # ``.json`` suffix but is a different schema: it names a session and
            # the pid of the owner that *already exited*, and deliberately has no
            # ``persistent_session`` flag. Read as a watchdog record it looks
            # exactly like an orphan of a throwaway session, so the reaper below
            # would revoke the durable identity of a saved profile on the very
            # next start. Only real watchdog records are reapable.
            if not entry.name.endswith(_OWNER_EXIT_RECORD_SUFFIX)
        )
    except OSError:
        return ()
    for entry in entries:
        try:
            record = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            record = {}
        owner = record.get("owner_pid") if isinstance(record, dict) else None
        if isinstance(owner, int) and (
            owner == os.getpid() or _process_is_alive(owner)
        ):
            continue
        if os.name != "nt":
            for key in ("tunnel_pid", "bridge_pid"):
                pid = record.get(key) if isinstance(record, dict) else None
                if isinstance(pid, int) and _process_is_alive(pid):
                    try:
                        os.killpg(os.getpgid(pid), signal.SIGTERM)
                    except (OSError, AttributeError):
                        pass
        session_id = record.get("session_id") if isinstance(record, dict) else None
        # Naming a saved profile is itself proof of a durable identity. Trusting
        # only the explicit flag makes any record that predates it -- or any
        # neighbouring artifact that merely resembles one -- read as a throwaway
        # session, and revoking is not an error this side can afford to guess at.
        persistent = bool(
            record.get("persistent_session") or record.get("saved_profile")
            if isinstance(record, dict)
            else False
        )
        if not persistent and isinstance(session_id, str) and session_id.startswith(
            "web-saved-"
        ):
            # The durable session id scheme is reserved for saved profiles.
            persistent = True
        if isinstance(session_id, str) and session_id and not persistent:
            # A record is now written before the session and credential exist, so
            # "reaped" may only name the ones that were really there to revoke.
            # Saved profiles deliberately keep their session and keyring secret:
            # the dead process tree is reaped above, but the durable connector
            # identity survives the restart.
            revoked = False
            try:
                BridgeCredentialStore().delete(session_id)
                revoked = True
            except Exception:
                pass
            try:
                SessionStore(session_dir()).revoke(session_id)
                revoked = True
            except Exception:
                pass
            if revoked:
                reaped.append(session_id)
        try:
            entry.unlink()
        except OSError:
            pass
    return tuple(reaped)


def _persistent_session(config: WebBridgeConnectConfig) -> bool:
    """Saved profiles are durable identities, not one-launch throwaways."""
    return bool(config.saved_profile_name)


def saved_web_bridge_session_id(profile_name: str) -> str:
    """Return the durable session/keyring identity for one saved profile name.

    The saved profile name is the registry identity. Target profile, tool bundle,
    tunnel, and language are editable configuration; including any of them in the
    digest would strand the previous session and credential after an ordinary
    edit.
    """
    if not isinstance(profile_name, str) or not profile_name:
        raise ValueError("saved web bridge profile name must not be empty")
    digest = hashlib.sha256(profile_name.encode("utf-8")).hexdigest()[:24]
    return f"web-saved-{digest}"


def _legacy_saved_web_bridge_session_id(
    profile_name: str,
    target_profile: str,
) -> str:
    """Identity emitted by the first durable-profile implementation."""
    digest = hashlib.sha256(
        f"{target_profile}\0{profile_name}".encode("utf-8")
    ).hexdigest()[:24]
    return f"web-saved-{digest}"


def saved_web_bridge_session_candidates(profile_name: str) -> tuple[str, ...]:
    """Current identity followed by every legacy target-profile identity."""
    ordered = [saved_web_bridge_session_id(profile_name)]
    ordered.extend(
        _legacy_saved_web_bridge_session_id(profile_name, target)
        for target in WEB_BRIDGE_PROFILES
    )
    return tuple(dict.fromkeys(ordered))


class SecretPurpose:
    """Typed secret purpose tags so callers never confuse one credential for another.

    The OAuth approval password is the bridge credential the ChatGPT OAuth consent
    page validates. A static Bearer credential is a different secret (e.g. ClickUp
    API key) copied in a different format. Both are never returned to the UI; only
    the fingerprint and clipboard status are.
    """

    OAUTH_APPROVAL_PASSWORD = "oauth_approval_password"
    STATIC_BEARER = "static_bearer"


@dataclass(frozen=True)
class SecretCopyResult:
    """Result of copying a typed secret to the clipboard.

    Never contains the secret itself — only the fingerprint, clipboard status,
    and auto-clear deadline.
    """

    purpose: str
    copied: bool
    fingerprint: str
    reference: Optional[str]
    auto_clear_seconds: int
    error: Optional[str] = None


def resolve_oauth_approval_password(profile_name: str) -> tuple[Optional[str], Optional[str]]:
    """Resolve the current OAuth approval password for a saved bridge profile.

    Returns ``(secret, reference)`` where ``secret`` is the current value from the
    OS keyring (or ``None`` if not found) and ``reference`` is the keyring ref.
    Never prints or logs the secret. The caller is responsible for clipboard copy.
    """
    from .bridge import BridgeCredentialStore

    candidates = saved_web_bridge_session_candidates(profile_name)
    store = BridgeCredentialStore()
    for session_id in candidates:
        ref = f"os-keyring:bridge/{session_id}"
        try:
            secret = store.resolve(ref)
            return secret, ref
        except CredentialError:
            continue
    return None, None


def copy_oauth_approval_password(profile_name: str) -> SecretCopyResult:
    """Copy the OAuth approval password to the clipboard without printing it.

    Canonical production service used by both the CLI
    (``karox bridge oauth approval-password --saved <profile> --copy --quiet``)
    and the TUI ServiceConnectScreen P key. Resolves the CURRENT value at copy
    time from the OS keyring, copies to clipboard, and schedules auto-clear.
    The secret is never returned to the caller — only the fingerprint and status.
    """
    from . import clipboard
    from .credentials import CredentialStore

    secret, ref = resolve_oauth_approval_password(profile_name)
    if secret is None:
        return SecretCopyResult(
            purpose=SecretPurpose.OAUTH_APPROVAL_PASSWORD,
            copied=False,
            fingerprint="",
            reference=None,
            auto_clear_seconds=0,
            error=f"no bridge credential found for saved profile '{profile_name}'",
        )
    fingerprint = CredentialStore.fingerprint(secret)
    copied = clipboard.write_text(secret)
    clipboard.schedule_clear()
    return SecretCopyResult(
        purpose=SecretPurpose.OAUTH_APPROVAL_PASSWORD,
        copied=copied,
        fingerprint=fingerprint,
        reference=ref,
        auto_clear_seconds=120,
    )


def copy_static_bearer_credential_for_saved_bridge(profile_name: str) -> SecretCopyResult:
    """Copy a saved bridge credential as an RFC 6750 Authorization value.

    Bearer-hosted clients such as Notion use the same durable bridge credential
    store as the OAuth approval flow, but they need a different clipboard format:
    ``Bearer <secret>`` rather than the raw approval password.  Resolve the secret
    only inside this service and return metadata/fingerprint, never the value.
    """
    secret, ref = resolve_oauth_approval_password(profile_name)
    if secret is None:
        return SecretCopyResult(
            purpose=SecretPurpose.STATIC_BEARER,
            copied=False,
            fingerprint="",
            reference=None,
            auto_clear_seconds=0,
            error=f"no bridge credential found for saved profile '{profile_name}'",
        )
    return _copy_bearer_secret_internal(secret, ref)


def copy_static_bearer_token_for_saved_bridge(profile_name: str) -> SecretCopyResult:
    """Copy only the raw bearer token for clients that add the Bearer prefix themselves.

    Notion's custom MCP form has a separate Prefix field set to ``Bearer``. Feeding
    it an RFC 6750 Authorization value would produce ``Bearer Bearer <secret>``.
    This service therefore copies only the underlying secret while preserving the
    same typed result, fingerprinting, and clipboard auto-clear guarantees.
    """
    from . import clipboard
    from .credentials import CredentialStore

    secret, ref = resolve_oauth_approval_password(profile_name)
    if secret is None:
        return SecretCopyResult(
            purpose=SecretPurpose.STATIC_BEARER,
            copied=False,
            fingerprint="",
            reference=None,
            auto_clear_seconds=0,
            error=f"no bridge credential found for saved profile '{profile_name}'",
        )
    fingerprint = CredentialStore.fingerprint(secret)
    copied = clipboard.write_text(secret)
    clipboard.schedule_clear()
    return SecretCopyResult(
        purpose=SecretPurpose.STATIC_BEARER,
        copied=copied,
        fingerprint=fingerprint,
        reference=ref,
        auto_clear_seconds=120,
    )


def copy_static_bearer_credential_reference(credential_reference: str) -> SecretCopyResult:
    """Copy a static Bearer credential from an OS-keyring reference to the clipboard.

    Reference-based API for UI/controller callers: takes a credential reference
    (e.g. ``os-keyring:connection/c-abc123``) and resolves the secret inside
    the service, never returning the raw secret to the caller. The secret is
    copied to the clipboard in ``Bearer <secret>`` format with auto-clear.

    Distinct from the OAuth approval password: different purpose, different
    format, never mixed.
    """
    from .connections import ConnectionCredentialStore, ConnectionConfigurationError

    if not credential_reference:
        return SecretCopyResult(
            purpose=SecretPurpose.STATIC_BEARER,
            copied=False,
            fingerprint="",
            reference=None,
            auto_clear_seconds=0,
            error="no credential reference provided",
        )
    store = ConnectionCredentialStore()
    try:
        secret = store.resolve(credential_reference)
    except CredentialError as exc:
        return SecretCopyResult(
            purpose=SecretPurpose.STATIC_BEARER,
            copied=False,
            fingerprint="",
            reference=credential_reference,
            auto_clear_seconds=0,
            error=str(exc),
        )
    except ConnectionConfigurationError as exc:
        return SecretCopyResult(
            purpose=SecretPurpose.STATIC_BEARER,
            copied=False,
            fingerprint="",
            reference=credential_reference,
            auto_clear_seconds=0,
            error=str(exc),
        )
    if not secret:
        return SecretCopyResult(
            purpose=SecretPurpose.STATIC_BEARER,
            copied=False,
            fingerprint="",
            reference=credential_reference,
            auto_clear_seconds=0,
            error="credential reference resolved to empty value",
        )
    return _copy_bearer_secret_internal(secret, credential_reference)


def copy_static_bearer_credential_for_connection(connection_id: str) -> SecretCopyResult:
    """Copy a static Bearer credential for a saved connection by connection ID.

    Looks up the connection in the registry, resolves its credential reference,
    and copies the Bearer value to the clipboard. The raw secret never leaves
    the service.
    """
    from .connections import connection_registry

    try:
        target = connection_registry().get(connection_id)
    except Exception as exc:
        return SecretCopyResult(
            purpose=SecretPurpose.STATIC_BEARER,
            copied=False,
            fingerprint="",
            reference=None,
            auto_clear_seconds=0,
            error=f"connection not found: {exc}",
        )
    ref = getattr(target, "credential_ref", None)
    if not ref:
        return SecretCopyResult(
            purpose=SecretPurpose.STATIC_BEARER,
            copied=False,
            fingerprint="",
            reference=None,
            auto_clear_seconds=0,
            error=f"connection '{connection_id}' has no credential reference",
        )
    return copy_static_bearer_credential_reference(ref)


def _copy_bearer_secret_internal(secret: str, reference: Optional[str] = None) -> SecretCopyResult:
    """Internal helper: copy a raw Bearer secret to clipboard.

    Private — never called by UI or controller callers. Those use the
    reference-based APIs (:func:`copy_static_bearer_credential_reference` or
    :func:`copy_static_bearer_credential_for_connection`).
    """
    from . import clipboard
    from .credentials import CredentialStore

    if not secret:
        return SecretCopyResult(
            purpose=SecretPurpose.STATIC_BEARER,
            copied=False,
            fingerprint="",
            reference=reference,
            auto_clear_seconds=0,
            error="no static bearer credential provided",
        )
    fingerprint = CredentialStore.fingerprint(secret)
    bearer_value = f"Bearer {secret}"
    copied = clipboard.write_text(bearer_value)
    clipboard.schedule_clear()
    return SecretCopyResult(
        purpose=SecretPurpose.STATIC_BEARER,
        copied=copied,
        fingerprint=fingerprint,
        reference=reference,
        auto_clear_seconds=120,
    )


def saved_web_bridge_identity_exists(profile_name: str) -> bool:
    """Return whether any current/legacy durable session or watchdog exists."""
    session_root = session_dir()
    watchdog_root = watchdog_dir()
    return any(
        (session_root / session_id / "session.json").exists()
        or (watchdog_root / f"{session_id}.json").exists()
        for session_id in saved_web_bridge_session_candidates(profile_name)
    )


def _session_id(config: WebBridgeConnectConfig) -> str:
    if config.session_id:
        return config.session_id
    if config.saved_profile_name:
        candidates = saved_web_bridge_session_candidates(config.saved_profile_name)
        session_root = session_dir()
        watchdog_root = watchdog_dir()

        def exists(session_id: str) -> bool:
            return (
                (session_root / session_id / "session.json").exists()
                or (watchdog_root / f"{session_id}.json").exists()
            )

        if exists(candidates[0]):
            return candidates[0]
        legacy = [session_id for session_id in candidates[1:] if exists(session_id)]
        if len(legacy) > 1:
            raise WebBridgeLaunchError(
                "saved bridge profile has multiple legacy durable identities; "
                "stop all matching launchers and delete/recreate the profile"
            )
        if legacy:
            return legacy[0]
        return candidates[0]
    return f"web-{int(time.time())}-{uuid.uuid4().hex[:8]}"


def delete_saved_web_bridge_identity(profile_name: str) -> dict[str, Any]:
    """Delete current and legacy durable identities when every launcher is offline.

    Configuration deletion must not strand OS-keyring tokens or repository-bound
    sessions.  The operation performs a complete preflight over both the current
    name-only identity and identities emitted by the first implementation before
    it revokes anything, so one forgotten live legacy launcher cannot cause a
    partial cleanup.
    """
    candidates = saved_web_bridge_session_candidates(profile_name)
    session_root = session_dir()
    watchdog_root = watchdog_dir()
    store = SessionStore(session_root)

    selected: list[str] = [candidates[0]]
    for candidate in candidates[1:]:
        if (
            store.state_path(candidate).exists()
            or (watchdog_root / f"{candidate}.json").exists()
        ):
            selected.append(candidate)

    stale_watchdog = False
    for session_id in selected:
        watchdog = watchdog_root / f"{session_id}.json"
        if watchdog.exists():
            try:
                record = json.loads(watchdog.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise WebBridgeLaunchError(
                    "saved bridge watchdog is unreadable; run `karox bridge doctor` "
                    "before deleting the profile"
                ) from exc
            owner = record.get("owner_pid") if isinstance(record, dict) else None
            if isinstance(owner, int) and _process_is_alive(owner):
                raise WebBridgeLaunchError(
                    f"saved bridge profile is running in process {owner}; stop it "
                    "before deleting the profile"
                )
            stale_watchdog = True

        state_path = store.state_path(session_id)
        lease_path = store.lease_path(session_id)
        if state_path.exists() and lease_path.exists():
            try:
                lease = json.loads(lease_path.read_text(encoding="utf-8"))
                expires_at = float(lease.get("expires_at", 0))
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                raise WebBridgeLaunchError(
                    "saved bridge mutation lease is unreadable; run "
                    "`karox bridge doctor` before deleting the profile"
                ) from exc
            if expires_at >= time.time():
                raise WebBridgeLaunchError(
                    "saved bridge session still has an active mutation lease; "
                    "wait for the operation to finish before deleting the profile"
                )

    if stale_watchdog:
        # Persistent identities are preserved by the orphan reaper; it only
        # terminates the dead process tree and removes its watchdog.
        reap_orphaned_web_bridges()
    for session_id in selected:
        if (watchdog_root / f"{session_id}.json").exists():
            raise WebBridgeLaunchError(
                "saved bridge watchdog could not be reconciled; run "
                "`karox bridge doctor` before deleting the profile"
            )

    credential_store = BridgeCredentialStore()
    identities: list[dict[str, str]] = []
    for session_id in selected:
        session_status = "not_found"
        if store.state_path(session_id).exists():
            try:
                store.revoke(session_id)
                session_status = "revoked"
            except SessionError as exc:
                raise WebBridgeLaunchError(
                    f"saved bridge session could not be revoked: {exc}"
                ) from exc
            try:
                shutil.rmtree(store.session_dir(session_id))
                session_status = "deleted"
            except OSError:
                # Revocation is the security boundary. A filesystem cleanup
                # failure leaves an inert record for bridge doctor.
                session_status = "revoked_cleanup_pending"

        credential_status = "not_found"
        try:
            credential_store.delete(session_id)
            credential_status = "deleted"
        except CredentialError:
            pass
        identities.append(
            {
                "session_id": session_id,
                "session": session_status,
                "credential": credential_status,
            }
        )

    def aggregate(field: str) -> str:
        values = [item[field] for item in identities if item[field] != "not_found"]
        if not values:
            return "not_found"
        return values[0] if len(set(values)) == 1 else "multiple"

    return {
        "profile_name": profile_name,
        "session_id": candidates[0],
        "session": aggregate("session"),
        "credential": aggregate("credential"),
        "identities": identities,
    }


def _bridge_argv(
    config: WebBridgeConnectConfig,
    *,
    session_id: str,
    public_url: str,
) -> tuple[str, ...]:
    values = [
        sys.executable,
        "-m",
        "karox.cli",
        "bridge",
        "serve",
        "--profile",
        config.profile,
        "--protocol",
        "mcp",
        "--repository",
        str(config.repository),
        "--session-id",
        session_id,
        "--credential",
        session_id,
        "--port",
        str(config.port),
        "--deadline-seconds",
        str(config.deadline_seconds),
    ]
    if config.saved_profile_name:
        # Bind the local MCP child and its managed browser to the exact durable
        # saved profile without exposing or rotating any credential material.
        values.extend(("--saved-profile-name", config.saved_profile_name))
    for project in config.projects:
        values.extend(
            (
                "--project",
                json.dumps(project, ensure_ascii=False, separators=(",", ":")),
            )
        )
    if config.default_project_id is not None:
        values.extend(("--default-project-id", config.default_project_id))
    profile_meta = next(
        (item for item in known_bridge_profiles() if item.name == config.profile),
        None,
    )
    # `--public-url` is an OAuth authorization-server input, not a generic
    # publication hint. OAuth profiles (including Notion Custom Agent) need the
    # stable public origin so protected-resource and authorization-server metadata
    # point at the exact external resource. Static bearer profiles receive only
    # the public host through KAROX_ALLOWED_HOSTS and reject this OAuth-only option.
    if profile_meta is not None and profile_meta.auth_scheme == "oauth":
        values.extend(("--public-url", public_url))
    redirect_hosts = profile_redirect_hosts(config.profile)
    if redirect_hosts:
        values.extend(
            ("--allowed-redirect-hosts", ",".join(sorted(redirect_hosts)))
        )
    for tool in config.tools:
        values.extend(("--tool", tool))
    for server_id in config.mcp_servers:
        values.extend(("--server", server_id))
    for command in config.verification_commands:
        serialized = json.dumps(
            list(command), ensure_ascii=False, separators=(",", ":")
        )
        values.extend(("--verification-command", serialized))
    # Server profiles are secret-free (argv + env key set + env allowlist), so
    # they travel to the bridge child as JSON the same way verification commands
    # do.  The child deserializes and validates them before it will start a
    # dev server.
    for profile in config.server_profiles:
        serialized = json.dumps(profile.to_public_dict(), ensure_ascii=False)
        values.extend(("--server-profile", serialized))
    if config.browser_external_https:
        values.append("--browser-external-https")
    for domain in config.browser_allowed_domains:
        values.extend(("--browser-domain", domain))
    for domain in config.browser_denied_domains:
        values.extend(("--browser-deny-domain", domain))
    if config.browser_headed:
        values.append("--browser-headed")
    if config.browser_user_takeover:
        values.append("--browser-user-takeover")
    if config.browser_network_inspection:
        values.append("--browser-network-inspection")
    if config.browser_payment_confirmation:
        values.append("--browser-payment-confirmation")
    for email in config.browser_allowed_emails:
        values.extend(("--browser-allowed-email", email))
    for reference in config.browser_credential_refs:
        values.extend(("--browser-credential-ref", reference))
    return tuple(values)


def _sync_external_mcp_selections(
    config: WebBridgeConnectConfig,
    sessions: SessionStore,
    session_id: str,
    repository: Path,
) -> None:
    """Bind saved external MCP allowlists before the hosted bridge takes its lease.

    The saved profile stores only secret-free server IDs. On first attachment we
    allow tools the server record explicitly classifies as read-only and leave
    mutating tools at ``ask``. Existing decisions survive when the server/tool
    schema digest is unchanged.
    """

    if not config.mcp_servers:
        return
    from .mcp_client import McpClient, McpRegistry, mcp_selection
    from .paths import config_dir

    registry = McpRegistry(config_dir() / "vnext" / "mcp-servers.json")
    client = McpClient(registry)
    discovered: list[tuple[Any, list[Any]]] = []
    for server_id in config.mcp_servers:
        server = registry.get(server_id)
        tools = client.discover_record(server, repository)
        discovered.append((server, tools))

    with sessions.mutate(
        session_id,
        f"web-bridge-mcp-sync-{os.getpid()}",
        ttl_seconds=30.0,
    ) as session:
        sessions.validate_repository(session, repository)
        for server, tools in discovered:
            previous = next(
                (
                    item
                    for item in session.mcp_servers
                    if isinstance(item, dict)
                    and item.get("server_id") == server.server_id
                ),
                None,
            )
            decisions = {
                tool.remote_name: "allow"
                for tool in tools
                if tool.read_only
            }
            selection = mcp_selection(
                server,
                tools,
                decisions,
                previous=previous,
            )
            session.mcp_servers = [
                item
                for item in session.mcp_servers
                if not (
                    isinstance(item, dict)
                    and item.get("server_id") == server.server_id
                )
            ]
            session.mcp_servers.append(selection)
        session.mcp_servers.sort(
            key=lambda item: str(item.get("server_id", ""))
            if isinstance(item, dict)
            else ""
        )


def _expected_verification_seconds(
    commands: tuple[tuple[str, ...], ...]
) -> Optional[float]:
    estimate = 0.0
    for command in commands:
        joined = " ".join(command).lower()
        if "run_v5_preflight.py" in joined and "--full" in command:
            estimate = max(estimate, 900.0)
        elif "pytest" in joined or "unittest" in joined or "coverage" in joined:
            estimate = max(estimate, 300.0)
    return estimate or None


def web_bridge_diagnostics(
    config: WebBridgeConnectConfig,
    *,
    public_url: Optional[str] = None,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    """Describe the effective bridge contract without exposing credentials.

    After a restart this is the first thing a hosted client (ChatGPT, Claude)
    reads, so it must name *every* capability surface and the exact reason any
    of them is off, including the browser, managed-server and screenshot
    capabilities that were previously absent from this payload.
    """
    disabled: list[dict[str, str]] = []
    # Every name the runtimes could serve, not just the Core half: a client
    # asking "can I screenshot?" must see the answer here even if the answer
    # is "no, the profile does not allow it".
    for tool in sorted(KNOWN_HOSTED_TOOL_NAMES):
        if tool in config.tools or tool in config.profile_denied_tools:
            continue
        reason = "not selected by the connection profile"
        if tool == "karox.checks.run" and not config.verification_commands:
            reason = "no approved verification-command allowlist"
        if tool.startswith("karox.dev_server.") and not config.server_profiles:
            reason = "no approved server-profile allowlist"
        if tool == "karox.browser.network_requests" and not config.browser_network_inspection:
            reason = "network inspection is not enabled for this browser session"
        if tool in {
            "karox.browser.request_user_takeover",
            "karox.browser.resume_after_user_takeover",
        } and not config.browser_user_takeover:
            reason = "user takeover is not enabled for this browser session"
        if tool in BROWSER_INPUT_TOOL_NAMES and config.access_profile == AccessProfile.READ_ONLY:
            reason = "browser input requires browser_control, workspace_write, or elevated access"
        if tool in {
            "karox.dev_server.start",
            "karox.dev_server.stop",
        } and config.access_profile == AccessProfile.READ_ONLY:
            reason = "write actions require the workspace_write or elevated profile"
        disabled.append({"name": tool, "reason": reason})
    # Tools the config dropped because the access profile cannot grant their
    # capability. They were never usable under this profile, but a saved
    # profile may still select them; the honest answer to "why is my tool
    # missing" is the drop reason, not the generic "not selected" line.
    for name, reason in sorted(config.profile_denied_tools.items()):
        disabled.append({"name": name, "reason": reason})
    expected = _expected_verification_seconds(config.verification_commands)
    advisory = None
    if expected is not None and config.deadline_seconds < expected:
        advisory = (
            f"effective deadline {config.deadline_seconds:g}s is below the "
            f"estimated {expected:g}s needed by the selected verification command"
        )
    stability = {
        "cloudflare": "ephemeral",
        "tailscale": "stable_device_hostname",
        "custom": "stable_user_managed",
    }[config.tunnel]
    # Derived from the same canonical groups that ``__post_init__`` normalizes
    # against, so diagnostics reflects the *effective* capability set, not raw
    # checkbox values: because input implies read, ``browser_read`` is True
    # whenever ``browser_input`` is.
    browser_read = any(name in config.tools for name in BROWSER_READ_TOOL_NAMES)
    browser_input = any(name in config.tools for name in BROWSER_INPUT_TOOL_NAMES)
    managed_server = any(name in config.tools for name in (
        "karox.dev_server.start",
        "karox.dev_server.status",
        "karox.dev_server.logs",
        "karox.dev_server.stop",
    ))
    screenshot = "karox.browser.screenshot" in config.tools
    extension_backend = config.browser_headed and config.browser_user_takeover
    # Safe repository + executable diagnostics.  ``config.repository`` is
    # already the single canonicalized path (resolved strictly once in
    # ``_direct_connect_config`` / saved-profile load), so these fields reflect
    # that one value rather than re-deriving it from ``os.getcwd()``.  No
    # environment variables or credentials are emitted.
    repo_raw = str(config.repository)
    try:
        repo_resolved = str(Path(config.repository).expanduser().resolve(strict=False))
    except OSError:
        repo_resolved = repo_raw
    repo_exists = Path(config.repository).exists()
    repo_is_dir = Path(config.repository).is_dir()
    # Resolve the executable of every guarded command the bridge may spawn
    # (verification commands + the dev-server argv of each profile).  The key
    # is the logical argv0 (what the allowlist matches); the value is the
    # absolute path the runtime will actually launch.  Failures degrade to
    # ``None`` so diagnostics never raise on a missing tool.
    executable_resolution: dict[str, str] = {}
    seen_argv0: set[str] = set()
    for command in config.verification_commands:
        if command:
            seen_argv0.add(command[0])
    for profile in config.server_profiles:
        if profile.argv:
            seen_argv0.add(profile.argv[0])
    for argv0 in sorted(seen_argv0):
        try:
            resolved = _resolve_executable([argv0])
            if resolved and resolved[0] != argv0:
                executable_resolution[argv0] = resolved[0]
        except Exception:
            executable_resolution[argv0] = ""
    client_capabilities = negotiate_client_capabilities(
        client_kind=config.profile,
        available_tools=config.tools,
        access_profile=config.access_profile.value,
        disabled_tools=disabled,
        practical_output_size_limit=4 * 1024 * 1024,
        persistent_session=bool(config.saved_profile_name),
    )
    return {
        "schema_version": 1,
        "saved_profile": config.saved_profile_name,
        "target_profile": config.profile,
        "client_capabilities": client_capabilities.to_dict(),
        "repository": str(config.repository),
        "repository_raw": repo_raw,
        "repository_resolved": repo_resolved,
        "repository_exists": repo_exists,
        "repository_is_dir": repo_is_dir,
        "runtime_cwd": os.getcwd(),
        "executable_resolution": executable_resolution,
        "access_profile": config.access_profile.value,
        "write_permission": config.access_profile in {
            AccessProfile.WORKSPACE_WRITE,
            AccessProfile.ELEVATED,
        },
        "available_tools": list(config.tools),
        "disabled_tools": disabled,
        "tool_catalog": {
            "strategy": (
                "stable_across_access_modes"
                if config.profile == "hyperagent-web"
                else "profile_scoped"
            ),
            "schema_snapshot_version": client_capabilities.tool_schema_snapshot_version,
            "advertised_tool_count": len(config.tools),
            # P1.5 deterministic groups: a pure function of the tool name, so
            # operators can predict availability per profile with no hidden
            # magic. Sorted members keep equal catalogs byte-identical.
            "groups": {
                group: list(names)
                for group, names in catalog_groups(config.tools).items()
            },
            "permission_toggle_changes_tool_catalog": config.profile != "hyperagent-web",
            "legacy_cached_catalog_compatible": config.profile == "hyperagent-web",
            "legacy_fallbacks": (
                {
                    "repo.search": "task.execute_plan action=search",
                    "repo.read_lines": "task.execute_plan action=read with mode=lines",
                    "repo.inspect": "task.execute_plan action=inspect",
                    "runtime.status": "task.execute_plan action=read with mode=runtime_status",
                    "git.log": "task.execute_plan action=command with git log argv",
                    "git.commit": "task.execute_plan action=command with git commit argv while Full access is elevated",
                    "command.run": "task.execute_plan action=command while Full access is elevated",
                    "task.bootstrap": "task.checkpoint auto-bootstraps verified base state when absent",
                    "tests.run": "project-aware: pytest for Python, package test script for Node/Vite",
                }
                if config.profile == "hyperagent-web"
                else {}
            ),
        },
        "verification_commands": [
            list(command) for command in config.verification_commands
        ],
        "command_allowlist": [
            list(command) for command in config.verification_commands
        ],
        "server_profiles": [p.to_public_dict() for p in config.server_profiles],
        "browser_permission": {
            "read": browser_read,
            "input": browser_input,
            "localhost": True,
            "external_https": config.browser_external_https,
            "user_takeover": config.browser_user_takeover,
            "network_inspection": config.browser_network_inspection,
            "payment_confirmation": config.browser_payment_confirmation,
            "headed": config.browser_headed,
            "backend": "extension" if extension_backend else "playwright",
            "allowed_domains": list(config.browser_allowed_domains),
            "denied_domains": list(config.browser_denied_domains),
            "allowed_email_count": len(config.browser_allowed_emails),
            "allowed_credential_refs": list(config.browser_credential_refs),
            "allowed_credential_count": len(config.browser_credential_refs),
            # Backward-compatible summary for older clients/tests. The detailed
            # policy above remains authoritative; this is never the only guard.
            "localhost_only": not config.browser_external_https,
        },
        "browser_isolation": {
            "context_per_session": True,
            "cross_session_control": False,
            "artifacts_bound_to_session": True,
            "dedicated_chrome_profile": extension_backend,
            "main_chrome_profile_visible": False,
            "dns_pinning_proxy": not extension_backend,
            "proxy_authentication": "per_session" if not extension_backend else "not_used",
            "proxy_port": "random_loopback" if not extension_backend else "not_used",
        },
        "browser_lifecycle": {
            "survives_bridge_restart": extension_backend,
            "reconnects_from_extension_config": extension_backend,
            "close_requires_explicit_user_confirmation": True,
        },
        "url_policy": {
            "https_external": "allowed" if config.browser_external_https else "blocked",
            "localhost": "allowed",
            "external_http": "blocked",
            "private_network": "blocked",
            "metadata_endpoints": "blocked",
            "unsafe_schemes": "blocked",
            "redirects_revalidated": True,
            "external_to_localhost": "blocked",
            "dns_rebinding": (
                "navigation_validation_only"
                if extension_backend
                else "pinned_ip_connect_proxy"
            ),
            "service_workers": "browser_default" if extension_backend else "blocked",
            "downloads": "browser_default" if extension_backend else "cancelled",
        },
        "localhost_policy": "http(s) on 127.0.0.1/localhost/::1 remains available",
        "screenshot_capability": screenshot,
        "image_capability": screenshot or "karox.artifact.read_image" in config.tools,
        "managed_server_capability": managed_server,
        "session_deadline_seconds": config.deadline_seconds,
        "requested_deadline_seconds": config.deadline_seconds,
        "effective_deadline_seconds": config.deadline_seconds,
        "expected_verification_seconds": expected,
        "deadline_advisory": advisory,
        "mode_restrictions": {
            "read_only": config.access_profile == AccessProfile.READ_ONLY,
            # Hyperagent Full is a trusted unrestricted developer command grant.
            # Protected profiles keep the legacy hard stops; Full/Elevated with
            # karox.command.run intentionally permits push/publish/auth/deploy.
            "no_git_push": not (
                config.access_profile == AccessProfile.ELEVATED
                and "karox.command.run" in config.tools
            ),
            "no_publish": not (
                config.access_profile == AccessProfile.ELEVATED
                and "karox.command.run" in config.tools
            ),
            "no_auth_commands": not (
                config.access_profile == AccessProfile.ELEVATED
                and "karox.command.run" in config.tools
            ),
        },
        "tunnel": config.tunnel,
        "url_stability": stability,
        "public_url": public_url,
        "session_id": session_id,
        "session_expiration": (
            "persists across managed launcher restarts"
            if config.saved_profile_name
            else "when the managed launcher exits"
        ),
        "language": config.language,
    }


def run_web_bridge(config: WebBridgeConnectConfig) -> int:
    """Own the tunnel and bridge processes until Ctrl+C or either child exits."""
    repository = config.repository.expanduser().resolve(strict=True)
    if not repository.is_dir():
        raise WebBridgeLaunchError("web bridge repository must be a directory")
    config = replace(config, repository=repository)
    if not _port_is_available(config.port):
        raise WebBridgeLaunchError(f"local bridge port is already in use: {config.port}")

    reaped = reap_orphaned_web_bridges()
    if reaped:
        print(
            f"Revoked {len(reaped)} orphaned KaroX web bridge session(s) "
            "left behind by an earlier run.",
            flush=True,
        )

    tunnel: Optional[
        CloudflareQuickTunnel | TailscaleForegroundFunnel | TailscaleBackgroundFunnel
    ] = None
    bridge: Optional[subprocess.Popen[str]] = None
    bridge_output: Optional[MirroredChildOutput] = None
    credential_created = False
    session_created = False
    bridge_ready = False
    sessions: Optional[SessionStore] = None
    watchdog: Optional[Path] = None
    persistent_session = _persistent_session(config)
    session_id = _session_id(config)
    owner_lock: Optional[Any] = None
    if persistent_session and config.saved_profile_name:
        owner_lock = _try_acquire_saved_bridge_owner_lock(config.saved_profile_name)
        if owner_lock is None:
            raise WebBridgeLaunchError(
                "another durable owner is already active for saved profile "
                f"'{config.saved_profile_name}'"
            )
    job: Optional[int] = None
    route_health = RouteHealthTracker() if config.tunnel == "tailscale" else None
    tunnel_recovery_failures = 0
    tunnel_retry_at = 0.0
    # The record is written by the main loop *and* by the heartbeat thread below,
    # so every write is serialized and made from a snapshot: rendering the live
    # dict while the other writer mutates it would raise mid-serialization and
    # kill the owner over bookkeeping.
    watchdog_lock = threading.Lock()
    heartbeat_stop = threading.Event()
    heartbeat_thread: Optional[threading.Thread] = None
    child_respawn_failures = 0
    child_respawn_retry_at = 0.0
    exit_reason = "stopped"
    exit_detail = ""
    try:
        job = _create_child_job()
        if job is None and os.name == "nt":
            print(
                "Warning: this bridge could not create a Windows job object, so a "
                "hard kill of KaroX may leave the tunnel running.",
                flush=True,
            )
        if config.tunnel == "cloudflare":
            tunnel = start_cloudflare_quick_tunnel(
                config.port,
                executable=config.cloudflared,
                timeout_seconds=config.tunnel_timeout_seconds,
                job=job,
            )
            public_url = tunnel.public_url
        elif config.tunnel == "tailscale":
            tunnel = start_tailscale_background_funnel(
                config.port,
                https_port=profile_tailscale_https_port(config.profile),
                executable=config.tailscale,
                timeout_seconds=config.tunnel_timeout_seconds,
                # The user just confirmed ``bridge connect``; Tailscale being
                # brought online is the thing they are waiting on, so progress
                # reaches them instead of looking like a hang on the first try.
                emit=lambda line: print(line, flush=True),
            )
            public_url = tunnel.public_url
        else:
            assert config.public_url is not None
            public_url = config.public_url

        # Recorded as soon as the first child exists. Written after the second
        # one instead, a kill landing between the two spawns left a live public
        # tunnel that nothing on disk knew about, so nothing could reap it.
        watchdog_path = watchdog_dir() / f"{session_id}.json"
        watchdog_record: dict[str, Any] = {
            "session_id": session_id,
            "owner_pid": os.getpid(),
            "profile": config.profile,
            "saved_profile": config.saved_profile_name,
            "repository": str(repository),
            "persistent_session": persistent_session,
            "stop_protocol": "request-v1",
            "port": config.port,
            "public_url": public_url,
            "tunnel": config.tunnel,
            "url_stability": web_bridge_diagnostics(config)["url_stability"],
            "effective_deadline_seconds": config.deadline_seconds,
            "available_tools": list(config.tools),
            "started_at": time.time(),
            "owner_heartbeat_at": time.time(),
            "tunnel_pid": _pid_of(getattr(tunnel, "process", None)),
            "tunnel_lifecycle": (
                "daemon_background"
                if isinstance(tunnel, TailscaleBackgroundFunnel)
                else "owned_child"
            ),
            "bridge_pid": None,
        }
        # `watchdog` is what the cleanup below deletes, so it is assigned only
        # once the claim succeeded: this process must never remove a record it
        # does not own.
        claim_watchdog(watchdog_path, watchdog_record)
        watchdog = watchdog_path

        def publish() -> None:
            """Persist the current record under the writer lock."""
            with watchdog_lock:
                write_watchdog(watchdog_path, dict(watchdog_record))

        def beat() -> None:
            """Prove this owner is alive even while it is busy repairing itself.

            The heartbeat used to be written only at the top of the main loop, so
            every blocking repair -- waiting for a respawned child to bind, waiting
            for Funnel propagation, reapplying a daemon route -- looked exactly like
            a hung owner. The sibling supervisor treats a heartbeat older than
            ``OWNER_HEARTBEAT_STALE_SECONDS`` as hung and force-kills the owner's
            whole process tree, which turned self-repair into an outage. A dedicated
            thread keeps the liveness signal independent of that work.
            """
            while not heartbeat_stop.is_set():
                watchdog_record["owner_heartbeat_at"] = time.time()
                try:
                    publish()
                except OSError:
                    pass
                heartbeat_stop.wait(1.0)

        heartbeat_thread = threading.Thread(
            target=beat, name=f"karox-bridge-owner-heartbeat-{session_id}", daemon=True
        )
        heartbeat_thread.start()

        sessions = SessionStore(session_dir())
        state_exists = sessions.state_path(session_id).exists()
        if persistent_session and state_exists:
            try:
                record = sessions.load(session_id)
                sessions.validate_repository(record, repository)
            except SessionError:
                record = sessions.reactivate(
                    session_id, repository, config.access_profile
                )
            if record.revoked or record.access_profile != config.access_profile.value:
                record = sessions.reactivate(
                    session_id, repository, config.access_profile
                )
        else:
            sessions.create(
                repository,
                f"{config.profile} managed web bridge",
                config.access_profile,
                session_id=session_id,
            )
            session_created = True

        _sync_external_mcp_selections(config, sessions, session_id, repository)

        credential_store = BridgeCredentialStore()
        if persistent_session:
            try:
                secret = credential_store.resolve(
                    f"os-keyring:bridge/{session_id}"
                )
            except BridgeCredentialMissing:
                # Proven absent: this is the first launch of the durable
                # identity, so minting the connector's token is correct.
                credential = credential_store.set(session_id)
                credential_created = True
                secret = credential.get("secret")
            except CredentialError as exc:
                # Momentarily unreadable, not absent. Minting a replacement here
                # would overwrite the durable secret an already-configured
                # connector authenticates with, so fail closed instead.
                raise WebBridgeLaunchError(
                    "bridge credential is temporarily unreadable; refusing to "
                    f"replace the saved connector token ({type(exc).__name__})"
                ) from exc
        else:
            credential = credential_store.set(session_id)
            credential_created = True
            secret = credential.get("secret")
        if not isinstance(secret, str) or not secret:
            raise WebBridgeLaunchError("bridge credential generator returned no secret")

        environment = dict(os.environ)
        # The listener cannot guess the tunnel host name, and without it every
        # request through the tunnel looks like a rebound DNS name.
        environment[ALLOWED_HOSTS_ENVIRONMENT] = urlsplit(public_url).hostname or ""
        # Its output is decoded as UTF-8 below, so it has to be encoded as UTF-8:
        # a Windows console code page would otherwise turn every non-ASCII path in
        # a traceback into replacement characters.
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["KAROX_UI_LANGUAGE"] = config.language
        if config.browser_headed and config.browser_user_takeover:
            environment["KAROX_BROWSER_BACKEND"] = "extension"
        diagnostics = web_bridge_diagnostics(
            config, public_url=public_url, session_id=session_id
        )
        environment["KAROX_BRIDGE_DIAGNOSTICS_JSON"] = json.dumps(
            diagnostics, ensure_ascii=False, sort_keys=True
        )
        def spawn_bridge_child() -> tuple[subprocess.Popen[str], MirroredChildOutput]:
            """Start one local MCP child while the durable owner keeps tunnel/session state."""
            try:
                child = subprocess.Popen(
                    _bridge_argv(
                        config,
                        session_id=session_id,
                        public_url=public_url,
                    ),
                    cwd=repository,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    **_child_options(),
                )
            except OSError as exc:
                raise WebBridgeLaunchError(
                    f"cannot start KaroX bridge: {type(exc).__name__}"
                ) from exc
            _adopt_child(job, child)
            return child, _mirror_child_output(child, name="bridge")

        bridge_recovery_count = 0
        bridge, bridge_output = spawn_bridge_child()
        watchdog_record["bridge_pid"] = _pid_of(bridge)
        watchdog_record["bridge_recoveries"] = bridge_recovery_count
        publish()
        _wait_for_bridge(bridge, config.port, output=bridge_output)
        if config.tunnel == "tailscale":
            try:
                _wait_for_public_mcp_route(
                    bridge,
                    public_url,
                    path=web_bridge_mcp_path(config.profile),
                    timeout_seconds=min(15.0, float(config.tunnel_timeout_seconds)),
                    output=bridge_output,
                )
            except WebBridgeLaunchError:
                # Funnel propagation can lag behind an already healthy local MCP
                # listener. For a durable bridge, keep the owner alive so its
                # independent route-health loop can repair ingress; tearing it
                # down here creates an avoidable owner-level outage.
                if not persistent_session or not config.saved_profile_name:
                    raise
                watchdog_record["public_route_healthy"] = False
                watchdog_record["last_public_route_recovery_pending_at"] = time.time()
                publish()
                print(
                    "[tailscale] Local MCP is ready; public Funnel is still propagating "
                    "and will be repaired independently.",
                    flush=True,
                )
        bridge_ready = True

        # Durable saved bridges get a credential-free sibling supervisor. The
        # owner already heals its local MCP child and Funnel; this sibling heals
        # the owner itself if the detached process disappears. The owner also
        # periodically re-ensures the sibling, so either side can recover the
        # other after a single-process crash. Explicit Stop persists
        # desired_running=false before asking this owner to exit, preventing an
        # intentional shutdown from being resurrected.
        supervisor_check_at = 0.0
        if persistent_session and config.saved_profile_name:
            try:
                from .saved_bridge_supervisor import ensure_saved_bridge_supervisor

                supervisor_pid = ensure_saved_bridge_supervisor(
                    config.saved_profile_name,
                    desired_running=True,
                )
                watchdog_record["supervisor_pid"] = supervisor_pid
                watchdog_record["supervision"] = "mutual-v1"
                publish()
            except Exception as exc:
                # Supervision is an availability enhancement, not a reason to
                # tear down an otherwise healthy OAuth bridge. Keep serving and
                # retry from the main loop.
                watchdog_record["last_supervisor_error"] = type(exc).__name__
                publish()

        endpoint = web_bridge_mcp_endpoint(public_url, config.profile)
        print(f"KaroX {config.profile} bridge is ready")
        print(f"MCP URL: {endpoint}")
        # The bridge credential is never printed. OAuth profiles need the raw
        # approval password on KaroX's consent page; bearer profiles need an
        # RFC 6750 Authorization value. Show only a fingerprint and the typed
        # clipboard command for the active auth scheme.
        from .credentials import CredentialStore as _CS
        profile_meta = next(
            (item for item in known_bridge_profiles() if item.name == config.profile),
            None,
        )
        if profile_meta is not None and profile_meta.auth_scheme == "oauth":
            print(f"OAuth approval password fingerprint: {_CS.fingerprint(secret)}")
            print(
                "To copy the OAuth approval password for the consent page, run: "
                "karox bridge oauth approval-password --saved "
                f"{config.saved_profile_name or session_id} --copy --quiet"
            )
        else:
            print(f"Bearer credential fingerprint: {_CS.fingerprint(secret)}")
            print(
                "To copy the Authorization value without printing the secret, run: "
                f"karox bridge credential copy {session_id} --quiet"
            )
        print(f"Session: {session_id}")
        print(
            "Bridge diagnostics JSON: "
            + json.dumps(diagnostics, ensure_ascii=False, sort_keys=True)
        )
        advisory = diagnostics.get("deadline_advisory")
        if isinstance(advisory, str):
            print(f"Warning: {advisory}")
        for line in web_bridge_connection_instructions(
            config.profile, language=config.language
        ):
            _write_console_utf8(line)
        note = ephemeral_url_warning(
            config.profile,
            config.public_url,
            language=config.language,
            tunnel=config.tunnel,
        )
        if note:
            _write_console_utf8(note)
        if config.language == "ru":
            print(
                "Не закрывайте KaroX: мост работает, пока открыто это окно. "
                "Ctrl+C — остановить.",
                flush=True,
            )
        else:
            print(
                "Keep KaroX open while using the connector. "
                "Press Ctrl+C to stop the bridge and tunnel.",
                flush=True,
            )

        while True:
            if _consume_stop_request(session_id, os.getpid()):
                if persistent_session and config.saved_profile_name:
                    try:
                        from .saved_bridge_supervisor import set_saved_bridge_desired_running

                        set_saved_bridge_desired_running(
                            config.saved_profile_name,
                            False,
                        )
                    except OSError:
                        # The stop request itself is authoritative for this owner.
                        # A state-write failure is still surfaced indirectly by a
                        # supervisor restart, but must not trap the owner forever.
                        pass
                print("KaroX saved bridge stop requested; shutting down cleanly...", flush=True)
                return 0

            loop_now = time.monotonic()
            if (
                persistent_session
                and config.saved_profile_name
                and loop_now >= supervisor_check_at
            ):
                supervisor_check_at = loop_now + 10.0
                try:
                    from .saved_bridge_supervisor import ensure_saved_bridge_supervisor

                    supervisor_pid = ensure_saved_bridge_supervisor(
                        config.saved_profile_name,
                        desired_running=None,
                    )
                    watchdog_record["supervisor_pid"] = supervisor_pid
                    watchdog_record.pop("last_supervisor_error", None)
                    publish()
                except Exception as exc:
                    watchdog_record["last_supervisor_error"] = type(exc).__name__
                    publish()

            bridge_code = bridge.poll()
            if bridge_code is not None:
                detail = bridge_output.detail()
                if not persistent_session or not config.saved_profile_name:
                    raise WebBridgeLaunchError(
                        "KaroX bridge stopped unexpectedly with code "
                        f"{bridge_code}{detail}"
                    )
                if loop_now < child_respawn_retry_at:
                    # A child that will not come back yet must not be respawned in
                    # a tight loop: the backoff below is what keeps the owner, the
                    # OAuth identity and the public URL alive across it.
                    time.sleep(0.2)
                    continue

                bridge_recovery_count += 1
                watchdog_record["last_bridge_exit_code"] = bridge_code
                watchdog_record["last_bridge_exit_detail"] = detail.lstrip(": ")[:1000]
                watchdog_record["bridge_pid"] = None
                watchdog_record["bridge_recoveries"] = bridge_recovery_count
                watchdog_record["last_bridge_recovery_at"] = time.time()
                publish()
                print(
                    "[bridge] Local MCP child stopped unexpectedly; restarting it "
                    "without rotating OAuth/session identity or the public URL…",
                    flush=True,
                )
                if bridge.stdout is not None:
                    try:
                        bridge.stdout.close()
                    except OSError:
                        pass
                # The child that just died may still be releasing the listener.
                # Spawning into an occupied port makes the replacement fail to
                # bind, which used to be fatal for the owner as well.
                _wait_for_local_port_release(config.port)
                try:
                    bridge, bridge_output = spawn_bridge_child()
                    watchdog_record["bridge_pid"] = _pid_of(bridge)
                    publish()
                    _wait_for_bridge(bridge, config.port, output=bridge_output)
                except WebBridgeLaunchError as exc:
                    # A failed *replacement* is a recoverable condition, not a
                    # reason to end the durable session. Ending it here dropped
                    # the public route and every MCP session with it, and left
                    # the sibling supervisor to rebuild the whole lifecycle.
                    child_respawn_failures += 1
                    delay = min(
                        _CHILD_RESPAWN_MAX_BACKOFF_SECONDS,
                        _CHILD_RESPAWN_MIN_BACKOFF_SECONDS
                        * float(2 ** min(child_respawn_failures - 1, 5)),
                    )
                    child_respawn_retry_at = time.monotonic() + delay
                    watchdog_record["bridge_pid"] = None
                    watchdog_record["child_respawn_failures"] = child_respawn_failures
                    watchdog_record["last_child_respawn_error"] = str(exc)[:300]
                    watchdog_record["last_child_respawn_failure_at"] = time.time()
                    publish()
                    print(
                        "[bridge] Replacement MCP child did not become ready; keeping "
                        f"the owner, OAuth identity and public URL and retrying in {delay:g}s.",
                        flush=True,
                    )
                    _stop_process(bridge)
                    continue
                child_respawn_failures = 0
                child_respawn_retry_at = 0.0
                watchdog_record.pop("last_child_respawn_error", None)
                if config.tunnel == "tailscale":
                    try:
                        _wait_for_public_mcp_route(
                            bridge,
                            public_url,
                            path=web_bridge_mcp_path(config.profile),
                            timeout_seconds=min(15.0, float(config.tunnel_timeout_seconds)),
                            output=bridge_output,
                        )
                    except WebBridgeLaunchError:
                        # The local MCP child is already healthy. Public ingress is
                        # supervised independently below; a simultaneous Funnel
                        # outage must not tear down the durable owner and rotate the
                        # whole lifecycle just after a successful child recovery.
                        watchdog_record["public_route_healthy"] = False
                        watchdog_record["last_public_route_recovery_pending_at"] = time.time()
                        publish()
                        print(
                            "[bridge] Local MCP child recovered; public Funnel is still "
                            "unhealthy and will be repaired independently.",
                            flush=True,
                        )
                print(
                    f"[bridge] Local MCP child recovered (recovery #{bridge_recovery_count}).",
                    flush=True,
                )
                continue
            if tunnel is not None:
                if isinstance(tunnel, TailscaleBackgroundFunnel):
                    now = time.monotonic()
                    if (
                        route_health is not None
                        and route_health.due(now)
                        and now >= tunnel_retry_at
                    ):
                        try:
                            owned, foreign = _matching_background_funnel_routes(
                                tunnel.executable,
                                public_url=tunnel.public_url,
                                port=tunnel.port,
                                https_port=tunnel.https_port,
                            )
                            if foreign:
                                # Fail closed: a route we do not own appeared on this
                                # daemon. Never reapply or remove anything based on a
                                # public-network probe when ownership is ambiguous.
                                route_health.observe(now=now, healthy=True)
                                watchdog_record["public_route_state"] = "ownership_ambiguous"
                                publish()
                            else:
                                public_healthy = public_mcp_route_healthy(
                                    public_url,
                                    path=web_bridge_mcp_path(config.profile),
                                )
                                route_present = bool(owned)
                                watchdog_record["public_route_healthy"] = public_healthy
                                watchdog_record["public_route_checked_at"] = time.time()
                                if public_healthy:
                                    watchdog_record["last_public_route_ok_at"] = time.time()
                                recovery_due = route_health.observe(
                                    now=now,
                                    healthy=route_present and public_healthy,
                                )
                                watchdog_record["public_route_failures"] = (
                                    route_health.consecutive_failures
                                )
                                publish()
                                if recovery_due:
                                    # Distinguish a dead/hung local MCP child from a
                                    # stale Funnel route. Restarting Funnel cannot heal
                                    # a listener that still owns the port but no longer
                                    # reaches KaroX authentication.
                                    local_healthy = public_mcp_route_healthy(
                                        f"http://127.0.0.1:{config.port}",
                                        path=web_bridge_mcp_path(config.profile),
                                        timeout_seconds=1.0,
                                    )
                                    watchdog_record["local_bridge_healthy"] = local_healthy
                                    watchdog_record["local_bridge_checked_at"] = time.time()
                                    if not local_healthy:
                                        assert bridge is not None
                                        print(
                                            "[bridge] Local MCP listener is alive but unhealthy; "
                                            "recycling only the owned child while preserving "
                                            "OAuth/session identity and the public URL…",
                                            flush=True,
                                        )
                                        watchdog_record["last_bridge_health_failure_at"] = time.time()
                                        watchdog_record["last_bridge_health_failure"] = (
                                            "local MCP auth probe did not reach the bridge"
                                        )
                                        publish()
                                        bridge.terminate()
                                        try:
                                            bridge.wait(timeout=3.0)
                                        except subprocess.TimeoutExpired:
                                            bridge.kill()
                                            bridge.wait(timeout=3.0)
                                        # The existing child-exit path on the next loop
                                        # performs the canonical respawn and crash-loop
                                        # accounting. Reset only this public failure streak.
                                        route_health.observe(
                                            now=time.monotonic(), healthy=True
                                        )
                                        continue

                                    reason = (
                                        "owned daemon Funnel route is missing"
                                        if not route_present
                                        else "public MCP ingress is unreachable"
                                    )
                                    print(
                                        f"[tailscale] {reason}; reapplying the exact owned "
                                        "route without restarting the bridge…",
                                        flush=True,
                                    )
                                    tunnel = refresh_tailscale_background_funnel(
                                        tunnel,
                                        timeout_seconds=config.tunnel_timeout_seconds,
                                    )
                                    route_health.recovered(now=time.monotonic())
                                    tunnel_recovery_failures = 0
                                    tunnel_retry_at = 0.0
                                    watchdog_record["tunnel_pid"] = None
                                    watchdog_record["tunnel_recoveries"] = route_health.recovery_count
                                    watchdog_record["last_tunnel_recovery_at"] = time.time()
                                    watchdog_record.pop("last_tunnel_recovery_error", None)
                                    publish()
                                    print("[tailscale] Daemon Funnel route restored.", flush=True)
                        except Exception as exc:
                            # Tunnel repair is best-effort supervision. A transient
                            # tailscaled/CLI failure must not kill the durable bridge
                            # owner and discard its OAuth/session identity.
                            tunnel_recovery_failures += 1
                            delay = min(60.0, float(2 ** min(tunnel_recovery_failures, 5)))
                            tunnel_retry_at = time.monotonic() + delay
                            watchdog_record["tunnel_recovery_failures"] = tunnel_recovery_failures
                            watchdog_record["last_tunnel_recovery_error"] = type(exc).__name__
                            watchdog_record["last_tunnel_recovery_attempt_at"] = time.time()
                            publish()
                            print(
                                "[tailscale] Funnel recovery attempt failed; keeping the "
                                f"local bridge alive and retrying in {delay:g}s "
                                f"({type(exc).__name__}).",
                                flush=True,
                            )
                elif isinstance(tunnel, TailscaleForegroundFunnel):
                    tunnel_code = tunnel.process.poll()
                    now = time.monotonic()
                    recovery_reason: Optional[str] = None
                    if tunnel_code is not None:
                        recovery_reason = f"foreground process exited with code {tunnel_code}"
                    elif route_health is not None and route_health.due(now):
                        healthy = public_mcp_route_healthy(
                            public_url, path=web_bridge_mcp_path(config.profile)
                        )
                        if route_health.observe(now=now, healthy=healthy):
                            recovery_reason = (
                                f"public MCP ingress failed {route_health.failure_threshold} "
                                "consecutive health probes"
                            )
                    if recovery_reason is not None and now >= tunnel_retry_at:
                        print(
                            f"[tailscale] Route unhealthy ({recovery_reason}); "
                            "recycling the owned Funnel without restarting the bridge…",
                            flush=True,
                        )
                        try:
                            tunnel = recover_tailscale_foreground_funnel(
                                tunnel,
                                port=config.port,
                                expected_public_url=public_url,
                                executable=config.tailscale,
                                timeout_seconds=config.tunnel_timeout_seconds,
                                job=job,
                                emit=lambda line: print(line, flush=True),
                            )
                            if route_health is not None:
                                route_health.recovered(now=time.monotonic())
                            tunnel_recovery_failures = 0
                            tunnel_retry_at = 0.0
                            watchdog_record["tunnel_pid"] = _pid_of(tunnel.process)
                            watchdog_record["tunnel_recoveries"] = (
                                route_health.recovery_count if route_health is not None else 1
                            )
                            watchdog_record["last_tunnel_recovery_at"] = time.time()
                            watchdog_record.pop("last_tunnel_recovery_error", None)
                            publish()
                            print("[tailscale] Public MCP route recovered.", flush=True)
                        except Exception as exc:
                            tunnel_recovery_failures += 1
                            delay = min(60.0, float(2 ** min(tunnel_recovery_failures, 5)))
                            tunnel_retry_at = time.monotonic() + delay
                            watchdog_record["tunnel_recovery_failures"] = tunnel_recovery_failures
                            watchdog_record["last_tunnel_recovery_error"] = type(exc).__name__
                            watchdog_record["last_tunnel_recovery_attempt_at"] = time.time()
                            publish()
                            print(
                                "[tailscale] Funnel recovery attempt failed; keeping the "
                                f"local bridge alive and retrying in {delay:g}s "
                                f"({type(exc).__name__}).",
                                flush=True,
                            )
                else:
                    tunnel_code = tunnel.process.poll()
                    if tunnel_code is not None:
                        raise WebBridgeLaunchError(
                            f"{config.tunnel} tunnel stopped unexpectedly with code {tunnel_code}"
                        )
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\nStopping KaroX web bridge…", flush=True)
        exit_reason = "keyboard_interrupt"
        return 0
    except BaseException as exc:
        # Nothing else records this. The detached owner's console goes to
        # DEVNULL and the watchdog record is removed below, so an owner that
        # died left the user with a dead connector and no reason anywhere on
        # disk. The reason is redacted metadata: an exception type and its own
        # message, never a secret, a token or tool output.
        exit_reason = type(exc).__name__
        exit_detail = str(exc)[:500]
        raise
    finally:
        heartbeat_stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=2.0)
        _record_owner_exit(
            session_id,
            saved_profile=config.saved_profile_name,
            reason=exit_reason,
            detail=exit_detail,
        )
        _stop_process(bridge)
        if bridge_output is not None:
            # The child is gone, so the drain thread is at end of pipe; joining it
            # before the handle closes is what keeps that read from failing.
            bridge_output.reader.join(timeout=2.0)
        if bridge is not None and bridge.stdout is not None:
            bridge.stdout.close()
        if tunnel is not None:
            tunnel.stop()
        _close_job(job)
        if watchdog is not None:
            # Only ever remove a record that is still ours. A durable record can
            # legitimately have been reclaimed by a successor while this owner was
            # still shutting down, and an unconditional unlink then deletes the
            # live successor's registration -- leaving a serving bridge that
            # nothing on disk knows about, which is the state the record exists
            # to prevent.
            _release_own_watchdog(watchdog)
        discard_credential = credential_created and (
            not persistent_session or not bridge_ready
        )
        if discard_credential and persistent_session:
            # For a durable profile the keyring name is the *shared* identity of
            # the saved connector, and the OS keyring is machine-wide while the
            # owner lock lives under KAROX_RUNTIME_DIR. A losing owner -- one
            # that minted the token and then failed, typically on a port bind --
            # would therefore delete the very secret a live owner of the same
            # profile is serving with, leaving the service up but its identity
            # destroyed and every configured connector unable to re-authenticate.
            # Only discard when this process is provably the sole owner.
            if not _saved_bridge_identity_is_exclusive(config.saved_profile_name):
                discard_credential = False
        if discard_credential:
            # A durable credential becomes part of the saved connector identity
            # only after the bridge actually reached readiness. If the first
            # launch failed earlier, discard that never-used token; the retained
            # repository-bound session can generate a fresh one on the retry.
            try:
                BridgeCredentialStore().delete(session_id)
            except Exception:
                pass
        if session_created and sessions is not None and not persistent_session:
            try:
                sessions.revoke(session_id)
            except Exception:
                pass
        _release_saved_bridge_owner_lock(owner_lock)


# --------------------------------------------------------------------------- #
# B7: Saved-bridge service API for TUI Start/Repair and Stop                  #
# --------------------------------------------------------------------------- #
#
# These two functions are the canonical service the TUI ServiceConnectScreen
# calls for the S (Start / Repair) and X (Stop) keys. They reuse the *same*
# production launch path the CLI ``bridge connect --saved NAME`` uses -- by
# launching it as a detached KaroX-owned process that survives TUI close --
# and the same ownership check ``bridge stop --saved NAME`` uses.
#
# Contract:
#   * Start/Repair is idempotent: a proven live bridge is reused, not relaunched.
#   * A foreign process on the port is never touched.
#   * A stale owned PID (reused) is never terminated.
#   * Credentials are never rotated, OAuth never reset, ClickUp/Chrome never
#     disturbed.
#   * The result never contains a Bearer secret, approval password, OAuth
#     token, Authorization header, tool arguments, or file contents.
#   * The bridge survives TUI close because it is a detached process.


def _saved_bridge_repository(profile: Any) -> Optional[Path]:
    """Resolve a saved profile's repository path, or ``None`` if unset."""
    repo = getattr(profile, "repository", None)
    if not repo:
        return None
    return Path(str(repo))


def _verify_bridge_endpoint(
    public_url: str,
    secret: str,
    *,
    path: str = "/mcp",
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    """Verify a running bridge endpoint without leaking the secret.

    Returns a redacted dict with:
      * ``unauth_401``: True if an unauthenticated request was rejected with 401.
      * ``auth_initialized``: True if an authenticated MCP initialize succeeded.
      * ``tools_list_ok``: True if an authenticated tools/list succeeded.
      * ``tool_count``: number of tools discovered (0 if tools/list failed).
    Never returns the secret, Authorization header, or tool arguments.
    """
    import httpx

    from datetime import timedelta

    endpoint = f"{public_url.rstrip('/')}{path}"
    result: dict[str, Any] = {
        "unauth_401": False,
        "auth_initialized": False,
        "tools_list_ok": False,
        "tool_count": 0,
        "endpoint": endpoint,
    }

    # 1. Unauthenticated request must be rejected with 401.
    try:
        with httpx.Client(timeout=timeout_seconds, follow_redirects=False) as client:
            response = client.get(endpoint)
            result["unauth_401"] = response.status_code == 401
    except Exception:
        # Network/TLS failure: leave unauth_401=False.
        pass

    # 2. Authenticated MCP initialize + tools/list via the Streamable HTTP
    #    transport, with the bridge credential as the Bearer token. The secret
    #    is used only inside this function and never returned.
    headers = {"Authorization": f"Bearer {secret}"}
    try:
        from mcp import ClientSession
        from .mcp_client import streamable_http_transport
        import anyio

        async def _run() -> list[Any]:
            with anyio.fail_after(timeout_seconds):
                async with streamable_http_transport(
                    endpoint,
                    headers=headers,
                    timeout_seconds=timeout_seconds,
                    terminate_on_close=False,
                ) as streams:
                    async with ClientSession(
                        streams[0],
                        streams[1],
                        read_timeout_seconds=timedelta(seconds=timeout_seconds),
                    ) as session:
                        await session.initialize()
                        response = await session.list_tools()
                        return list(response.tools)

        tools = anyio.run(_run)
        result["auth_initialized"] = True
        result["tools_list_ok"] = True
        result["tool_count"] = len(tools)
    except Exception:
        # Classification is the caller's job; this helper just reports booleans.
        pass

    return result


def start_saved_bridge(
    profile_name: str,
    *,
    timeout_seconds: float = 120.0,
) -> dict[str, Any]:
    """Start or repair the production persistent bridge for a saved profile.

    This is the canonical service the TUI ServiceConnectScreen S key calls. It
    reuses the *same* production launch path as ``karox bridge connect --saved
    NAME`` by spawning it as a detached KaroX-owned process that survives TUI
    close. The bridge is a real persistent process -- not a subprocess hack,
    not a shell job, not a test signal file.

    Idempotency: if a proven live bridge for this profile already exists, it is
    reused and no second bridge is launched. A foreign process on the port is
    never touched. A stale owned PID (reused by the OS) is never terminated.

    Never rotates credentials, resets OAuth, changes the public hostname, or
    disturbs ClickUp / personal Chrome.

    Returns a redacted dict with:
      * ``saved_profile``, ``session_id``, ``port``, ``public_url``, ``endpoint``
      * ``bridge_pid``, ``tunnel_pid``, ``owner_pid``
      * ``credential_fingerprint`` (sha256 prefix, never the secret)
      * ``credential_status`` (``"available"`` / ``"missing"`` / ``"error"``)
      * ``unauth_401``, ``auth_initialized``, ``tools_list_ok``, ``tool_count``
      * ``overall`` (``"ready_for_chatgpt_setup"`` / ``"waiting_for_chatgpt"`` / ...)
      * ``action`` (``"started"`` / ``"reused"`` / ``"error"``)
      * ``error`` (optional, short redacted string)

    Never contains: Bearer secret, approval password, OAuth token,
    Authorization header, tool arguments, file contents.
    """
    from .web_bridge_profiles import WebBridgeProfileStore, WebBridgeProfileError
    from .port_ownership import check_port_ownership
    from .connection_status import (
        CredentialStatus as _CredStatus,
        _check_bridge_credential,
    )

    def _error(message: str, **extra: Any) -> dict[str, Any]:
        return {
            "saved_profile": profile_name,
            "action": "error",
            "error": str(message)[:200],
            **extra,
        }

    # 1. Load the saved profile.
    try:
        profile = WebBridgeProfileStore().get(profile_name)
    except WebBridgeProfileError as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"cannot load saved profile: {type(exc).__name__}")

    port = profile.port
    session_id = saved_web_bridge_session_id(profile_name)

    # 2. Check repository exists.
    repository = _saved_bridge_repository(profile)
    if repository is not None and not repository.is_dir():
        return _error(
            f"repository does not exist: {repository}",
            session_id=session_id,
            port=port,
        )

    # 3. Check credential state without revealing the secret.
    #
    # A saved profile can legitimately exist before its first successful bridge
    # launch. In that bootstrap state the credential is MISSING because
    # run_web_bridge() creates it only after the public tunnel and repository
    # session are ready. Refusing to launch here creates a deadlock: the only
    # production path capable of creating the credential is exactly the detached
    # `bridge connect --saved NAME` process below. Missing is therefore allowed
    # for a stopped/free bridge and is re-checked after readiness. Revoked/error
    # credentials still fail closed.
    cred_status, cred_fingerprint = _check_bridge_credential(session_id)
    if cred_status in {_CredStatus.REVOKED, _CredStatus.ERROR}:
        return _error(
            "bridge credential is revoked or unresolved",
            session_id=session_id,
            port=port,
            credential_status=cred_status,
        )

    # 4. Check ownership: is a bridge already running for this profile?
    verdict = check_port_ownership(profile_name, port=port)
    if verdict.verdict == "reuse_same_profile" and verdict.metadata.pid_proven:
        # A live bridge cannot be safely claimed if its credential disappeared;
        # only the stopped/free bootstrap path is allowed to create a missing one.
        if cred_status != _CredStatus.AVAILABLE:
            return _error(
                "live bridge is serving with a credential that is no longer in "
                "the OS keyring; restart this saved profile to reissue it",
                session_id=session_id,
                port=port,
                credential_status=cred_status,
                recovery="restart",
            )
        # Idempotent: a proven live bridge already exists. Verify its endpoint
        # and return the current state without launching a second bridge.
        public_url = verdict.metadata.public_url or ""
        if public_url:
            # Resolve the secret *internally* for verification only.
            from .bridge import BridgeCredentialStore
            from .credentials import CredentialError

            ref = f"os-keyring:bridge/{session_id}"
            try:
                secret = BridgeCredentialStore().resolve(ref)
            except CredentialError:
                secret = None
            verification: dict[str, Any] = {}
            if secret:
                verification = _verify_bridge_endpoint(
                    public_url,
                    secret,
                    path=web_bridge_mcp_path(profile.target_profile),
                    timeout_seconds=15.0,
                )
            else:
                verification = {
                    "unauth_401": False,
                    "auth_initialized": False,
                    "tools_list_ok": False,
                    "tool_count": 0,
                    "endpoint": web_bridge_mcp_endpoint(public_url, profile.target_profile),
                }
            return {
                "saved_profile": profile_name,
                "session_id": session_id,
                "port": port,
                "public_url": public_url,
                "endpoint": verification.get(
                    "endpoint", web_bridge_mcp_endpoint(public_url, profile.target_profile)
                ),
                "bridge_pid": verdict.metadata.pid,
                "tunnel_pid": None,
                "owner_pid": verdict.metadata.pid,
                "credential_fingerprint": cred_fingerprint,
                "credential_status": "available",
                "unauth_401": verification.get("unauth_401", False),
                "auth_initialized": verification.get("auth_initialized", False),
                "tools_list_ok": verification.get("tools_list_ok", False),
                "tool_count": verification.get("tool_count", 0),
                "overall": _overall_status(
                    verification.get("auth_initialized", False),
                    verification.get("tools_list_ok", False),
                    verification.get("tool_count", 0),
                ),
                "action": "reused",
                "error": None,
            }

    if verdict.verdict == "unrelated_process":
        # A foreign process holds the port. Never touch it.
        return _error(
            f"port {port} is held by an unrelated process; cannot start bridge",
            session_id=session_id,
            port=port,
            verdict=verdict.verdict,
            unrelated_pid=verdict.unrelated_pid,
        )

    # 4b. A dead owner can leave its own bridge child holding the port. That
    # child is provably ours, so reclaim the port instead of failing forever.
    orphan_pid = getattr(verdict, "owned_orphan_pid", None)
    if isinstance(orphan_pid, int) and orphan_pid > 0:
        orphan_session = verdict.metadata.session_id or session_id
        # An owner whose watchdog record vanished is invisible to the ownership
        # check, so its still-serving child looks orphaned while the owner is
        # alive. Reclaiming only that child cannot work: the live owner respawns
        # it and every fresh owner then dies on the bind. Recycle the proven
        # owner tree first; saved-profile session, credential and public
        # hostname are all derived from the profile name, so none of them move.
        live_owner_pid = getattr(verdict, "live_unrecorded_owner_pid", None)
        if isinstance(live_owner_pid, int) and live_owner_pid > 0:
            if not _recycle_unrecorded_saved_bridge_owner(
                live_owner_pid, profile_name=profile_name, port=port
            ):
                return _error(
                    f"port {port} is held by this profile's own bridge whose "
                    "owner is running without a watchdog record, and that "
                    "proven owner could not be recycled",
                    session_id=session_id,
                    port=port,
                    verdict=verdict.verdict,
                    owned_orphan_pid=orphan_pid,
                    live_unrecorded_owner_pid=live_owner_pid,
                )
        elif not _reclaim_orphaned_bridge_listener(
            orphan_pid, port=port, session_id=orphan_session
        ):
            return _error(
                f"could not reclaim port {port} from this profile's orphaned "
                "bridge process",
                session_id=session_id,
                port=port,
                verdict=verdict.verdict,
                owned_orphan_pid=orphan_pid,
            )

    # 5. Persist running intent before launching. A preceding canonical Stop
    # leaves desired_running=false; spawning first creates a race where the new
    # owner can disappear before it has installed its sibling supervisor, after
    # which nothing is allowed to restore it.
    try:
        from .saved_bridge_supervisor import set_saved_bridge_desired_running

        set_saved_bridge_desired_running(profile_name, True)
    except OSError as exc:
        return _error(
            "cannot persist saved bridge running state",
            session_id=session_id,
            port=port,
            detail=type(exc).__name__,
        )

    # 6. Launch the production bridge as a detached KaroX-owned process.
    #    This reuses the exact CLI path: ``karox bridge connect --saved NAME``.
    #    The process is detached from the TUI so it survives TUI close, and it
    #    leaves the caller's job object when Windows permits -- when it does not
    #    (a Task Scheduler action, which is how an unattended revival gets here)
    #    the caller's job is the right owner anyway. See ``karox.detached_process``.
    launch_argv: list[str] = [
        sys.executable,
        "-m",
        "karox.cli",
        "bridge",
        "connect",
        "--saved",
        profile_name,
    ]
    child_cwd = str(repository) if repository is not None else None
    # Anything already on disk belongs to a previous owner. Only a record written
    # from here on can explain *this* child's exit.
    spawned_at = time.time()
    detached, spawn_mechanism = spawn_detached(launch_argv, cwd=child_cwd)
    if detached is None:
        return _error(
            f"cannot start detached bridge: {spawn_mechanism}",
            session_id=session_id,
            port=port,
        )

    # 7. Poll the watchdog record until the bridge is ready (bridge_pid set,
    #    public_url set, owner alive). The detached process writes it during
    #    its run_web_bridge() call.
    watchdog_path = watchdog_dir() / f"{session_id}.json"
    deadline = time.monotonic() + timeout_seconds
    watchdog_record: Optional[dict[str, Any]] = None
    while time.monotonic() < deadline:
        if watchdog_path.exists():
            try:
                watchdog_record = json.loads(
                    watchdog_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                watchdog_record = None
            if watchdog_record is not None:
                bridge_pid = watchdog_record.get("bridge_pid")
                public_url = watchdog_record.get("public_url")
                owner_pid = watchdog_record.get("owner_pid")
                if (
                    isinstance(bridge_pid, int)
                    and bridge_pid > 0
                    and isinstance(public_url, str)
                    and public_url
                    and isinstance(owner_pid, int)
                    and _process_is_alive(owner_pid)
                ):
                    break
        # If the detached process died early, stop waiting. Read the owner exit
        # record the child wrote; the detached console goes to DEVNULL so this
        # file is the only record of why it stopped.
        if detached.poll() is not None:
            exit_record_path = owner_exit_record_path(session_id)
            detail_lines: list[str] = []
            if exit_record_path.exists():
                try:
                    exit_payload = json.loads(
                        exit_record_path.read_text(encoding="utf-8")
                    )
                    recorded_at = (
                        exit_payload.get("recorded_at")
                        if isinstance(exit_payload, dict)
                        else None
                    )
                    fresh = (
                        isinstance(recorded_at, (int, float))
                        # Clock granularity, not a grace window: a record written
                        # in the same tick as the spawn is still this child's.
                        and float(recorded_at) >= spawned_at - 1.0
                    )
                    if isinstance(exit_payload, dict) and fresh:
                        reason = str(exit_payload.get("reason") or "")[:60]
                        detail = str(exit_payload.get("detail") or "")[:200]
                        if reason:
                            detail_lines.append(f"exit reason: {reason}")
                        if detail:
                            detail_lines.append(f"detail: {detail}")
                except (OSError, json.JSONDecodeError):
                    pass
            message = "detached bridge process exited before becoming ready"
            if detail_lines:
                message += f" ({', '.join(detail_lines)})"
            return _error(
                message,
                session_id=session_id,
                port=port,
            )
        time.sleep(0.5)
    else:
        return _error(
            f"bridge did not become ready within {timeout_seconds:.0f}s",
            session_id=session_id,
            port=port,
        )

    if watchdog_record is None:
        return _error(
            "no watchdog record was written",
            session_id=session_id,
            port=port,
        )

    public_url = str(watchdog_record.get("public_url") or "")
    bridge_pid = watchdog_record.get("bridge_pid")
    tunnel_pid = watchdog_record.get("tunnel_pid")
    owner_pid = watchdog_record.get("owner_pid")

    if not public_url:
        return _error(
            "bridge started but has no public URL",
            session_id=session_id,
            port=port,
            bridge_pid=bridge_pid,
        )

    # 8. Verify the endpoint: unauthenticated → 401, authenticated MCP initialize.
    #    The secret is resolved *internally* for verification only and never
    #    returned.
    from .bridge import BridgeCredentialStore
    from .credentials import CredentialError

    ref = f"os-keyring:bridge/{session_id}"
    try:
        secret = BridgeCredentialStore().resolve(ref)
    except CredentialError:
        secret = None
    if not secret:
        return _error(
            "credential vanished after launch",
            session_id=session_id,
            port=port,
            public_url=public_url,
            bridge_pid=bridge_pid,
        )
    # The first successful launch may have created the credential we deliberately
    # allowed to be missing during bootstrap. Refresh only its redacted status /
    # fingerprint now that readiness has proved the production process created it.
    post_cred_status, post_cred_fingerprint = _check_bridge_credential(session_id)
    if post_cred_status == _CredStatus.AVAILABLE:
        cred_fingerprint = post_cred_fingerprint

    verification = _verify_bridge_endpoint(
        public_url,
        secret,
        path=web_bridge_mcp_path(profile.target_profile),
        timeout_seconds=15.0,
    )

    # Readiness was observed once, but a bridge can still die between the
    # watchdog write and the endpoint check (an incompatible saved profile
    # does exactly that). Reporting "started" for a process that is already
    # gone told the user the connection works while it did not. One final
    # liveness check makes the result honest; the caller can retry.
    owner_alive = (
        isinstance(owner_pid, int)
        and owner_pid > 0
        and _process_is_alive(owner_pid)
    )
    if not owner_alive:
        return _error(
            "bridge process exited right after becoming ready",
            session_id=session_id,
            port=port,
            public_url=public_url,
            bridge_pid=bridge_pid,
            owner_pid=owner_pid,
            unauth_401=verification.get("unauth_401", False),
        )

    return {
        "saved_profile": profile_name,
        "session_id": session_id,
        "port": port,
        "public_url": public_url,
        "endpoint": verification.get(
            "endpoint", web_bridge_mcp_endpoint(public_url, profile.target_profile)
        ),
        "bridge_pid": bridge_pid,
        "tunnel_pid": tunnel_pid,
        "owner_pid": owner_pid,
        "credential_fingerprint": cred_fingerprint,
        "credential_status": "available",
        "unauth_401": verification.get("unauth_401", False),
        "auth_initialized": verification.get("auth_initialized", False),
        "tools_list_ok": verification.get("tools_list_ok", False),
        "tool_count": verification.get("tool_count", 0),
        "overall": _overall_status(
            verification.get("auth_initialized", False),
            verification.get("tools_list_ok", False),
            verification.get("tool_count", 0),
        ),
        "action": "started",
        "error": None,
    }


def _overall_status(
    auth_initialized: bool,
    tools_list_ok: bool,
    tool_count: int,
) -> str:
    """Compute the redacted overall status from verification booleans."""
    if auth_initialized and tools_list_ok and tool_count > 0:
        return "ready_for_chatgpt_setup"
    if auth_initialized:
        return "waiting_for_chatgpt"
    return "bridge_started_unverified"


def _sync_saved_bridge_session_access_profile(profile_name: str, profile: Any) -> bool:
    """Update a stopped saved bridge's durable session to the saved access profile.

    Saved-profile identity is intentionally stable across ordinary configuration
    edits.  Toggling Hyperagent Full access therefore must not rotate the session
    id, keyring credential, or OAuth grants just because the capability tier
    changes.  This helper mutates only the durable session's access_profile and
    leaves every identity/credential field untouched.

    It is called only from the owned restart path after the old bridge has been
    stopped, so no live runtime can observe a half-switched profile.
    """
    session_id = saved_web_bridge_session_id(profile_name)
    sessions = SessionStore(session_dir())
    if not sessions.state_path(session_id).exists():
        return False

    repository_raw = str(getattr(profile, "repository", "") or "").strip()
    if not repository_raw:
        raise SessionError("saved bridge profile has no repository")
    repository = Path(repository_raw).expanduser().resolve(strict=True)
    target_raw = getattr(profile, "access_profile", AccessProfile.READ_ONLY)
    target = target_raw if isinstance(target_raw, AccessProfile) else AccessProfile(str(target_raw))

    current = sessions.load(session_id)
    sessions.validate_repository(current, repository)
    if current.revoked:
        raise SessionError("saved bridge session has been revoked")
    if current.access_profile == target.value:
        return False

    with sessions.mutate(
        session_id,
        owner=f"saved-profile-access-{os.getpid()}",
        ttl_seconds=30.0,
    ) as record:
        sessions.validate_repository(record, repository)
        if record.revoked:
            raise SessionError("saved bridge session has been revoked")
        record.access_profile = target.value
    return True


def restart_saved_bridge(
    profile_name: str,
    *,
    timeout_seconds: float = 120.0,
    allow_legacy_migration: bool = False,
) -> dict[str, Any]:
    """Safely restart one saved bridge without exposing CLI details to the user.

    The existing ownership-checked stop path runs first. KaroX then waits for the
    local listener to disappear before using the canonical detached start path.
    Credentials, OAuth grants, saved profile identity, and public hostname are
    preserved. A foreign or still-live listener fails closed rather than being
    terminated or replaced.
    """
    from .web_bridge_profiles import WebBridgeProfileError, WebBridgeProfileStore

    try:
        profile = WebBridgeProfileStore().get(profile_name)
    except WebBridgeProfileError as exc:
        return {
            "saved_profile": profile_name,
            "action": "error",
            "error": str(exc)[:200],
        }
    except Exception as exc:
        return {
            "saved_profile": profile_name,
            "action": "error",
            "error": f"cannot load saved profile: {type(exc).__name__}",
        }

    stopped = stop_saved_bridge(
        profile_name,
        allow_legacy_migration=allow_legacy_migration,
    )
    if stopped.get("action") == "error":
        return {
            "saved_profile": profile_name,
            "action": "error",
            "phase": "stop",
            "error": str(stopped.get("error") or "bridge stop failed")[:200],
        }

    # taskkill/SIGTERM may return just before the TCP listener has completely
    # disappeared. Do not let that tiny race turn the old owner into an
    # "unrelated process" on the immediate start attempt.
    release_deadline = time.monotonic() + min(max(timeout_seconds, 1.0), 15.0)
    while not _port_is_available(profile.port):
        if time.monotonic() >= release_deadline:
            return {
                "saved_profile": profile_name,
                "action": "error",
                "phase": "stop",
                "port": profile.port,
                "error": "owned bridge stopped but its local listener did not release in time",
            }
        time.sleep(0.1)

    try:
        _sync_saved_bridge_session_access_profile(profile_name, profile)
    except (OSError, SessionError, ValueError) as exc:
        return {
            "saved_profile": profile_name,
            "action": "error",
            "phase": "session_profile",
            "port": profile.port,
            "error": f"cannot update durable session access profile: {type(exc).__name__}: {exc}"[:200],
        }

    started = dict(start_saved_bridge(profile_name, timeout_seconds=timeout_seconds))
    if started.get("action") == "error":
        started["phase"] = "start"
        return started
    started["action"] = "restarted"
    return started


def stop_saved_bridge(
    profile_name: str,
    *,
    allow_legacy_migration: bool = False,
) -> dict[str, Any]:
    """Stop the owned bridge/tunnel for a saved profile.

    Canonical service for the TUI ServiceConnectScreen X key. Uses the same
    ownership check as ``karox bridge stop --saved NAME``: only a proven
    KaroX-owned bridge may be stopped.

    Stop is not Revoke: this shuts down the bridge and tunnel processes but
    does NOT delete credentials, revoke OAuth authorization, or disturb
    ClickUp / personal Chrome.

    Returns a redacted dict with:
      * ``saved_profile``, ``action`` (``"stopped"`` / ``"no_action"`` / ``"error"``)
      * ``verdict``, ``reason``, ``pid``
      * ``error`` (optional)

    Never contains secrets.
    """
    from .web_bridge_profiles import WebBridgeProfileStore, WebBridgeProfileError
    from .port_ownership import check_port_ownership

    def _result(
        action: str,
        *,
        verdict: str,
        reason: str = "",
        pid: Optional[int] = None,
        error: Optional[str] = None,
    ) -> dict[str, Any]:
        return {
            "saved_profile": profile_name,
            "action": action,
            "verdict": verdict,
            "reason": str(reason)[:200],
            "pid": pid,
            "error": error,
        }

    try:
        profile = WebBridgeProfileStore().get(profile_name)
    except WebBridgeProfileError as exc:
        return _result("error", verdict="invalid_profile", error=str(exc))
    except Exception as exc:
        return _result(
            "error", verdict="invalid_profile", error=f"{type(exc).__name__}"
        )

    port = profile.port

    # Persist the user's intent before touching the owner process. Otherwise a
    # sibling supervisor could correctly observe the owner disappearing and
    # immediately resurrect a bridge the user explicitly asked to stop.
    try:
        from .saved_bridge_supervisor import set_saved_bridge_desired_running

        set_saved_bridge_desired_running(profile_name, False)
    except OSError as exc:
        return _result(
            "error",
            verdict="supervisor_state_error",
            reason="could not persist stopped state; refusing a stop that might auto-restart",
            error=str(exc)[:200],
        )

    ownership = check_port_ownership(profile_name, port=port)

    if ownership.verdict != "reuse_same_profile":
        # A live owner whose watchdog record vanished is not "nothing to stop":
        # it still holds the port and still respawns its child. It proves from
        # its own argv that it owns this saved profile, so it can be recycled
        # here instead of being reported as unmanageable.
        live_owner_pid = getattr(ownership, "live_unrecorded_owner_pid", None)
        if isinstance(live_owner_pid, int) and live_owner_pid > 0:
            if _recycle_unrecorded_saved_bridge_owner(
                live_owner_pid, profile_name=profile_name, port=port
            ):
                return _result(
                    "stopped",
                    verdict=ownership.verdict,
                    reason=(
                        f"stopped proven owner PID {live_owner_pid} that was "
                        "running without a watchdog record"
                    ),
                    pid=live_owner_pid,
                )
            return _result(
                "error",
                verdict=ownership.verdict,
                reason=(
                    "this profile's proven owner is running without a watchdog "
                    "record and could not be stopped"
                ),
                pid=live_owner_pid,
                error="unrecorded owner shutdown failed",
            )
        # Free, stale, or foreign: nothing safe to stop.
        return _result(
            "no_action",
            verdict=ownership.verdict,
            reason=ownership.reason,
            pid=ownership.metadata.pid,
        )

    pid = ownership.metadata.pid
    if pid is None or not ownership.metadata.pid_proven:
        # PID is not proven (reused or unverifiable): refuse to terminate.
        return _result(
            "no_action",
            verdict="stale_owned_process",
            reason="owner PID is alive but its identity cannot be proven; refusing to terminate",
            pid=pid,
        )

    supports_request = _watchdog_supports_stop_request(ownership.metadata.watchdog_path)
    if not supports_request:
        # Compatibility for a bridge started before request-v1 existed. A TUI
        # process is outside the bridge tree, so a one-time Windows tree kill is
        # safe there. A restart worker invoked *through* the bridge is inside the
        # tree; refuse the legacy kill or it would kill itself before relaunch.
        relation = _current_process_is_descendant_of(pid)
        if os.name == "nt" and (allow_legacy_migration or relation is False):
            try:
                completed = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    timeout=10,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError as exc:
                return _result(
                    "error",
                    verdict="reuse_same_profile",
                    reason=f"legacy bridge shutdown failed: {exc}",
                    pid=pid,
                    error=str(exc)[:200],
                )
            if completed.returncode not in {0, 128}:
                return _result(
                    "error",
                    verdict="reuse_same_profile",
                    reason="legacy bridge shutdown command failed",
                    pid=pid,
                    error="legacy bridge shutdown failed",
                )
            legacy_deadline = time.monotonic() + 10.0
            while time.monotonic() < legacy_deadline:
                if not _process_is_alive(pid) and _port_is_available(port):
                    watchdog_path = ownership.metadata.watchdog_path
                    if watchdog_path:
                        try:
                            Path(watchdog_path).unlink()
                        except OSError:
                            pass
                    return _result(
                        "stopped",
                        verdict="reuse_same_profile",
                        reason=f"stopped legacy owned bridge PID {pid}",
                        pid=pid,
                    )
                time.sleep(0.1)
            return _result(
                "error",
                verdict="reuse_same_profile",
                reason="legacy bridge stopped but its listener did not release in time",
                pid=pid,
                error="legacy bridge shutdown timed out",
            )
        return _result(
            "error",
            verdict="reuse_same_profile",
            reason="legacy bridge must be restarted from the KaroX UI once before in-bridge restart is safe",
            pid=pid,
            error="legacy bridge restart requires KaroX UI",
        )

    # Ask a request-v1 supervisor to leave through run_web_bridge's finally-block.
    # The caller survives because no broad descendant-tree termination occurs.
    session_id = ownership.metadata.session_id or saved_web_bridge_session_id(profile_name)
    try:
        request_path = _write_stop_request(session_id, pid)
    except OSError as exc:
        return _result(
            "error",
            verdict="reuse_same_profile",
            reason=f"failed to request shutdown: {exc}",
            pid=pid,
            error=str(exc)[:200],
        )

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if not _process_is_alive(pid) and _port_is_available(port):
            try:
                request_path.unlink()
            except OSError:
                pass

            from .saved_bridge_supervisor import saved_bridge_supervisor_status

            supervisor_deadline = time.monotonic() + 10.0
            while saved_bridge_supervisor_status(profile_name).get("supervisor_alive"):
                if time.monotonic() >= supervisor_deadline:
                    return _result(
                        "error",
                        verdict="reuse_same_profile",
                        reason="bridge owner stopped but its supervisor is still active",
                        pid=pid,
                        error="saved bridge supervisor shutdown timed out",
                    )
                time.sleep(0.1)
            return _result(
                "stopped",
                verdict="reuse_same_profile",
                reason=f"cleanly stopped owned bridge PID {pid}",
                pid=pid,
            )
        time.sleep(0.1)

    # A hung supervisor may eventually resume; do not leave a delayed stop
    # request armed after we have reported failure to the caller.
    try:
        request_path.unlink()
    except OSError:
        pass

    return _result(
        "error",
        verdict="reuse_same_profile",
        reason="owned bridge did not acknowledge the clean shutdown request in time",
        pid=pid,
        error="bridge shutdown timed out",
    )


def apply_saved_bridge_profile(
    profile_name: str,
    updated: Any,
    *,
    allow_restart: bool = False,
    allow_legacy_migration: bool = False,
) -> dict[str, Any]:
    """Atomically apply one saved-profile edit across persisted and live state.

    Ownership is sampled from the *previous* profile before any write.  This is
    what prevents a port/tunnel edit from making the old live bridge invisible to
    its own restart path.  A running or supervisor-desired profile is restarted;
    a stopped profile is only persisted.  Failed activation rolls the previous
    profile back and attempts to restore its prior running state.
    """
    from .port_ownership import check_port_ownership
    from .saved_bridge_supervisor import saved_bridge_supervisor_status
    from .web_bridge_profiles import WebBridgeProfileError, WebBridgeProfileStore

    store = WebBridgeProfileStore()
    try:
        previous = store.get(profile_name)
    except WebBridgeProfileError as exc:
        raise ValueError(str(exc)) from exc
    if getattr(updated, "name", None) != previous.name:
        raise ValueError("saved profile identity cannot be changed by an edit")
    if updated == previous:
        return {"status": "unchanged", "profile": previous}

    dynamic_project_fields = {"projects", "default_project_id"}
    profile_fields = getattr(previous, "__dataclass_fields__", {})
    projects_only = bool(profile_fields) and all(
        getattr(updated, name) == getattr(previous, name)
        for name in profile_fields
        if name not in dynamic_project_fields
    )
    if projects_only:
        # Project membership/default selection is a live routing policy, not
        # connector identity. Saved-profile bridge children reload this registry
        # on the next project-scoped tool call, so changing it must not rotate a
        # bearer secret, URL, OAuth grant, tunnel, or running owner process.
        store.put(updated)
        return {
            "status": "hot_updated",
            "profile": updated,
            "runtime": {
                "action": "unchanged",
                "live_reload": "next_tool_call",
            },
        }

    ownership = check_port_ownership(profile_name, port=previous.port)
    was_running = bool(
        ownership.verdict == "reuse_same_profile"
        and ownership.metadata.pid_proven
    )
    try:
        supervisor = saved_bridge_supervisor_status(profile_name)
        desired_running = bool(supervisor.get("desired_running"))
    except Exception:
        desired_running = False
    should_restart = was_running or desired_running
    if should_restart and not allow_restart:
        raise SavedBridgeRestartRequired(
            "saved bridge is running; explicit restart confirmation is required"
        )

    # A proven-free stopped profile does not need a lifecycle call at all. This
    # keeps a simple settings/credential edit side-effect free when nothing is
    # running. Other ownership verdicts still go through stop_saved_bridge so a
    # stale owned child can be reclaimed and any foreign holder fails closed.
    if not should_restart and ownership.verdict == "free":
        store.put(updated)
        return {
            "status": "saved",
            "profile": updated,
            "runtime": {"action": "stopped", "verdict": "free"},
        }

    # Stop before writing so all ownership checks still use the previous port,
    # repository binding and runtime identity.
    stopped = dict(
        stop_saved_bridge(
            profile_name,
            allow_legacy_migration=allow_legacy_migration,
        )
    )
    if stopped.get("action") == "error":
        raise RuntimeError(
            str(stopped.get("error") or "could not stop the owned bridge")[:240]
        )

    store.put(updated)
    if not should_restart:
        return {"status": "saved", "profile": updated, "runtime": stopped}

    started = dict(
        start_saved_bridge(
            profile_name,
            allow_legacy_migration=allow_legacy_migration,
        )
    )
    if started.get("action") != "error":
        return {"status": "restarted", "profile": updated, "runtime": started}

    # Activation failed. Restore the profile first, then restore the old runtime.
    store.put(previous)
    rollback = dict(
        start_saved_bridge(
            profile_name,
            allow_legacy_migration=allow_legacy_migration,
        )
    )
    message = str(started.get("error") or "new bridge configuration failed")[:240]
    if rollback.get("action") != "error":
        raise RuntimeError(f"{message}; previous connection was restored")
    rollback_error = str(rollback.get("error") or "rollback start failed")[:200]
    raise RuntimeError(f"{message}; rollback also failed: {rollback_error}")


def delete_saved_bridge_profile(
    profile_name: str,
    *,
    allow_legacy_migration: bool = False,
) -> dict[str, Any]:
    """Permanently remove one saved bridge and only resources it provably owns.

    This is the canonical destructive service for the TUI.  It deliberately does
    more than ``stop_saved_bridge``: durable bridge identity/session data, the
    saved profile, its managed Chrome profile, supervisor state and unshared
    browser-test credentials are retired.  Personal Chrome, other saved profiles,
    shared browser credentials and unverifiable live processes are never touched.
    """
    from .browser_credentials import BrowserCredentialReference, BrowserCredentialStore
    from .extension_browser import _terminate_profile_chrome
    from .managed_browser import BrowserInstanceRegistry, verify_managed_browser
    from .saved_bridge_supervisor import (
        saved_bridge_supervisor_status,
        supervisor_desired_state_path,
        supervisor_heartbeat_path,
        supervisor_state_path,
    )
    from .security import redact
    from .web_bridge_profiles import WebBridgeProfileError, WebBridgeProfileStore

    store = WebBridgeProfileStore()
    try:
        profile = store.get(profile_name)
    except WebBridgeProfileError as exc:
        return {
            "saved_profile": profile_name,
            "action": "error",
            "phase": "profile",
            "error": str(exc)[:200],
        }
    except Exception as exc:
        return {
            "saved_profile": profile_name,
            "action": "error",
            "phase": "profile",
            "error": f"cannot load saved profile: {type(exc).__name__}",
        }

    stopped = dict(
        stop_saved_bridge(
            profile_name,
            allow_legacy_migration=allow_legacy_migration,
        )
    )
    if stopped.get("action") == "error":
        return {
            "saved_profile": profile_name,
            "action": "error",
            "phase": "stop",
            "error": str(stopped.get("error") or "bridge stop failed")[:200],
        }

    try:
        identity_cleanup = delete_saved_web_bridge_identity(profile_name)
    except Exception as exc:
        return {
            "saved_profile": profile_name,
            "action": "error",
            "phase": "identity",
            "error": str(redact(str(exc)))[:200],
        }

    # Snapshot refs before the profile disappears, and compute which are shared
    # while all sibling profiles are still readable.
    attached_refs = tuple(profile.browser_credential_refs)
    shared_refs: set[str] = set()
    try:
        for sibling in store.list():
            if sibling.name == profile_name:
                continue
            shared_refs.update(sibling.browser_credential_refs)
    except Exception:
        # Losing this inventory must never cause us to delete a possibly shared
        # secret.  Treat every attached ref as shared and leave keyring cleanup
        # for doctor rather than guessing.
        shared_refs.update(attached_refs)

    try:
        deleted_profile = store.delete(profile_name)
    except Exception as exc:
        return {
            "saved_profile": profile_name,
            "action": "error",
            "phase": "profile_delete",
            "identity_cleanup": identity_cleanup,
            "error": f"identity revoked but profile cleanup failed: {type(exc).__name__}",
        }

    candidates = set(saved_web_bridge_session_candidates(profile_name))
    browser_registry = BrowserInstanceRegistry()
    browser_deleted: list[str] = []
    browser_cleanup_pending: list[str] = []
    for instance in browser_registry.list_instances():
        if (
            instance.saved_profile_id != profile_name
            or instance.session_id not in candidates
        ):
            continue
        alive = bool(instance.browser_pid > 0 and _process_is_alive(instance.browser_pid))
        if alive and not verify_managed_browser(instance):
            # A live process whose identity no longer proves ownership is exactly
            # the situation in which cleanup must fail closed.
            browser_cleanup_pending.append(instance.instance_id)
            continue
        if alive:
            _terminate_profile_chrome(Path(instance.user_data_dir))
            alive = bool(instance.browser_pid > 0 and _process_is_alive(instance.browser_pid))
        if alive:
            browser_cleanup_pending.append(instance.instance_id)
            continue
        if browser_registry.delete(instance.instance_id):
            browser_deleted.append(instance.instance_id)
        else:
            browser_cleanup_pending.append(instance.instance_id)

    credential_store = BrowserCredentialStore()
    credentials_deleted: list[str] = []
    credentials_shared: list[str] = []
    credentials_cleanup_pending: list[str] = []
    for reference in attached_refs:
        if reference in shared_refs:
            credentials_shared.append(reference)
            continue
        try:
            parsed = BrowserCredentialReference.parse(reference)
            credential_store.delete(parsed.name)
            credentials_deleted.append(reference)
        except Exception:
            # Runtime authority is already gone with the profile; a keyring
            # cleanup failure leaves only an inert local secret for doctor.
            credentials_cleanup_pending.append(reference)

    # ``stop_saved_bridge`` has already waited for the durable supervisor to
    # retire. Remove its secret-free telemetry/intent files only when it is truly
    # offline, otherwise retain evidence for doctor instead of racing a process.
    supervisor_cleanup = "pending"
    try:
        status = saved_bridge_supervisor_status(profile_name)
        if not bool(status.get("supervisor_alive")):
            for path in (
                supervisor_state_path(profile_name),
                supervisor_heartbeat_path(profile_name),
                supervisor_desired_state_path(profile_name),
            ):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            supervisor_cleanup = "deleted"
    except Exception:
        supervisor_cleanup = "pending"

    cleanup_pending = bool(
        browser_cleanup_pending
        or credentials_cleanup_pending
        or supervisor_cleanup != "deleted"
    )
    return {
        "saved_profile": profile_name,
        "action": "deleted",
        "profile": deleted_profile.to_dict(),
        "identity_cleanup": identity_cleanup,
        "browser_instances_deleted": browser_deleted,
        "browser_cleanup_pending": browser_cleanup_pending,
        "browser_credentials_deleted": credentials_deleted,
        "browser_credentials_shared": credentials_shared,
        "browser_credentials_cleanup_pending": credentials_cleanup_pending,
        "supervisor_cleanup": supervisor_cleanup,
        "cleanup_pending": cleanup_pending,
    }
