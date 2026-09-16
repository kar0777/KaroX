"""Strong process identity for safe cross-restart runtime reconciliation.

A PID on its own is never authority to adopt or terminate a process. Operating
systems reuse PIDs, so a record written by an earlier KaroX run can point at a
completely unrelated process after a reboot or a long uptime.

This module records the identity signals that are cheap, dependency-free and
stable enough to defeat PID reuse:

* the operating-system process creation time, which is unique per PID instance;
* a digest of the executable KaroX expected to launch;
* a digest of the argument vector KaroX expected to launch;
* a digest of the owning account;
* the capture timestamp.

No raw command line, environment or credential material is stored. Only digests
and a monotonic-ish creation timestamp are persisted, so a runtime registry
stays safe to read, export and attach to a support record.

When the platform cannot supply a creation time the verdict is ``unproven``
rather than ``verified``. KaroX then reports an honest unmanaged state instead
of guessing, which is the behaviour the connection lifecycle depends on.
"""

from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

IDENTITY_SCHEMA_VERSION = 1

#: Verdict reasons. These are stable machine values; user-facing text is
#: produced by the message catalogs, never by this module.
REASON_VERIFIED = "verified"
REASON_NOT_RECORDED = "identity_not_recorded"
REASON_NOT_RUNNING = "process_not_running"
REASON_CREATE_TIME_UNAVAILABLE = "create_time_unavailable"
REASON_CREATE_TIME_MISMATCH = "create_time_mismatch"
REASON_EXECUTABLE_MISMATCH = "executable_mismatch"
REASON_ARGV_MISMATCH = "argv_mismatch"
REASON_OWNER_MISMATCH = "owner_mismatch"


class ProcessIdentityError(RuntimeError):
    """A process identity value was malformed."""


def _digest(parts: Iterable[str]) -> str:
    joined = "\x1f".join(parts)
    return hashlib.sha256(joined.encode("utf-8", "surrogatepass")).hexdigest()


def executable_digest(executable: str) -> str:
    """Digest a launcher path in a case- and separator-stable way."""

    if not isinstance(executable, str) or not executable:
        raise ProcessIdentityError("executable is invalid")
    try:
        resolved = str(Path(executable).expanduser().resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        resolved = executable
    if os.name == "nt":
        resolved = resolved.casefold()
    return _digest(["exe", resolved])


def argv_digest(argv: Sequence[str]) -> str:
    """Digest an argument vector without persisting its contents."""

    if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence):
        raise ProcessIdentityError("argv must be a sequence of strings")
    items = list(argv)
    for item in items:
        if not isinstance(item, str):
            raise ProcessIdentityError("argv must be a sequence of strings")
    return _digest(["argv", str(len(items)), *items])


def owner_digest(owner: str) -> str:
    """Digest an account name so the registry never leaks the user name."""

    if not isinstance(owner, str) or not owner:
        raise ProcessIdentityError("owner is invalid")
    return _digest(["owner", owner.casefold()])


def current_owner() -> Optional[str]:
    for key in ("USERNAME", "USER", "LOGNAME"):
        value = os.environ.get(key)
        if value:
            return value
    try:
        return os.getlogin()
    except (OSError, AttributeError):
        return None


def _windows_create_time_ns(pid: int) -> Optional[int]:
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:  # pragma: no cover - non-Windows import guard
        return None

    process_query_limited_information = 0x1000
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    except (AttributeError, OSError):  # pragma: no cover - non-Windows
        return None

    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    handle = open_process(process_query_limited_information, False, int(pid))
    if not handle:
        return None
    try:
        creation = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        ok = kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exited),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        )
        if not ok:
            return None
        filetime = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
        if filetime <= 0:
            return None
        # FILETIME counts 100 ns intervals.
        return filetime * 100
    finally:
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle(handle)


