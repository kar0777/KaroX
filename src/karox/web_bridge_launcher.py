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
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from .bridge import BridgeCredentialStore, known_bridge_profiles
from .browser_access import BrowserAccessPolicy
from .credentials import CredentialError
from .hosted_bridge import (
    DEFAULT_HOSTED_DEADLINE_SECONDS,
    KNOWN_HOSTED_TOOL_NAMES,
)
from .hosted_tools_runtime import (
    ManagedServerProfile,
)
from .models import AccessProfile
from .process_launcher import resolve_executable as _resolve_executable
from .tailscale import (
    TailscaleError,
    classify_funnel_failure,
    prepare_tailscale_funnel,
)
from .paths import runtime_dir, session_dir
from .proxy_server import ALLOWED_HOSTS_ENVIRONMENT
from .sessions import SessionError, SessionStore


WEB_BRIDGE_PROFILES = ("chatgpt-web", "claude-web", "hyperagent-web")
# The redirect hosts a strict profile will send authorization codes to. ``None``
# means "any HTTPS host" -- the permissive default chatgpt-web/claude-web and
# library callers rely on. hyperagent-web pins the one verified canonical host.
# The exact callback path is deliberately not hardcoded: the documented
# endpoint has moved (``/api/mcp`` today, ``/api/mcp-serve`` in one screenshot
# that no source corroborates), so the host is pinned and any path on it is
# accepted. Arbitrary subdomains, wildcards, HTTP, and non-web schemes are
# rejected by the structural check in ``_redirect_uri`` before the host is ever
# compared.
PROFILE_REDIRECT_HOSTS: dict[str, Optional[frozenset[str]]] = {
    "hyperagent-web": frozenset({"hyperagent.com"}),
}


def profile_redirect_hosts(profile: str) -> Optional[frozenset[str]]:
    """Return the pinned redirect-host allowlist for a strict profile, or None."""
    return PROFILE_REDIRECT_HOSTS.get(profile)


DEFAULT_WEB_TOOLS = (
    "karox.repo.read_file",
    "karox.repo.read_lines",
    "karox.repo.list_files",
    "karox.repo.search",
    "karox.git.status",
    "karox.git.diff",
    "karox.git.log",
    "karox.runtime.status",
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
)
WRITE_WEB_TOOLS = (
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
    "karox.browser.select",
    "karox.browser.press",
    "karox.browser.close",
    "karox.dev_server.start",
    "karox.dev_server.stop",
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
        # Supplying a verification allowlist is the explicit approval needed by
        # checks.run. Hiding the tool after accepting that allowlist produced a
        # contradictory bridge: diagnostics listed approved commands while the
        # client received tool_not_exposed. Publish both guarded verification
        # surfaces whenever a write-capable profile carries that approval.
        if verification_commands:
            include(CHECKS_RUN_TOOL)
            include("karox.tests.run")

    browser_family = set(BROWSER_READ_TOOL_NAMES) | set(BROWSER_INPUT_TOOL_NAMES)
    if selected.intersection(browser_family):
        include("karox.browser.command")

    return tuple(ordered)


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


class WebBridgeLaunchError(RuntimeError):
    """A managed web bridge could not be prepared or kept alive."""


@dataclass(frozen=True)
class WebBridgeConnectConfig:
    profile: str
    repository: Path
    port: int = 8765
    tools: tuple[str, ...] = DEFAULT_WEB_TOOLS
    session_id: Optional[str] = None
    # The dangerous profile must be asked for. A caller that forgets to pass one
    # publishes a repository to a third-party agent, so the omission defaults to
    # the profile that cannot write.
    access_profile: AccessProfile = AccessProfile.READ_ONLY
    tunnel: str = "cloudflare"
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
    language: str = "en"
    saved_profile_name: Optional[str] = None

    def __post_init__(self) -> None:
        if self.profile not in WEB_BRIDGE_PROFILES:
            raise ValueError("web bridge profile must be chatgpt-web or claude-web")
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
        object.__setattr__(
            self,
            "tools",
            _include_stable_worker_commands(
                tuple(self.tools),
                access_profile=self.access_profile,
                verification_commands=tuple(self.verification_commands),
            ),
        )
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
        )
        object.__setattr__(self, "browser_allowed_domains", browser_policy.allowed_domains)
        object.__setattr__(self, "browser_denied_domains", browser_policy.denied_domains)
        object.__setattr__(self, "browser_allowed_emails", browser_policy.allowed_emails)
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
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


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


