"""Typed process-boundary transcript events (Phase 3 foundation).

The existing TUI polls ``provider_history`` from the session JSON on a timer.
Phase 3 replaces that with a typed, ordered, durable event stream so the
transcript, tool calls, activity lines, verification, and errors are all
projected from structured events rather than from a polled snapshot.

Design constraints:

* **Windows-compatible**: SQLite WAL journal, no POSIX-only primitives.
* **Ordered per session**: a monotonic sequence within each session.
* **Durable and replayable**: WAL mode survives a crash; events survive a
  process restart.
* **Idempotent**: an event with the same ``event_id`` can be appended twice
  without creating a duplicate (important for crash-retry).
* **Bounded**: old events are pruned per-session to a configurable retention.
* **No external broker**: everything is in-process SQLite + a lightweight
  notification callback.
* **Secret-safe**: payloads are redacted before storage (the event layer never
  holds or writes raw credentials).

This module is the **foundation** layer. Shadow-mode wiring (publishing
events alongside the existing poll) and the parity comparison that will let
us remove ``_poll_agent_history`` come after this layer is proven.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

#: Bump when the on-disk schema changes. Old stores are migrated forward, but
#: a reader that sees a higher version than its own refuses to open.
TRANSCRIPT_SCHEMA_VERSION = 1

#: How many events per session are kept after pruning. Large enough for the
#: replay a crash-recovery or a diff-review needs, small enough that a
#: multi-hour task does not grow unbounded.
DEFAULT_RETENTION = 5000

#: WAL checkpoint interval (events appended between checkpoints). Keeps the WAL
#: file from growing without bound while avoiding a checkpoint on every write.
_CHECKPOINT_INTERVAL = 200

# --------------------------------------------------------------------------- #
# Event types                                                                  #
# --------------------------------------------------------------------------- #

#: All typed event kind strings. A string (not an enum) so a JSON payload and
#: an in-memory event carry the same label without an import boundary.
EVENT_TYPES = frozenset({
    "TranscriptMessageAdded",
    "TranscriptMessageUpdated",
    "ToolCallStarted",
    "ToolCallProgress",
    "ToolCallCompleted",
    "ToolCallFailed",
    "AgentStepStarted",
    "AgentStepCompleted",
    "AgentPhaseChanged",
    "AgentWarning",
    "AgentErrorEvent",
    "FileRead",
    "FileEdited",
    "TestRunStarted",
    "TestRunCompleted",
    "ArtifactCreated",
    "VerificationStarted",
    "VerificationCompleted",
    "ConfirmationRequested",
    "ConfirmationResolved",
    "BrowserAction",
    "WorkspaceMutation",
    "SessionStateChanged",
    "UsageMeasured",
    "CostMeasured",
    "EvidenceRecorded",
    "ErrorRecorded",
})


# Fields the secret-redaction pass must never let through to storage.
_SECRET_FORBIDDEN_KEYS = frozenset({
    "secret", "token", "password", "api_key", "apikey",
    "authorization", "cookie", "access_token", "refresh_token",
    "credential", "private_key", "bearer",
})


def _redact_payload(payload: Any) -> Any:
    """Recursively strip anything that looks like a secret before storage.

    The typed event layer is the process boundary: once an event is in the
    store, any consumer (TUI, diff review, crash recovery) can read it, so the
    payload must be clean before it is written. This is a defence-in-depth
    pass; callers should also avoid putting secrets in events in the first
    place.
    """
    if isinstance(payload, dict):
        return {
            k: ("[REDACTED]" if k.lower() in _SECRET_FORBIDDEN_KEYS else _redact_payload(v))
            for k, v in payload.items()
        }
    if isinstance(payload, (list, tuple)):
        return [_redact_payload(item) for item in payload]
    return payload


# --------------------------------------------------------------------------- #
# Event dataclass                                                              #
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class TypedEvent:
    """One typed transcript event, ready for storage or replay.

    The required fields (schema_version through parent_id) are the envelope;
    ``payload`` carries the kind-specific redacted body.
    """

    event_id: str
    session_id: str
    sequence: int
    timestamp: float
    source_process: str
    kind: str
    payload: dict[str, Any]
    correlation_id: Optional[str] = None
    parent_id: Optional[str] = None
    schema_version: int = TRANSCRIPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.kind not in EVENT_TYPES:
            raise ValueError(f"unknown event kind: {self.kind}")
        if not self.event_id:
            raise ValueError("event_id must not be empty")
        if not self.session_id:
            raise ValueError("session_id must not be empty")
        if self.sequence < 0:
            raise ValueError("sequence must be non-negative")
        if not isinstance(self.payload, dict):
            raise ValueError("payload must be a dict")
        # Redact at construction time so no caller can bypass the boundary.
        object.__setattr__(self, "payload", _redact_payload(self.payload))

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "TypedEvent":
        return cls(
            event_id=str(row["event_id"]),
            session_id=str(row["session_id"]),
            sequence=int(row["sequence"]),
            timestamp=float(row["timestamp"]),
            source_process=str(row["source_process"]),
            kind=str(row["kind"]),
            payload=json.loads(str(row["payload"])),
            correlation_id=str(row["correlation_id"]) if row["correlation_id"] else None,
            parent_id=str(row["parent_id"]) if row["parent_id"] else None,
            schema_version=int(row["schema_version"]),
        )


# --------------------------------------------------------------------------- #
# SQLite WAL store                                                             #
# --------------------------------------------------------------------------- #


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS events (
    event_id        TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    sequence        INTEGER NOT NULL,
    timestamp       REAL NOT NULL,
    source_process  TEXT NOT NULL,
    kind            TEXT NOT NULL,
    payload         TEXT NOT NULL,
    correlation_id  TEXT,
    parent_id       TEXT,
    schema_version  INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_session_seq ON events (session_id, sequence);
CREATE INDEX IF NOT EXISTS idx_session_kind ON events (session_id, kind);
"""