def _linux_create_time_ns(pid: int) -> Optional[int]:
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None
    close = raw.rfind(")")
    if close < 0:
        return None
    fields = raw[close + 1 :].split()
    # /proc/<pid>/stat field 22 is starttime; the slice above starts at field 3.
    if len(fields) < 20:
        return None
    try:
        ticks = int(fields[19])
    except ValueError:
        return None
    try:
        # ``os.sysconf`` does not exist on Windows, and the Windows type stubs
        # do not declare it, so it is resolved dynamically rather than imported.
        sysconf = getattr(os, "sysconf", None)
        if sysconf is None:
            return None
        hz = int(sysconf("SC_CLK_TCK"))
    except (AttributeError, ValueError, OSError):  # pragma: no cover - exotic libc
        return None
    if not hz or hz <= 0:
        return None
    if ticks <= 0:
        # A process started in the first tick after boot is indistinguishable
        # from "unknown"; refuse to treat that as proof.
        return None
    return int(ticks * (1_000_000_000 // int(hz)))


def _mac_create_time_ns(pid: int) -> Optional[int]:
    """macOS has no /proc and KaroX loads no extra native crate for this;
    psutil ships with the runtime already, so read creation time from it."""
    try:
        import psutil  # type: ignore[import-untyped]

        return int(round(float(psutil.Process(int(pid)).create_time()) * 1_000_000_000))
    except Exception:
        return None


def read_process_create_time_ns(pid: int) -> Optional[int]:
    """Return the OS creation time of ``pid`` in nanoseconds, or ``None``.

    ``None`` means "this platform cannot prove it", never "the process is
    fine". Callers must treat it as an unproven identity.
    """

    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    try:
        if sys.platform.startswith("win"):
            return _windows_create_time_ns(pid)
        if sys.platform.startswith("linux"):
            return _linux_create_time_ns(pid)
        if sys.platform == "darwin":
            return _mac_create_time_ns(pid)
    except Exception:
        return None
    # Other platforms need a native call KaroX does not make yet.
    return None


def process_is_running(pid: int) -> bool:
    """Return whether a PID is executing, not merely still queryable by handle."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if sys.platform.startswith("win"):
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
            query_limited = 0x1000
            still_active = 259
            open_process = kernel32.OpenProcess
            open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            open_process.restype = wintypes.HANDLE
            handle = open_process(query_limited, False, int(pid))
            if not handle:
                return False
            try:
                exit_code = wintypes.DWORD()
                ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                return bool(ok) and int(exit_code.value) == still_active
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


CreateTimeReader = Callable[[int], Optional[int]]
PidAlive = Callable[[int], bool]


@dataclass(frozen=True)
class ProcessIdentity:
    """Persisted, credential-free identity of one launched process."""

    pid: int
    create_time_ns: Optional[int] = None
    executable_sha256: Optional[str] = None
    argv_sha256: Optional[str] = None
    owner_sha256: Optional[str] = None
    captured_at: Optional[float] = None

    def __post_init__(self) -> None:
        if not isinstance(self.pid, int) or isinstance(self.pid, bool) or self.pid <= 0:
            raise ProcessIdentityError("process identity PID is invalid")
        if self.create_time_ns is not None and (
            not isinstance(self.create_time_ns, int)
            or isinstance(self.create_time_ns, bool)
            or self.create_time_ns <= 0
        ):
            raise ProcessIdentityError("process creation time is invalid")
        for label, value in (
            ("executable digest", self.executable_sha256),
            ("argv digest", self.argv_sha256),
            ("owner digest", self.owner_sha256),
        ):
            if value is None:
                continue
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)
            ):
                raise ProcessIdentityError(f"{label} is invalid")
        if self.captured_at is not None and (
            not isinstance(self.captured_at, (int, float))
            or isinstance(self.captured_at, bool)
            or self.captured_at <= 0
        ):
            raise ProcessIdentityError("process identity capture time is invalid")

    @property
    def provable(self) -> bool:
        """True when the record carries a signal that defeats PID reuse."""

        return self.create_time_ns is not None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Any) -> "ProcessIdentity":
        if not isinstance(value, dict):
            raise ProcessIdentityError("process identity must be an object")
        allowed = set(cls.__dataclass_fields__)
        unknown = set(value).difference(allowed)
        if unknown:
            raise ProcessIdentityError(
                f"unknown process identity fields: {sorted(unknown)}"
            )
        try:
            return cls(**value)
        except (KeyError, TypeError, ValueError) as exc:
            raise ProcessIdentityError("process identity is malformed") from exc


@dataclass(frozen=True)
class ProcessIdentityVerdict:
    """The result of checking a persisted identity against the live system."""

    proven: bool
    alive: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def capture_process_identity(
    pid: int,
    *,
    executable: Optional[str] = None,
    argv: Optional[Sequence[str]] = None,
    owner: Optional[str] = None,
    now: Optional[float] = None,
    create_time_reader: CreateTimeReader = read_process_create_time_ns,
) -> ProcessIdentity:
    """Capture identity for a process KaroX has just launched."""

    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise ProcessIdentityError("process identity PID is invalid")
    import time as _time

    resolved_owner = owner if owner is not None else current_owner()
    return ProcessIdentity(
        pid=pid,
        create_time_ns=create_time_reader(pid),
        executable_sha256=executable_digest(executable) if executable else None,
        argv_sha256=argv_digest(argv) if argv is not None else None,
        owner_sha256=owner_digest(resolved_owner) if resolved_owner else None,
        captured_at=float(now if now is not None else _time.time()),
    )


def verify_process_identity(
    identity: Optional[ProcessIdentity],
    *,
    pid_alive: PidAlive,
    create_time_reader: CreateTimeReader = read_process_create_time_ns,
    expected_executable: Optional[str] = None,
    expected_argv: Optional[Sequence[str]] = None,
    expected_owner: Optional[str] = None,
) -> ProcessIdentityVerdict:
    """Decide whether a persisted identity still describes the live process.

    The function never terminates or signals anything. It only answers the one
    question the connection lifecycle needs: may KaroX treat this PID as the
    process it launched earlier?
    """

    if identity is None:
        return ProcessIdentityVerdict(False, False, REASON_NOT_RECORDED)

    try:
        alive = bool(pid_alive(identity.pid))
    except Exception:
        alive = False
    if not alive:
        return ProcessIdentityVerdict(False, False, REASON_NOT_RUNNING)

    if identity.create_time_ns is None:
        return ProcessIdentityVerdict(False, True, REASON_CREATE_TIME_UNAVAILABLE)

    try:
        observed = create_time_reader(identity.pid)
    except Exception:
        observed = None
    if observed is None:
        return ProcessIdentityVerdict(False, True, REASON_CREATE_TIME_UNAVAILABLE)
    if int(observed) != int(identity.create_time_ns):
        # The PID was reused. This is exactly the case a stored PID cannot see.
        return ProcessIdentityVerdict(False, True, REASON_CREATE_TIME_MISMATCH)

    if expected_executable is not None and identity.executable_sha256 is not None:
        if executable_digest(expected_executable) != identity.executable_sha256:
            return ProcessIdentityVerdict(False, True, REASON_EXECUTABLE_MISMATCH)
    if expected_argv is not None and identity.argv_sha256 is not None:
        if argv_digest(expected_argv) != identity.argv_sha256:
            return ProcessIdentityVerdict(False, True, REASON_ARGV_MISMATCH)
    if expected_owner is not None and identity.owner_sha256 is not None:
        if owner_digest(expected_owner) != identity.owner_sha256:
            return ProcessIdentityVerdict(False, True, REASON_OWNER_MISMATCH)

    return ProcessIdentityVerdict(True, True, REASON_VERIFIED)


__all__ = [
    "IDENTITY_SCHEMA_VERSION",
    "REASON_ARGV_MISMATCH",
    "REASON_CREATE_TIME_MISMATCH",
    "REASON_CREATE_TIME_UNAVAILABLE",
    "REASON_EXECUTABLE_MISMATCH",
    "REASON_NOT_RECORDED",
    "REASON_NOT_RUNNING",
    "REASON_OWNER_MISMATCH",
    "REASON_VERIFIED",
    "ProcessIdentity",
    "ProcessIdentityError",
    "ProcessIdentityVerdict",
    "argv_digest",
    "capture_process_identity",
    "current_owner",
    "executable_digest",
    "owner_digest",
    "process_is_running",
    "read_process_create_time_ns",
    "verify_process_identity",
]
