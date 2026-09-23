"""Shared, delta-aware context bus for KaroX orchestration.

Agents should not independently pay to rediscover the same repository state.
This module stores a local, redacted, content-addressed context graph and produces
role-specific projections plus deltas relative to hashes a worker already knows.
It performs no model calls and contains no model-specific heuristics.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from .paths import runtime_dir
from .security import redact_content


_SCHEMA_VERSION = 1
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}$")
KINDS = frozenset(
    {
        "constitution",
        "environment",
        "architecture",
        "project_map",
        "file",
        "symbol",
        "test",
        "diff",
        "evidence",
        # Published by the subscription CLI once it runs an approved verification
        # command. This vocabulary is validation-only, so a kind the producer
        # already emits has to be listed here; otherwise the whole orchestration
        # run aborts on the evidence it just produced.
        "verification",
        "decision",
        "task",
        "browser",
        "log_summary",
        "handoff",
        "diagnostic",
    }
)


class ContextBusError(RuntimeError):
    pass


def _safe_id(value: str, label: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{label} must contain 1-256 safe characters")
    return value


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()


_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(?:token|secret|password|api[_-]?key|authorization)\b\s*[:=]\s*\S+"
)


def _redacted_text(value: str) -> str:
    redacted = redact_content(value)
    text = redacted if isinstance(redacted, str) else str(redacted)
    # ContextBus is a cross-agent reuse layer, so it deliberately applies a
    # second conservative assignment-style filter. A value that ordinary audit
    # redaction leaves alone must not become durable shared context merely
    # because it used ``api_key=...`` rather than a provider-specific prefix.
    return _SECRET_ASSIGNMENT.sub("[REDACTED]", text)


@dataclasses.dataclass(frozen=True)
class ContextItem:
    item_id: str
    kind: str
    content: str
    content_hash: str
    tags: tuple[str, ...] = ()
    priority: int = 50
    stable: bool = False
    source_ref: Optional[str] = None
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        _safe_id(self.item_id, "context item id")
        if self.kind not in KINDS:
            raise ValueError(f"unsupported context kind: {self.kind}")
        if not isinstance(self.content, str):
            raise ValueError("context content must be text")
        if _hash(self.content) != self.content_hash:
            raise ValueError("context content hash mismatch")
        for tag in self.tags:
            _safe_id(tag, "context tag")
        if isinstance(self.priority, bool) or not isinstance(self.priority, int) or not 0 <= self.priority <= 100:
            raise ValueError("context priority must be an integer between 0 and 100")
        if not isinstance(self.stable, bool):
            raise ValueError("context stable flag must be boolean")
        if self.source_ref is not None and (
            not isinstance(self.source_ref, str)
            or len(self.source_ref) > 1000
            or any(char in self.source_ref for char in "\r\n\x00")
        ):
            raise ValueError("context source_ref must be short plain text")
        if self.updated_at < 0:
            raise ValueError("context updated_at must be non-negative")

    @classmethod
    def build(
        cls,
        *,
        item_id: str,
        kind: str,
        content: str,
        tags: Iterable[str] = (),
        priority: int = 50,
        stable: bool = False,
        source_ref: Optional[str] = None,
        updated_at: Optional[float] = None,
    ) -> "ContextItem":
        safe_content = _redacted_text(content)
        return cls(
            item_id=item_id,
            kind=kind,
            content=safe_content,
            content_hash=_hash(safe_content),
            tags=tuple(dict.fromkeys(tags)),
            priority=priority,
            stable=stable,
            source_ref=source_ref,
            updated_at=time.time() if updated_at is None else float(updated_at),
        )

    def to_dict(self, *, include_content: bool = True) -> dict[str, Any]:
        result = dataclasses.asdict(self)
        result["tags"] = list(self.tags)
        if not include_content:
            result.pop("content", None)
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ContextItem":
        return cls(
            item_id=str(value["item_id"]),
            kind=str(value["kind"]),
            content=str(value["content"]),
            content_hash=str(value["content_hash"]),
            tags=tuple(value.get("tags") or ()),
            priority=value.get("priority", 50),
            stable=value.get("stable", False),
            source_ref=value.get("source_ref"),
            updated_at=float(value.get("updated_at", 0.0)),
        )


@dataclasses.dataclass(frozen=True)
class ContextUpdate:
    item_id: str
    changed: bool
    previous_hash: Optional[str]
    content_hash: str
    chars_avoided: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class ContextProjection:
    role: str
    items: tuple[ContextItem, ...]
    total_chars: int
    omitted_items: int
    stable_prefix_hash: str

    def to_dict(self, *, include_content: bool = True) -> dict[str, Any]:
        return {
            "role": self.role,
            "items": [item.to_dict(include_content=include_content) for item in self.items],
            "total_chars": self.total_chars,
            "omitted_items": self.omitted_items,
            "stable_prefix_hash": self.stable_prefix_hash,
        }


@dataclasses.dataclass(frozen=True)
class ContextDelta:
    role: str
    changed: tuple[ContextItem, ...]
    unchanged_ids: tuple[str, ...]
    removed_ids: tuple[str, ...]
    sent_chars: int
    reused_chars: int

    def to_dict(self, *, include_content: bool = True) -> dict[str, Any]:
        return {
            "role": self.role,
            "changed": [item.to_dict(include_content=include_content) for item in self.changed],
            "unchanged_ids": list(self.unchanged_ids),
            "removed_ids": list(self.removed_ids),
            "sent_chars": self.sent_chars,
            "reused_chars": self.reused_chars,
        }


_ROLE_KIND_WEIGHT: dict[str, dict[str, int]] = {
    "orchestrator": {"task": 50, "architecture": 45, "decision": 45, "project_map": 40, "evidence": 25},
    "planner": {"task": 50, "architecture": 50, "project_map": 45, "decision": 35, "file": 15},
    "implementer": {"task": 45, "file": 50, "symbol": 50, "diagnostic": 45, "test": 35, "architecture": 30},
    "reviewer": {"task": 45, "diff": 55, "evidence": 50, "test": 45, "decision": 30, "architecture": 25},
    "tester": {"task": 40, "test": 55, "diagnostic": 50, "diff": 30, "file": 20},
    "ui": {"task": 35, "browser": 55, "evidence": 45, "diff": 25, "diagnostic": 30},
    "security": {"task": 40, "diff": 55, "evidence": 50, "architecture": 35, "diagnostic": 35},
    "scout": {"task": 45, "project_map": 55, "symbol": 45, "file": 35, "architecture": 30},
    "summarizer": {"handoff": 55, "decision": 50, "task": 45, "evidence": 40, "diff": 25},
}


class ContextBus:
    def __init__(self, namespace: str, *, path: Optional[Path] = None) -> None:
        _safe_id(namespace, "context namespace")
        self.namespace = namespace
        self.path = (
            path
            or (runtime_dir() / "vnext" / "context-bus" / f"{namespace}.json")
        ).expanduser().resolve()

    def _load(self) -> dict[str, ContextItem]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContextBusError(f"cannot read context bus: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != _SCHEMA_VERSION:
            raise ContextBusError("context bus has unsupported schema")
        raw = payload.get("items")
        if not isinstance(raw, list):
            raise ContextBusError("context bus items must be an array")
        try:
            items = [ContextItem.from_dict(item) for item in raw if isinstance(item, Mapping)]
        except (KeyError, TypeError, ValueError) as exc:
            raise ContextBusError(f"context bus is invalid: {exc}") from exc
        return {item.item_id: item for item in items}

    def _save(self, items: Mapping[str, ContextItem]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, raw = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        temp = Path(raw)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(
                    {
                        "schema_version": _SCHEMA_VERSION,
                        "namespace": self.namespace,
                        "items": [items[key].to_dict() for key in sorted(items)],
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

    def upsert(self, item: ContextItem) -> ContextUpdate:
        items = self._load()
        old = items.get(item.item_id)
        if old is not None and old.content_hash == item.content_hash:
            return ContextUpdate(
                item_id=item.item_id,
                changed=False,
                previous_hash=old.content_hash,
                content_hash=item.content_hash,
                chars_avoided=len(item.content),
            )
        items[item.item_id] = item
        self._save(items)
        return ContextUpdate(
            item_id=item.item_id,
            changed=True,
            previous_hash=None if old is None else old.content_hash,
            content_hash=item.content_hash,
            chars_avoided=0,
        )

    def put_text(self, **kwargs: Any) -> ContextUpdate:
        return self.upsert(ContextItem.build(**kwargs))

    def remove(self, item_id: str) -> bool:
        items = self._load()
        if item_id not in items:
            return False
        items.pop(item_id)
        self._save(items)
        return True

    def items(self) -> list[ContextItem]:
        return [self._load()[key] for key in sorted(self._load())]

    @staticmethod
    def _stable_hash(items: Iterable[ContextItem]) -> str:
        stable = [f"{item.item_id}:{item.content_hash}" for item in items if item.stable]
        return _hash("\n".join(sorted(stable)))

    @staticmethod
    def _score(item: ContextItem, role: str, required_ids: frozenset[str]) -> tuple[int, float, str]:
        bonus = _ROLE_KIND_WEIGHT.get(role, {}).get(item.kind, 0)
        required = 10_000 if item.item_id in required_ids else 0
        stable = 25 if item.stable else 0
        return (required + item.priority + bonus + stable, item.updated_at, item.item_id)

    def projection(
        self,
        *,
        role: str,
        budget_chars: int = 60_000,
        required_ids: Iterable[str] = (),
        include_tags: Iterable[str] = (),
    ) -> ContextProjection:
        if budget_chars <= 0:
            raise ValueError("context projection budget must be positive")
        required = frozenset(required_ids)
        tags = frozenset(include_tags)
        items = self._load()
        candidates = [
            item
            for item in items.values()
            if not tags or tags.intersection(item.tags) or item.item_id in required
        ]
        candidates.sort(key=lambda item: self._score(item, role, required), reverse=True)
        selected: list[ContextItem] = []
        chars = 0
        for item in candidates:
            size = len(item.content)
            if chars + size > budget_chars and item.item_id not in required:
                continue
            selected.append(item)
            chars += size
        # Stable items first and deterministic within each tier: provider cache
        # prefixes remain byte-identical as current task items churn.
        selected.sort(key=lambda item: (not item.stable, item.kind, item.item_id))
        return ContextProjection(
            role=role,
            items=tuple(selected),
            total_chars=chars,
            omitted_items=max(0, len(candidates) - len(selected)),
            stable_prefix_hash=self._stable_hash(selected),
        )

    def delta(
        self,
        *,
        role: str,
        known_hashes: Mapping[str, str],
        budget_chars: int = 60_000,
        required_ids: Iterable[str] = (),
        include_tags: Iterable[str] = (),
    ) -> ContextDelta:
        projection = self.projection(
            role=role,
            budget_chars=budget_chars,
            required_ids=required_ids,
            include_tags=include_tags,
        )
        changed: list[ContextItem] = []
        unchanged: list[str] = []
        reused_chars = 0
        current_ids = {item.item_id for item in projection.items}
        for item in projection.items:
            if known_hashes.get(item.item_id) == item.content_hash:
                unchanged.append(item.item_id)
                reused_chars += len(item.content)
            else:
                changed.append(item)
        removed = sorted(set(known_hashes).difference(current_ids))
        return ContextDelta(
            role=role,
            changed=tuple(changed),
            unchanged_ids=tuple(sorted(unchanged)),
            removed_ids=tuple(removed),
            sent_chars=sum(len(item.content) for item in changed),
            reused_chars=reused_chars,
        )

    def manifest(self) -> dict[str, str]:
        return {item.item_id: item.content_hash for item in self._load().values()}

    def stats(self) -> dict[str, Any]:
        items = list(self._load().values())
        return {
            "namespace": self.namespace,
            "items": len(items),
            "chars": sum(len(item.content) for item in items),
            "stable_items": sum(1 for item in items if item.stable),
            "kinds": {
                kind: sum(1 for item in items if item.kind == kind)
                for kind in sorted({item.kind for item in items})
            },
        }


__all__ = [
    "ContextBus",
    "ContextBusError",
    "ContextDelta",
    "ContextItem",
    "ContextProjection",
    "ContextUpdate",
    "KINDS",
]
