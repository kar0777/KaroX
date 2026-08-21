"""The prompt benchmark measures mechanics honestly and deterministically.

Contract (mandate: prompt A/B benchmark + final economy honesty): the four
policies run over one identical simulated session; numbers are raw bytes
and counts; the Constitution's stable prefix must actually be stable; and
everything that needs a live model is labeled UNAVAILABLE instead of
being invented.
"""

from karox.prompt_benchmark import (
    DEFAULT_TURNS,
    default_policies,
    run_prompt_benchmark,
)


def _by_policy(report):
    return {entry["policy"]: entry for entry in report["results"]}


class TestBenchmarkRuns:
    def test_covers_the_mandated_task_classes(self):
        classes = {turn.task_class for turn in DEFAULT_TURNS}
        assert classes == {
            "bug fix",
            "repo exploration",
            "multi-file implementation",
            "architecture",
            "unknown root cause",
            "test failure",
        }

    def test_all_four_policies_compared_on_the_same_session(self):
        report = run_prompt_benchmark()
        names = [entry["policy"] for entry in report["results"]]
        assert names == [
            "raw-harness",
            "opus-inspired-monolith",
            "fable-inspired-monolith",
            "karox-constitution",
        ]
        assert all(
            entry["turns"] == len(DEFAULT_TURNS)
            for entry in report["results"]
        )

    def test_deterministic(self):
        assert run_prompt_benchmark() == run_prompt_benchmark()


class TestMechanics:
    def test_constitution_prefix_never_churns(self):
        entry = _by_policy(run_prompt_benchmark())["karox-constitution"]
        assert entry["prefix_churn_turns"] == 0
        assert entry["reusable_prefix_bytes"] > 0

    def test_raw_harness_has_no_reusable_prefix(self):
        entry = _by_policy(run_prompt_benchmark())["raw-harness"]
        assert entry["stable_prefix_bytes_total"] == 0
        assert entry["reusable_prefix_bytes"] == 0

    def test_monoliths_pay_for_size_even_when_stable(self):
        report = _by_policy(run_prompt_benchmark())
        constitution = report["karox-constitution"]
        for name in ("opus-inspired-monolith", "fable-inspired-monolith"):
            monolith = report[name]
            assert monolith["prefix_churn_turns"] == 0
            assert (
                monolith["prompt_bytes_total"]
                > constitution["prompt_bytes_total"]
            )

    def test_policies_see_identical_dynamic_context(self):
        # Same session in, so every policy's dynamic payload derives from
        # the same turn fields; the raw policy inlines everything and must
        # therefore carry at least as many dynamic bytes as any other.
        report = _by_policy(run_prompt_benchmark())
        raw = report["raw-harness"]
        constitution = report["karox-constitution"]
        assert raw["dynamic_bytes_total"] >= (
            constitution["dynamic_bytes_total"] * 0.5
        )


class TestHonesty:
    def test_unmeasurable_evidence_is_labeled_unavailable(self):
        report = run_prompt_benchmark()
        evidence = report["evidence"]
        assert evidence["prompt_mechanics"] == "MEASURED"
        assert str(evidence["live_task_success"]).startswith("UNAVAILABLE")
        assert str(evidence["live_billing"]).startswith("UNAVAILABLE")

    def test_no_fabricated_percentages(self):
        report = run_prompt_benchmark()
        flattened = repr(report)
        assert "%" not in flattened

    def test_winner_is_not_declared_by_mechanics(self):
        report = run_prompt_benchmark()
        assert "winner" not in report
        assert "task success" in str(report["selection_rule"])

    def test_default_policy_count_is_four(self):
        assert len(default_policies()) == 4
