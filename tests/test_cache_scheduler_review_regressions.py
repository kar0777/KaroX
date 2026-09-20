"""Cache accounting and immutable capability upgrade regressions.

Fixtures expose only the pricing/key attributes consumed by this scheduler;
provider registry integration belongs to the full repository suite.
"""
from __future__ import annotations

import dataclasses
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from karox.cache_scheduler import CacheAwareScheduler, CacheDecision


def pricing(input_rate=10.0, cached_rate=2.0, write_rate=12.0):
    return SimpleNamespace(
        input_per_mtok=input_rate,
        cached_input_per_mtok=cached_rate,
        cache_write_per_mtok=write_rate,
    )


class CacheSchedulerRegressionTests(unittest.TestCase):
    def test_observation_only_allocates_on_capability_upgrade(self) -> None:
        scheduler = CacheAwareScheduler()
        unknown = scheduler.capability
        with patch("karox.cache_scheduler.dataclasses.replace", wraps=dataclasses.replace) as replace:
            scheduler.observe_usage(cache_read_tokens=10, cache_write_tokens=20)
            confirmed = scheduler.capability
            scheduler.observe_usage(cache_read_tokens=10, cache_write_tokens=20)
            scheduler.observe_usage(cache_read_tokens=-1, cache_write_tokens=-2)
            self.assertEqual(replace.call_count, 1)
        self.assertIs(scheduler.capability, confirmed)
        self.assertIsNone(unknown.supports_prompt_caching)
        self.assertTrue(confirmed.reports_cache_reads)
        self.assertTrue(confirmed.reports_cache_writes)
        self.assertEqual(scheduler.cache_read_tokens_total, 20)
        self.assertEqual(scheduler.cache_write_tokens_total, 40)

    def test_separate_capability_upgrades_and_zero_evidence(self) -> None:
        scheduler = CacheAwareScheduler()
        scheduler.observe_usage(cache_read_tokens=0, cache_write_tokens=-1)
        self.assertEqual(scheduler.capability.label(), "UNKNOWN")
        scheduler.observe_usage(cache_write_tokens=10)
        write_only = scheduler.capability
        self.assertTrue(write_only.reports_cache_writes)
        self.assertIsNone(write_only.reports_cache_reads)
        scheduler.observe_usage(cache_read_tokens=1)
        self.assertTrue(scheduler.capability.reports_cache_reads)
        self.assertTrue(scheduler.capability.reports_cache_writes)
        scheduler.observe_usage()
        self.assertEqual(scheduler.capability.label(), "CONFIRMED")

    def test_price_switch_does_not_reprice_previous_reads(self) -> None:
        scheduler = CacheAwareScheduler(pricing=pricing())
        scheduler.observe_usage(cache_read_tokens=1_000_000)
        self.assertEqual(scheduler.estimated_reuse_saving_usd(), 8.0)
        scheduler.set_pricing(pricing(20.0, 5.0))
        self.assertEqual(scheduler.estimated_reuse_saving_usd(), 8.0)
        scheduler.observe_usage(cache_read_tokens=1_000_000)
        self.assertEqual(scheduler.estimated_reuse_saving_usd(), 23.0)
        self.assertEqual(scheduler.cache_read_tokens_total, 2_000_000)

    def test_later_prices_do_not_invent_missing_historical_savings(self) -> None:
        for case, missing in enumerate((None, pricing(None, 2.0), pricing(10.0, None))):
            # xdist transports subtest metadata; use a scalar case label rather
            # than a pricing object that execnet cannot serialize.
            with self.subTest(pricing_case=case):
                scheduler = CacheAwareScheduler(pricing=missing)
                scheduler.observe_usage(cache_read_tokens=1000)
                scheduler.set_pricing(pricing())
                scheduler.observe_usage(cache_read_tokens=1000)
                self.assertIsNone(scheduler.estimated_reuse_saving_usd())

    def test_unknown_reads_do_not_produce_a_partial_saving(self) -> None:
        scheduler = CacheAwareScheduler(pricing=pricing())
        scheduler.observe_usage(cache_read_tokens=1000)
        scheduler.set_pricing(None)
        # Missing current rates hide estimates without repricing past reads.
        self.assertIsNone(scheduler.estimated_reuse_saving_usd())
        scheduler.set_pricing(pricing(20.0, 5.0))
        self.assertAlmostEqual(scheduler.estimated_reuse_saving_usd(), 0.008)
        scheduler.set_pricing(None)
        scheduler.observe_usage(cache_read_tokens=1)
        scheduler.set_pricing(pricing())
        self.assertIsNone(scheduler.estimated_reuse_saving_usd())

    def test_decision_counters_and_current_price_estimates(self) -> None:
        scheduler = CacheAwareScheduler(pricing=pricing())
        one = SimpleNamespace(key="one", prefix_hash="aaa", token_estimate=1000)
        two = SimpleNamespace(key="two", prefix_hash="bbb", token_estimate=2000)
        first = scheduler.decide(prefix_key=one, previous_key=None)
        reuse = scheduler.decide(prefix_key=one, previous_key=one)
        write = scheduler.decide(prefix_key=two, previous_key=one, dynamic_chars=42)
        self.assertEqual(first.verdict, CacheDecision.VERDICT_FIRST)
        self.assertEqual(reuse.verdict, CacheDecision.VERDICT_REUSE)
        self.assertEqual(write.verdict, CacheDecision.VERDICT_WRITE)
        self.assertTrue(write.prefix_changed)
        self.assertTrue(write.advertise_cache_key)
        self.assertEqual(write.dynamic_chars, 42)
        self.assertAlmostEqual(first.estimated_write_usd, 0.012)
        self.assertAlmostEqual(write.estimated_saving_per_reuse_usd, 0.016)
        self.assertEqual((scheduler.decisions, scheduler.reuse_decisions, scheduler.invalidations), (3, 1, 1))
        scheduler.set_pricing(None)
        unknown = scheduler.decide(prefix_key=one, previous_key=one)
        self.assertIsNone(unknown.estimated_write_usd)
        self.assertIsNone(unknown.estimated_saving_per_reuse_usd)
        self.assertIsNone(scheduler.estimated_reuse_saving_usd())

    def test_zero_and_negative_savings_are_not_hidden(self) -> None:
        for input_rate, cached_rate, expected in ((2, 2, 0.0), (1, 2, -1.0)):
            with self.subTest(input_rate=input_rate, cached_rate=cached_rate):
                scheduler = CacheAwareScheduler(pricing=pricing(input_rate, cached_rate))
                scheduler.observe_usage(cache_read_tokens=1_000_000)
                self.assertEqual(scheduler.estimated_reuse_saving_usd(), expected)


if __name__ == "__main__":
    unittest.main()
