"""Pricing metadata acceptance: provenance required, unknown stays unknown."""

from __future__ import annotations

import unittest

from _support import SRC  # noqa: F401
from karox.provider_pricing import (
    ModelPricing,
    PricingProvenance,
    PricingRegistry,
)

PROVENANCE = PricingProvenance(
    source="https://example.com/pricing", recorded_at="2026-08-21"
)


def _record(**overrides: object) -> ModelPricing:
    base: dict[str, object] = dict(
        provider="openai",
        model="gpt-test",
        provenance=PROVENANCE,
        input_per_mtok=2.0,
        cached_input_per_mtok=0.5,
        output_per_mtok=8.0,
    )
    base.update(overrides)
    return ModelPricing(**base)  # type: ignore[arg-type]


class EstimateTests(unittest.TestCase):
    def test_priced_components_sum(self) -> None:
        estimate = _record().estimate_usd(
            input_tokens=1_000_000,
            cached_input_tokens=2_000_000,
            output_tokens=500_000,
        )
        self.assertIsNotNone(estimate)
        assert estimate is not None
        self.assertAlmostEqual(estimate, 2.0 + 1.0 + 4.0)

    def test_unpriced_used_component_makes_estimate_unknown(self) -> None:
        estimate = _record(cache_write_per_mtok=None).estimate_usd(
            input_tokens=100, cache_write_tokens=100
        )
        self.assertIsNone(estimate)

    def test_unused_unpriced_component_does_not_block(self) -> None:
        estimate = _record(reasoning_per_mtok=None).estimate_usd(
            input_tokens=100, output_tokens=100
        )
        self.assertIsNotNone(estimate)

    def test_long_context_tier_applies_over_threshold(self) -> None:
        record = _record(
            long_context_threshold_tokens=200_000,
            long_context_input_per_mtok=4.0,
        )
        below = record.estimate_usd(input_tokens=100_000)
        above = record.estimate_usd(input_tokens=1_000_000)
        assert below is not None and above is not None
        self.assertAlmostEqual(below, 0.2)
        self.assertAlmostEqual(above, 4.0)

    def test_zero_usage_costs_zero(self) -> None:
        self.assertEqual(_record().estimate_usd(), 0.0)


class DocumentTests(unittest.TestCase):
    def _document(self, **model_overrides: object) -> dict[str, object]:
        model: dict[str, object] = {
            "provider": "anthropic",
            "model": "claude-test",
            "provenance": {
                "source": "https://example.com",
                "recorded_at": "2026-08-21",
                "version": "v1",
            },
            "input_per_mtok": 3.0,
        }
        model.update(model_overrides)
        return {"models": [model]}

    def test_document_roundtrip(self) -> None:
        registry = PricingRegistry.from_document(self._document())
        record = registry.lookup("anthropic", "claude-test")
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.provenance.version, "v1")
        self.assertEqual(record.input_per_mtok, 3.0)
        self.assertIsNone(record.output_per_mtok)

    def test_missing_provenance_is_rejected(self) -> None:
        document = self._document()
        models = document["models"]
        assert isinstance(models, list)
        del models[0]["provenance"]
        with self.assertRaises(ValueError):
            PricingRegistry.from_document(document)

    def test_missing_source_is_rejected(self) -> None:
        document = self._document(
            provenance={"recorded_at": "2026-08-21"}
        )
        with self.assertRaises(ValueError):
            PricingRegistry.from_document(document)

    def test_negative_rate_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            PricingRegistry.from_document(
                self._document(input_per_mtok=-1.0)
            )

    def test_unknown_model_lookup_returns_none(self) -> None:
        registry = PricingRegistry.from_document(self._document())
        self.assertIsNone(registry.lookup("openai", "other"))


if __name__ == "__main__":
    unittest.main()
