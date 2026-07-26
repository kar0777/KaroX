"""One-command lifecycle manager for ChatGPT/Claude web MCP bridges."""

from __future__ import annotations

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
from .hosted_bridge import DEFAULT_HOSTED_DEADLINE_SECONDS
from .models import AccessProfile
from .paths import runtime_dir, session_dir
from .proxy_server import ALLOWED_HOSTS_ENVIRONMENT
from .sessions import SessionStore


WEB_BRIDGE_PROFILES = ("chatgpt-web", "claude-web")
DEFAULT_WEB_TOOLS = (
    "karox.repo.read_file",
    "karox.repo.read_lines",
    "karox.repo.list_files",
    "karox.repo.search",
    "karox.git.status",
    "karox.git.diff",
    "karox.git.log",
)
WRITE_WEB_TOOLS = (
    "karox.repo.edit_file",
    "karox.repo.write_file",
)
_QUICK_TUNNEL_URL = re.compile(
    r"https://[A-Za-z0-9-]+\.trycloudflare\.com(?=$|[\s/])"
)
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
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
    tunnel_timeout_seconds: float = 30.0
    deadline_seconds: float = DEFAULT_HOSTED_DEADLINE_SECONDS
    verification_commands: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.profile not in WEB_BRIDGE_PROFILES:
            raise ValueError("web bridge profile must be chatgpt-web or claude-web")
        if not 1 <= self.port <= 65_535:
            raise ValueError("web bridge port must be between 1 and 65535")
        if self.tunnel not in {"cloudflare", "custom"}:
            raise ValueError("web bridge tunnel must be cloudflare or custom")
        if self.tunnel == "custom" and not self.public_url:
            raise ValueError("custom web bridge tunnel requires --public-url")
        if self.tunnel == "cloudflare" and self.public_url:
            raise ValueError(
                "--public-url is determined automatically for a Cloudflare Quick Tunnel"
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


def find_cloudflared(explicit: Optional[str] = None) -> Optional[str]:
    """Resolve cloudflared from an explicit path, PATH, or KaroX runtime bin."""
    if explicit:
        candidate = Path(explicit).expanduser().resolve()
        return str(candidate) if candidate.is_file() else None
    discovered = shutil.which("cloudflared")
    if discovered:
        return discovered
    executable = "cloudflared.exe" if os.name == "nt" else "cloudflared"
    bundled = runtime_dir() / "bin" / executable
    return str(bundled) if bundled.is_file() else None


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
    limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
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
        raise WebBridgeLaunchError(
            "cloudflared was not found; rerun the KaroX installer or pass "
            "--cloudflared PATH"
        )
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
        detail = next((line for line in reversed(output_tail) if line), "")
        suffix = f": {detail}" if detail else ""
        if code is None:
            raise WebBridgeLaunchError(
                f"cloudflared did not provide a public URL within {timeout_seconds:g}s"
                f"{suffix}"
            )
        raise WebBridgeLaunchError(f"cloudflared exited with code {code}{suffix}")
    return CloudflareQuickTunnel(process, public_url, output_tail, reader)


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
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        code = process.poll()
        if code is not None:
            raise WebBridgeLaunchError(f"KaroX bridge exited with code {code}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise WebBridgeLaunchError("KaroX bridge did not open its local port")


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


def ephemeral_url_warning(profile_name: str, public_url: Optional[str]) -> Optional[str]:
    """Warn when a profile that needs a stable URL is published on a throwaway one.

    ``chatgpt-web`` and ``claude-web`` both declare ``persistent_url=True``, and
    nothing read that field. A Cloudflare Quick Tunnel hands out a fresh
    ``*.trycloudflare.com`` name on every start, so the URL a user has just pasted
    into their connector stops existing the moment the bridge restarts -- and the
    connector then fails on their side with nothing here to explain why.

    Returns ``None`` when the user supplied their own origin, which is exactly the
    case the note would be telling them to move to.
    """
    if public_url:
        return None
    profile = next(
        (item for item in known_bridge_profiles() if item.name == profile_name), None
    )
    if profile is None or not profile.persistent_url:
        return None
    return (
        "\nNote: this is a Cloudflare Quick Tunnel, so the URL above is temporary. "
        "It changes every time the bridge restarts, and the connector you paste it "
        "into will stop working when it does. For something you keep, publish a "
        "stable HTTPS origin and pass --tunnel custom --public-url."
    )


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
        if isinstance(session_id, str) and session_id:
            # A record is now written before the session and credential exist, so
            # "reaped" may only name the ones that were really there to revoke.
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


def _session_id(config: WebBridgeConnectConfig) -> str:
    return config.session_id or (
        f"web-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    )


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
    for tool in config.tools:
        values.extend(("--tool", tool))
    for command in config.verification_commands:
        values.extend(("--verification-command", command))
    return tuple(values)


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

    tunnel: Optional[CloudflareQuickTunnel] = None
    bridge: Optional[subprocess.Popen[str]] = None
    credential_created = False
    session_created = False
    sessions: Optional[SessionStore] = None
    watchdog: Optional[Path] = None
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
            "port": config.port,
            "public_url": public_url,
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
        sessions.create(
            repository,
            f"{config.profile} managed web bridge",
            config.access_profile,
            session_id=session_id,
        )
        session_created = True
        credential = BridgeCredentialStore().set(session_id)
        credential_created = True
        secret = credential.get("secret")
        if not isinstance(secret, str) or not secret:
            raise WebBridgeLaunchError("bridge credential generator returned no secret")

        environment = dict(os.environ)
        # The listener cannot guess the tunnel host name, and without it every
        # request through the tunnel looks like a rebound DNS name.
        environment[ALLOWED_HOSTS_ENVIRONMENT] = urlsplit(public_url).hostname or ""
        try:
            bridge = subprocess.Popen(
                _bridge_argv(
                    config,
                    session_id=session_id,
                    public_url=public_url,
                ),
                cwd=repository,
                env=environment,
                **_child_options(),
            )
        except OSError as exc:
            raise WebBridgeLaunchError(
                f"cannot start KaroX bridge: {type(exc).__name__}"
            ) from exc
        _adopt_child(job, bridge)
        watchdog_record["bridge_pid"] = _pid_of(bridge)
        write_watchdog(watchdog, watchdog_record)
        _wait_for_bridge(bridge, config.port)

        endpoint = f"{public_url.rstrip('/')}/mcp"
        print(f"KaroX {config.profile} bridge is ready")
        print(f"MCP URL: {endpoint}")
        print(f"OAuth approval password: {secret}")
        print(f"Session: {session_id}")
        if config.profile == "chatgpt-web":
            print("Add the MCP URL as a custom app in ChatGPT developer mode.")
        else:
            print("Add the MCP URL in Claude Settings > Connectors.")
        note = ephemeral_url_warning(config.profile, config.public_url)
        if note:
            print(note)
        print("Press Ctrl+C to stop the bridge and tunnel.", flush=True)

        while True:
            bridge_code = bridge.poll()
            if bridge_code is not None:
                raise WebBridgeLaunchError(
                    f"KaroX bridge stopped unexpectedly with code {bridge_code}"
                )
            if tunnel is not None:
                tunnel_code = tunnel.process.poll()
                if tunnel_code is not None:
                    raise WebBridgeLaunchError(
                        f"Cloudflare tunnel stopped unexpectedly with code {tunnel_code}"
                    )
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\nStopping KaroX web bridge…", flush=True)
        return 0
    finally:
        _stop_process(bridge)
        if tunnel is not None:
            tunnel.stop()
        _close_job(job)
        if watchdog is not None:
            try:
                watchdog.unlink()
            except OSError:
                pass
        if credential_created:
            try:
                BridgeCredentialStore().delete(session_id)
            except Exception:
                pass
        if session_created and sessions is not None:
            try:
                sessions.revoke(session_id)
            except Exception:
                pass
