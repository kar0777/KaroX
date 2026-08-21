"""/model auto contract: capability gate first, honest explained ranking.

Mandate: AUTO must never silently downgrade quality. Required capabilities
are hard gates satisfied only by explicit "true" verdicts; unknown metadata
is rejected, not invented. Ranking happens only among compatible models and
an explicit user selection always wins (the TUI applies AUTO only when the
user asks for it). Continuity: switching the model preserves the catalog and
the very next request resolves the new selection from the registry.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from _support import SRC  # noqa: F401

from karox.model_auto import REQUIRED_CAPABILITIES, recommend_model
from karox.registry import (
    ModelPricing,
    ModelRecord,
    ProviderRecord,
    ProviderRegistry,
)


def _model(model_id: str, **kwargs: object) -> ModelRecord:
    base: dict = dict(provider_id="prov", model_id=model_id)
    base.update(kwargs)
    return ModelRecord(**base)


def _pricing(input_per_million: float, output_per_million: float) -> ModelPricing:
    return ModelPricing(
        version="discovered",
        currency="USD",
        input_per_million=input_per_million,
        output_per_million=output_per_million,
        source="provider-reported",
    )


CAPABLE = dict(tools="true", streaming="true")


class RequirementGateTests(unittest.TestCase):
    def test_default_requirements_are_tools_and_streaming(self) -> None:
        self.assertEqual(REQUIRED_CAPABILITIES, ("tools", "streaming"))

    def test_explicit_false_is_rejected_with_reason(self) -> None:
        result = recommend_model(
            [_model("no-tools", tools="false", streaming="true")]
        )
        self.assertIsNone(result.record)
        self.assertIn("prov/no-tools: tools false", result.rejected)

    def test_unknown_is_rejected_not_invented(self) -> None:
        result = recommend_model([_model("mystery")])
        self.assertIsNone(result.record)
        self.assertIn(
            "prov/mystery: tools unknown, streaming unknown", result.rejected
        )

    def test_ranking_only_among_compatible(self) -> None:
        cheap_incompatible = _model("cheap", pricing=_pricing(0.0, 0.0))
        capable = _model("capable", pricing=_pricing(5.0, 15.0), **CAPABLE)
        result = recommend_model([cheap_incompatible, capable])
        assert result.record is not None
        self.assertEqual(result.record.model_id, "capable")


class RankingTests(unittest.TestCase):
    def test_known_context_beats_unknown(self) -> None:
        known = _model("known", context_window=8_000, **CAPABLE)
        unknown = _model("anon", **CAPABLE)
        result = recommend_model([unknown, known])
        assert result.record is not None
        self.assertEqual(result.record.model_id, "known")

    def test_larger_context_wins(self) -> None:
        small = _model("small", context_window=8_000, **CAPABLE)
        large = _model("large", context_window=200_000, **CAPABLE)
        result = recommend_model([small, large])
        assert result.record is not None
        self.assertEqual(result.record.model_id, "large")

    def test_published_cheaper_price_breaks_context_ties(self) -> None:
        pricey = _model(
            "pricey", context_window=100_000, pricing=_pricing(10.0, 30.0), **CAPABLE
        )
        cheap = _model(
            "cheap", context_window=100_000, pricing=_pricing(1.0, 2.0), **CAPABLE
        )
        unpriced = _model("unpriced", context_window=100_000, **CAPABLE)
        result = recommend_model([pricey, unpriced, cheap])
        assert result.record is not None
        self.assertEqual(result.record.model_id, "cheap")

    def test_deterministic_tie_break(self) -> None:
        a = _model("aaa", **CAPABLE)
        b = _model("bbb", **CAPABLE)
        first = recommend_model([b, a]).record
        second = recommend_model([a, b]).record
        assert first is not None and second is not None
        self.assertEqual(first.model_id, "aaa")
        self.assertEqual(second.model_id, "aaa")

    def test_reasons_explain_the_choice(self) -> None:
        best = _model(
            "best", context_window=128_000, pricing=_pricing(2.5, 10.0), **CAPABLE
        )
        result = recommend_model([best, _model("mystery")])
        text = "; ".join(result.reasons)
        self.assertIn("tools true", text)
        self.assertIn("streaming true", text)
        self.assertIn("context 128000", text)
        self.assertIn("price $2.5/$10 per M", text)
        self.assertIn("compatible 1 of 2", text)

    def test_no_compatible_model_reports_requirements(self) -> None:
        result = recommend_model([_model("m1"), _model("m2", tools="true")])
        self.assertIsNone(result.record)
        self.assertIn("tools, streaming", result.reasons[0])
        self.assertEqual(len(result.rejected), 2)


class ContinuityTests(unittest.TestCase):
    """Model switch preserves everything but the selection (mandate 6)."""

    def test_selection_persists_and_catalog_survives_switch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "providers.json"
            registry = ProviderRegistry(path)
            registry.put_provider(
                ProviderRecord(
                    provider_id="prov",
                    adapter_kind="openai_compatible_chat",
                    base_url="https://example.invalid/v1",
                )
            )
            registry.put_model(_model("m1", **CAPABLE))
            registry.put_model(_model("m2", **CAPABLE))
            registry.select_model("prov", "m1")
            registry.select_model("prov", "m2")
            fresh = ProviderRegistry(path)
            selected = fresh.selected_model()
            assert selected is not None
            # The next real request resolves the switch from the registry.
            self.assertEqual(selected.model_id, "m2")
            # The catalog and its metadata survive the switch untouched.
            self.assertEqual(
                [item.model_id for item in fresh.models()], ["m1", "m2"]
            )

    def test_agent_argv_does_not_pin_a_model(self) -> None:
        """Continuity by construction: argv never embeds the model.

        ``_agent_argv`` must not pass --model/--route, because the agent run
        resolves ``registry.selected_model()`` when the request starts. That
        is what makes a TUI switch reach the very next real request while
        session, map, and memory state stay untouched.
        """

        from karox import tui

        argv = tui._agent_argv("task", Path("."), (), "session")
        self.assertNotIn("--model", argv)
        self.assertNotIn("--route", argv)


if __name__ == "__main__":
    unittest.main()
