"""Provider pricing metadata with mandatory provenance.

CostGovernor and the usage surface may only represent money honestly:
every rate carries where it came from and when it was recorded, unknown
stays ``None``, and nothing here invents a number. Pricing is loaded from
a versioned JSON document (an explicit update path) rather than being
hard-coded, because provider price lists change under our feet.

An estimate produced from these records is exactly that -- the caller must
label it ESTIMATED, never mix it with provider-reported (MEASURED) cost,
and show UNAVAILABLE when no record exists.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Optional, Tuple

_MTOK = 1_000_000.0


@dataclass(frozen=True)
class PricingProvenance:
    """Where a price came from; required for every record."""

    source: str
    recorded_at: str
    version: Optional[str] = None


@dataclass(frozen=True)
class ModelPricing:
    """USD per million tokens for one provider model.

    Any component may be ``None`` when the provider does not publish it or
    does not bill it separately. ``None`` means unknown, not zero.
    """

    provider: str
    model: str
    provenance: PricingProvenance
    input_per_mtok: Optional[float] = None
    cached_input_per_mtok: Optional[float] = None
    cache_write_per_mtok: Optional[float] = None
    output_per_mtok: Optional[float] = None
    reasoning_per_mtok: Optional[float] = None
    long_context_threshold_tokens: Optional[int] = None
    long_context_input_per_mtok: Optional[float] = None

    def estimate_usd(
        self,
        *,
        input_tokens: int = 0,
        cached_input_tokens: int = 0,
        cache_write_tokens: int = 0,
        output_tokens: int = 0,
        reasoning_tokens: int = 0,
    ) -> Optional[float]:
        """Estimated cost, or ``None`` when any used component is unpriced.

        A component participates only when its token count is non-zero, so
        a missing reasoning rate does not block estimating a plain
        completion. When a non-zero component has no rate the whole
        estimate is unknown: a partial sum would be a lie.
        """

        parts: list[tuple[int, Optional[float]]] = [
            (input_tokens, self._input_rate(input_tokens)),
            (cached_input_tokens, self.cached_input_per_mtok),
            (cache_write_tokens, self.cache_write_per_mtok),
            (output_tokens, self.output_per_mtok),
            (reasoning_tokens, self.reasoning_per_mtok),
        ]
        total = 0.0
        for count, rate in parts:
            if count <= 0:
                continue
            if rate is None:
                return None
            total += (count / _MTOK) * rate
        return total

    def _input_rate(self, input_tokens: int) -> Optional[float]:
        threshold = self.long_context_threshold_tokens
        if (
            threshold is not None
            and input_tokens > threshold
            and self.long_context_input_per_mtok is not None
        ):
            return self.long_context_input_per_mtok
        return self.input_per_mtok


class PricingRegistry:
    """Versioned lookup from (provider, model) to a pricing record."""

    def __init__(self, records: Tuple[ModelPricing, ...] = ()) -> None:
        self._records: Dict[Tuple[str, str], ModelPricing] = {}
        for record in records:
            self._records[(record.provider, record.model)] = record

    def lookup(self, provider: str, model: str) -> Optional[ModelPricing]:
        return self._records.get((provider, model))

    def __len__(self) -> int:
        return len(self._records)

    @classmethod
    def from_document(cls, document: Mapping[str, object]) -> "PricingRegistry":
        """Parse the versioned pricing document; provenance is mandatory.

        A record without a source or recorded_at date is rejected outright:
        an unattributed price cannot be shown to a user as anything other
        than a guess, and guesses are not represented here.
        """

        entries = document.get("models")
        if not isinstance(entries, list):
            raise ValueError("pricing document requires a 'models' list")
        records: list[ModelPricing] = []
        for index, raw in enumerate(entries):
            if not isinstance(raw, dict):
                raise ValueError(f"models[{index}] must be an object")
            provider = raw.get("provider")
            model = raw.get("model")
            provenance = raw.get("provenance")
            if not isinstance(provider, str) or not provider:
                raise ValueError(f"models[{index}] missing provider")
            if not isinstance(model, str) or not model:
                raise ValueError(f"models[{index}] missing model")
            if not isinstance(provenance, dict):
                raise ValueError(f"models[{index}] missing provenance")
            source = provenance.get("source")
            recorded_at = provenance.get("recorded_at")
            if not isinstance(source, str) or not source:
                raise ValueError(f"models[{index}] provenance missing source")
            if not isinstance(recorded_at, str) or not recorded_at:
                raise ValueError(
                    f"models[{index}] provenance missing recorded_at"
                )
            version = provenance.get("version")
            records.append(
                ModelPricing(
                    provider=provider,
                    model=model,
                    provenance=PricingProvenance(
                        source=source,
                        recorded_at=recorded_at,
                        version=version if isinstance(version, str) else None,
                    ),
                    input_per_mtok=_rate(raw, "input_per_mtok", index),
                    cached_input_per_mtok=_rate(
                        raw, "cached_input_per_mtok", index
                    ),
                    cache_write_per_mtok=_rate(
                        raw, "cache_write_per_mtok", index
                    ),
                    output_per_mtok=_rate(raw, "output_per_mtok", index),
                    reasoning_per_mtok=_rate(raw, "reasoning_per_mtok", index),
                    long_context_threshold_tokens=_count(
                        raw, "long_context_threshold_tokens", index
                    ),
                    long_context_input_per_mtok=_rate(
                        raw, "long_context_input_per_mtok", index
                    ),
                )
            )
        return cls(tuple(records))

    @classmethod
    def from_path(cls, path: Path) -> "PricingRegistry":
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls(())
        if not isinstance(document, dict):
            raise ValueError("pricing document must be a JSON object")
        return cls.from_document(document)


def _rate(raw: Mapping[str, object], key: str, index: int) -> Optional[float]:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"models[{index}].{key} must be a number")
    if value < 0:
        raise ValueError(f"models[{index}].{key} must not be negative")
    return float(value)


def _count(raw: Mapping[str, object], key: str, index: int) -> Optional[int]:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"models[{index}].{key} must be an integer")
    if value < 0:
        raise ValueError(f"models[{index}].{key} must not be negative")
    return value


__all__ = [
    "ModelPricing",
    "PricingProvenance",
    "PricingRegistry",
]
