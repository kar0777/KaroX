"""Cooperative deadlines for desktop operations.

Desktop work is a chain of bounded Win32 calls and short waits. Every layer
carries one :class:`Deadline` and checks it between steps, so a cancelled or
timed-out tool call stops posting input instead of running to completion in the
background. The clock is injectable for deterministic tests.
"""

from __future__ import annotations

import time
from typing import Callable

from .errors import DesktopTimeoutError


class Deadline:
    """A monotonic point in time after which work must stop."""

    def __init__(self, now: Callable[[], float], expires_at: float) -> None:
        self._now = now
        self._expires_at = float(expires_at)

    @classmethod
    def after(cls, seconds: float, now: Callable[[], float] = time.monotonic) -> "Deadline":
        return cls(now, float(now()) + float(seconds))

    def remaining(self) -> float:
        return max(0.0, self._expires_at - float(self._now()))

    @property
    def expired(self) -> bool:
        return float(self._now()) >= self._expires_at

    def check(self, what: str = "desktop operation") -> None:
        if self.expired:
            raise DesktopTimeoutError(f"{what} exceeded its deadline")


__all__ = ["Deadline"]
