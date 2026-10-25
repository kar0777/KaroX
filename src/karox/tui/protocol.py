"""The line protocol between ``karox agent run --stream-json`` and the TUI.

One JSON document per line. Events carry only the fields the runtime already
promises are safe to show; the final document is the redacted report. Parsing is
total: an unknown or malformed line is ignored rather than taking the screen
down, because progress is advisory and the exit code plus the final report are
the authority.

The protocol is versioned. A document may carry ``"v": <int>``; a missing
version means 1. A newer version is still parsed for the fields this client
knows and every unknown field or kind is ignored, so a kernel that extends the
stream never breaks an older screen.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

#: The stream version this client writes into its own contracts. Decoding is
#: forward tolerant: any ``v >= 1`` is accepted and parsed best-effort.
PROTOCOL_VERSION = 1


@dataclass(frozen=True)
class Event:
    """One typed progress event from a live run."""

    kind: str
    step: int = 0
    text: str = ""
    reasoning: str = ""
    reasoning_summary: str = ""
    tool: str = ""
    call_id: str = ""
    ok: bool | None = None
    summary: str = ""
    duration_seconds: float = 0.0
    reason: str = ""
    status: str = ""
    phase: str = ""
    path: str = ""
    detail: str = ""
    usage: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Event":
        return cls(
            kind=_text(payload.get("kind")),
            step=_int(payload.get("step")),
            text=_text(payload.get("text_delta")),
            reasoning=_text(payload.get("reasoning_delta")),
            reasoning_summary=_text(payload.get("reasoning_summary_delta")),
            tool=_text(payload.get("tool")),
            call_id=_text(payload.get("call_id")),
            ok=_optional_bool(payload.get("ok")),
            summary=_text(payload.get("summary")),
            duration_seconds=_float(payload.get("duration_seconds")),
            reason=_text(payload.get("reason")),
            status=_text(payload.get("status")),
            phase=_text(payload.get("phase")),
            path=_text(payload.get("path")),
            detail=_text(payload.get("detail")),
            usage=_int_map(payload.get("usage")),
        )


@dataclass(frozen=True)
class Outcome:
    """The parts of the final report a screen needs, already bounded."""

    session_id: str = ""
    status: str = ""
    phase: str = ""
    verified: bool = False
    reason: str = ""
    steps: int = 0
    changed_files: tuple[str, ...] = ()
    checks: tuple[dict[str, Any], ...] = ()
    usage: dict[str, Any] = field(default_factory=dict)
    provider_message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_report(cls, report: Any) -> "Outcome":
        if not isinstance(report, dict):
            return cls()
        checks = tuple(
            dict(item) for item in _list(report.get("checks")) if isinstance(item, dict)
        )
        return cls(
            session_id=_text(report.get("session_id")),
            status=_text(report.get("status")),
            phase=_text(report.get("phase")),
            verified=bool(report.get("verified")),
            reason=_text(report.get("reason")),
            steps=_int(report.get("steps")),
            changed_files=tuple(
                item for item in _list(report.get("changed_files")) if isinstance(item, str)
            ),
            checks=checks,
            usage=_intish_map(report.get("usage")),
            provider_message=_text(report.get("provider_message")),
            raw=report,
        )

    @property
    def check_tally(self) -> tuple[int, int]:
        passed = sum(1 for item in self.checks if item.get("ok") is True)
        return passed, len(self.checks)

    @property
    def token_total(self) -> int:
        for key in ("total_tokens", "total"):
            value = self.usage.get(key)
            if isinstance(value, int) and value > 0:
                return value
        return sum(
            value for key, value in self.usage.items()
            if key in {"input_tokens", "output_tokens"} and isinstance(value, int)
        )

    @property
    def costs(self) -> dict[str, float]:
        raw = self.usage.get("costs")
        if not isinstance(raw, dict):
            return {}
        return {
            str(key): float(value)
            for key, value in raw.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }


@dataclass(frozen=True)
class Decoded:
    kind: str
    event: Event | None = None
    outcome: Outcome | None = None


def decode_line(line: str) -> Decoded | None:
    """Decode one stdout line, or ``None`` when it is not part of the protocol.

    A document may carry ``"v"``; any integer version parses best-effort so a
    newer kernel degrades to the fields this client knows instead of breaking.
    """

    stripped = line.strip()
    if not stripped:
        return None
    try:
        payload = json.loads(stripped)
    except (ValueError, RecursionError):
        return None
    if not isinstance(payload, dict):
        return None
    # The version marker is advisory: an absent, future or malformed "v" never
    # drops a document, because losing progress lines is worse than guessing.
    kind = payload.get("type")
    if kind == "event":
        return Decoded("event", event=Event.from_payload(payload))
    if kind == "result":
        return Decoded("result", outcome=Outcome.from_report(payload.get("report")))
    return None


# The canonical dotted tool names the kernel emits, mirrored from agent.py so a
# card's category can never disagree with the runtime's own classification.
_READ_TOOLS = frozenset(
    {"repo.read_file", "repo.read_lines", "repo.list_files", "repo.search", "repo.tree"}
)
_EDIT_TOOLS = frozenset({"repo.edit_file", "repo.write_file", "repo.apply_patch"})
_TEST_TOOLS = frozenset({"checks.run", "checks.run_affected", "tests.run"})
_SUBAGENT_PREFIXES = ("agent.", "task.", "subagent.")


def tool_category(tool: str) -> str:
    """Classify one canonical tool name for icons, colours and run counters."""

    if not tool:
        return "other"
    if tool.startswith("mcp."):
        return "mcp"
    if tool.startswith("skill."):
        return "skill"
    if tool.startswith("web."):
        return "web"
    if tool.startswith(_SUBAGENT_PREFIXES):
        return "subagent"
    if tool == "plan.update":
        return "plan"
    if tool == "repo.inspect":
        return "inspect"
    if tool in _READ_TOOLS:
        return "read"
    if tool in _EDIT_TOOLS:
        return "edit"
    if tool in _TEST_TOOLS:
        return "test"
    return "other"


_MAX_PLAN_ITEMS = 60
_MAX_CHECKPOINTS = 20
_MAX_LIST_ENTRIES = 100


@dataclass(frozen=True)
class SessionState:
    """The durable session record, bounded and total-parsed for one panel.

    Source is ``karox session show <id> --json``; every field degrades to an
    empty default, because a half-written or older-schema record must still
    render rather than take the screen down.
    """

    session_id: str = ""
    name: str = ""
    task: str = ""
    task_history: tuple[str, ...] = ()
    status: str = ""
    phase: str = ""
    branch: str = ""
    revision: int = 0
    updated_at: float = 0.0
    plan_items: tuple[dict[str, Any], ...] = ()
    plan_todo: int = 0
    plan_doing: int = 0
    plan_done: int = 0
    checkpoints: tuple[dict[str, Any], ...] = ()
    checkpoint_count: int = 0
    last_checkpoint_at: float = 0.0
    checks: tuple[dict[str, Any], ...] = ()
    changed_files: tuple[str, ...] = ()
    skills: tuple[dict[str, Any], ...] = ()
    mcp_servers: tuple[dict[str, Any], ...] = ()
    jobs: tuple[dict[str, Any], ...] = ()
    requests: int = 0
    total_tokens: int = 0
    costs: dict[str, float] = field(default_factory=dict)
    verified_step: int = 0

    @classmethod
    def from_record(cls, record: Any) -> "SessionState":
        if not isinstance(record, dict):
            return cls()
        plan_items = tuple(
            dict(item)
            for item in _list(record.get("plan_items"))[:_MAX_PLAN_ITEMS]
            if isinstance(item, dict)
        )
        plan_todo = sum(1 for item in plan_items if item.get("status") == "todo")
        plan_doing = sum(1 for item in plan_items if item.get("status") == "doing")
        plan_done = sum(1 for item in plan_items if item.get("status") == "done")
        checkpoints = tuple(
            dict(item)
            for item in _list(record.get("checkpoints"))
            if isinstance(item, dict)
        )
        kept_checkpoints = checkpoints[-_MAX_CHECKPOINTS:]
        last_at = 0.0
        for item in checkpoints:
            created = item.get("created_at")
            if isinstance(created, (int, float)) and not isinstance(created, bool):
                last_at = max(last_at, float(created))
        checks = tuple(
            dict(item)
            for item in _list(record.get("checks"))[-_MAX_LIST_ENTRIES:]
            if isinstance(item, dict)
        )
        usage = record.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        costs_raw = usage.get("costs")
        costs = (
            {
                str(key): float(value)
                for key, value in costs_raw.items()
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            }
            if isinstance(costs_raw, dict)
            else {}
        )
        snapshot = record.get("verified_snapshot")
        verified_step = (
            _int(snapshot.get("step")) if isinstance(snapshot, dict) else 0
        )
        return cls(
            session_id=_text(record.get("session_id")),
            name=_text(record.get("name")),
            task=_text(record.get("task")),
            task_history=tuple(
                item
                for item in _list(record.get("task_history"))[:_MAX_LIST_ENTRIES]
                if isinstance(item, str)
            ),
            status=_text(record.get("status")),
            phase=_text(record.get("phase")),
            branch=_text(record.get("branch")),
            revision=_int(record.get("revision")),
            updated_at=_float(record.get("updated_at")),
            plan_items=plan_items,
            plan_todo=plan_todo,
            plan_doing=plan_doing,
            plan_done=plan_done,
            checkpoints=kept_checkpoints,
            checkpoint_count=len(checkpoints),
            last_checkpoint_at=last_at,
            checks=checks,
            changed_files=tuple(
                item
                for item in _list(record.get("changed_files"))[:_MAX_LIST_ENTRIES]
                if isinstance(item, str)
            ),
            skills=tuple(
                dict(item)
                for item in _list(record.get("skills"))[:_MAX_LIST_ENTRIES]
                if isinstance(item, dict)
            ),
            mcp_servers=tuple(
                dict(item)
                for item in _list(record.get("mcp_servers"))[:_MAX_LIST_ENTRIES]
                if isinstance(item, dict)
            ),
            jobs=tuple(
                dict(item)
                for item in _list(record.get("jobs"))[:_MAX_LIST_ENTRIES]
                if isinstance(item, dict)
            ),
            requests=_int(usage.get("requests")),
            total_tokens=_int(usage.get("total_tokens")),
            costs=costs,
            verified_step=verified_step,
        )

    @property
    def plan_tally(self) -> tuple[int, int, int]:
        return self.plan_todo, self.plan_doing, self.plan_done

    @property
    def check_tally(self) -> tuple[int, int]:
        passed = sum(1 for item in self.checks if item.get("ok") is True)
        return passed, len(self.checks)

    @property
    def has_checkpoint(self) -> bool:
        return self.checkpoint_count > 0

    @property
    def token_total(self) -> int:
        return self.total_tokens

    @property
    def skill_names(self) -> tuple[str, ...]:
        return tuple(
            name for name in (_text(item.get("name")) for item in self.skills) if name
        )

    @property
    def mcp_server_ids(self) -> tuple[str, ...]:
        return tuple(
            server
            for server in (_text(item.get("server_id")) for item in self.mcp_servers)
            if server
        )



def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    return int(value) if isinstance(value, (int, float)) else 0


def _float(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    return float(value) if isinstance(value, (int, float)) else 0.0


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _int_map(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): int(item)
        for key, item in value.items()
        if isinstance(item, (int, float)) and not isinstance(item, bool)
    }


def _intish_map(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}