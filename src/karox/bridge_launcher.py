"""Lifecycle layer for ``karox bridge connect`` and the bridge child.

This module owns everything around the MCP server itself:

* validation of the effective connection configuration;
* creation/reactivation of the durable repository session;
* the bridge credential in the OS keyring (write once, never print);
* the stable Tailscale Funnel route (reuse when owned, create when free);
* spawning, supervising and stopping the local ``bridge serve`` child;
* loopback and public readiness probes;
* watchdog records, single-owner locking and cooperative stop requests;
* safe, redacted start/stop/status helpers for saved profiles.

The child is always ``sys.executable -m karox.cli bridge serve ...`` and binds
loopback only. No secret is ever printed or returned: callers receive a
fingerprint and an opaque ``os-keyring:bridge/<session>`` reference.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
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

from .bridge_profiles import (
    BridgeCredentialMissing,
    BridgeCredentialStore,
    BridgeProfileError,
    BridgeProfileStore,
    BridgeRegistry,
    SavedBridgeProfile,
    WEB_BRIDGE_PROFILES,
    DEFAULT_BRIDGE_DEADLINE_SECONDS,
    profile_redirect_hosts,
    profile_tailscale_https_port,
    saved_web_bridge_session_candidates,
    saved_web_bridge_session_id,
    watchdog_dir,
    web_bridge_mcp_endpoint,
)
from .bridge_tailscale import (
    TailscaleError,
    TailscaleFunnel,
    ensure_tailscale_funnel,
    find_owned_funnel,
    find_tailscale,
    query_tailscale_status,
    stop_tailscale_funnel,
    stop_tailscale_route_for_port,
)
from .credentials import CredentialError
from .models import AccessProfile
from .paths import session_dir
from .process_identity import (
    process_is_running,
    read_process_create_time_ns,
    verify_process_identity,
)
from .project_registry import ProjectRegistry, ProjectRegistryError
from .sessions import SessionError, SessionStore


DEFAULT_BRIDGE_PORT = 8765
DEFAULT_BRIDGE_TOOLS: tuple[str, ...] = (
    "karox.repo.read_file",
    "karox.repo.read_lines",
    "karox.repo.list_files",
    "karox.repo.search",
    "karox.git.status",
    "karox.git.diff",
    "karox.git.log",
    "karox.runtime.status",
    "karox.web.fetch",
    "karox.lsp.diagnostics",
)
# Fallback core catalogue if hosted_core is unavailable at config-validation
# time. The names are the stable Core wire names; hosted_core remains
# authoritative and is imported first whenever it can be.
_FALLBACK_CORE_TOOL_NAMES = frozenset(DEFAULT_BRIDGE_TOOLS) | frozenset(
    {
        "karox.repo.write_file",
        "karox.repo.edit_file",
        "karox.repo.command",
        "karox.command.run",
        "karox.checks.run",
        "karox.tests.run",
        "karox.git.commit",
        "karox.git.push",
    }
)
_TOOL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x0800
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_PR_SET_PDEATHSIG = 1

# A fresh Python child may spend several seconds importing the runtime before
# uvicorn can bind. This is a bounded startup budget, not a readiness claim:
# acceptance still requires a reachable listener owned by that process tree.
_BRIDGE_STARTUP_TIMEOUT_SECONDS = 45.0
_PUBLIC_READY_TIMEOUT_SECONDS = 30.0
_HEARTBEAT_INTERVAL_SECONDS = 2.0
_CHILD_PORT_RELEASE_TIMEOUT_SECONDS = 10.0
_WATCHDOG_REPLACE_TIMEOUT_SECONDS = 5.0
_WATCHDOG_REPLACE_INITIAL_DELAY_SECONDS = 0.02
_WATCHDOG_REPLACE_MAX_DELAY_SECONDS = 0.25
_OWNER_EXIT_RECORD_SUFFIX = ".last-exit.json"


class WebBridgeLaunchError(RuntimeError):
    """A managed web bridge could not be prepared or kept alive."""


class SavedBridgeRestartRequired(RuntimeError):
    """A saved-profile edit is safe only after explicit live-restart consent."""


# --------------------------------------------------------------------------- #
# Tool selection                                                               #
# --------------------------------------------------------------------------- #


def core_bridge_tool_names() -> frozenset[str]:
    """The Core tool names the bridge child can serve.

    ``hosted_core`` owns the authoritative map. Importing it lazily keeps a
    diagnostics-only or stop/status invocation from loading the tool runtime.
    """
    try:
        from .hosted_core import CORE_TOOL_NAMES

        return frozenset(CORE_TOOL_NAMES)
    except Exception:
        return _FALLBACK_CORE_TOOL_NAMES


def _select_core_tools(
    tools: Sequence[str],
    *,
    saved_profile_name: Optional[str],
) -> tuple[tuple[str, ...], dict[str, str]]:
    """Drop non-Core names for saved profiles; fail closed for ad-hoc launches.

    A saved profile written by KaroX 5 can select browser, dev-server, or
    autonomy tools that KaroX 6 does not serve. One impossible name must not
    take the whole connector down; it is dropped and reported instead. An
    explicit command-line tool list gets the honest startup error.
    """
    supported = core_bridge_tool_names()
    kept: list[str] = []
    denied: dict[str, str] = {}
    for name in tools:
        if name in supported:
            kept.append(name)
        else:
            denied[name] = "not served by the KaroX 6 core bridge"
    if denied and saved_profile_name is None:
        raise ValueError(
            "web bridge contains tools KaroX 6 does not serve: "
            + ", ".join(sorted(denied))
        )
    if not kept:
        raise ValueError(
            "web bridge has no tools the KaroX 6 core bridge can serve: "
            + ", ".join(sorted(denied))
        )
    return tuple(kept), denied


# --------------------------------------------------------------------------- #
# Connection configuration                                                     #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class WebBridgeConnectConfig:
    """Validated, secret-free description of one managed bridge launch."""

    profile: str
    repository: Path
    projects: tuple[dict[str, str], ...] = ()
    default_project_id: Optional[str] = None
    port: int = DEFAULT_BRIDGE_PORT
    tools: tuple[str, ...] = DEFAULT_BRIDGE_TOOLS
    session_id: Optional[str] = None
    # The dangerous profile must be asked for. A caller that forgets to pass one
    # publishes a repository to a third-party agent, so the omission defaults to
    # the profile that cannot write.
    access_profile: AccessProfile = AccessProfile.READ_ONLY
    tunnel: str = "auto"
    public_url: Optional[str] = None
    tailscale: Optional[str] = None
    tunnel_timeout_seconds: float = 30.0
    deadline_seconds: float = DEFAULT_BRIDGE_DEADLINE_SECONDS
    verification_commands: tuple[tuple[str, ...], ...] = ()
    language: str = "en"
    saved_profile_name: Optional[str] = None
    # Tools dropped at launch because KaroX 6 cannot serve them. Kept as
    # name -> reason so diagnostics explains the drop instead of silently
    # presenting a smaller catalogue.
    profile_denied_tools: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.repository, Path):
            object.__setattr__(self, "repository", Path(str(self.repository)))
        tunnel = self.tunnel
        if tunnel == "auto":
            # The Cloudflare Quick Tunnel was removed with the hosted surface;
            # presence is not readiness, so auto resolves to Tailscale and a
            # missing daemon fails honestly rather than silently republishing.
            tunnel = "tailscale"
        elif tunnel == "cloudflare":
            raise ValueError(
                "the Cloudflare Quick Tunnel is not supported; use tunnel "
                "'tailscale' or 'custom' with --public-url"
            )
        if tunnel not in {"tailscale", "custom"}:
            raise ValueError("web bridge tunnel must be auto, tailscale, or custom")
        object.__setattr__(self, "tunnel", tunnel)
        if self.profile not in WEB_BRIDGE_PROFILES:
            raise ValueError(
                "web bridge profile must be chatgpt-web, claude-web, notion, "
                "hyperagent-web, or adapt"
            )
        if not isinstance(self.access_profile, AccessProfile):
            raise ValueError("web bridge access_profile must be a valid AccessProfile")
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
            object.__setattr__(
                self, "default_project_id", project_registry.default_project_id
            )
            object.__setattr__(self, "repository", Path(anchor.path))
        if not 1 <= int(self.port) <= 65_535:
            raise ValueError("web bridge port must be between 1 and 65535")
        if tunnel == "custom" and not self.public_url:
            raise ValueError("custom web bridge tunnel requires --public-url")
        if tunnel != "custom" and self.public_url:
            raise ValueError("--public-url is only valid with --tunnel custom")
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
        if not all(
            isinstance(item, str) and _TOOL_NAME.fullmatch(item) for item in self.tools
        ):
            raise ValueError("web bridge tools must be safe tool names")
        if self.saved_profile_name is not None and not _SAFE_PROFILE_NAME.fullmatch(
            self.saved_profile_name
        ):
            raise ValueError("saved web bridge profile name is invalid")
        selected, denied = _select_core_tools(
            self.tools, saved_profile_name=self.saved_profile_name
        )
        object.__setattr__(self, "tools", selected)
        if denied:
            object.__setattr__(
                self,
                "profile_denied_tools",
                {**dict(self.profile_denied_tools), **denied},
            )
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
        if self.language not in {"en", "ru"}:
            raise ValueError("web bridge language must be en or ru")
        if not 1.0 <= float(self.tunnel_timeout_seconds) <= 300.0:
            raise ValueError("tunnel timeout must be between 1 and 300 seconds")
        if not 0.1 <= float(self.deadline_seconds) <= 3600.0:
            raise ValueError("bridge deadline must be between 0.1 and 3600 seconds")


# --------------------------------------------------------------------------- #
# Small process ownership primitives                                           #
# --------------------------------------------------------------------------- #


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
    runs the cleanup path, and an orphaned bridge could otherwise keep a public
    Funnel route pointed at an authenticated listener.
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
        options["preexec_fn"] = hook
    return options


def _pid_of(process: Optional[object]) -> Optional[int]:
    pid = getattr(process, "pid", None)
    return pid if isinstance(pid, int) and pid > 0 else None


def _create_child_job() -> Optional[int]:
    """Open a Windows job whose closure kills everything assigned to it.

    A hard kill of the launcher (``taskkill /F``, a closed console) never runs
    the cleanup path. The job handle dies with this process and Windows then
    terminates the children, which is the only kill-on-parent-death primitive
    available here.
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
    # The bridge and tunnel remain kill-on-close children. Breakaway stays
    # allowed for descendants that intentionally own separate user work.
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
    """Compatibility spelling for the shared, PID-reuse-safe liveness check."""
    return process_is_running(pid)


