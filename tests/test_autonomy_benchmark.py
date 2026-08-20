from __future__ import annotations

from _support import SRC  # noqa: F401
from karox.autonomy_benchmark import run_autonomy_benchmark


def test_deterministic_autonomy_benchmark_is_honestly_separated() -> None:
    result = run_autonomy_benchmark()
    assert result["server_side"]["status"] == "completed"
    assert result["server_side"]["tasks_executed"] == 10
    simulated = result["simulated_client"]
    assert simulated["status"] == "completed"
    assert "not a model benchmark" in simulated["honesty_note"]
    assert len(simulated["baseline"]["tasks"]) == 10
    assert len(simulated["optimized"]["tasks"]) == 10
    assert simulated["comparison"]["all_targets_met"] is True, {
        "targets": simulated["comparison"]["targets"],
        "optimized_failure_ids": [
            item["task_id"]
            for item in simulated["optimized"]["tasks"]
            if not item["success"]
        ],
    }
    assert result["paired_chatgpt_web"]["status"] == "not_run_user_gate"
    assert result["fixture_policy"]["main_working_tree_mutated"] is False
    assert result["fixture_policy"]["external_model_used"] is False
