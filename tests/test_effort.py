"""The effort ladder is a runtime contract, not a prompt string.

These tests pin the three product guarantees from the v5 mandate:
levels resolve predictably (sections 20/23), every budget dimension is
monotonically non-decreasing up the ladder (section 22), and the provider
mapping stays inside the provider vocabulary (section 21).
"""

import dataclasses

import pytest

from karox.agent import ContextBudget
from karox.effort import (
    AUTO_EFFORT,
    DEFAULT_EFFORT,
    EFFORT_LEVELS,
    EffortBudget,
    TaskSignals,
    budget_for,
    effort_summary,
    normalize_effort,
    recommend_effort,
)
from karox.providers import REASONING_EFFORTS


def test_levels_are_ordered_and_complete():
    assert EFFORT_LEVELS == ("low", "medium", "high", "extra-high", "ultra")
    assert DEFAULT_EFFORT in EFFORT_LEVELS
    assert AUTO_EFFORT not in EFFORT_LEVELS


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("low", "low"),
        ("  MEDIUM ", "medium"),
        ("normal", "medium"),
        ("default", "medium"),
        ("high", "high"),
        ("xhigh", "extra-high"),
        ("extra_high", "extra-high"),
        ("extra-high", "extra-high"),
        ("Extra High", "extra-high"),
        ("max", "ultra"),
        ("ultra", "ultra"),
        ("auto", AUTO_EFFORT),
    ],
)
def test_normalize_accepts_real_spellings(typed, expected):
    assert normalize_effort(typed) == expected


@pytest.mark.parametrize("bad", ["", "turbo", "9000", None, 3, "medium-rare"])
def test_normalize_rejects_unknown_with_full_menu(bad):
    with pytest.raises(ValueError) as excinfo:
        normalize_effort(bad)
    message = str(excinfo.value)
    assert "auto" in message
    for level in EFFORT_LEVELS:
        assert level in message


def test_every_level_has_a_budget_and_valid_provider_mapping():
    for level in EFFORT_LEVELS:
        budget = budget_for(level)
        assert isinstance(budget, EffortBudget)
        assert budget.level == level
        assert budget.reasoning_effort in REASONING_EFFORTS


def test_budgets_grow_monotonically_up_the_ladder():
    """No dimension may quietly shrink as the user pays for more effort."""

    numeric_fields = (
        "investigation_breadth",
        "hypothesis_budget",
        "verification_rung",
        "git_history_depth",
        "allowed_subagents",
        "research_calls",
    )
    breadth_rank = {"targeted": 0, "affected": 1, "full": 2}
    previous = None
    for level in EFFORT_LEVELS:
        budget = budget_for(level)
        if previous is not None:
            for field in numeric_fields:
                assert getattr(budget, field) >= getattr(previous, field), (
                    f"{field} shrank from {previous.level} to {budget.level}"
                )
            assert budget.agent_limits.max_steps >= previous.agent_limits.max_steps
            assert budget.agent_limits.max_seconds >= previous.agent_limits.max_seconds
            assert budget.context.utilization >= previous.context.utilization
            assert (
                budget.context.keep_recent_groups
                >= previous.context.keep_recent_groups
            )
            assert (
                budget.context.max_tool_result_chars
                >= previous.context.max_tool_result_chars
            )
            assert (
                breadth_rank[budget.test_breadth]
                >= breadth_rank[previous.test_breadth]
            )
        previous = budget


def test_effort_is_not_just_a_prompt_string():
    """LOW and ULTRA must differ in every concrete runtime dimension."""

    low = budget_for("low")
    ultra = budget_for("ultra")
    assert ultra.agent_limits.max_steps > low.agent_limits.max_steps
    assert ultra.agent_limits.max_seconds > low.agent_limits.max_seconds
    assert ultra.context.max_tool_result_chars > low.context.max_tool_result_chars
    assert ultra.investigation_breadth > low.investigation_breadth
    assert ultra.hypothesis_budget > low.hypothesis_budget
    assert ultra.verification_rung > low.verification_rung
    assert ultra.git_history_depth > low.git_history_depth
    assert ultra.allowed_subagents > low.allowed_subagents
    assert ultra.research_calls > low.research_calls
    assert ultra.test_breadth == "full" and low.test_breadth == "targeted"


def test_apply_to_context_preserves_model_window():
    base = ContextBudget(max_input_tokens=200_000)
    high = budget_for("high")
    merged = high.apply_to_context(base)
    assert merged.max_input_tokens == 200_000
    assert merged.utilization == high.context.utilization
    assert merged.keep_recent_groups == high.context.keep_recent_groups
    assert merged.max_tool_result_chars == high.context.max_tool_result_chars
    # The base object is frozen and must not be mutated in place.
    assert base.utilization != merged.utilization or base is not merged


def test_auto_without_signals_recommends_default_with_reason():
    recommendation = recommend_effort(None)
    assert recommendation.level == DEFAULT_EFFORT
    assert recommendation.reasons
    assert budget_for("auto") is budget_for(DEFAULT_EFFORT)


def test_auto_escalates_on_risky_migration_signals():
    signals = TaskSignals(
        ambiguity=True,
        files_likely_affected=25,
        dependency_breadth=12,
        risk_area=True,
        migration_involved=True,
        regression_history=True,
    )
    recommendation = recommend_effort(signals)
    assert recommendation.level == "ultra"
    joined = " ".join(recommendation.reasons)
    assert "high-risk" in joined
    assert "migration" in joined
    assert recommendation.budget.test_breadth == "full"


def test_auto_stays_cheap_for_trivial_tasks():
    recommendation = recommend_effort(TaskSignals())
    assert recommendation.level == "low"
    assert any("targeted" in reason for reason in recommendation.reasons)


def test_auto_medium_for_small_local_change():
    recommendation = recommend_effort(TaskSignals(files_likely_affected=2))
    assert recommendation.level == "medium"


def test_budget_tables_are_frozen():
    budget = budget_for("medium")
    with pytest.raises(dataclasses.FrozenInstanceError):
        budget.level = "high"  # type: ignore[misc]


def test_summary_is_honest_in_both_languages():
    for language in ("en", "ru"):
        line = effort_summary("high", language)
        assert "48" in line  # max steps
        assert "1800" in line  # max seconds
        assert "4/5" in line  # verification rung
