"""Offline HARD20 accounting / fail-closed gates; zero provider requests."""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from hard20_preflight import cost, readiness, reservation_allowed  # noqa: E402


def test_no_live_dispatch_without_hard_spend_cap():
    report = readiness()
    assert report["status"] == "BLOCKED_NO_PAID_CALLS"
    assert report["paid_calls_authorized"] is False
    assert "PROVIDER_HARD_CAP_NOT_PROVEN" in report["unique_blockers"]
    assert "NO_ENFORCING_EGRESS_GATEWAY_IN_THIS_PROGRAM" in report["unique_blockers"]


def test_no_hard20_run_count_fabrication():
    report = readiness()
    assert report["task_candidates"] == 20
    assert report["task_frozen"] == 0
    assert report["task_locally_red_green_ready"] == 1
    assert report["pilot_paid_attempts"] == 0


@pytest.mark.parametrize(
    "pilot,already_spent,already_piloted,next_request,allowed",
    [
        (True, "0", "0", "3", True),
        (True, "0", "0", "3.000001", False),
        (True, "2.50", "2.5", "0.50", True),
        (True, "2.50", "2.5", "0.50001", False),
        (False, "29.9", "3", "0.1", True),
        (False, "29.9", "3", "0.101", False),
        (False, "0", "0", "0", False),
        (False, "31", "0", "0.001", False),
        (False, "0", "3.1", "0.001", False),
    ],
)
def test_arithmetic_reservations_never_exceed_pilot_or_global(
    pilot, already_spent, already_piloted, next_request, allowed
):
    assert reservation_allowed(
        pilot=pilot,
        committed_total=already_spent,
        committed_pilot=already_piloted,
        additional_reservation=next_request,
    ) is allowed


@pytest.mark.parametrize("bad_value", [None, True, "-1", "NaN", "Infinity", 0.0001])
def test_accounting_rejects_invalid_unverifiable_values(bad_value):
    with pytest.raises(ValueError):
        cost(bad_value)


def test_decimal_not_float_accounting():
    assert cost("0.1") + cost("0.2") == Decimal("0.3")


def test_fail_closed_on_tampered_protocol_copy(tmp_path: Path):
    # Only modify a disposable temp copy of the protocol/manifest.
    import shutil

    root = HERE.parent
    dest = tmp_path / "hard20-isolated-test"
    (dest / "docs/benchmarks").mkdir(parents=True)
    shutil.copy2(
        root / "docs/benchmarks/HARD20_KAROX_TRIAD_PROTOCOL_20261108.md",
        dest / "docs/benchmarks/HARD20_KAROX_TRIAD_PROTOCOL_20261108.md"
    )
    for filename in (
        "HARD20_TASK_MANIFEST.json", "HARD20_BUDGET_POLICY.json",
        "HARD20_RESOURCE_POLICY.json", "HARD20_TELEMETRY_SCHEMA.json",
        "HARD20_ORDER_SCHEDULE.json",
    ):
        shutil.copy2(root / filename, dest / filename)
    path = dest / "docs/benchmarks/HARD20_KAROX_TRIAD_PROTOCOL_20261108.md"
    path.write_text(path.read_text(encoding="utf-8") + "\nmodified\n", encoding="utf-8")
    report = readiness(dest)
    assert "PROTOCOL_SHA_CHANGED" in report["unique_blockers"]
    assert report["paid_calls_authorized"] is False


def test_telemetry_schema_requires_reasoning_provenance():
    import json

    root = HERE.parent
    schema = json.loads(
        (root / "HARD20_TELEMETRY_SCHEMA.json").read_text(encoding="utf-8")
    )
    assert schema["principles"]["no_hidden_reasoning_reconstruction"] is True
    assert "provider_returned_reasoning" in schema["event_types"]["reasoning"]["keys"]
