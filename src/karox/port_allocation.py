"""Atomic local port reservation and evidence-only listener ownership.

Two mandate rules live here:

* **No TOCTOU allocation.** ``find_free -> release -> bind later`` hands
  the port to whoever binds first in the gap. ``reserve_port`` binds
  exclusively once and *holds the socket* until the real server adopts or
  releases it, so the OS itself is the arbiter -- there is no window.
* **A foreign port owner is never killed.** Freeing a port may stop only
  a process that is *provably* a KaroX orphan. Proof is positive
  evidence in the process identity; "probably stale" and "cannot tell"
  both mean hands off, because the OS reuses PIDs and the port may
  legitimately belong to someone else's server.
"""

from __future__ import annotations

import dataclasses
import os
import socket
from typing import Optional


@dataclasses.dataclass
class PortReservation:
    """An exclusively bound socket holding one local port.

    The reservation *is* the bound socket: while this object lives, no
    other process can bind the port, and there is nothing to re-check
    later. Hand the socket to a server that can adopt one, or release at
    the last possible moment.
    """

    sock: socket.socket
    host: str
    port: int
    _released: bool = False

    def fileno(self) -> int:
        return self.sock.fileno()

    def detach_socket(self) -> socket.socket:
        """Transfer ownership of the bound socket to the caller.

        The reservation is consumed: the caller's server now holds the
        port with no release/bind gap at all. This is the preferred
        handoff for any server that accepts an existing socket.
        """

        if self._released:
            raise ValueError("port reservation was already released")
        self._released = True
        return self.sock

    def release(self) -> None:
        """Give the port back. Anything may bind it after this returns."""

        if self._released:
            return
        self._released = True
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self) -> "PortReservation":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


def reserve_port(host: str = "127.0.0.1", port: int = 0) -> PortReservation:
    """Atomically reserve ``host:port`` (or an OS-chosen free port).

    The bind is exclusive on every platform: Windows gets
    ``SO_EXCLUSIVEADDRUSE`` (otherwise another socket may bind over a
    non-listening holder), POSIX simply never sets ``SO_REUSEADDR``.
    Failure raises ``OSError`` exactly as the OS reported it -- a taken
    port is the caller's signal to pick another, never to kill the owner.
    """

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt":  # pragma: no cover - exercised on Windows CI/dev
            exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if exclusive is not None:
                sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        sock.bind((host, port))
    except OSError:
        sock.close()
        raise
    bound_port = int(sock.getsockname()[1])
    return PortReservation(sock=sock, host=host, port=bound_port)


#: Substrings that count as positive KaroX identity evidence in a process
#: command line or executable path. Lowercase; matching is case-insensitive.
KAROX_PROCESS_EVIDENCE: tuple[str, ...] = ("karox",)


def karox_owned_process(pid: int) -> Optional[bool]:
    """Evidence-only ownership check for one live process.

    ``True`` only when the process identity (name, executable path, or
    command line) positively names KaroX. ``False`` when the process is
    alive and provably something else. ``None`` when the platform cannot
    tell (no psutil, access denied, process already gone) -- and unknown
    must be treated exactly like foreign by every caller that kills.
    """

    try:
        import psutil  # type: ignore[import-untyped]
    except ImportError:
        return None
    try:
        process = psutil.Process(pid)
        pieces: list[str] = []
        try:
            pieces.append(process.name() or "")
        except Exception:
            pass
        try:
            pieces.append(process.exe() or "")
        except Exception:
            pass
        try:
            pieces.extend(process.cmdline() or [])
        except Exception:
            pass
        identity = " ".join(piece.lower() for piece in pieces if piece)
        if not identity:
            return None
        return any(evidence in identity for evidence in KAROX_PROCESS_EVIDENCE)
    except Exception:
        return None


__all__ = [
    "KAROX_PROCESS_EVIDENCE",
    "PortReservation",
    "karox_owned_process",
    "reserve_port",
]
