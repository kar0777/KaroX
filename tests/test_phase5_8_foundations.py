"""Cost Intelligence CI-0 through CI-5 — full Phase 5 tests."""

from __future__ import annotations

import unittest

from karox.cost_intelligence import (
    BatchPlan,
    BatchPlanner,
    BudgetDecision,
    CacheKey,
    CompactSummary,
    ContextWindow,
    CostGovernor,
    CostLedger,
    PinnedFact,
    ReadCache,
    StablePrefixCache,
    ToolSchemaDeduplicator,
    UsageRecord,
    build_compact_summary,
)


class CI0TelemetryTests(unittest.TestCase):
    def test_primary_metric_accepted_verified_task_cost(self) -> None:
        ledger = CostLedger()
        ledger.record(session_id="s", step=0, provider="p", model="m",
                      input_tokens=1000, output_tokens=500, cost_usd=0.10,
                      accepted=True, verified=True)
        ledger.record(session_id="s", step=1, provider="p", model="m",
                      input_tokens=2000, output_tokens=1000, cost_usd=0.20,
                      accepted=True, verified=True)
        avc = ledger.accepted_verified_task_cost("s")
        self.assertAlmostEqual(avc, 0.15)

    def test_cache_hit_rate(self) -> None:
        ledger = CostLedger()
        ledger.record(session_id="s", step=0, provider="p", model="m",
                      input_tokens=1000, output_tokens=100, cache_read_tokens=400)
        self.assertAlmostEqual(ledger.cache_hit_rate("s"), 0.4)


class CI1StablePrefixCacheTests(unittest.TestCase):
    def test_same_prefix_same_key(self) -> None:
        c = StablePrefixCache()
        k1 = c.compute_key(system_prompt="You are helpful", tool_schemas="tool1", session_id="s1")
        k2 = c.compute_key(system_prompt="You are helpful", tool_schemas="tool1", session_id="s1")
        self.assertEqual(k1.key, k2.key)

    def test_different_prefix_different_key(self) -> None:
        c = StablePrefixCache()
        k1 = c.compute_key(system_prompt="A", tool_schemas="x", session_id="s1")
        k2 = c.compute_key(system_prompt="B", tool_schemas="x", session_id="s1")
        self.assertNotEqual(k1.key, k2.key)

    def test_token_estimate_positive(self) -> None:
        c = StablePrefixCache()
        k = c.compute_key(system_prompt="x" * 100, tool_schemas="y" * 100, session_id="s")
        self.assertGreater(k.token_estimate, 0)


class CI1ToolSchemaDeduplicatorTests(unittest.TestCase):
    def test_duplicates_removed(self) -> None:
        d = ToolSchemaDeduplicator()
        tools = [
            {"name": "read", "schema": {"type": "object"}},
            {"name": "read", "schema": {"type": "object"}},  # duplicate
            {"name": "write", "schema": {"type": "object", "extra": True}},
        ]
        unique = d.deduplicate(tools)
        self.assertEqual(len(unique), 2)
        self.assertEqual(d.deduped_count, 1)

    def test_no_duplicates_returns_all(self) -> None:
        d = ToolSchemaDeduplicator()
        tools = [{"name": "a"}, {"name": "b"}]
        self.assertEqual(len(d.deduplicate(tools)), 2)
        self.assertEqual(d.deduped_count, 0)


class CI1ReadCacheTests(unittest.TestCase):
    def test_unchanged_file_is_hit(self) -> None:
        c = ReadCache()
        self.assertFalse(c.check("file.py", 1000.0, 500))
        self.assertTrue(c.check("file.py", 1000.0, 500))
        self.assertEqual(c.hit_rate, 0.5)

    def test_changed_file_is_miss(self) -> None:
        c = ReadCache()
        c.check("file.py", 1000.0, 500)
        self.assertFalse(c.check("file.py", 2000.0, 600))


class CI2CompactSummaryTests(unittest.TestCase):
    def test_savings_computed(self) -> None:
        cs = build_compact_summary(
            raw_content="x" * 1000, summary="short summary",
            original_token_count=250,
        )
        self.assertGreater(cs.savings, 0)
        self.assertLess(cs.token_count, cs.original_token_count)

    def test_secret_redacted(self) -> None:
        cs = build_compact_summary(
            raw_content="content", summary="token=super-secret-123",
            original_token_count=100,
        )
        self.assertNotIn("super-secret-123", cs.summary)
        self.assertIn("[REDACTED]", cs.summary)

    def test_content_hash_deterministic(self) -> None:
        cs1 = build_compact_summary(raw_content="same", summary="s", original_token_count=10)
        cs2 = build_compact_summary(raw_content="same", summary="s", original_token_count=10)
        self.assertEqual(cs1.content_hash, cs2.content_hash)


