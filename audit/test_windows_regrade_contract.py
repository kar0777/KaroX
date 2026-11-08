"""Offline unit cases for the pre-registered external Three.js outcome scorer.

No models, real credentials, fake competitor runs, browser or network calls.
"""
from __future__ import annotations

from copy import deepcopy

from windows_regrade_three_35003 import classify_agent_run


def original() -> dict:
    return {
        "quality_classification": {"cli_completed": True, "status": "SOLVED"},
        "harness_config_untampered": True,
        "agent_timed_out": False,
        "patch_bytes": 128,
    }


def test_real_external_pass_and_clean_cli_is_clean_success() -> None:
    score = classify_agent_run(original(), {"grade": "PASS"})
    assert score["artifact_correct"] and score["clean_user_success"]


def test_empty_patch_cannot_solve_a_real_bug_despite_green_grader() -> None:
    data = original()
    data["patch_bytes"] = 0
    score = classify_agent_run(data, {"grade": "PASS"})
    assert not score["patch_nonempty"]
    assert not score["artifact_correct"]
    assert not score["clean_user_success"]


def test_missing_patch_metadata_cannot_be_assumed_nonempty() -> None:
    data = original()
    del data["patch_bytes"]
    score = classify_agent_run(data, {"grade": "PASS"})
    assert not score["artifact_correct"]


def test_cli_incomplete_is_not_clean_even_when_real_artifact_passes() -> None:
    data = original()
    data["quality_classification"]["cli_completed"] = False
    score = classify_agent_run(data, {"grade": "PASS"})
    assert score["artifact_correct"] and not score["clean_user_success"]


def test_infrastructure_block_is_not_a_quality_loss() -> None:
    data = original()
    data["quality_classification"]["status"] = "INFRA_SETUP_BLOCKED"
    score = classify_agent_run(data, {"grade": "PASS"})
    assert score["artifact_correct"]
    assert score["agent_infra_blocked"]
    assert not score["clean_user_success"]


def test_modified_harness_config_disqualifies_clean_success() -> None:
    data = original()
    data["harness_config_untampered"] = False
    score = classify_agent_run(data, {"grade": "PASS"})
    assert score["artifact_correct"] and not score["clean_user_success"]


def test_independent_failure_is_never_overridden_by_self_verified_flag() -> None:
    data = original()
    data["quality_classification"]["self_verified"] = True
    score = classify_agent_run(data, {"grade": "FAIL"})
    assert not score["artifact_correct"]
    assert not score["clean_user_success"]


def test_missing_grade_is_not_success_and_data_not_mutated() -> None:
    data = original()
    original_data = deepcopy(data)
    score = classify_agent_run(data, {"grade": "ENV_UNAVAILABLE"})
    assert not score["artifact_correct"]
    assert not score["clean_user_success"]
    assert data == original_data
