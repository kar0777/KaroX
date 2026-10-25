"""Window/process identity pinning for desktop targets.

An HWND alone is not authority: Windows recycles handle values, and a PID can
be reused after a process exits. Every attachment therefore pins the triple
(HWND, PID, process creation time) plus an executable digest, and every later
action re-verifies it before a single message is posted. A recycled handle or
a restarted process fails closed with :class:`TargetLostError` instead of
redirecting input into whatever now owns the handle.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Optional

from .errors import TargetLostError


def _executable_digest(executable: str) -> str:
    normalized = (executable or "").replace("\\", "/").casefold()
    return hashlib.sha256(normalized.encode("utf-8", "surrogatepass")).hexdigest()


@dataclass(frozen=True)
class WindowIdentity:
    """The pinned identity plus the live geometry of one desktop window."""

    hwnd: int
    pid: int
    title: str
    class_name: str
    rect: tuple[int, int, int, int]
    dpi: int
    minimized: bool
    process_create_time: Optional[float] = None
    process_executable_digest: str = ""

    @property
    def width(self) -> int:
        return max(0, self.rect[2] - self.rect[0])

    @property
    def height(self) -> int:
        return max(0, self.rect[3] - self.rect[1])

    @property
    def dpi_scale(self) -> float:
        return round((self.dpi or 96) / 96.0, 4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hwnd": self.hwnd,
            "pid": self.pid,
            "title": self.title[:300],
            "class_name": self.class_name[:120],
            "rect": list(self.rect),
            "dpi": self.dpi,
            "minimized": self.minimized,
            "process_create_time": self.process_create_time,
            "process_executable_digest": self.process_executable_digest,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WindowIdentity":
        rect = data.get("rect") or [0, 0, 0, 0]
        create_time = data.get("process_create_time")
        return cls(
            hwnd=int(data.get("hwnd", 0)),
            pid=int(data.get("pid", 0)),
            title=str(data.get("title", "")),
            class_name=str(data.get("class_name", "")),
            rect=(int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3])),
            dpi=int(data.get("dpi", 96) or 96),
            minimized=bool(data.get("minimized", False)),
            process_create_time=None if create_time is None else float(create_time),
            process_executable_digest=str(data.get("process_executable_digest", "")),
        )

    def public(self) -> dict[str, Any]:
        """The shape tools show a model: geometry and labels, no raw identity."""

        return {
            "window_id": self.hwnd,
            "title": self.title[:300],
            "class": self.class_name[:120],
            "process_id": self.pid,
            "process_executable_sha256_16": self.process_executable_digest[:16],
            "rect": list(self.rect),
            "width": self.width,
            "height": self.height,
            "dpi": self.dpi,
            "dpi_scale": self.dpi_scale,
            "minimized": self.minimized,
        }


def capture_identity(probe: Any, hwnd: int) -> WindowIdentity:
    """Build the pinned identity for one live HWND."""

    if not probe.is_window(hwnd):
        raise TargetLostError(f"window {hwnd} is not open")
    pid = int(probe.window_pid(hwnd) or 0)
    rect = probe.window_rect(hwnd)
    if rect is None or rect[2] <= rect[0] or rect[3] <= rect[1]:
        raise TargetLostError(f"window {hwnd} has no readable bounds")
    create_time: Optional[float] = None
    exe_digest = ""
    info = probe.process_info(pid) if pid else None
    if info is not None:
        create_time, executable = info
        exe_digest = _executable_digest(executable)
    return WindowIdentity(
        hwnd=int(hwnd),
        pid=pid,
        title=str(probe.window_text(hwnd, 300) or ""),
        class_name=str(probe.class_name(hwnd) or ""),
        rect=(int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3])),
        dpi=int(probe.dpi_for_window(hwnd) or 96),
        minimized=bool(probe.is_iconic(hwnd)),
        process_create_time=create_time,
        process_executable_digest=exe_digest,
    )


def refresh_identity(probe: Any, pinned: WindowIdentity) -> WindowIdentity:
    """Re-verify the pin and return fresh geometry, or fail closed.

    Checks, in order: the HWND still exists; it still belongs to the pinned
    PID; that PID still has the pinned creation time (a restart under a reused
    PID is a different process); the executable digest did not change. Only
    then are live rect/title/minimized/dpi values re-read, because a window may
    legitimately move between actions and clicks must use current bounds.
    """

    if not probe.is_window(pinned.hwnd):
        raise TargetLostError("attached window is no longer open")
    pid = int(probe.window_pid(pinned.hwnd) or 0)
    if pid != pinned.pid:
        raise TargetLostError("bound window handle was reused by a different process")
    info = probe.process_info(pid) if pid else None
    if pinned.process_create_time is not None:
        if info is None:
            raise TargetLostError("attached process identity can no longer be proven")
        create_time, executable = info
        if abs(float(create_time) - pinned.process_create_time) > 0.01:
            raise TargetLostError("attached process restarted under the same process id")
        if pinned.process_executable_digest and executable:
            if _executable_digest(executable) != pinned.process_executable_digest:
                raise TargetLostError("attached process executable changed")
    live = capture_identity(probe, pinned.hwnd)
    return WindowIdentity(
        hwnd=pinned.hwnd,
        pid=pinned.pid,
        title=live.title,
        class_name=live.class_name or pinned.class_name,
        rect=live.rect,
        dpi=live.dpi,
        minimized=live.minimized,
        process_create_time=pinned.process_create_time,
        process_executable_digest=pinned.process_executable_digest,
    )


__all__ = ["WindowIdentity", "capture_identity", "refresh_identity"]
