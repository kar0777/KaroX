"""One bounded, redacted event stream for every subsystem.

Before this module the only way to know what an agent was doing was to read the
stdout of whichever process happened to be doing it. That is why logs became
the main interface: the TUI had nothing else to render.

The event bus replaces that. Every subsystem publishes typed events, and the
TUI, the CLI and a support export all read the same stream:

* agent actions and tool calls;
* browser actions;
* session and connection state;
* risk decisions and confirmations;
* performance spans;
* health changes, evidence and errors.

Four properties are non-negotiable, and each one is a defect this module
exists to prevent:

**Bounded.** A long session must not grow memory without limit, so the buffer
has a hard capacity and reports how many events it dropped rather than
pretending the history is complete.

**Redacted.** Every payload goes through :func:`karox.security.redact` on the
way in, not on the way out. A credential that never enters the buffer cannot
leak from a UI, an evidence record or a support bundle later.

**Isolated.** A subscriber that raises must not take down the publisher or the
other subscribers. A UI bug is not allowed to stop an agent.

**Ordered and addressable.** Every event carries a monotonic sequence number,
so a UI can ask for everything after 412 instead of re-reading and re-parsing
the world on each refresh.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterator, Mapping, Optional

from .security import redact


class EventKind(str, Enum):
    """Typed event families. A UI switches on these, never on log text."""

    AGENT_ACTION = "agent_action"
    TOOL_CALL = "tool_call"
    BROWSER_ACTION = "browser_action"
    SESSION_STATE = "session_state"
    CONNECTION_STATE = "connection_state"
    RISK_DECISION = "risk_decision"
    CONFIRMATION = "confirmation"
    PERFORMANCE_SPAN = "performance_span"
    HEALTH_CHANGE = "health_change"
    EVIDENCE = "evidence"
    ERROR = "error"


class EventLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


# Default ring size. Large enough for a long working session, small enough that
# the worst case stays a few megabytes rather than unbounded growth.
DEFAULT_CAPACITY = 2000
# Summaries are one UI line, not a log paragraph.
MAX_SUMMARY = 300


def _clip(value: Any, limit: int = MAX_SUMMARY) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r", " ").replace("\n", " ").strip()
    if len(text) > limit:
        return text[: limit - 1] + "\u2026"
    return text


@dataclass(frozen=True)
class Event:
    """One thing that happened, safe to render, store and export."""

    seq: int
    kind: EventKind
    session_id: str
    source: str
    timestamp: float
    summary: str
    level: EventLevel = EventLevel.INFO
    data: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "kind": self.kind.value,
            "session_id": self.session_id,
            "source": self.source,
            "timestamp": self.timestamp,
            "summary": self.summary,
            "level": self.level.value,
            "data": dict(self.data),
        }


Subscriber = Callable[[Event], None]


class EventBus:
    """A bounded, thread-safe, redacted ring of typed events."""

    def __init__(
        self,
        *,
        capacity: int = DEFAULT_CAPACITY,
        now: Callable[[], float] = time.time,
    ) -> None:
        if capacity <= 0:
            raise ValueError("event bus capacity must be positive")
        self._events: deque[Event] = deque(maxlen=int(capacity))
        self._capacity = int(capacity)
        self._now = now
        self._seq = 0
        self._dropped = 0
        self._subscriber_errors = 0
        self._subscribers: dict[int, Subscriber] = {}
        self._next_subscriber_id = 0
        self._lock = threading.RLock()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def dropped(self) -> int:
        """Events evicted by the capacity limit. Reported, never hidden."""

        return self._dropped

    @property
    def subscriber_errors(self) -> int:
        return self._subscriber_errors

    def publish(
        self,
        kind: EventKind,
        *,
        session_id: str,
        summary: str = "",
        source: str = "karox",
        level: EventLevel = EventLevel.INFO,
        data: Optional[Mapping[str, Any]] = None,
        secrets: tuple[str, ...] = (),
    ) -> Event:
        """Record one event and hand it to every subscriber.

        ``secrets`` lets a caller name values it knows are sensitive, such as a
        token it just resolved, so they are removed even when they do not look
        secret-shaped and are not under a secret-shaped key.
        """

        payload = redact(dict(data or {}), secrets=secrets)
        if not isinstance(payload, dict):  # pragma: no cover - defensive
            payload = {}
        safe_summary = _clip(redact(summary, secrets=secrets))

        with self._lock:
            self._seq += 1
            event = Event(
                seq=self._seq,
                kind=kind,
                session_id=str(session_id or ""),
                source=str(source or "karox"),
                timestamp=float(self._now()),
                summary=safe_summary,
                level=level,
                data=payload,
            )
            if len(self._events) == self._capacity:
                self._dropped += 1
            self._events.append(event)
            listeners = list(self._subscribers.values())

        # Deliver outside the lock: a slow or reentrant subscriber must not
        # block a publisher, and a UI callback that publishes must not deadlock.
        for listener in listeners:
            try:
                listener(event)
            except Exception:
                # A broken UI is not allowed to stop an agent.
                with self._lock:
                    self._subscriber_errors += 1
        return event

    def subscribe(self, callback: Subscriber) -> Callable[[], None]:
        """Register a listener and return its unsubscribe handle."""

        if not callable(callback):
            raise TypeError("event subscriber must be callable")
        with self._lock:
            self._next_subscriber_id += 1
            key = self._next_subscriber_id
            self._subscribers[key] = callback

        def unsubscribe() -> None:
            with self._lock:
                self._subscribers.pop(key, None)

        return unsubscribe

    def snapshot(
        self,
        *,
        since_seq: int = 0,
        limit: Optional[int] = None,
        kinds: Optional[tuple[EventKind, ...]] = None,
        session_id: Optional[str] = None,
    ) -> list[Event]:
        """Read the buffer without consuming it.

        ``since_seq`` is how a UI refreshes incrementally instead of rebuilding
        its whole view on every tick.
        """

        with self._lock:
            items = [event for event in self._events if event.seq > since_seq]
        if kinds:
            wanted = set(kinds)
            items = [event for event in items if event.kind in wanted]
        if session_id is not None:
            items = [event for event in items if event.session_id == session_id]
        if limit is not None and limit >= 0:
            items = items[-limit:] if limit else []
        return items

    def latest_seq(self) -> int:
        with self._lock:
            return self._seq

    def clear(self) -> None:
        """Drop buffered events without touching anything on disk.

        This is the clear-logs action, and it is deliberately separate from
        anything that touches repository changes.
        """

        with self._lock:
            self._events.clear()

    def export_support_record(self, *, limit: int = 500) -> dict[str, Any]:
        """A bounded, already-redacted record safe to attach to a bug report."""

        with self._lock:
            events = list(self._events)[-max(int(limit), 0) :]
            return {
                "capacity": self._capacity,
                "buffered": len(self._events),
                "dropped": self._dropped,
                "subscriber_errors": self._subscriber_errors,
                "latest_seq": self._seq,
                "events": [event.to_dict() for event in events],
            }

    @contextmanager
    def span(
        self,
        name: str,
        *,
        session_id: str,
        source: str = "karox",
        component: str = "karox",
        data: Optional[Mapping[str, Any]] = None,
        monotonic: Callable[[], float] = time.perf_counter,
    ) -> Iterator[dict[str, Any]]:
        """Measure one path and publish it as a performance span.

        ``component`` separates KaroX overhead from network, provider, browser,
        subprocess, disk and keyring time. Without that split an optimisation
        effort is guesswork: a slow browser action and a slow KaroX dispatch
        look identical from the outside.

        The span is published even when the body raises, because a failed slow
        path is exactly the one worth measuring.
        """

        started = monotonic()
        extra: dict[str, Any] = dict(data or {})
        failed: Optional[str] = None
        try:
            yield extra
        except Exception as exc:
            failed = type(exc).__name__
            raise
        finally:
            duration_ms = round((monotonic() - started) * 1000.0, 3)
            payload = dict(extra)
            payload.update(
                {
                    "span": name,
                    "component": component,
                    "duration_ms": duration_ms,
                }
            )
            if failed is not None:
                payload["failed"] = failed
            self.publish(
                EventKind.PERFORMANCE_SPAN,
                session_id=session_id,
                source=source,
                summary=f"{name} {duration_ms} ms",
                level=EventLevel.WARNING if failed else EventLevel.INFO,
                data=payload,
            )


_BUS: Optional[EventBus] = None
_BUS_LOCK = threading.Lock()


def event_bus() -> EventBus:
    """The process-wide bus, so one stream describes the whole application."""

    global _BUS
    with _BUS_LOCK:
        if _BUS is None:
            _BUS = EventBus()
        return _BUS


__all__ = [
    "DEFAULT_CAPACITY",
    "Event",
    "EventBus",
    "EventKind",
    "EventLevel",
    "event_bus",
]