def _stop_process(process: Optional[subprocess.Popen[str]]) -> None:
    if process is None or process.poll() is not None:
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
        for raw_line in stream:
            line = raw_line.rstrip()
            output_tail.append(line)
            print(f"[{name}] {line}", flush=True)

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
        for raw_line in stream:
            line = raw_line.rstrip()
            output_tail.append(line)
            match = _QUICK_TUNNEL_URL.search(line)
            if match and state["public_url"] is None:
                state["public_url"] = match.group(0)
                ready.set()
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
    restart_service: bool = True,
    elevated_run: Optional[Callable[..., subprocess.CompletedProcess[str]]] = None,
) -> TailscaleForegroundFunnel:
    """Start a stable Funnel without replacing or globally resetting routes.

    ``emit`` receives human-readable progress while Tailscale is brought online
    (see :func:`karox.tailscale.ensure_tailscale_ready`); the bridge launcher
    passes a flushed ``print`` so a user who confirmed ``bridge connect`` sees
    that the daemon is being started rather than a silent hang. ``restart_service``
    is on by default: a stuck engine (the state a freshly-booted Windows machine
    reports) is recovered by restarting the Tailscale service -- one UAC prompt
    the user consents to -- instead of the bridge dying on a daemon ``up`` cannot
    fix.
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
        for raw_line in stream:
            line = raw_line.rstrip()
            output_tail.append(line)
            print(f"[tailscale] {line}", flush=True)
            if plan.public_url in line or ".ts.net" in line:
                confirmed["public_url"] = True
                ready.set()
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


def _port_is_available(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", port))
    except OSError:
        return False
    return True


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
                return
        except OSError:
            time.sleep(0.05)
    suffix = _tail_detail(output.tail) if output is not None else ""
    raise WebBridgeLaunchError(f"KaroX bridge did not open its local port{suffix}")


def watchdog_dir() -> Path:
    return runtime_dir() / "web-bridge"


def write_watchdog(path: Path, payload: dict[str, Any]) -> None:
    """Record the running bridge so a survivor of a hard kill is findable.

    The file deliberately holds no secret: it is only the identity a later run
    needs to decide that a public tunnel is orphaned and revoke it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


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
                "  5. На странице KaroX вставьте пароль подтверждения из строки выше и нажмите «Разрешить».",
                "Важно: пароль вводится только на странице KaroX, не в настройках приложения.",
            )
        if profile == "hyperagent-web":
            return (
                "Как подключить мост к HyperAgent:",
                "  1. В HyperAgent откройте Add MCP server.",
                "  2. Name: KaroX.",
                "  3. URL: MCP URL из строки выше (заканчивается на /mcp).",
                "  4. Оставьте «Bring my own OAuth app» выключенным: KaroX публикует OAuth discovery и поддерживает Dynamic Client Registration, поэтому Client ID и Client Secret вводить вручную не нужно.",
                "  5. Включите «I trust this server», только если вы доверяете этому локальному экземпляру KaroX.",
                "  6. Нажмите Connect.",
                "  7. На открывшейся странице KaroX вставьте пароль подтверждения из строки выше и нажмите «Разрешить».",
                "Важно: пароль подтверждения вводится только на странице KaroX, а не в настройках HyperAgent. Не вставляйте ключ KaroX в поле Client Secret.",
            )
        return (
            "Как подключить мост к Claude:",
            "  1. Нужен тариф Claude с поддержкой custom connectors.",
            "  2. Откройте Claude: Settings → Connectors → Add custom connector.",
            "  3. Вставьте MCP URL из строки выше и добавьте connector.",
            "  4. На странице KaroX вставьте пароль подтверждения из строки выше и нажмите «Разрешить».",
            "Важно: пароль вводится только на странице KaroX, не в настройках коннектора.",
        )
    if profile == "chatgpt-web":
        return (
            "How to connect the bridge to ChatGPT:",
            "  1. Use ChatGPT Web with access to developer mode and custom MCP apps.",
            "  2. Open Settings → Apps and enable developer mode in Advanced Settings when available.",
            "  3. Create a custom app from Apps → Create and paste the MCP URL shown above.",
            "  4. Scan tools and complete the OAuth prompt through KaroX.",
            "  5. On the KaroX page, paste the approval password shown above and click Authorize.",
            "Important: enter the password only on the KaroX page, not in the app settings.",
        )
    if profile == "hyperagent-web":
        return (
            "How to connect the bridge to HyperAgent:",
            "  1. In HyperAgent, open Add MCP server.",
            "  2. Name: KaroX.",
            "  3. URL: the MCP URL shown above (it ends in /mcp).",
            "  4. Leave \"Bring my own OAuth app\" disabled: KaroX publishes OAuth discovery metadata and supports Dynamic Client Registration, so you do not enter a Client ID or Client Secret by hand.",
            "  5. Enable \"I trust this server\" only if you trust this local KaroX instance.",
            "  6. Click Connect.",
            "  7. On the KaroX page that opens, paste the approval password shown above and click Authorize.",
            "Important: enter the approval password only on the KaroX page, never in HyperAgent settings. Do not put the KaroX key in the Client Secret field.",
        )
    return (
        "How to connect the bridge to Claude:",
        "  1. Open Claude Settings → Connectors → Add custom connector.",
        "  2. Paste the MCP URL shown above and start connecting.",
        "  3. On the KaroX page, paste the approval password shown above and click Authorize.",
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
        raise WebBridgeLaunchError(_watchdog_conflict(path)) from None
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(body)


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
        entries = sorted(watchdog_dir().glob("*.json"))
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
        persistent = bool(
            record.get("persistent_session") if isinstance(record, dict) else False
        )
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
        "--public-url",
        public_url,
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
    redirect_hosts = profile_redirect_hosts(config.profile)
    if redirect_hosts:
        values.extend(
            ("--allowed-redirect-hosts", ",".join(sorted(redirect_hosts)))
        )
    for tool in config.tools:
        values.extend(("--tool", tool))
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
    return tuple(values)


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
        if tool in config.tools:
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
    return {
        "schema_version": 1,
        "saved_profile": config.saved_profile_name,
        "target_profile": config.profile,
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
            "no_git_push": True,
            "no_publish": True,
            "no_auth_commands": True,
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

    tunnel: Optional[CloudflareQuickTunnel | TailscaleForegroundFunnel] = None
    bridge: Optional[subprocess.Popen[str]] = None
    bridge_output: Optional[MirroredChildOutput] = None
    credential_created = False
    session_created = False
    bridge_ready = False
    sessions: Optional[SessionStore] = None
    watchdog: Optional[Path] = None
    persistent_session = _persistent_session(config)
    session_id = _session_id(config)
    job = _create_child_job()
    if job is None and os.name == "nt":
        print(
            "Warning: this bridge could not create a Windows job object, so a "
            "hard kill of KaroX may leave the tunnel running.",
            flush=True,
        )
    try:
        if config.tunnel == "cloudflare":
            tunnel = start_cloudflare_quick_tunnel(
                config.port,
                executable=config.cloudflared,
                timeout_seconds=config.tunnel_timeout_seconds,
                job=job,
            )
            public_url = tunnel.public_url
        elif config.tunnel == "tailscale":
            tunnel = start_tailscale_foreground_funnel(
                config.port,
                executable=config.tailscale,
                timeout_seconds=config.tunnel_timeout_seconds,
                job=job,
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
            "persistent_session": persistent_session,
            "port": config.port,
            "public_url": public_url,
            "tunnel": config.tunnel,
            "url_stability": web_bridge_diagnostics(config)["url_stability"],
            "effective_deadline_seconds": config.deadline_seconds,
            "available_tools": list(config.tools),
            "started_at": time.time(),
            "tunnel_pid": _pid_of(tunnel.process if tunnel else None),
            "bridge_pid": None,
        }
        # `watchdog` is what the cleanup below deletes, so it is assigned only
        # once the claim succeeded: this process must never remove a record it
        # does not own.
        claim_watchdog(watchdog_path, watchdog_record)
        watchdog = watchdog_path

        sessions = SessionStore(session_dir())
        state_exists = sessions.state_path(session_id).exists()
        if persistent_session and state_exists:
            try:
                record = sessions.load(session_id)
                sessions.validate_repository(record, repository)
            except SessionError as exc:
                raise WebBridgeLaunchError(
                    f"saved bridge session is invalid: {exc}"
                ) from exc
            if record.revoked:
                raise WebBridgeLaunchError(
                    "saved bridge session was revoked; delete and recreate the saved profile"
                )
            if record.access_profile != config.access_profile.value:
                raise WebBridgeLaunchError(
                    "saved bridge access profile changed; delete and recreate the saved profile"
                )
        else:
            sessions.create(
                repository,
                f"{config.profile} managed web bridge",
                config.access_profile,
                session_id=session_id,
            )
            session_created = True

        credential_store = BridgeCredentialStore()
        if persistent_session:
            try:
                secret = credential_store.resolve(
                    f"os-keyring:bridge/{session_id}"
                )
            except CredentialError:
                credential = credential_store.set(session_id)
                credential_created = True
                secret = credential.get("secret")
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
        try:
            bridge = subprocess.Popen(
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
        _adopt_child(job, bridge)
        bridge_output = _mirror_child_output(bridge, name="bridge")
        watchdog_record["bridge_pid"] = _pid_of(bridge)
        write_watchdog(watchdog, watchdog_record)
        _wait_for_bridge(bridge, config.port, output=bridge_output)
        bridge_ready = True

        endpoint = f"{public_url.rstrip('/')}/mcp"
        print(f"KaroX {config.profile} bridge is ready")
        print(f"MCP URL: {endpoint}")
        print(f"OAuth approval password: {secret}")
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
            bridge_code = bridge.poll()
            if bridge_code is not None:
                raise WebBridgeLaunchError(
                    "KaroX bridge stopped unexpectedly with code "
                    f"{bridge_code}{bridge_output.detail()}"
                )
            if tunnel is not None:
                tunnel_code = tunnel.process.poll()
                if tunnel_code is not None:
                    raise WebBridgeLaunchError(
                        f"{config.tunnel} tunnel stopped unexpectedly with code {tunnel_code}"
                    )
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\nStopping KaroX web bridge…", flush=True)
        return 0
    finally:
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
            try:
                watchdog.unlink()
            except OSError:
                pass
        if credential_created and (not persistent_session or not bridge_ready):
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
