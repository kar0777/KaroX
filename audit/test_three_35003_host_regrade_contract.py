"""Non-scored, model-free acceptance of Windows/Linux 3D adjudication rules.

The public issue grader is frozen and remains outside each agent workspace.
These tests protect the external scoring/provenance layer, not the task grader.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def _module():
    path = HERE / "windows_regrade_three_35003.py"
    spec = importlib.util.spec_from_file_location("three35003_host_regrader", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _raw(status="CLEAN_USER_SUCCESS", valid=True, patch_bytes=432, completed=True):
    return {
        "task_id": "real-three-orbitcontrols-35003-v2",
        "harness_config_untampered": True,
        "agent_timed_out": False,
        "patch_bytes": patch_bytes,
        "quality_classification": {
            "status": status,
            "validity": valid,
            "cli_completed": completed,
        },
    }


def test_hash_tree_matches_actual_wsl_pathlib_order_on_mixed_case_fixture():
    regrader = _module()
    expected = json.loads(
        (HERE / "LINUX_ORIGINAL_TREE_HASHES_20261108.json").read_text("utf-8")
    )["tree_sha256"]
    for relative, reference in expected.items():
        assert regrader.hash_tree(ROOT / relative) == reference, relative


def test_clean_pass_requires_nonempty_patch_and_completed_cli():
    fn = _module().classify_agent_run
    passed = fn(_raw(), {"grade": "PASS"})
    assert passed["artifact_correct"] is True
    assert passed["clean_user_success"] is True

    no_patch = fn(_raw(patch_bytes=0), {"grade": "PASS"})
    assert no_patch["artifact_correct"] is False
    assert no_patch["clean_user_success"] is False

    unfinished = fn(_raw(completed=False), {"grade": "PASS"})
    assert unfinished["artifact_correct"] is True
    assert unfinished["clean_user_success"] is False


def test_invalid_original_evidence_cannot_become_clean_success():
    raw = _raw(status="INVALID", valid=False)
    verdict = _module().classify_agent_run(raw, {"grade": "PASS"})
    assert verdict["clean_user_success"] is False
    assert verdict["source_evidence_valid"] is False


def test_infrastructure_block_remains_block_even_after_external_visual_pass():
    raw = _raw(status="INFRA_SETUP_BLOCKED", valid=False)
    verdict = _module().classify_agent_run(raw, {"grade": "PASS"})
    assert verdict["agent_infra_blocked"] is True
    assert verdict["clean_user_success"] is False


def test_failed_or_unavailable_browser_grade_is_not_success():
    fn = _module().classify_agent_run
    for grade in ("FAIL", "ENV_UNAVAILABLE", "INVALID", "SKIP"):
        verdict = fn(_raw(), {"grade": grade})
        assert verdict["artifact_correct"] is False
        assert verdict["clean_user_success"] is False


def test_model_route_must_match_frozen_provider_and_deepseek_model():
    import pytest

    validate = _module().verify_model_route
    row = {"model": "deepseek-ai/DeepSeek-V4.1-Flash",
           "provider_base_url": "https://api.inference.wandb.ai/v1"}
    assert validate(dict(row)) is None
    for mutation in ({"model": "different-model"},
                     {"provider_base_url": "https://different.provider/v1"},
                     {"model": None}):
        forged = dict(row)
        forged.update(mutation)
        with pytest.raises(RuntimeError, match="model/provider"):
            validate(forged)
