"""Cache-aware scheduler: evidence-only capability, honest economics.

Pins the Part 2 mandate invariants: UNKNOWN stays UNKNOWN (no dumb
``cache = true``), zeros never demote or confirm anything, invalidations
are counted, and every dollar figure is None unless the pricing record
really carries the rates it needs.
"""

from __future__ import annotations

import unittest

from _support import SRC  # noqa: F401
from karox.cache_scheduler import CacheAwareScheduler, CacheDecision
from karox.cost_intelligence import StablePrefixCache
from karox.provider_pricing import ModelPricing, PricingProvenance


def _key(cache: StablePrefixCache, system: str, schemas: str = "schemas"):
    return cache.compute_key(
        system_prompt=system, tool_schemas=schemas, session_id="s"
    )


def _pricing(**rates: float) -> ModelPricing:
    return ModelPricing(
        provider="p",
        model="m",
        provenance=PricingProvenance(source="test", recorded_at="2026-08-22"),
        **rates,
    )


class CapabilityEvidenceTests(unittest.TestCase):
    def test_capability_starts_unknown(self) -> None:
        scheduler = CacheAwareScheduler()
        capability = scheduler.capability
        self.assertIsNone(capability.supports_prompt_caching)
        self.assertIsNone(capability.reports_cache_reads)
        self.assertIsNone(capability.reports_cache_writes)
        self.assertIsNone(capability.ttl_seconds)
        self.assertEqual(capability.label(), "UNKNOWN")

    def test_zero_usage_never_confirms_or_denies(self) -> None:
        scheduler = CacheAwareScheduler()
        scheduler.observe_usage(cache_read_tokens=0, cache_write_tokens=0)
        self.assertIsNone(scheduler.capability.supports_prompt_caching)
        self.assertEqual(scheduler.cache_read_tokens_total, 0)

    def test_positive_read_confirms_support_and_read_reporting_only(self) -> None:
        scheduler = CacheAwareScheduler()
        scheduler.observe_usage(cache_read_tokens=128)
        capability = scheduler.capability
        self.assertTrue(capability.supports_prompt_caching)
        self.assertTrue(capability.reports_cache_reads)
        self.assertIsNone(capability.reports_cache_writes)
        self.assertEqual(capability.label(), "CONFIRMED")

    def test_positive_write_confirms_write_reporting(self) -> None:
        scheduler = CacheAwareScheduler()
        scheduler.observe_usage(cache_write_tokens=64)
        self.assertTrue(scheduler.capability.reports_cache_writes)
        self.assertIsNone(scheduler.capability.reports_cache_reads)

    def test_confirmed_capability_survives_a_later_zero(self) -> None:
        scheduler = CacheAwareScheduler()
        scheduler.observe_usage(cache_read_tokens=10)
        scheduler.observe_usage(cache_read_tokens=0)
        self.assertTrue(scheduler.capability.supports_prompt_caching)
        self.assertEqual(scheduler.cache_read_tokens_total, 10)

    def test_negative_counts_are_ignored(self) -> None:
        scheduler = CacheAwareScheduler()
        scheduler.observe_usage(cache_read_tokens=-5, cache_write_tokens=-1)
        self.assertIsNone(scheduler.capability.supports_prompt_caching)
        self.assertEqual(scheduler.cache_read_tokens_total, 0)
        self.assertEqual(scheduler.cache_write_tokens_total, 0)


class DecisionTests(unittest.TestCase):
    def test_first_request_then_reuse_then_invalidation(self) -> None:
        scheduler = CacheAwareScheduler()
        cache = StablePrefixCache()
        first = _key(cache, "system")
        d1 = scheduler.decide(prefix_key=first, previous_key=None)
        self.assertEqual(d1.verdict, CacheDecision.VERDICT_FIRST)
        self.assertFalse(d1.prefix_changed)

        second = _key(cache, "system")
        d2 = scheduler.decide(prefix_key=second, previous_key=first)
        self.assertEqual(d2.verdict, CacheDecision.VERDICT_REUSE)

        changed = _key(cache, "system CHANGED")
        d3 = scheduler.decide(prefix_key=changed, previous_key=second)
        self.assertEqual(d3.verdict, CacheDecision.VERDICT_WRITE)
        self.assertTrue(d3.prefix_changed)
        self.assertEqual(scheduler.decisions, 3)
        self.assertEqual(scheduler.reuse_decisions, 1)
        self.assertEqual(scheduler.invalidations, 1)

    def test_decision_always_advertises_and_carries_prefix_sha(self) -> None:
        scheduler = CacheAwareScheduler()
        cache = StablePrefixCache()
        key = _key(cache, "system")
        decision = scheduler.decide(prefix_key=key, previous_key=None)
        self.assertTrue(decision.advertise_cache_key)
        self.assertEqual(decision.prefix_sha, key.prefix_hash)
        self.assertEqual(
            decision.stable_prefix_token_estimate, key.token_estimate
        )

    def test_unknown_reuse_expectation_stays_none(self) -> None:
        scheduler = CacheAwareScheduler()
        cache = StablePrefixCache()
        decision = scheduler.decide(
            prefix_key=_key(cache, "system"), previous_key=None
        )
        self.assertIsNone(decision.expected_reuse_steps)
        self.assertIsNone(decision.dynamic_chars)


