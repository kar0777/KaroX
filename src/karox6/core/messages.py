"""Provider-neutral conversation model.

Every provider adapter converts to and from these types; sessions persist them as
JSON. Provider-specific round-trip data (Gemini thought signatures, Anthropic
thinking signatures, Responses item ids) rides in ``extra`` and is never
interpreted by the core.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON text exactly as the model produced it
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "name": self.name, "arguments": self.arguments}
        if self.extra:
            out["extra"] = self.extra
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolCall:
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            arguments=str(data.get("arguments", "")),
            extra=dict(data.get("extra") or {}),
        )


@dataclass(slots=True)
class Message:
    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None  # role == "tool"
    name: str | None = None  # tool name for role == "tool"
    is_error: bool = False  # tool result failed
    reasoning: str | None = None  # assistant private reasoning text (replayed where required)
    extra: dict[str, Any] = field(default_factory=dict)  # provider round-trip data
    meta: dict[str, Any] = field(default_factory=dict)  # KaroX-only, never sent to a model
    # Pictures attached to a user message: [{"path": file in the session, "media_type":
    # "image/png"}]. Kept as files, read when a request is built (see image_payload).
    images: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.images:
            out["images"] = self.images
        if self.tool_calls:
            out["tool_calls"] = [c.to_dict() for c in self.tool_calls]
        if self.tool_call_id is not None:
            out["tool_call_id"] = self.tool_call_id
        if self.name is not None:
            out["name"] = self.name
        if self.is_error:
            out["is_error"] = True
        if self.reasoning:
            out["reasoning"] = self.reasoning
        if self.extra:
            out["extra"] = self.extra
        if self.meta:
            out["meta"] = self.meta
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Message:
        role = data["role"]
        if role not in ("system", "user", "assistant", "tool"):
            raise ValueError(f"unknown role {role!r}")
        return cls(
            role=role,
            content=str(data.get("content") or ""),
            tool_calls=[ToolCall.from_dict(c) for c in data.get("tool_calls") or []],
            tool_call_id=data.get("tool_call_id"),
            name=data.get("name"),
            is_error=bool(data.get("is_error", False)),
            reasoning=data.get("reasoning"),
            extra=dict(data.get("extra") or {}),
            meta=dict(data.get("meta") or {}),
            images=[dict(i) for i in data.get("images") or [] if isinstance(i, dict)],
        )


IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
IMAGE_MAX_BYTES = 5 * 1024 * 1024  # what the vision APIs accept per picture
IMAGE_TOKENS = 1_500  # a rough cost of one picture, for context estimates


def image_payload(image: dict[str, str]) -> tuple[str, str] | None:
    """(media type, base64 data) of an attached picture; None when its file is gone or
    too large. The bytes are read as they are, so every request encodes the picture the
    same way and a provider's prompt cache keeps matching."""
    import base64
    from pathlib import Path

    try:
        data = Path(image["path"]).read_bytes()
    except (OSError, KeyError):
        return None
    if not data or len(data) > IMAGE_MAX_BYTES:
        return None
    return image.get("media_type") or "image/png", base64.b64encode(data).decode("ascii")


def image_note(image: dict[str, str]) -> str:
    """Text that stands in for a picture that can no longer be read."""
    name = str(image.get("path") or "image").replace("\\", "/").rsplit("/", 1)[-1]
    return f"[image {name} is no longer available]"


@dataclass(slots=True)
class Usage:
    input_tokens: int = 0  # all prompt tokens, cached ones included
    cached_tokens: int = 0  # part of input_tokens served from a prompt cache
    output_tokens: int = 0  # completion tokens, reasoning included
    reasoning_tokens: int = 0  # part of output_tokens spent on reasoning
    requests: int = 0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.cached_tokens += other.cached_tokens
        self.output_tokens += other.output_tokens
        self.reasoning_tokens += other.reasoning_tokens
        self.requests += other.requests

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "cached_tokens": self.cached_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "requests": self.requests,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Usage:
        fields = ("input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens", "requests")
        return cls(**{k: int(data.get(k, 0) or 0) for k in fields})


@dataclass(slots=True)
class Price:
    """USD per 1M tokens. ``cached`` defaults to ``input`` when unknown."""

    input: float
    output: float
    cached: float | None = None

    def cost(self, usage: Usage) -> float:
        cached_rate = self.input if self.cached is None else self.cached
        fresh = max(usage.input_tokens - usage.cached_tokens, 0)
        return (
            fresh * self.input
            + usage.cached_tokens * cached_rate
            + usage.output_tokens * self.output
        ) / 1_000_000