def _signal_process_group(process: Optional[object], *, hard: bool) -> None:
    pid = _pid_of(process)
    if pid is None or os.name == "nt":
        return
    try:
        group = os.getpgid(pid)  # type: ignore[attr-defined,unused-ignore]
    except (OSError, AttributeError):
        pass
    else:
        # Signal the group only when the child is its own group leader
        # (`start_new_session` took effect). Otherwise the group is the
        # caller's, and killpg would signal the runtime along with the child.
        if group != pid:
            try:
                os.kill(pid, signal.SIGKILL if hard else signal.SIGTERM)
            except (OSError, AttributeError):
                pass
            return
        try:
            os.killpg(pid, signal.SIGKILL if hard else signal.SIGTERM)
        except (OSError, AttributeError):
            pass
        return
    # getpgid unusable on this platform: fall back to the single process.
    try:
        os.kill(pid, signal.SIGKILL if hard else signal.SIGTERM)
    except (OSError, AttributeError):
        pass


def _close_child_pipes(process: subprocess.Popen[str]) -> None:
    """Release a stopped child's piped handles instead of leaving them to GC."""
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


@dataclass(frozen=True)
class MirroredChildOutput:
    """A child's recent output and the thread still draining it."""

    tail: deque[str]
    reader: threading.Thread

    def detail(self) -> str:
        """The last line the child printed, as a suffix for an error message."""
        self.reader.join(timeout=2.0)
        return _tail_detail(self.tail)


def _tail_detail(output_tail: deque[str]) -> str:
    detail = next((line for line in reversed(output_tail) if line), "")
    return f": {detail}" if detail else ""


def _mirror_child_output(
    process: subprocess.Popen[str],
    *,
    name: str,
) -> MirroredChildOutput:
    """Echo a child's merged output to this console and keep its last lines.

    Windows starts these children with ``CREATE_NO_WINDOW``; without redirected
    handles such a child would print into a hidden console. Draining the pipe is
    what turns ``exited with code 2`` back into a reason.
    """
    output_tail: deque[str] = deque(maxlen=30)

    def drain() -> None:
        stream = process.stdout
        if stream is None:
            return
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


# --------------------------------------------------------------------------- #
# Port and readiness probes                                                    #
# --------------------------------------------------------------------------- #


def _port_is_available(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", port))
    except OSError:
        return False
    return True


def _port_has_listener(port: int, *, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=0.2):
            return True
    except OSError:
        return False


def _wait_for_local_port_release(
    port: int,
    *,
    timeout_seconds: float = _CHILD_PORT_RELEASE_TIMEOUT_SECONDS,
) -> bool:
    """Wait until nothing accepts on ``port`` again, bounded."""
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while time.monotonic() < deadline:
        if _port_is_available(port):
            return True
        time.sleep(0.1)
    return _port_is_available(port)


def _windows_parent_pid(pid: int) -> Optional[int]:
    """Return a Windows process parent without requiring optional psutil."""
    if os.name != "nt" or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                (
                    "$p=Get-CimInstance Win32_Process -Filter "
                    f"'ProcessId = {pid}'; "
                    "if($null -ne $p){[Console]::Write($p.ParentProcessId)}"
                ),
            ],
            capture_output=True,
            text=True,
            encoding="ascii",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=2.0,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        value = completed.stdout.strip()
        return int(value) if value.isdigit() else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _windows_pid_descends_from(holder_pid: int, root_pid: int) -> Optional[bool]:
    """Prove a listener PID is the spawned process or one of its descendants."""
    seen: set[int] = set()
    current = holder_pid
    for _ in range(32):
        if current == root_pid:
            return True
        if current <= 0 or current in seen:
            return False
        seen.add(current)
        parent = _windows_parent_pid(current)
        if parent is None:
            return None
        current = parent
    return False


