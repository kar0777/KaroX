"""Capability-aware ``/model auto`` recommendation.

AUTO is a recommendation engine, not an override. Required capabilities come
first: a model qualifies only when every required capability is an explicit
``"true"`` verdict in the registry -- ``"unknown"`` is rejected rather than
invented, so AUTO can never silently downgrade a task onto a model that may
not support tool calling or streaming. Ranking happens only among compatible
models, every verdict carries its reasons, and an explicit user selection
always wins: the TUI applies AUTO's choice only when the user runs
``/model auto`` themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

# The KaroX agent loop calls tools and renders streamed output, so a model
# recommended for real work must declare both. Callers may narrow or extend
# the requirement for special tasks.
REQUIRED_CAPABILITIES: tuple[str, ...] = ("tools", "streaming")


@dataclass(frozen=True)
class ModelAutoResult:
    """One AUTO verdict: the pick (or None), its reasons, the rejections."""

    record: Optional[Any]
    reasons: tuple[str, ...]
    rejected: tuple[str, ...]


def _capability(record: Any, name: str) -> str:
    value = str(getattr(record, name, "unknown"))
    return value if value in ("true", "false") else "unknown"


def _price_total(record: Any) -> Optional[float]:
    pricing = getattr(record, "pricing", None)
    if pricing is None:
        return None
    try:
        return float(pricing.input_per_million) + float(pricing.output_per_million)
    except (TypeError, ValueError):
        return None


def _sort_key(item: Any) -> tuple[Any, ...]:
    raw_context = getattr(item, "context_window", None)
    context = (
        raw_context
        if isinstance(raw_context, int)
        and not isinstance(raw_context, bool)
        and raw_context > 0
        else 0
    )
    price = _price_total(item)
    return (
        0 if context > 0 else 1,
        -context,
        0 if price is not None else 1,
        price if price is not None else 0.0,
        str(getattr(item, "provider_id", "")),
        str(getattr(item, "model_id", "")),
    )


def recommend_model(
    models: Iterable[Any], required: Sequence[str] = REQUIRED_CAPABILITIES
) -> ModelAutoResult:
    """Pick the best compatible model, with honest reasons for the choice.

    Compatibility is a hard gate: every required capability must be an
    explicit ``"true"``. Ranking prefers a known context window (larger
    first), then a published cheaper price; unpublished metadata ranks after
    published metadata but is never treated as a capability failure. Ties
    break deterministically by provider and model id.
    """

    compatible: list[Any] = []
    rejected: list[str] = []
    for item in models:
        misses = [
            f"{name} {_capability(item, name)}"
            for name in required
            if _capability(item, name) != "true"
        ]
        key = (
            f"{getattr(item, 'provider_id', '?')}/"
            f"{getattr(item, 'model_id', '?')}"
        )
        if misses:
            rejected.append(f"{key}: " + ", ".join(misses))
        else:
            compatible.append(item)
    if not compatible:
        needed = ", ".join(required)
        return ModelAutoResult(
            None,
            (f"no configured model declares {needed}",),
            tuple(rejected),
        )
    best = sorted(compatible, key=_sort_key)[0]
    reasons = [f"{name} true" for name in required]
    context = getattr(best, "context_window", None)
    if isinstance(context, int) and not isinstance(context, bool) and context > 0:
        reasons.append(f"context {context}")
    else:
        reasons.append("context unknown")
    pricing = getattr(best, "pricing", None)
    if pricing is None or _price_total(best) is None:
        reasons.append("price unpublished")
    else:
        reasons.append(
            f"price ${float(pricing.input_per_million):g}/"
            f"${float(pricing.output_per_million):g} per M"
        )
    reasons.append(
        f"compatible {len(compatible)} of {len(compatible) + len(rejected)}"
    )
    return ModelAutoResult(best, tuple(reasons), tuple(rejected))


__all__ = ["REQUIRED_CAPABILITIES", "ModelAutoResult", "recommend_model"]
