"""Typed, evidence-linked communication between KaroX workers."""

from __future__ import annotations

import dataclasses
import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from .paths import runtime_dir
from .security import redact_content


MESSAGE_REQUEST = "request"
MESSAGE_RESULT = "result"
MESSAGE_REVIEW_PASS = "review_pass"
MESSAGE_REVIEW_FAIL = "review_fail"
MESSAGE_ESCALATE = "escalate"
MESSAGE_TYPES = frozenset(
    {MESSAGE_REQUEST, MESSAGE_RESULT, MESSAGE_REVIEW_PASS, MESSAGE_REVIEW_FAIL, MESSAGE_ESCALATE}
)
SEVERITIES = frozenset({"info", "low", "medium", "high", "critical"})
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,255}$")
_SCHEMA_VERSION = 1


class AgentProtocolError(RuntimeError):
    pass


def _safe_id(value: str, label: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{label} must contain 1-256 safe characters")
    return value


def _safe_text(value: str, label: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    value = value.replace("\x00", "").strip()
    if len(value) > limit:
        raise ValueError(f"{label} exceeds {limit} characters")
    redacted = redact_content(value)
    return redacted if isinstance(redacted, str) else str(redacted)


# The bound a handoff summary and question have to respect. Named because the
# producer clips model output to the same number the validator enforces: two
# literals would drift apart.
SUMMARY_LIMIT = 3000
QUESTION_LIMIT = 1000


def bounded_summary(text: str, limit: int = SUMMARY_LIMIT) -> str:
    """Clip a model-produced summary to what a message may carry.

    A worker's summary is whatever the model wrote, so it can be longer than the
    protocol allows. Rejecting it there aborts the mission at the handoff --
    after the work was done and verified, which is the worst moment to fail. The
    text is normalised exactly as the validator does and then clipped, so the
    result always passes that validation. This is a length bound, not a content
    filter: the validator's own redaction rules are unchanged.
    """
    if not isinstance(text, str):
        text = str(text)
    redacted = redact_content(text.replace("\x00", "").strip())
    normalised = redacted if isinstance(redacted, str) else str(redacted)
    if len(normalised) <= limit:
        return normalised
    return normalised[: limit - 1].rstrip() + "…"


@dataclasses.dataclass(frozen=True)
class EvidenceReference:
    artifact_id: str
    kind: str
    summary: str = ""

    def __post_init__(self) -> None:
        _safe_id(self.artifact_id, "artifact id")
        _safe_id(self.kind, "evidence kind")
        _safe_text(self.summary, "evidence summary", 500)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EvidenceReference":
        return cls(str(value["artifact_id"]), str(value["kind"]), str(value.get("summary", "")))


@dataclasses.dataclass(frozen=True)
class ReviewFinding:
    severity: str
    reason: str
    path: Optional[str] = None
    line: Optional[int] = None
    code: Optional[str] = None

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(f"unsupported finding severity: {self.severity}")
        _safe_text(self.reason, "review reason", 2000)
        if self.path is not None:
            _safe_text(self.path, "review path", 1000)
        if self.line is not None and (
            isinstance(self.line, bool) or not isinstance(self.line, int) or self.line <= 0
        ):
            raise ValueError("review line must be a positive integer")
        if self.code is not None:
            _safe_id(self.code, "review code")

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ReviewFinding":
        return cls(
            severity=str(value["severity"]),
            reason=str(value["reason"]),
            path=value.get("path"),
            line=value.get("line"),
            code=value.get("code"),
        )


@dataclasses.dataclass(frozen=True)
class AgentMessage:
    message_id: str
    message_type: str
    task_id: str
    source_endpoint_id: str
    target_endpoint_id: Optional[str]
    source_role: str
    target_role: Optional[str]
    summary: str
    baseline_ref: Optional[str] = None
    changeset_ref: Optional[str] = None
    evidence: tuple[EvidenceReference, ...] = ()
    questions: tuple[str, ...] = ()
    findings: tuple[ReviewFinding, ...] = ()
    created_at: float = 0.0

    def __post_init__(self) -> None:
        _safe_id(self.message_id, "message id")
        if self.message_type not in MESSAGE_TYPES:
            raise ValueError(f"unsupported message type: {self.message_type}")
        _safe_id(self.task_id, "task id")
        _safe_id(self.source_endpoint_id, "source endpoint id")
        if self.target_endpoint_id is not None:
            _safe_id(self.target_endpoint_id, "target endpoint id")
        _safe_id(self.source_role, "source role")
        if self.target_role is not None:
            _safe_id(self.target_role, "target role")
        _safe_text(self.summary, "message summary", SUMMARY_LIMIT)
        for ref in (self.baseline_ref, self.changeset_ref):
            if ref is not None:
                _safe_id(ref, "artifact reference")
        if len(self.evidence) > 100 or len(self.questions) > 50 or len(self.findings) > 100:
            raise ValueError("agent message exceeds bounded evidence/questions/findings")
        for question in self.questions:
            _safe_text(question, "question", QUESTION_LIMIT)
        if self.created_at < 0:
            raise ValueError("created_at must be non-negative")

    @classmethod
    def create(
        cls,
        *,
        message_type: str,
        task_id: str,
        source_endpoint_id: str,
        target_endpoint_id: Optional[str],
        source_role: str,
        target_role: Optional[str],
        summary: str,
        baseline_ref: Optional[str] = None,
        changeset_ref: Optional[str] = None,
        evidence: Iterable[EvidenceReference] = (),
        questions: Iterable[str] = (),
        findings: Iterable[ReviewFinding] = (),
    ) -> "AgentMessage":
        return cls(
            message_id=f"msg-{uuid.uuid4().hex}",
            message_type=message_type,
            task_id=task_id,
            source_endpoint_id=source_endpoint_id,
            target_endpoint_id=target_endpoint_id,
            source_role=source_role,
            target_role=target_role,
            summary=_safe_text(summary, "message summary", SUMMARY_LIMIT),
            baseline_ref=baseline_ref,
            changeset_ref=changeset_ref,
            evidence=tuple(evidence),
            questions=tuple(_safe_text(item, "question", QUESTION_LIMIT) for item in questions),
            findings=tuple(findings),
            created_at=time.time(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "message_type": self.message_type,
            "task_id": self.task_id,
            "source_endpoint_id": self.source_endpoint_id,
            "target_endpoint_id": self.target_endpoint_id,
            "source_role": self.source_role,
            "target_role": self.target_role,
            "summary": self.summary,
            "baseline_ref": self.baseline_ref,
            "changeset_ref": self.changeset_ref,
            "evidence": [item.to_dict() for item in self.evidence],
            "questions": list(self.questions),
            "findings": [item.to_dict() for item in self.findings],
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AgentMessage":
        return cls(
            message_id=str(value["message_id"]),
            message_type=str(value["message_type"]),
            task_id=str(value["task_id"]),
            source_endpoint_id=str(value["source_endpoint_id"]),
            target_endpoint_id=value.get("target_endpoint_id"),
            source_role=str(value["source_role"]),
            target_role=value.get("target_role"),
            summary=str(value.get("summary", "")),
            baseline_ref=value.get("baseline_ref"),
            changeset_ref=value.get("changeset_ref"),
            evidence=tuple(
                EvidenceReference.from_dict(item)
                for item in value.get("evidence", [])
                if isinstance(item, Mapping)
            ),
            questions=tuple(str(item) for item in value.get("questions", [])),
            findings=tuple(
                ReviewFinding.from_dict(item)
                for item in value.get("findings", [])
                if isinstance(item, Mapping)
            ),
            created_at=float(value.get("created_at", 0.0)),
        )


class HandoffStore:
    def __init__(self, session_id: str, *, path: Optional[Path] = None, limit: int = 1000) -> None:
        _safe_id(session_id, "session id")
        self.session_id = session_id
        self.path = (
            path
            or (runtime_dir() / "vnext" / "orchestration" / session_id / "handoffs.json")
        ).expanduser().resolve()
        if limit <= 0:
            raise ValueError("handoff limit must be positive")
        self.limit = limit

    def _load(self) -> list[AgentMessage]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentProtocolError(f"cannot read handoff ledger: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != _SCHEMA_VERSION:
            raise AgentProtocolError("handoff ledger has unsupported schema")
        raw = payload.get("messages")
        if not isinstance(raw, list):
            raise AgentProtocolError("handoff ledger messages must be an array")
        try:
            return [AgentMessage.from_dict(item) for item in raw if isinstance(item, Mapping)]
        except (KeyError, TypeError, ValueError) as exc:
            raise AgentProtocolError(f"handoff ledger is invalid: {exc}") from exc

    def _save(self, messages: list[AgentMessage]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        messages = messages[-self.limit :]
        fd, raw = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        temp = Path(raw)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(
                    {
                        "schema_version": _SCHEMA_VERSION,
                        "session_id": self.session_id,
                        "messages": [item.to_dict() for item in messages],
                    },
                    handle,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                    allow_nan=False,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    def append(self, message: AgentMessage) -> AgentMessage:
        messages = self._load()
        messages.append(message)
        self._save(messages)
        return message

    def list(self, *, task_id: Optional[str] = None, message_type: Optional[str] = None) -> list[AgentMessage]:
        result = self._load()
        if task_id is not None:
            result = [item for item in result if item.task_id == task_id]
        if message_type is not None:
            result = [item for item in result if item.message_type == message_type]
        return result

    def latest(self, task_id: str) -> Optional[AgentMessage]:
        items = self.list(task_id=task_id)
        return items[-1] if items else None


def independent_review_exclusions(
    *, implementer_endpoint_id: str, implementer_provider_id: Optional[str], candidates: Iterable[Any]
) -> tuple[str, ...]:
    """Prefer a genuinely independent reviewer without making review impossible.

    If at least one enabled candidate exists on another provider/source, exclude
    same-provider endpoints. Otherwise exclude only the exact implementer so the
    caller can surface a degraded but still independent-endpoint review.
    """

    different_provider: list[str] = []
    same_provider_other_endpoint: list[str] = []
    for candidate in candidates:
        endpoint_id = getattr(candidate, "endpoint_id", None)
        provider_id = getattr(candidate, "provider_id", None)
        enabled = bool(getattr(candidate, "enabled", False))
        if not enabled or not isinstance(endpoint_id, str) or endpoint_id == implementer_endpoint_id:
            continue
        if implementer_provider_id is None or provider_id != implementer_provider_id:
            different_provider.append(endpoint_id)
        else:
            same_provider_other_endpoint.append(endpoint_id)
    if different_provider:
        excluded = [implementer_endpoint_id]
        excluded.extend(same_provider_other_endpoint)
        return tuple(sorted(set(excluded)))
    return (implementer_endpoint_id,)


__all__ = [
    "AgentMessage",
    "AgentProtocolError",
    "EvidenceReference",
    "HandoffStore",
    "MESSAGE_ESCALATE",
    "MESSAGE_REQUEST",
    "MESSAGE_RESULT",
    "MESSAGE_REVIEW_FAIL",
    "MESSAGE_REVIEW_PASS",
    "MESSAGE_TYPES",
    "ReviewFinding",
    "SEVERITIES",
    "independent_review_exclusions",
]