class EconomicsTests(unittest.TestCase):
    def test_no_pricing_means_every_estimate_is_none(self) -> None:
        scheduler = CacheAwareScheduler()
        cache = StablePrefixCache()
        decision = scheduler.decide(
            prefix_key=_key(cache, "system"), previous_key=None
        )
        self.assertIsNone(decision.estimated_write_usd)
        self.assertIsNone(decision.estimated_saving_per_reuse_usd)
        scheduler.observe_usage(cache_read_tokens=1000)
        self.assertIsNone(scheduler.estimated_reuse_saving_usd())

    def test_partial_pricing_never_produces_a_partial_sum(self) -> None:
        scheduler = CacheAwareScheduler(
            pricing=_pricing(input_per_mtok=10.0)
        )
        cache = StablePrefixCache()
        decision = scheduler.decide(
            prefix_key=_key(cache, "system"), previous_key=None
        )
        self.assertIsNone(decision.estimated_write_usd)
        self.assertIsNone(decision.estimated_saving_per_reuse_usd)
        scheduler.observe_usage(cache_read_tokens=1000)
        self.assertIsNone(scheduler.estimated_reuse_saving_usd())

    def test_full_pricing_yields_estimates(self) -> None:
        scheduler = CacheAwareScheduler(
            pricing=_pricing(
                input_per_mtok=10.0,
                cached_input_per_mtok=1.0,
                cache_write_per_mtok=12.5,
            )
        )
        cache = StablePrefixCache()
        key = _key(cache, "s" * 4_000)
        decision = scheduler.decide(prefix_key=key, previous_key=None)
        expected_write = (key.token_estimate / 1_000_000.0) * 12.5
        expected_saving = (key.token_estimate / 1_000_000.0) * 9.0
        self.assertAlmostEqual(
            decision.estimated_write_usd or 0.0, expected_write
        )
        self.assertAlmostEqual(
            decision.estimated_saving_per_reuse_usd or 0.0, expected_saving
        )

    def test_measured_reads_with_full_rates_estimate_saving(self) -> None:
        scheduler = CacheAwareScheduler(
            pricing=_pricing(input_per_mtok=10.0, cached_input_per_mtok=1.0)
        )
        scheduler.observe_usage(cache_read_tokens=2_000_000)
        saving = scheduler.estimated_reuse_saving_usd()
        self.assertIsNotNone(saving)
        assert saving is not None
        self.assertAlmostEqual(saving, 18.0)

    def test_no_measured_reads_means_no_saving_claim(self) -> None:
        scheduler = CacheAwareScheduler(
            pricing=_pricing(input_per_mtok=10.0, cached_input_per_mtok=1.0)
        )
        self.assertIsNone(scheduler.estimated_reuse_saving_usd())

    def test_cached_rate_above_input_rate_is_reported_not_hidden(self) -> None:
        scheduler = CacheAwareScheduler(
            pricing=_pricing(input_per_mtok=1.0, cached_input_per_mtok=2.0)
        )
        scheduler.observe_usage(cache_read_tokens=1_000_000)
        saving = scheduler.estimated_reuse_saving_usd()
        self.assertIsNotNone(saving)
        assert saving is not None
        self.assertAlmostEqual(saving, -1.0)

    def test_pricing_can_follow_a_model_switch(self) -> None:
        scheduler = CacheAwareScheduler(
            pricing=_pricing(input_per_mtok=10.0, cached_input_per_mtok=1.0)
        )
        scheduler.observe_usage(cache_read_tokens=1_000_000)
        scheduler.set_pricing(None)
        self.assertIsNone(scheduler.estimated_reuse_saving_usd())


if __name__ == "__main__":
    unittest.main()
