"""Events: the single output channel of the core.

Every interface (headless JSON stream, TUI, MCP server) consumes the same event
objects from ``Session.run()``; nothing renders state any other way. ``to_dict``
is the JSON-stream wire format: one object per line with a ``type`` field.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, ClassVar


@dataclass(slots=True)
class Event:
    type: ClassVar[str] = "event"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["type"] = self.type
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
