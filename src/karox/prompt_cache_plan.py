"""Deterministic prompt envelope for provider-cache-friendly orchestration.

Stable project material is rendered before role/task-specific material.  KaroX
never claims a provider cache hit from this layout; actual cache capability and
savings remain provider-reported through UsageAnalytics/CacheAwareScheduler.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from typing import Any, Iterable

from .context_bus import ContextItem


@dataclasses.dataclass(frozen=True)
class PromptEnvelope:
    stable_prefix: str
    stable_prefix_hash: str
    volatile_context: str
    stable_chars: int
    volatile_chars: int

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def build_prompt_envelope(items: Iterable[ContextItem]) -> PromptEnvelope:
    stable = sorted(
        (item for item in items if item.stable),
        key=lambda item: (item.kind, item.item_id),
    )
    volatile = sorted(
        (item for item in items if not item.stable),
        key=lambda item: (item.kind, item.item_id),
    )

    def render(rows: list[ContextItem]) -> str:
        payload = [
            {
                "id": item.item_id,
                "kind": item.kind,
                "hash": item.content_hash,
                "content": item.content,
            }
            for item in rows
        ]
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    stable_text = render(stable)
    volatile_text = render(volatile)
    digest = hashlib.sha256(stable_text.encode("utf-8")).hexdigest()
    return PromptEnvelope(
        stable_prefix=stable_text,
        stable_prefix_hash=digest,
        volatile_context=volatile_text,
        stable_chars=len(stable_text),
        volatile_chars=len(volatile_text),
    )


__all__ = ["PromptEnvelope", "build_prompt_envelope"]