def _listener_belongs_to_process_tree(port: int, root_pid: int) -> Optional[bool]:
    """Best-effort proof that the local listener belongs to ``root_pid``.

    ``None`` means the platform cannot prove ownership. ``False`` is meaningful:
    a different live process owns the port, so readiness must not accept that
    listener as proof that the child we just spawned is healthy.
    """
    try:
        import psutil  # type: ignore[import-untyped]
    except ImportError:
        if os.name != "nt":
            return None
        try:
            completed = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"],
                capture_output=True,
                text=True,
                encoding="ascii",
                errors="replace",
                stdin=subprocess.DEVNULL,
                timeout=2.0,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        wanted = f":{int(port)}"
        for line in completed.stdout.splitlines():
            fields = line.split()
            if len(fields) < 5 or fields[0].upper() != "TCP":
                continue
            if fields[3].upper() != "LISTENING" or not fields[1].endswith(wanted):
                continue
            try:
                holder_pid = int(fields[4])
            except ValueError:
                return None
            return _windows_pid_descends_from(holder_pid, root_pid)
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


def local_mcp_route_healthy(
    port: int,
    *,
    path: str = "/mcp",
    timeout_seconds: float = 1.0,
    get: Optional[Callable[..., Any]] = None,
) -> bool:
    """Probe this bridge's own loopback MCP listener.

    KaroX's MCP endpoint intentionally rejects an unauthenticated ``GET /mcp``
    with HTTP 401. Seeing that response proves the listener is alive and reached
    the auth layer without exposing a credential or creating a session.
    """
    endpoint = f"http://127.0.0.1:{int(port)}{path}"
    if get is None:
        import httpx

        get = httpx.get
    try:
        response = get(
            endpoint,
            timeout=float(timeout_seconds),
            follow_redirects=False,
        )
    except Exception:
        return False
    return int(getattr(response, "status_code", 0) or 0) == 401