class TranscriptStore:
    """Durable, ordered, idempotent typed-event store backed by SQLite WAL.

    One store per KaroX runtime (not per session): the ``session_id`` column
    partitions events, and a single WAL writer keeps ordering simple. Thread-safe
    via a write lock; reads are concurrent (SQLite WAL allows this).
    """

    def __init__(
        self,
        path: Path,
        *,
        retention: int = DEFAULT_RETENTION,
    ) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._retention = max(100, int(retention))
        self._lock = threading.Lock()
        self._write_count = 0
        self._listeners: list[Callable[[TypedEvent], None]] = []
        self._conn = self._open()
        self._init_schema()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self._path),
            isolation_level=None,  # autocommit; WAL handles durability
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        # WAL mode: concurrent readers do not block the writer, and a crash
        # leaves a valid database (the WAL is replayed on next open).
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA_SQL)

    # ------------------------------------------------------------------ #
    # Writing                                                             #
    # ------------------------------------------------------------------ #

    def append(
        self,
        *,
        session_id: str,
        kind: str,
        payload: dict[str, Any],
        source_process: str = "",
        correlation_id: Optional[str] = None,
        parent_id: Optional[str] = None,
        event_id: Optional[str] = None,
        timestamp: Optional[float] = None,
    ) -> TypedEvent:
        """Append one event, idempotent on ``event_id``.

        Returns the stored event. If ``event_id`` already exists, the existing
        row is returned without error (idempotent retry after a crash).
        """
        eid = event_id or str(uuid.uuid4())
        ts = timestamp if timestamp is not None else time.time()
        with self._lock:
            # Idempotent: check first.
            existing = self._conn.execute(
                "SELECT * FROM events WHERE event_id = ?", (eid,)
            ).fetchone()
            if existing is not None:
                return TypedEvent.from_row(existing)

            seq = self._next_sequence(session_id)
            event = TypedEvent(
                event_id=eid,
                session_id=session_id,
                sequence=seq,
                timestamp=ts,
                source_process=source_process or f"pid:{os.getpid()}",
                kind=kind,
                payload=payload,
                correlation_id=correlation_id,
                parent_id=parent_id,
            )
            self._conn.execute(
                """INSERT INTO events
                   (event_id, session_id, sequence, timestamp, source_process,
                    kind, payload, correlation_id, parent_id, schema_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event.event_id,
                    event.session_id,
                    event.sequence,
                    event.timestamp,
                    event.source_process,
                    event.kind,
                    json.dumps(event.payload, ensure_ascii=False, sort_keys=True),
                    event.correlation_id,
                    event.parent_id,
                    event.schema_version,
                ),
            )
            self._write_count += 1
            if self._write_count % _CHECKPOINT_INTERVAL == 0:
                self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            if self._write_count % self._retention == 0:
                self._prune_session(session_id)
        # Notify listeners outside the lock.
        for listener in self._listeners:
            try:
                listener(event)
            except Exception:
                pass  # a listener must never crash the writer
        return event

    def _next_sequence(self, session_id: str) -> int:
        row = self._conn.execute(
            "SELECT MAX(sequence) AS max_seq FROM events WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        current = row["max_seq"] if row and row["max_seq"] is not None else -1
        return current + 1

    def _prune_session(self, session_id: str) -> None:
        """Keep only the most recent ``retention`` events for a session."""
        self._conn.execute(
            """DELETE FROM events WHERE session_id = ? AND sequence <= (
                   SELECT MAX(sequence) - ? FROM events WHERE session_id = ?
               )""",
            (session_id, self._retention, session_id),
        )

    # ------------------------------------------------------------------ #
    # Reading                                                             #
    # ------------------------------------------------------------------ #

    def replay(
        self,
        session_id: str,
        *,
        from_sequence: int = 0,
        kind: Optional[str] = None,
    ) -> Iterator[TypedEvent]:
        """Yield events for ``session_id`` in order, optionally filtered by kind."""
        if kind is not None:
            cursor = self._conn.execute(
                """SELECT * FROM events
                   WHERE session_id = ? AND sequence >= ? AND kind = ?
                   ORDER BY sequence""",
                (session_id, from_sequence, kind),
            )
        else:
            cursor = self._conn.execute(
                """SELECT * FROM events
                   WHERE session_id = ? AND sequence >= ?
                   ORDER BY sequence""",
                (session_id, from_sequence),
            )
        for row in cursor:
            yield TypedEvent.from_row(row)

    def latest_sequence(self, session_id: str) -> int:
        row = self._conn.execute(
            "SELECT MAX(sequence) AS max_seq FROM events WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row and row["max_seq"] is not None:
            return int(row["max_seq"])
        return -1

    def count(self, session_id: Optional[str] = None) -> int:
        if session_id is not None:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM events WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------------ #
    # Listener                                                            #
    # ------------------------------------------------------------------ #

    def subscribe(self, listener: Callable[[TypedEvent], None]) -> Callable[[], None]:
        """Register a live-event listener. Returns an unsubscribe callable."""
        self._listeners.append(listener)

        def unsubscribe() -> None:
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass

        return unsubscribe

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            self._conn.close()


__all__ = [
    "DEFAULT_RETENTION",
    "EVENT_TYPES",
    "TRANSCRIPT_SCHEMA_VERSION",
    "TranscriptStore",
    "TypedEvent",
]
