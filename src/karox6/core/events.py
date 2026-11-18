"""Events: the single output channel of the core.

Every interface (headless JSON stream, TUI, MCP server) consumes the same event
objects from ``Session.run()``; nothing renders state any other way. ``to_dict``
is the JSON-stream wire format: one object per line with a ``type`` field.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, ClassVar


@dataclass(slots=True)
class Event:
    type: ClassVar[str] = "event"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["type"] = self.type
        # Wall clock at serialization; consumers serialize as events arrive, so this is
        # when the event happened (to the millisecond), for time breakdowns of a run.
        data["ts"] = round(time.time(), 3)
        return data


@dataclass(slots=True)
class RunStarted(Event):
    type: ClassVar[str] = "run_started"
    session_id: str
    model: str
    provider: str
    workspace: str
    version: str
    resumed: bool = False


@dataclass(slots=True)
class TextDelta(Event):
    type: ClassVar[str] = "text_delta"
    text: str


@dataclass(slots=True)
class ReasoningDelta(Event):
    type: ClassVar[str] = "reasoning_delta"
    text: str


@dataclass(slots=True)
class AssistantMessage(Event):
    type: ClassVar[str] = "assistant_message"
    step: int
    text: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str = ""


@dataclass(slots=True)
class ToolStarted(Event):
    type: ClassVar[str] = "tool_started"
    call_id: str
    name: str
    arguments: str


@dataclass(slots=True)
class ToolFinished(Event):
    type: ClassVar[str] = "tool_finished"
    call_id: str
    name: str
    is_error: bool
    output: str
    duration_ms: int
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ApprovalRequested(Event):
    type: ClassVar[str] = "approval_requested"
    request_id: str
    tool: str
    summary: str
    rule: str
    reason: str


@dataclass(slots=True)
class ApprovalResolved(Event):
    type: ClassVar[str] = "approval_resolved"
    request_id: str
    approved: bool
    by: str


@dataclass(slots=True)
class UsageUpdate(Event):
    type: ClassVar[str] = "usage"
    step: int
    usage: dict[str, int]
    total: dict[str, int]
    cost_usd: float | None
    total_cost_usd: float | None
    # Timing of the request that produced this usage: latency_s of the attempt that
    # answered, ttft_s from that attempt's start to its first streamed delta (None
    # without deltas), total_s including failed attempts and retry waits.
    latency_s: float | None = None
    ttft_s: float | None = None
    total_s: float | None = None
    finish_reason: str = ""
    retries: int = 0


@dataclass(slots=True)
class Retry(Event):
    type: ClassVar[str] = "retry"
    attempt: int
    delay_s: float
    error: str


@dataclass(slots=True)
class Notice(Event):
    """A message the harness itself added to the conversation (measurable failures only)."""

    type: ClassVar[str] = "notice"
    kind: str
    text: str


@dataclass(slots=True)
class Compacted(Event):
    type: ClassVar[str] = "compacted"
    mode: str  # "prune" | "summary" | "drop" (overflow fallback without a model)
    tokens_before: int
    tokens_after: int
    duration_s: float = 0.0


@dataclass(slots=True)
class ErrorEvent(Event):
    type: ClassVar[str] = "error"
    code: str
    message: str


@dataclass(slots=True)
class RunFinished(Event):
    type: ClassVar[str] = "run_finished"
    status: str  # completed | limit | error | cancelled | loop
    reason: str
    final_text: str
    steps: int
    elapsed_s: float
    usage: dict[str, int]
    cost_usd: float | None
    files_changed: list[str] = field(default_factory=list)
    session_id: str = ""