def _wait_for_bridge(
    process: subprocess.Popen[str],
    port: int,
    *,
    timeout_seconds: float = _BRIDGE_STARTUP_TIMEOUT_SECONDS,
    output: Optional[MirroredChildOutput] = None,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        code = process.poll()
        if code is not None:
            suffix = output.detail() if output is not None else ""
            raise WebBridgeLaunchError(f"KaroX bridge exited with code {code}{suffix}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                ownership = _listener_belongs_to_process_tree(port, process.pid)
                if ownership is False:
                    # A pre-existing listener can make a freshly spawned bridge
                    # look ready even though that child is about to fail bind().
                    time.sleep(0.05)
                    continue
                # When ownership cannot be proven, give a fast bind failure one
                # scheduling turn to surface before accepting the port.
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
    timeout_seconds: float = _PUBLIC_READY_TIMEOUT_SECONDS,
    output: Optional[MirroredChildOutput] = None,
) -> None:
    """Wait until the public MCP ingress reaches the local auth layer."""
    import httpx

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        code = process.poll()
        if code is not None:
            suffix = output.detail() if output is not None else ""
            raise WebBridgeLaunchError(f"KaroX bridge exited with code {code}{suffix}")
        remaining = max(0.1, deadline - time.monotonic())
        try:
            response = httpx.get(
                f"{public_url.rstrip('/')}{path}",
                timeout=min(1.5, remaining),
                follow_redirects=False,
            )
            if int(getattr(response, "status_code", 0) or 0) == 401:
                return
        except Exception:
            pass
        time.sleep(0.2)
    suffix = _tail_detail(output.tail) if output is not None else ""
    raise WebBridgeLaunchError(
        "Tailscale Funnel did not make the public MCP route reachable before "
        f"the bridge readiness deadline{suffix}"
    )


# --------------------------------------------------------------------------- #
# Watchdog records, owner lock, and stop requests                              #
# --------------------------------------------------------------------------- #


def _write_watchdog(path: Path, payload: dict[str, Any]) -> None:
    """Atomically record bridge ownership/health without ever exposing secrets."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        temporary.write_text(rendered, encoding="utf-8")
        # Windows denies the rename while another reader still holds the record
        # open. That is transient contention, not a broken bridge, so retry
        # briefly instead of letting the owner die over bookkeeping.
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


def _read_watchdog(path: Path) -> Optional[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _claim_watchdog(path: Path, payload: dict[str, Any]) -> None:
    """Refuse to replace a record owned by another live process."""
    existing = _read_watchdog(path)
    if existing is not None:
        owner_pid = existing.get("owner_pid")
        if (
            isinstance(owner_pid, int)
            and owner_pid > 0
            and owner_pid != os.getpid()
            and process_is_running(owner_pid)
        ):
            raise WebBridgeLaunchError(
                "another bridge owner is already active for this session"
            )
    _write_watchdog(path, payload)


def _release_own_watchdog(path: Path) -> bool:
    """Remove a record only while this process is still its recorded owner."""
    record = _read_watchdog(path)
    if record is not None and record.get("owner_pid") != os.getpid():
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def owner_exit_record_path(session_id: str) -> Path:
    return watchdog_dir() / f"{session_id}{_OWNER_EXIT_RECORD_SUFFIX}"


def _record_owner_exit(
    session_id: str,
    *,
    saved_profile: Optional[str],
    reason: str,
    detail: str = "",
) -> None:
    """Persist why an owner stopped, outside the record it deletes on the way out."""
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


def _saved_bridge_owner_lock_path(profile_name: str) -> Path:
    digest = hashlib.sha256(profile_name.encode("utf-8")).hexdigest()[:24]
    return watchdog_dir() / "owner-locks" / f"{digest}.lock"


def _try_acquire_saved_bridge_owner_lock(profile_name: str) -> Optional[Any]:
    """Acquire a lifetime single-owner lock for one saved bridge profile.

    The lock is held by the durable owner process itself. OS file locks
    disappear automatically when the owner dies, so a crash cannot permanently
    brick the profile. Returning ``None`` means another owner already holds it.
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


def _write_stop_request(
    session_id: str,
    owner_pid: int,
    *,
    intent: str = "stop",
) -> Path:
    """Ask the exact bridge owner to stop or hand off for restart."""
    if intent not in {"stop", "restart"}:
        raise ValueError("saved bridge stop request intent must be stop or restart")
    root = watchdog_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = _stop_request_path(session_id)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            {
                "owner_pid": owner_pid,
                "requested_at": time.time(),
                "intent": intent,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def _consume_stop_request(session_id: str, owner_pid: int) -> Optional[str]:
    """Consume an addressed request and return ``stop`` or ``restart``.

    Requests without an ``intent`` field are deliberately interpreted as
    ``stop`` so an old or malformed request can never silently widen itself into
    a self-restarting lifecycle action.
    """
    path = _stop_request_path(session_id)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = None
    addressed = isinstance(raw, dict) and raw.get("owner_pid") == owner_pid
    if not addressed:
        # Multiple owners should never exist. A request is addressed to one
        # exact owner; a different owner must not steal or delete it before the
        # target sees it. Malformed requests are the only safe exception.
        if raw is None:
            try:
                path.unlink()
            except OSError:
                pass
        return None
    raw_intent = raw.get("intent") if isinstance(raw, dict) else None
    intent = raw_intent if raw_intent in {"stop", "restart"} else "stop"
    try:
        path.unlink()
    except OSError:
        pass
    return intent


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


# --------------------------------------------------------------------------- #
# Diagnostics and output                                                       #
# --------------------------------------------------------------------------- #


def bridge_diagnostics(
    config: WebBridgeConnectConfig,
    *,
    public_url: Optional[str] = None,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    """Describe the effective bridge contract without exposing credentials."""
    try:
        from . import __version__ as runtime_version
    except Exception:  # pragma: no cover - version module always exists
        runtime_version = "unknown"
    stability = {
        "tailscale": "stable_device_hostname",
        "custom": "stable_user_managed",
    }[config.tunnel]
    return {
        "schema_version": 1,
        "saved_profile": config.saved_profile_name,
        "target_profile": config.profile,
        "repository": str(config.repository),
        "repository_exists": Path(config.repository).exists(),
        "runtime_build": {"version": str(runtime_version)},
        "ports": {"mcp_loopback": config.port},
        "access_profile": config.access_profile.value,
        "write_permission": config.access_profile
        in {AccessProfile.WORKSPACE_WRITE, AccessProfile.ELEVATED},
        "available_tools": list(config.tools),
        "disabled_tools": [
            {"name": name, "reason": reason}
            for name, reason in sorted(config.profile_denied_tools.items())
        ],
        "verification_commands": [
            list(command) for command in config.verification_commands
        ],
        "command_allowlist": [
            list(command) for command in config.verification_commands
        ],
        "session_deadline_seconds": config.deadline_seconds,
        "effective_deadline_seconds": config.deadline_seconds,
        "mode_restrictions": {
            "read_only": config.access_profile == AccessProfile.READ_ONLY,
            "no_git_push": "karox.git.push" not in config.tools,
            "no_publish": True,
            "no_auth_commands": True,
            "no_deploy_release": True,
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


@dataclass(frozen=True)
class BridgeConnectResult:
    """Safe connection description: a public URL plus a credential fingerprint.

    The mapping and human lines never contain the credential, the approval
    password, an OAuth token, or an Authorization header.
    """

    profile: str
    saved_profile: Optional[str]
    session_id: str
    repository: str
    port: int
    endpoint: str
    public_url: str
    tunnel: str
    url_stability: str
    credential_reference: str
    credential_fingerprint: str
    auth_scheme: str
    bridge_pid: Optional[int] = None
    tunnel_pid: Optional[int] = None
    owner_pid: Optional[int] = None
    action: str = "started"
    ready: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "saved_profile": self.saved_profile,
            "target_profile": self.profile,
            "session_id": self.session_id,
            "repository": self.repository,
            "port": self.port,
            "endpoint": self.endpoint,
            "public_url": self.public_url,
            "tunnel": self.tunnel,
            "url_stability": self.url_stability,
            "credential_reference": self.credential_reference,
            "credential_fingerprint": self.credential_fingerprint,
            "auth_scheme": self.auth_scheme,
            "bridge_pid": self.bridge_pid,
            "tunnel_pid": self.tunnel_pid,
            "owner_pid": self.owner_pid,
            "action": self.action,
            "ready": self.ready,
        }

    def safe_lines(self) -> list[str]:
        lines = [f"KaroX {self.profile} bridge is ready", f"MCP URL: {self.endpoint}"]
        if self.auth_scheme == "oauth":
            lines.append(
                f"OAuth approval password fingerprint: {self.credential_fingerprint}"
            )
            target = self.saved_profile or self.session_id
            lines.append(
                "To copy the OAuth approval password for the consent page, run: "
                f"karox bridge oauth approval-password --saved {target} --copy --quiet"
            )
        else:
            lines.append(f"Bearer credential fingerprint: {self.credential_fingerprint}")
            lines.append(
                "To copy the Authorization value without printing the secret, run: "
                f"karox bridge credential copy {self.session_id} --quiet"
            )
        lines.append(f"Session: {self.session_id}")
        lines.append(f"Tunnel: {self.tunnel} ({self.url_stability})")
        return lines


def build_connect_result(
    config: WebBridgeConnectConfig,
    *,
    session_id: str,
    public_url: str,
    credential_reference: str,
    credential_fingerprint: str,
    bridge_pid: Optional[int] = None,
    tunnel_pid: Optional[int] = None,
    owner_pid: Optional[int] = None,
    action: str = "started",
    ready: bool = True,
) -> BridgeConnectResult:
    scheme = BridgeRegistry().get(config.profile).auth_scheme
    stability = {
        "tailscale": "stable_device_hostname",
        "custom": "stable_user_managed",
    }[config.tunnel]
    return BridgeConnectResult(
        profile=config.profile,
        saved_profile=config.saved_profile_name,
        session_id=session_id,
        repository=str(config.repository),
        port=config.port,
        endpoint=web_bridge_mcp_endpoint(public_url, config.profile),
        public_url=public_url,
        tunnel=config.tunnel,
        url_stability=stability,
        credential_reference=credential_reference,
        credential_fingerprint=credential_fingerprint,
        auth_scheme=scheme,
        bridge_pid=bridge_pid,
        tunnel_pid=tunnel_pid,
        owner_pid=owner_pid,
        action=action,
        ready=ready,
    )


def connection_instructions(profile: str, language: str = "en") -> tuple[str, ...]:
    """Concise human steps printed after the bridge becomes reachable."""
    russian = language == "ru"
    if russian:
        common = (
            "Пароль подтверждения вводится только на странице KaroX.",
            "Не закрывайте KaroX, пока клиент использует мост; Ctrl+C — остановить.",
        )
        if profile == "notion":
            return (
                "Как подключить мост к Notion Custom Agent:",
                "  1. Включите Custom MCP server в workspace.",
                "  2. Custom Agent → Settings → Tools & Access → Add connection.",
                "  3. Вставьте MCP URL из строки выше и завершите OAuth на странице KaroX.",
                *common,
            )
        if profile == "hyperagent-web":
            return (
                "Как подключить мост к Hyperagent:",
                "  1. Settings → Integrations → Custom MCP server.",
                "  2. URL: MCP URL из строки выше.",
                "  3. Используйте OAuth сервера и подтвердите доступ на странице KaroX.",
                *common,
            )
        return (
            "Как подключить мост:",
            "  1. Откройте настройки коннектора и добавьте MCP URL из строки выше.",
            "  2. Завершите OAuth на странице KaroX.",
            *common,
        )
    common = (
        "Enter the approval password only on the KaroX page.",
        "Keep KaroX open while the connector uses the bridge; Ctrl+C stops it.",
    )
    if profile == "notion":
        return (
            "Connect the bridge to a Notion Custom Agent:",
            "  1. Enable a Custom MCP server in the workspace.",
            "  2. Custom Agent > Settings > Tools & Access > Add connection.",
            "  3. Paste the MCP URL above and finish OAuth on the KaroX page.",
            *common,
        )
    if profile == "hyperagent-web":
        return (
            "Connect the bridge to Hyperagent:",
            "  1. Settings > Integrations > Custom MCP server.",
            "  2. URL: the MCP URL above.",
            "  3. Use the server OAuth flow and approve it on the KaroX page.",
            *common,
        )
    return (
        "Connect the bridge to the hosted client:",
        "  1. Add the MCP URL above as a custom connector.",
        "  2. Finish OAuth on the KaroX page.",
        *common,
    )


# --------------------------------------------------------------------------- #
# Child argv                                                                   #
# --------------------------------------------------------------------------- #


def _bridge_argv(
    config: WebBridgeConnectConfig,
    *,
    session_id: str,
    public_url: str,
    port: Optional[int] = None,
) -> tuple[str, ...]:
    target_port = config.port if port is None else int(port)
    if not 1 <= target_port <= 65_535:
        raise WebBridgeLaunchError("bridge child port must be between 1 and 65535")
    if not _TOOL_NAME.fullmatch(session_id):
        raise WebBridgeLaunchError("bridge session id is not a safe identifier")
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
        str(target_port),
        "--deadline-seconds",
        str(config.deadline_seconds),
    ]
    if config.saved_profile_name:
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
    profile_meta = BridgeRegistry().get(config.profile)
    # `--public-url` is an OAuth authorization-server input, not a generic
    # publication hint. Bearer profiles receive only the public host through
    # KAROX_MCP_ALLOWED_HOSTS and reject this OAuth-only option.
    if profile_meta.auth_scheme == "oauth":
        values.extend(("--public-url", public_url))
    redirect_hosts = profile_redirect_hosts(config.profile)
    if redirect_hosts:
        values.extend(("--allowed-redirect-hosts", ",".join(sorted(redirect_hosts))))
    for tool in config.tools:
        values.extend(("--tool", tool))
    for command in config.verification_commands:
        serialized = json.dumps(
            list(command), ensure_ascii=False, separators=(",", ":")
        )
        values.extend(("--verification-command", serialized))
    return tuple(values)


def _allowed_hosts_environment() -> str:
    try:
        from .bridge_proxy import ALLOWED_HOSTS_ENVIRONMENT

        return ALLOWED_HOSTS_ENVIRONMENT
    except Exception:
        return "KAROX_MCP_ALLOWED_HOSTS"


# --------------------------------------------------------------------------- #
# Blocking connect                                                             #
# --------------------------------------------------------------------------- #


def run_bridge_connect(
    config: WebBridgeConnectConfig,
    *,
    readiness_timeout_seconds: float = _BRIDGE_STARTUP_TIMEOUT_SECONDS,
    public_ready_timeout_seconds: float = _PUBLIC_READY_TIMEOUT_SECONDS,
) -> int:
    """Own the Funnel route, session, credential and child until stopped.

    Returns ``0`` after a clean stop request, Ctrl+C, or a restart hand-off. An
    unexpected child exit raises :class:`WebBridgeLaunchError`; the cleanup path
    always runs first, so no tunnel or credential is left half-created.
    """
    try:
        repository = config.repository.expanduser().resolve(strict=True)
    except OSError as exc:
        raise WebBridgeLaunchError(
            f"web bridge repository is unavailable: {type(exc).__name__}"
        ) from exc
    if not repository.is_dir():
        raise WebBridgeLaunchError("web bridge repository must be a directory")
    config = replace(config, repository=repository)
    if not _port_is_available(config.port):
        raise WebBridgeLaunchError(f"local bridge port is already in use: {config.port}")

    persistent_session = bool(config.saved_profile_name)
    session_id = _session_id(config)
    owner_lock: Optional[Any] = (
        _try_acquire_saved_bridge_owner_lock(config.saved_profile_name)
        if persistent_session and config.saved_profile_name
        else None
    )
    if persistent_session and config.saved_profile_name and owner_lock is None:
        raise WebBridgeLaunchError(
            "another durable owner is already active for saved profile "
            f"'{config.saved_profile_name}'"
        )

    tunnel: Optional[TailscaleFunnel] = None
    bridge: Optional[subprocess.Popen[str]] = None
    bridge_output: Optional[MirroredChildOutput] = None
    watchdog_path: Optional[Path] = None
    sessions: Optional[SessionStore] = None
    job: Optional[int] = None
    credential_created = False
    session_created = False
    bridge_ready = False
    eventual_tunnel_pid: Optional[int] = None
    exit_reason = "stopped"
    exit_detail = ""
    stop_request_seen = False
    try:
        job = _create_child_job()
        if config.tunnel == "tailscale":
            tunnel = ensure_tailscale_funnel(
                config.port,
                https_port=profile_tailscale_https_port(config.profile),
                executable=config.tailscale,
                timeout_seconds=config.tunnel_timeout_seconds,
                emit=lambda line: print(line, flush=True),
            )
            public_url = tunnel.public_url
        else:
            if not config.public_url:
                raise WebBridgeLaunchError("custom tunnel requires a public URL")
            public_url = config.public_url.rstrip("/")

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
            "url_stability": bridge_diagnostics(config)["url_stability"],
            "effective_deadline_seconds": config.deadline_seconds,
            "available_tools": list(config.tools),
            "started_at": time.time(),
            "owner_heartbeat_at": time.time(),
            "tunnel_pid": eventual_tunnel_pid,
            "tunnel_lifecycle": "daemon_background",
            "bridge_pid": None,
            # A PID alone is not readiness. The owner flips this after the
            # listener is reachable and owned by the spawned process tree.
            "bridge_ready": False,
            "readiness_state": "STARTING",
        }
        _claim_watchdog(watchdog_path, watchdog_record)

        def publish() -> None:
            if watchdog_path is not None:
                _write_watchdog(watchdog_path, dict(watchdog_record))

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

        credential_store = BridgeCredentialStore()
        credential_reference = f"os-keyring:bridge/{session_id}"
        if persistent_session:
            try:
                secret = credential_store.resolve(credential_reference)
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
        credential_fingerprint = BridgeCredentialStore.fingerprint(secret)

        environment = dict(os.environ)
        # The listener cannot guess the tunnel host name, and without it every
        # request through the tunnel looks like a rebound DNS name.
        environment[_allowed_hosts_environment()] = urlsplit(public_url).hostname or ""
        # Output is decoded as UTF-8 below, so it has to be encoded as UTF-8.
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["KAROX_UI_LANGUAGE"] = config.language
        diagnostics = bridge_diagnostics(
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
                    port=config.port,
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
        watchdog_record["bridge_pid"] = bridge.pid
        publish()

        _wait_for_bridge(
            bridge,
            config.port,
            timeout_seconds=readiness_timeout_seconds,
            output=bridge_output,
        )
        _wait_for_public_mcp_route(
            bridge,
            public_url,
            timeout_seconds=public_ready_timeout_seconds,
            output=bridge_output,
        )
        bridge_ready = True
        watchdog_record["bridge_ready"] = True
        watchdog_record["readiness_state"] = "READY"
        watchdog_record["ready_at"] = time.time()
        publish()

        result = build_connect_result(
            config,
            session_id=session_id,
            public_url=public_url,
            credential_reference=credential_reference,
            credential_fingerprint=credential_fingerprint,
            bridge_pid=bridge.pid,
            owner_pid=os.getpid(),
        )
        for line in result.safe_lines():
            print(line, flush=True)
        print(
            "Bridge diagnostics JSON: "
            + json.dumps(diagnostics, ensure_ascii=False, sort_keys=True),
            flush=True,
        )
        for line in connection_instructions(config.profile, config.language):
            print(line, flush=True)

        next_heartbeat = time.monotonic() + _HEARTBEAT_INTERVAL_SECONDS
        while True:
            intent = _consume_stop_request(session_id, os.getpid())
            if intent:
                stop_request_seen = True
                exit_reason = (
                    "restart_requested" if intent == "restart" else "stop_requested"
                )
                print(
                    f"KaroX saved bridge {intent} requested; shutting down cleanly...",
                    flush=True,
                )
                break
            code = bridge.poll()
            if code is not None:
                suffix = bridge_output.detail() if bridge_output is not None else ""
                raise WebBridgeLaunchError(
                    f"KaroX bridge exited unexpectedly with code {code}{suffix}"
                )
            now = time.monotonic()
            if now >= next_heartbeat:
                watchdog_record["owner_heartbeat_at"] = time.time()
                publish()
                next_heartbeat = now + _HEARTBEAT_INTERVAL_SECONDS
            time.sleep(0.2)
        return 0
    except KeyboardInterrupt:
        print("\nStopping KaroX web bridge…", flush=True)
        exit_reason = "keyboard_interrupt"
        return 0
    except BaseException as exc:
        # The detached owner's console may be DEVNULL, so the reason is kept on
        # disk as redacted metadata: an exception type and its own message.
        exit_reason = type(exc).__name__
        exit_detail = str(exc)[:500]
        raise
    finally:
        _record_owner_exit(
            session_id,
            saved_profile=config.saved_profile_name,
            reason=exit_reason,
            detail=exit_detail,
        )
        _stop_process(bridge)
        if bridge_output is not None:
            bridge_output.reader.join(timeout=2.0)
        if bridge is not None and bridge.stdout is not None:
            try:
                bridge.stdout.close()
            except OSError:
                pass
        if tunnel is not None:
            # A daemon-owned Funnel route survives this process. During a
            # requested restart keep the exact already-proven route in place:
            # the successor reuses the same local port and public hostname, so
            # removing and re-adding Funnel only creates a public outage.
            preserve_route = (
                exit_reason == "restart_requested"
                and isinstance(tunnel, TailscaleFunnel)
                and persistent_session
            )
            if not preserve_route:
                stop_tailscale_funnel(tunnel)
        _close_job(job)
        if watchdog_path is not None:
            _release_own_watchdog(watchdog_path)
        discard_credential = credential_created and not bridge_ready
        if discard_credential:
            # The owner lock proves no other owner can be serving this profile,
            # and an ad-hoc session has no successor at all.
            try:
                BridgeCredentialStore().delete(session_id)
            except Exception:
                pass
        if session_created and sessions is not None and not persistent_session:
            try:
                sessions.revoke(session_id)
            except Exception:
                pass
        if stop_request_seen:
            try:
                _stop_request_path(session_id).unlink()
            except OSError:
                pass
        _release_saved_bridge_owner_lock(owner_lock)


def connect_bridge(config: WebBridgeConnectConfig, **kwargs: Any) -> int:
    """Alias used by the CLI ``karox bridge connect`` path."""
    return run_bridge_connect(config, **kwargs)


def run_web_bridge(config: WebBridgeConnectConfig, **kwargs: Any) -> int:
    """KaroX 5 spelling retained for callers that have not migrated yet."""
    return run_bridge_connect(config, **kwargs)


# --------------------------------------------------------------------------- #
# Saved-profile configuration                                                  #
# --------------------------------------------------------------------------- #


def config_from_saved_profile(
    profile_name: str,
    *,
    tailscale: Optional[str] = None,
    session_id: Optional[str] = None,
) -> WebBridgeConnectConfig:
    """Build a launch configuration from a secret-free saved profile."""
    profile = BridgeProfileStore().get(profile_name)
    if profile.tunnel == "cloudflare":
        raise WebBridgeLaunchError(
            "saved profile uses the removed Cloudflare Quick Tunnel; edit it to "
            "use the tailscale tunnel or a custom HTTPS URL"
        )
    if profile.target_profile not in WEB_BRIDGE_PROFILES:
        raise WebBridgeLaunchError(
            f"saved profile target is not launchable: {profile.target_profile}"
        )
    repository_value = profile.repository
    if not repository_value and profile.projects:
        repository_value = str(profile.projects[0]["path"])
    if not repository_value:
        raise WebBridgeLaunchError("saved profile has no repository")
    try:
        repository = Path(repository_value).expanduser().resolve(strict=True)
    except OSError as exc:
        raise WebBridgeLaunchError(
            f"saved profile repository is unavailable: {type(exc).__name__}"
        ) from exc
    return WebBridgeConnectConfig(
        profile=profile.target_profile,
        repository=repository,
        projects=profile.projects,
        default_project_id=profile.default_project_id,
        port=profile.port,
        tools=profile.tools,
        session_id=session_id,
        access_profile=profile.access_profile,
        tunnel=profile.tunnel,
        public_url=profile.public_url,
        tailscale=tailscale,
        tunnel_timeout_seconds=profile.tunnel_timeout_seconds,
        deadline_seconds=profile.deadline_seconds,
        verification_commands=profile.verification_commands,
        language=profile.language,
        saved_profile_name=profile.name,
    )


def connect_saved_bridge(profile_name: str, **kwargs: Any) -> int:
    """Run the blocking managed bridge for a saved profile."""
    return run_bridge_connect(config_from_saved_profile(profile_name), **kwargs)


# --------------------------------------------------------------------------- #
# Start / stop / status for saved profiles                                     #
# --------------------------------------------------------------------------- #


def _watchdog_payload(profile_name: str) -> tuple[Optional[Path], Optional[dict[str, Any]]]:
    for session_id in saved_web_bridge_session_candidates(profile_name):
        path = watchdog_dir() / f"{session_id}.json"
        record = _read_watchdog(path)
        if record is not None:
            return path, record
    return None, None


def _last_exit_payload(profile_name: str) -> Optional[dict[str, Any]]:
    for session_id in saved_web_bridge_session_candidates(profile_name):
        record = _read_watchdog(owner_exit_record_path(session_id))
        if record is not None:
            return record
    return None


def _credential_status(session_id: str) -> dict[str, str]:
    return BridgeCredentialStore().status(f"os-keyring:bridge/{session_id}")


def _profile_record(profile_name: str) -> SavedBridgeProfile:
    return BridgeProfileStore().get(profile_name)


def _route_status_for(profile: SavedBridgeProfile) -> dict[str, Any]:
    """Read-only Tailscale route state for this profile's port."""
    executable = find_tailscale()
    if executable is None:
        return {"available": False, "reason": "tailscale_not_found"}
    status = query_tailscale_status(executable)
    if not status.get("ready"):
        return {
            "available": False,
            "reason": status.get("code"),
            "detail": status.get("detail"),
        }
    try:
        tunnel = find_owned_funnel(
            profile.port,
            https_port=profile_tailscale_https_port(profile.target_profile),
        )
    except TailscaleError as exc:
        return {"available": True, "owned": False, "error": str(exc)[:200]}
    return {
        "available": True,
        "owned": tunnel is not None,
        "public_url": tunnel.public_url if tunnel is not None else None,
    }


def status_bridge(profile_name: str) -> dict[str, Any]:
    """Describe the saved profile's live/owned state without side effects."""
    from .port_ownership import check_port_ownership

    try:
        profile = _profile_record(profile_name)
    except BridgeProfileError as exc:
        return {"saved_profile": profile_name, "status": "error", "error": str(exc)}
    session_id = saved_web_bridge_session_id(profile_name)
    ownership = check_port_ownership(profile_name, port=profile.port)
    ownership_payload = ownership.to_dict()
    watchdog = _watchdog_payload(profile_name)[1] or {}
    credential = _credential_status(session_id)
    public_url = (
        watchdog.get("public_url")
        if isinstance(watchdog.get("public_url"), str)
        else ownership.metadata.public_url
    )
    route = _route_status_for(profile) if profile.tunnel == "tailscale" else {}
    if ownership.verdict == "reuse_same_profile":
        overall = "running"
    elif ownership.verdict == "free":
        overall = "stopped"
    elif ownership.verdict == "unrelated_process":
        overall = "port_conflict"
    else:
        overall = ownership.verdict
    return {
        "saved_profile": profile_name,
        "target_profile": profile.target_profile,
        "status": overall,
        "session_id": session_id,
        "port": profile.port,
        "repository": profile.repository,
        "tunnel": profile.tunnel,
        "public_url": public_url,
        "endpoint": (
            web_bridge_mcp_endpoint(public_url, profile.target_profile)
            if isinstance(public_url, str) and public_url
            else None
        ),
        "ownership": ownership_payload,
        "bridge_pid": ownership.metadata.pid or watchdog.get("bridge_pid"),
        "owner_pid": ownership.metadata.pid,
        "tunnel_pid": watchdog.get("tunnel_pid"),
        "bridge_ready": bool(watchdog.get("bridge_ready")),
        "credential_status": credential["status"],
        "credential_fingerprint": credential["fingerprint"],
        "credential_reference": credential["reference"],
        "route": route,
        "last_exit": _last_exit_payload(profile_name),
        "secret_fields": [],
    }


def _terminate_proven_pid(
    pid: int,
    *,
    expected_create_time_ns: Optional[int] = None,
) -> bool:
    """Terminate one proven PID, re-verifying identity immediately before.

    Refuses when the creation time is unavailable or changed. The caller must
    have proven argv/profile ownership separately; this function only closes the
    PID-reuse race at the termination boundary.
    """
    if not isinstance(pid, int) or pid <= 0 or not process_is_running(pid):
        return True
    observed = read_process_create_time_ns(pid)
    if observed is None:
        return False
    if expected_create_time_ns is not None and observed != expected_create_time_ns:
        return False
    if read_process_create_time_ns(pid) != observed:
        return False
    try:
        import psutil  # type: ignore[import-untyped]

        process = psutil.Process(pid)
        process.terminate()
        try:
            process.wait(timeout=5.0)
        except Exception:
            process.kill()
            process.wait(timeout=5.0)
    except ImportError:
        # Without psutil the platform still offers the OS kill primitive, but
        # only after the creation-time proof above.
        try:
            if os.name == "nt":
                completed = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F"],
                    capture_output=True,
                    timeout=10,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if completed.returncode not in {0, 128}:
                    return False
            else:
                os.kill(pid, signal.SIGTERM)
        except (OSError, subprocess.SubprocessError):
            return False
    except Exception:
        return False
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if not process_is_running(pid):
            return True
        time.sleep(0.1)
    return not process_is_running(pid)


def _stop_owned_route(profile: SavedBridgeProfile, *, watchdog: Optional[dict[str, Any]]) -> None:
    """Remove the exact Funnel route after its owner is gone; never a foreign one."""
    if profile.tunnel != "tailscale":
        return
    tunnel: Optional[TailscaleFunnel] = None
    if isinstance(watchdog, dict):
        public_url = watchdog.get("public_url")
        port = watchdog.get("port")
        if isinstance(public_url, str) and public_url and isinstance(port, int):
            executable = find_tailscale()
            if executable is not None:
                tunnel = TailscaleFunnel(
                    executable,
                    public_url,
                    port,
                    https_port=profile_tailscale_https_port(profile.target_profile),
                    mount_path="/",
                )
    if tunnel is not None:
        stop_tailscale_funnel(tunnel)
    else:
        stop_tailscale_route_for_port(
            profile.port,
            https_port=profile_tailscale_https_port(profile.target_profile),
        )


def stop_bridge(
    profile_name: str,
    *,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    """Stop only a proven KaroX-owned bridge for a saved profile.

    Stop is not Revoke: this shuts down the bridge and its Funnel route but does
    not delete credentials or revoke OAuth authorizations.
    """
    from .port_ownership import (
        check_port_ownership,
        prove_bridge_process_identity,
        prove_saved_bridge_owner_identity,
    )

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
        profile = _profile_record(profile_name)
    except BridgeProfileError as exc:
        return _result("error", verdict="invalid_profile", error=str(exc))
    except Exception as exc:
        return _result("error", verdict="invalid_profile", error=type(exc).__name__)

    ownership = check_port_ownership(profile_name, port=profile.port)
    _path, watchdog = _watchdog_payload(profile_name)

    if ownership.verdict == "reuse_same_profile":
        pid = ownership.metadata.pid
        if pid is None or not ownership.metadata.pid_proven:
            return _result(
                "no_action",
                verdict="stale_owned_process",
                reason=(
                    "owner PID is alive but its identity cannot be proven; "
                    "refusing to terminate"
                ),
                pid=pid,
            )
        session_id = (
            ownership.metadata.session_id
            or saved_web_bridge_session_id(profile_name)
        )
        try:
            _write_stop_request(session_id, pid, intent="stop")
        except OSError as exc:
            return _result(
                "error",
                verdict=ownership.verdict,
                reason="failed to request shutdown",
                pid=pid,
                error=str(exc)[:200],
            )
        deadline = time.monotonic() + max(1.0, float(timeout_seconds))
        while time.monotonic() < deadline:
            if not process_is_running(pid):
                _stop_owned_route(profile, watchdog=watchdog)
                return _result(
                    "stopped",
                    verdict=ownership.verdict,
                    reason=f"cleanly stopped owned bridge PID {pid}",
                    pid=pid,
                )
            time.sleep(0.1)
        # A hung owner may eventually resume; do not leave a delayed stop armed.
        try:
            _stop_request_path(session_id).unlink()
        except OSError:
            pass
        return _result(
            "error",
            verdict=ownership.verdict,
            reason="owned bridge did not acknowledge the clean shutdown request in time",
            pid=pid,
            error="bridge shutdown timed out",
        )

    live_owner_pid = ownership.live_unrecorded_owner_pid
    if isinstance(live_owner_pid, int) and live_owner_pid > 0:
        if not prove_saved_bridge_owner_identity(live_owner_pid, profile_name):
            return _result(
                "error",
                verdict=ownership.verdict,
                reason="owner identity is not proven; refusing to terminate",
                pid=live_owner_pid,
            )
        create_time = read_process_create_time_ns(live_owner_pid)
        if create_time is None or not _terminate_proven_pid(
            live_owner_pid, expected_create_time_ns=create_time
        ):
            return _result(
                "error",
                verdict=ownership.verdict,
                reason="proven owner could not be stopped safely",
                pid=live_owner_pid,
                error="owner shutdown failed",
            )
        _stop_owned_route(profile, watchdog=watchdog)
        return _result(
            "stopped",
            verdict=ownership.verdict,
            reason=f"stopped proven owner PID {live_owner_pid} without a watchdog record",
            pid=live_owner_pid,
        )

    orphan_pid = ownership.owned_orphan_pid
    orphan_identity = ownership.owned_orphan_identity
    if isinstance(orphan_pid, int) and orphan_pid > 0:
        if orphan_identity is None:
            return _result(
                "no_action",
                verdict=ownership.verdict,
                reason="listener identity could not be captured; refusing to terminate",
                pid=orphan_pid,
            )
        verdict = verify_process_identity(
            orphan_identity, pid_alive=process_is_running
        )
        if not verdict.proven:
            return _result(
                "no_action",
                verdict=ownership.verdict,
                reason=f"listener identity is not proven ({verdict.reason})",
                pid=orphan_pid,
            )
        session_ids = saved_web_bridge_session_candidates(profile_name)
        if prove_bridge_process_identity(orphan_pid, session_ids) is None:
            return _result(
                "no_action",
                verdict=ownership.verdict,
                reason="listener no longer proves this profile's bridge identity",
                pid=orphan_pid,
            )
        if not _terminate_proven_pid(
            orphan_pid, expected_create_time_ns=orphan_identity.create_time_ns
        ):
            return _result(
                "error",
                verdict=ownership.verdict,
                reason="owned orphan listener could not be stopped",
                pid=orphan_pid,
                error="orphan shutdown failed",
            )
        _stop_owned_route(profile, watchdog=watchdog)
        return _result(
            "stopped",
            verdict=ownership.verdict,
            reason=f"reclaimed owned orphan bridge listener PID {orphan_pid}",
            pid=orphan_pid,
        )

    return _result(
        "no_action",
        verdict=ownership.verdict,
        reason=ownership.reason,
        pid=ownership.metadata.pid,
    )


stop_saved_bridge = stop_bridge


def _safe_start_result(
    profile: SavedBridgeProfile,
    *,
    action: str,
    session_id: str,
    watchdog: dict[str, Any],
) -> dict[str, Any]:
    public_url = watchdog.get("public_url")
    credential = _credential_status(session_id)
    return {
        "saved_profile": profile.name,
        "action": action,
        "session_id": session_id,
        "port": profile.port,
        "public_url": public_url if isinstance(public_url, str) else None,
        "endpoint": (
            web_bridge_mcp_endpoint(public_url, profile.target_profile)
            if isinstance(public_url, str) and public_url
            else None
        ),
        "bridge_pid": watchdog.get("bridge_pid"),
        "tunnel_pid": watchdog.get("tunnel_pid"),
        "owner_pid": watchdog.get("owner_pid"),
        "bridge_ready": bool(watchdog.get("bridge_ready")),
        "credential_status": credential["status"],
        "credential_fingerprint": credential["fingerprint"],
        "error": None,
    }


def start_saved_bridge(
    profile_name: str,
    *,
    timeout_seconds: float = 120.0,
) -> dict[str, Any]:
    """Start or repair the persistent bridge for a saved profile.

    Idempotent: a proven live bridge is reused, never duplicated. A foreign
    process on the port is never touched. A stale owned PID is never terminated.
    The actual owner is a detached ``karox bridge connect --saved NAME``
    process, so it survives the caller.
    """
    from .detached_process import spawn_detached
    from .port_ownership import check_port_ownership

    try:
        profile = _profile_record(profile_name)
    except BridgeProfileError as exc:
        return {
            "saved_profile": profile_name,
            "action": "error",
            "error": str(exc)[:200],
        }
    try:
        config_from_saved_profile(profile_name)
    except (WebBridgeLaunchError, ValueError) as exc:
        return {
            "saved_profile": profile_name,
            "action": "error",
            "error": str(exc)[:200],
        }
    session_id = saved_web_bridge_session_id(profile_name)
    ownership = check_port_ownership(profile_name, port=profile.port)
    if ownership.verdict == "reuse_same_profile" and ownership.metadata.pid_proven:
        _path, watchdog = _watchdog_payload(profile_name)
        if watchdog is not None:
            return _safe_start_result(
                profile, action="reused", session_id=session_id, watchdog=watchdog
            )
    if ownership.verdict == "unrelated_process":
        return {
            "saved_profile": profile_name,
            "action": "error",
            "error": (
                f"port {profile.port} is held by an unrelated process; "
                "cannot start bridge"
            )[:200],
        }
    if ownership.live_unrecorded_owner_pid is not None:
        return {
            "saved_profile": profile_name,
            "action": "error",
            "error": "this profile's proven owner is running without a watchdog record",
        }
    if ownership.owned_orphan_pid is not None:
        stopped = stop_bridge(profile_name)
        if stopped.get("action") == "error":
            return stopped
    launch_argv = [
        sys.executable,
        "-m",
        "karox.cli",
        "bridge",
        "connect",
        "--saved",
        profile_name,
    ]
    repository = (
        str(Path(profile.repository).expanduser()) if profile.repository else None
    )
    detached, mechanism = spawn_detached(launch_argv, cwd=repository)
    if detached is None:
        return {
            "saved_profile": profile_name,
            "action": "error",
            "error": f"cannot start detached bridge: {mechanism}"[:200],
        }
    spawned_at = time.time()
    deadline = time.monotonic() + max(1.0, float(timeout_seconds))
    watchdog: Optional[dict[str, Any]] = None
    while time.monotonic() < deadline:
        _path, candidate = _watchdog_payload(profile_name)
        if candidate is not None:
            bridge_pid = candidate.get("bridge_pid")
            public_url = candidate.get("public_url")
            owner_pid = candidate.get("owner_pid")
            if (
                isinstance(bridge_pid, int)
                and bridge_pid > 0
                and isinstance(public_url, str)
                and public_url
                and isinstance(owner_pid, int)
                and process_is_running(owner_pid)
                and candidate.get("bridge_ready", True) is True
            ):
                watchdog = candidate
                break
        if detached.poll() is not None:
            detail = ""
            last_exit = _last_exit_payload(profile_name)
            if last_exit is not None:
                recorded_at = last_exit.get("recorded_at")
                if isinstance(recorded_at, (int, float)) and float(recorded_at) >= spawned_at - 1.0:
                    reason = str(last_exit.get("reason") or "")[:60]
                    if reason:
                        detail = f" (exit reason: {reason})"
            return {
                "saved_profile": profile_name,
                "action": "error",
                "error": (
                    "detached bridge process exited before becoming ready" + detail
                )[:200],
            }
        time.sleep(0.5)
    if watchdog is None:
        return {
            "saved_profile": profile_name,
            "action": "error",
            "error": f"bridge did not become ready within {timeout_seconds:.0f}s",
        }
    return _safe_start_result(
        profile, action="started", session_id=session_id, watchdog=watchdog
    )


def restart_saved_bridge(
    profile_name: str,
    *,
    stop_timeout_seconds: float = 15.0,
    start_timeout_seconds: float = 120.0,
) -> dict[str, Any]:
    """Stop the proven owner, then start a fresh detached owner.

    There is no sibling supervisor in KaroX 6: a caller that disappears mid
    restart leaves the profile stopped, which the status surface reports
    honestly.
    """
    stopped = stop_bridge(profile_name, timeout_seconds=stop_timeout_seconds)
    if stopped.get("action") == "error":
        return {
            "saved_profile": profile_name,
            "action": "error",
            "error": f"restart stop failed: {stopped.get('error') or stopped.get('reason')}"[
                :200
            ],
            "stop": stopped,
        }
    started = start_saved_bridge(profile_name, timeout_seconds=start_timeout_seconds)
    started["stop"] = stopped
    return started


__all__ = [
    "BridgeConnectResult",
    "DEFAULT_BRIDGE_PORT",
    "DEFAULT_BRIDGE_TOOLS",
    "MirroredChildOutput",
    "SavedBridgeRestartRequired",
    "WebBridgeConnectConfig",
    "WebBridgeLaunchError",
    "bridge_diagnostics",
    "build_connect_result",
    "config_from_saved_profile",
    "connect_bridge",
    "connect_saved_bridge",
    "connection_instructions",
    "core_bridge_tool_names",
    "local_mcp_route_healthy",
    "owner_exit_record_path",
    "restart_saved_bridge",
    "run_bridge_connect",
    "run_web_bridge",
    "start_saved_bridge",
    "status_bridge",
    "stop_bridge",
    "stop_saved_bridge",
    "watchdog_dir",
]