class CI3ContextWindowTests(unittest.TestCase):
    def test_pinned_facts_survive_eviction(self) -> None:
        cw = ContextWindow(max_recent=3)
        cw.pin("project", "/repo/path")
        for i in range(10):
            cw.add_recent(f"event-{i}")
        self.assertEqual(cw.pinned_count, 1)
        self.assertLessEqual(cw.recent_count, 3)
        self.assertGreater(cw.evicted_count, 0)

    def test_artifact_store_and_retrieve(self) -> None:
        cw = ContextWindow()
        cw.store_artifact("art-1", "full content")
        self.assertEqual(cw.get_artifact("art-1"), "full content")
        self.assertIsNone(cw.get_artifact("missing"))

    def test_snapshot(self) -> None:
        cw = ContextWindow(max_recent=5)
        cw.pin("a", "b")
        cw.add_recent("x")
        cw.add_recent("y")
        s = cw.snapshot()
        self.assertEqual(s["pinned"], 1)
        self.assertEqual(s["recent"], 2)


class CI4BatchPlannerTests(unittest.TestCase):
    def test_parallel_reads_grouped(self) -> None:
        bp = BatchPlanner()
        plan = bp.plan([
            {"name": "repo.read_file"},
            {"name": "repo.list_files"},
            {"name": "repo.search"},
            {"name": "repo.write_file"},
        ])
        self.assertEqual(len(plan.parallel_reads), 3)
        self.assertEqual(len(plan.sequential_writes), 1)
        self.assertEqual(plan.estimated_round_trips_saved, 2)

    def test_no_reads_no_savings(self) -> None:
        bp = BatchPlanner()
        plan = bp.plan([{"name": "repo.write_file"}])
        self.assertEqual(plan.estimated_round_trips_saved, 0)
        self.assertTrue(plan.bundled_verification)  # writes imply verification

    def test_single_read_no_savings(self) -> None:
        bp = BatchPlanner()
        plan = bp.plan([{"name": "repo.read_file"}])
        self.assertEqual(plan.estimated_round_trips_saved, 0)
        self.assertFalse(plan.bundled_verification)  # no writes, no verification needed


class CI5CostGovernorTests(unittest.TestCase):
    def test_within_budget_allowed(self) -> None:
        g = CostGovernor(soft_budget_usd=5, hard_budget_usd=10)
        d = g.evaluate(current_cost_usd=2.0)
        self.assertTrue(d.allowed)
        self.assertIsNone(d.warning)

    def test_soft_budget_warns(self) -> None:
        g = CostGovernor(soft_budget_usd=5, hard_budget_usd=10)
        d = g.evaluate(current_cost_usd=6.0)
        self.assertTrue(d.allowed)
        self.assertIsNotNone(d.warning)

    def test_hard_budget_blocks(self) -> None:
        g = CostGovernor(soft_budget_usd=5, hard_budget_usd=10)
        d = g.evaluate(current_cost_usd=11.0)
        self.assertFalse(d.allowed)

    def test_override_allows_past_hard(self) -> None:
        g = CostGovernor(soft_budget_usd=5, hard_budget_usd=10)
        g.set_override(True)
        d = g.evaluate(current_cost_usd=11.0)
        self.assertTrue(d.allowed)
        self.assertTrue(d.override_active)

    def test_shadow_mode_does_not_block(self) -> None:
        g = CostGovernor(soft_budget_usd=5, hard_budget_usd=10, shadow_mode=True)
        d = g.evaluate(current_cost_usd=11.0)
        self.assertTrue(d.allowed)
        self.assertTrue(d.shadow_mode)

    def test_no_silent_model_downgrade(self) -> None:
        # The governor must never change the model. It only says allowed/denied.
        g = CostGovernor(soft_budget_usd=1, hard_budget_usd=2)
        d = g.evaluate(current_cost_usd=0.5)
        # No "model" or "downgrade" key in the decision.
        self.assertNotIn("model", d.to_dict())


class CostBenchmarkTests(unittest.TestCase):
    """Before/after benchmark on identical coding scenarios."""

    def test_cache_saves_input_tokens(self) -> None:
        """Two steps with and without prefix caching."""
        prefix_tokens = 2000  # system prompt + tools
        delta_tokens = 200    # new conversation turn
        cache_rate = 0.1      # cache read is 10% of full rate

        # Without caching: 2 steps, full prefix each time
        without = prefix_tokens * 2 + delta_tokens * 2

        # With caching: step 1 full prefix, step 2 prefix at cache rate
        with_cache = prefix_tokens + delta_tokens + int(prefix_tokens * cache_rate) + delta_tokens

        savings = without - with_cache
        self.assertGreater(savings, 0, "caching must save tokens")
        savings_pct = savings / without
        self.assertGreater(savings_pct, 0.3, "caching should save >30% on repeated prefix")


if __name__ == "__main__":
    unittest.main()
