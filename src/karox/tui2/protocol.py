"""The line protocol between ``karox agent run --stream-json`` and the TUI.

One JSON document per line. Events carry only the fields the runtime already
promises are safe to show; the final document is the redacted report. Parsing is
total: an unknown or malformed line is ignored rather than taking the screen
down, because progress is advisory and the exit code plus the final report are
the authority.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


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


@dataclass(frozen=True)
class Decoded:
    kind: str
    event: Event | None = None
    outcome: Outcome | None = None


def decode_line(line: str) -> Decoded | None:
    """Decode one stdout line, or ``None`` when it is not part of the protocol."""

    stripped = line.strip()
    if not stripped:
        return None
    try:
        payload = json.loads(stripped)
    except (ValueError, RecursionError):
        return None
    if not isinstance(payload, dict):
        return None
    kind = payload.get("type")
    if kind == "event":
        return Decoded("event", event=Event.from_payload(payload))
    if kind == "result":
        return Decoded("result", outcome=Outcome.from_report(payload.get("report")))
    return None


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