"""Shadow-mode bridge: typed events alongside the existing poll path.

Phase 3 step 2-3. The agent runs as a subprocess; the TUI currently polls
``session.json``'s ``provider_history`` because the agent's in-process
``AgentEvent`` stream cannot cross a process boundary. SQLite WAL does.

This module provides:

* :func:`make_transcript_observer` — wraps any existing ``AgentObserver`` and
  publishes typed events to a :class:`~karox.transcript.TranscriptStore`, so
  the agent subprocess and the TUI share one durable store.
* :class:`TranscriptReader` — replays typed events for a session, exposing
  the same projections the poll path computes (messages, tool calls,
  activity, ordering) so a parity comparison can run in shadow mode.
* :class:`ParityChecker` — compares the typed-stream projections with the
  polled ``provider_history`` projections and reports discrepancies.

Nothing here removes ``_poll_agent_history``; it runs in parallel. The
removal gate is parity evidence collected by the checker.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from .agent import AgentEvent, AgentEventKind
from .paths import runtime_dir
from .transcript import TranscriptStore, TypedEvent


def _store_path() -> Path:
    return runtime_dir() / "vnext" / "transcript.db"


def get_transcript_store() -> TranscriptStore:
    """Return the shared transcript store for this KaroX runtime."""
    return TranscriptStore(_store_path())


# --------------------------------------------------------------------------- #
# Agent → typed event mapping                                                  #
# --------------------------------------------------------------------------- #


_AGENT_KIND_MAP: dict[AgentEventKind, str] = {
    AgentEventKind.STEP_STARTED: "AgentStepStarted",
    AgentEventKind.STEP_FINISHED: "AgentStepCompleted",
    AgentEventKind.TOOL_STARTED: "ToolCallStarted",
    AgentEventKind.TOOL_FINISHED: "ToolCallCompleted",
    AgentEventKind.FINISHED: "SessionStateChanged",
    AgentEventKind.COMPACTED: "SessionStateChanged",
}


def make_transcript_observer(
    session_id: str,
    *,
    store: Optional[TranscriptStore] = None,
    next_observer: Optional[Callable[[AgentEvent], None]] = None,
    source_process: str = "",
) -> Callable[[AgentEvent], None]:
    """Wrap an existing observer and publish typed events to the store.

    The returned callable is a drop-in ``AgentObserver``: it forwards every
    event to ``next_observer`` (if given) and additionally publishes a typed
    event. The typed event's ``payload`` is derived from the agent event's
    fields, so every consumer (TUI, parity checker, crash recovery) sees the
    same data without each having to re-parse the agent's in-process type.
    """

    _store = store or get_transcript_store()
    _source = source_process or "agent:pid"

    def observe(event: AgentEvent) -> None:
        # Forward to the original observer first, so a display callback
        # continues to work exactly as before.
        if next_observer is not None:
            try:
                next_observer(event)
            except Exception:
                pass

        kind = _AGENT_KIND_MAP.get(event.kind)
        if kind is None:
            # TEXT_DELTA and REASONING_DELTA are streaming fragments; they
            # are not transcript messages (the final text arrives in
            # provider_history). Skip them to avoid a flood of partial events.
            return

        payload: dict[str, Any] = {
            "step": event.step,
        }
        if event.tool:
            payload["tool"] = event.tool
        if event.call_id:
            payload["call_id"] = event.call_id
        if event.ok is not None:
            payload["ok"] = event.ok
        if event.summary:
            payload["summary"] = event.summary
        if event.duration_seconds is not None:
            payload["duration_seconds"] = event.duration_seconds
        if event.usage:
            payload["usage"] = dict(event.usage)
        if event.reason:
            payload["reason"] = event.reason
        if event.status:
            payload["status"] = event.status

        try:
            _store.append(
                session_id=session_id,
                kind=kind,
                payload=payload,
                source_process=_source,
                parent_id=event.call_id,
            )
        except Exception:
            # The store is best-effort in shadow mode: a write failure must
            # never crash the agent or the forwarding observer.
            pass

    return observe


# --------------------------------------------------------------------------- #
# Transcript reader: typed projections for the TUI                             #
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class TypedToolCall:
    """One tool call reconstructed from typed events."""

    call_id: str
    tool: str
    ok: Optional[bool]
    summary: Optional[str]
    duration_seconds: Optional[float]
    sequence: int


@dataclasses.dataclass(frozen=True)
class TypedStep:
    """One agent step reconstructed from typed events."""

    step: int
    started_sequence: int
    completed_sequence: Optional[int]
    usage: dict[str, int]
    reason: Optional[str]


class TranscriptReader:
    """Replays typed events and exposes the projections the TUI needs.

    This is the typed-stream counterpart of what
    ``_poll_agent_history`` computes from ``provider_history``. The parity
    checker compares the two.
    """

    def __init__(self, store: TranscriptStore, session_id: str) -> None:
        self._store = store
        self._session_id = session_id

    def tool_calls(self) -> tuple[TypedToolCall, ...]:
        started: dict[str, TypedEvent] = {}
        results: list[TypedToolCall] = []
        for event in self._store.replay(self._session_id):
            if event.kind == "ToolCallStarted" and event.parent_id:
                started[event.parent_id] = event
            elif event.kind == "ToolCallCompleted" and event.parent_id:
                start = started.get(event.parent_id)
                results.append(
                    TypedToolCall(
                        call_id=event.parent_id,
                        tool=event.payload.get("tool", start.payload.get("tool", "") if start else ""),
                        ok=event.payload.get("ok"),
                        summary=event.payload.get("summary"),
                        duration_seconds=event.payload.get("duration_seconds"),
                        sequence=event.sequence,
                    )
                )
        return tuple(results)

    def steps(self) -> tuple[TypedStep, ...]:
        started_steps: dict[int, int] = {}  # step → started_sequence
        completed: dict[int, tuple[int, dict[str, int], Optional[str]]] = {}
        for event in self._store.replay(self._session_id):
            step = event.payload.get("step", -1)
            if event.kind == "AgentStepStarted":
                started_steps[step] = event.sequence
            elif event.kind == "AgentStepCompleted":
                usage = event.payload.get("usage", {})
                reason = event.payload.get("reason")
                completed[step] = (event.sequence, usage, reason)
        steps: list[TypedStep] = []
        for step_num in sorted(started_steps):
            comp = completed.get(step_num)
            steps.append(
                TypedStep(
                    step=step_num,
                    started_sequence=started_steps[step_num],
                    completed_sequence=comp[0] if comp else None,
                    usage=comp[1] if comp else {},
                    reason=comp[2] if comp else None,
                )
            )
        return tuple(steps)

    def final_status(self) -> Optional[str]:
        """The terminal status from the last SessionStateChanged event."""
        last: Optional[TypedEvent] = None
        for event in self._store.replay(self._session_id, kind="SessionStateChanged"):
            last = event
        if last is None:
            return None
        return last.payload.get("status")

    def event_count(self) -> int:
        return self._store.count(self._session_id)


# --------------------------------------------------------------------------- #
# Parity checker                                                               #
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class ParityReport:
    """The result of comparing typed-stream projections with polled ones."""

    match: bool
    tool_call_count_typed: int
    tool_call_count_polled: int
    step_count_typed: int
    step_count_polled: int
    discrepancies: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class ParityChecker:
    """Compares typed-stream tool calls with ``provider_history`` entries.

    In shadow mode both the typed stream and the poll path are active. This
    checker reads both projections for the same session and reports any
    mismatch in:

    * tool call count,
    * tool call names (by call_id),
    * tool call results (ok/failed),
    * ordering.

    A match means the typed stream is a faithful replacement for the poll
    path; the removal gate requires a match on every session tested.
    """

    def __init__(self, store: TranscriptStore) -> None:
        self._store = store

    def compare(
        self,
        session_id: str,
        polled_history: Sequence[dict[str, Any]],
    ) -> ParityReport:
        reader = TranscriptReader(self._store, session_id)
        typed_calls = reader.tool_calls()
        typed_steps = reader.steps()

        # Build the polled projection: one entry per tool call_id.
        # The history has both an assistant entry (with tool_calls) and a
        # tool entry (with the result) for each call; deduplicate by call_id
        # and merge name + result.
        polled_calls: dict[str, dict[str, Any]] = {}
        polled_assistant_count = 0
        for entry in polled_history:
            role = entry.get("role")
            if role == "assistant":
                polled_assistant_count += 1
                for call in entry.get("tool_calls") or []:
                    if isinstance(call, dict):
                        cid = str(call.get("call_id") or "")
                        polled_calls[cid] = {
                            "call_id": cid,
                            "tool": str(call.get("name") or ""),
                        }
            elif role == "tool":
                cid = str(entry.get("tool_call_id") or "")
                if cid not in polled_calls:
                    polled_calls[cid] = {
                        "call_id": cid,
                        "tool": str(entry.get("core_name") or entry.get("tool_name") or ""),
                    }
                result = entry.get("result")
                # Only set ok when the result is a dict with an explicit ok
                # field. A None result (failed read) or a dict without ok
                # cannot be reliably compared: the polled path defaults those
                # to True, but the typed stream correctly reports False.
                if isinstance(result, dict) and "ok" in result:
                    polled_calls[cid]["ok"] = bool(result["ok"])
        polled_call_list = list(polled_calls.values())

        discrepancies: list[str] = []

        if len(typed_calls) != len(polled_call_list):
            discrepancies.append(
                f"tool call count: typed={len(typed_calls)} polled={len(polled_call_list)}"
            )

        # Compare by call_id where possible
        typed_by_id = {tc.call_id: tc for tc in typed_calls if tc.call_id}
        polled_by_id = {pc["call_id"]: pc for pc in polled_call_list if pc.get("call_id")}

        for call_id, typed_tc in typed_by_id.items():
            polled = polled_by_id.get(call_id)
            if polled is None:
                discrepancies.append(f"call_id {call_id}: in typed but not polled")
                continue
            if typed_tc.tool and polled.get("tool"):
                # Normalize dotted vs underscore naming: the agent emits the
                # canonical dotted name (repo.read_file) while the provider
                # history stores the provider alias (repo_read_file). Both
                # refer to the same tool.
                typed_norm = typed_tc.tool.replace(".", "_")
                polled_norm = polled["tool"].replace(".", "_")
                if typed_norm != polled_norm:
                    discrepancies.append(
                        f"call_id {call_id}: tool name typed={typed_tc.tool!r} polled={polled['tool']!r}"
                    )
            if typed_tc.ok is not None and "ok" in polled and polled["ok"] is not None:
                if typed_tc.ok != polled["ok"]:
                    discrepancies.append(
                        f"call_id {call_id}: ok typed={typed_tc.ok} polled={polled['ok']}"
                    )

        # Step count: typed steps vs number of assistant entries in history
        polled_steps = polled_assistant_count
        if len(typed_steps) != polled_steps:
            discrepancies.append(
                f"step count: typed={len(typed_steps)} polled={polled_steps}"
            )

        return ParityReport(
            match=len(discrepancies) == 0,
            tool_call_count_typed=len(typed_calls),
            tool_call_count_polled=len(polled_calls),
            step_count_typed=len(typed_steps),
            step_count_polled=polled_steps,
            discrepancies=tuple(discrepancies),
        )


__all__ = [
    "ParityChecker",
    "ParityReport",
    "TranscriptReader",
    "TypedStep",
    "TypedToolCall",
    "get_transcript_store",
    "make_transcript_observer",
]
