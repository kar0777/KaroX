"""HARD20 observe-only financial policy and safety gate tests; zero API calls."""
from __future__ import annotations

import json
import shutil
import sys
from decimal import Decimal
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from hard20_preflight import cost, estimated_usd, readiness  # noqa: E402


def test_user_authorized_financial_mode_is_observe_only():
    result = readiness()
    assert result["financial_mode"] == "OBSERVE_ONLY_NO_HARD_CAP"
    assert result["paid_calls_user_authorized"] is True
    assert result["old_3_30_caps_enforced"] is False
    assert result["no_api_requests_dispatched_by_this_preflight"] is True
    assert result["status"] == "READY_LOCAL_ONLY", result["blockers"]


def test_frozen_first_task_only_not_all_twenty():
    result = readiness()
    assert result["task_candidates"] == 20
    assert result["task_frozen_for_this_run"] is True
    assert not result["blockers"]


def test_unknown_actual_invoice_stays_null():
    assert readiness()["actual_invoiced_usd"] is None


def test_bill_estimate_exact_public_rate():
    assert estimated_usd(input_uncached=37, input_cached=0, output=33) == Decimal("0.00002885")


@pytest.mark.parametrize("uncached,cached,output,expected", [
    (1000000, 0, 0, "0.20"),
    (0, 1000000, 0, "0.03"),
    (0, 0, 1000000, "0.65"),
    (1000000, 1000000, 1000000, "0.88"),
    (0, 0, 0, "0"),
])
def test_rates(uncached, cached, output, expected):
    assert estimated_usd(input_uncached=uncached, input_cached=cached, output=output) == Decimal(expected)


@pytest.mark.parametrize("bad", [None, True, "-1", "NaN", "Infinity", 0.0001, "-Infinity"])
def test_cost_bad_values(bad):
    with pytest.raises(ValueError):
        cost(bad)


def test_decimal_not_float():
    assert cost("0.1") + cost("0.2") == Decimal("0.3")


def test_missing_individual_freeze_blocks():
    assert "TASK_NOT_INDIVIDUALLY_FROZEN" in readiness(task_id="task-not-frozen")["blockers"]


def test_tampered_provenance_fails_without_any_api(tmp_path):
    root = HERE.parent
    dest = tmp_path / "hard20-frozen-copy"
    (dest / "docs/benchmarks").mkdir(parents=True)
    for path in [
        "HARD20_BUDGET_POLICY.json",
        "HARD20_RESOURCE_POLICY.json",
        "HARD20_TASK_MANIFEST.json",
        "HARD20_TASK_FREEZE_35003_v2.json",
        "docs/benchmarks/HARD20_SOL_EXECUTE_MAX_20261108.md",
        "docs/benchmarks/HARD20_KAROX_TRIAD_PROTOCOL_20261108.md",
        "tasks/real-three-orbitcontrols-35003-v2/task.json",
        "tasks/real-three-orbitcontrols-35003-v2/grader/test_browser.py",
    ]:
        file = dest / path
        file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / path, file)
    authority = dest / "docs/benchmarks/HARD20_SOL_EXECUTE_MAX_20261108.md"
    authority.write_text(authority.read_text(encoding="utf8") + "\nTAMPERED\n", encoding="utf8")
    result = readiness(dest)
    assert "NEW_AUTHORITY_SHA_CHANGED" in result["blockers"]
    assert result["status"] == "BLOCKED_NONFINANCIAL_PRECHECK"


def test_schema_requires_visible_reasoning_provenance():
    schema = json.loads((HERE.parent / "HARD20_TELEMETRY_SCHEMA.json").read_text(encoding="utf8"))
    assert schema["principles"]["no_hidden_reasoning_reconstruction"] is True
    assert "provider_returned_reasoning" in schema["event_types"]["reasoning"]["keys"]


def test_no_credential_values_in_budget_policy():
    p = json.loads((HERE.parent / "HARD20_BUDGET_POLICY.json").read_text(encoding="utf8"))
    assert p["no_anthropic"] and p["no_user_projects"] and p["no_github_push"]
    assert p["token_budget_cap"] is None and p["step_budget_cap"] is None
    assert p["api_request_budget_cap"] is None
