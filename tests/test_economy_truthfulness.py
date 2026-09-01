from __future__ import annotations

from karox.economy_engine import (
    ESTIMATED,
    MEASURED,
    UNAVAILABLE,
    EconomyCounters,
    build_savings_receipt,
)
from karox.usage_analytics import UsageSummary


def _usage() -> UsageSummary:
    return UsageSummary(
        requests=1,
        prompt_tokens=100,
        completion_tokens=20,
        total_tokens=120,
        cache_read_tokens=80,
        costs={"USD": 2.0},
        cache_savings={"USD": 0.5},
    )


def test_local_pricing_and_cache_money_default_to_estimated() -> None:
    receipt = build_savings_receipt(
        usage=_usage(),
        counters=EconomyCounters(context_chars_sent=400),
        measured_baseline_cost=8.0,
        accepted=True,
        verified=True,
    )

    assert receipt.actual_cost.evidence == ESTIMATED
    assert receipt.cache_savings.evidence == ESTIMATED
    assert receipt.baseline_cost.evidence == MEASURED
    assert receipt.savings.evidence == ESTIMATED
    assert receipt.savings.amount == 6.0
    assert receipt.savings_percent == 75.0


def test_measured_saving_requires_explicit_measured_actual() -> None:
    receipt = build_savings_receipt(
        usage=_usage(),
        measured_baseline_cost=8.0,
        actual_cost_evidence=MEASURED,
        cache_savings_evidence=MEASURED,
        accepted=True,
        verified=True,
    )

    assert receipt.actual_cost.evidence == MEASURED
    assert receipt.cache_savings.evidence == MEASURED
    assert receipt.savings.evidence == MEASURED
    assert receipt.savings_percent == 75.0


def test_missing_baseline_never_creates_dollar_saving() -> None:
    receipt = build_savings_receipt(
        usage=_usage(),
        accepted=True,
        verified=True,
    )

    assert receipt.baseline_cost.amount is None
    assert receipt.baseline_cost.evidence == UNAVAILABLE
    assert receipt.savings.amount is None
    assert receipt.savings.evidence == UNAVAILABLE
    assert receipt.savings_percent is None


def test_render_does_not_claim_uninstrumented_avoidance_counters() -> None:
    receipt = build_savings_receipt(
        usage=_usage(),
        counters=EconomyCounters(
            context_chars_reused=100,
            context_chars_sent=400,
            tool_schema_bytes_avoided=0,
            round_trips_avoided=0,
            premium_calls_avoided=0,
        ),
        accepted=True,
        verified=True,
    )
    text = "\n".join(receipt.render())

    assert "context reused locally: 100 chars" in text
    assert "context sent: 400 chars" in text
    assert "premium calls avoided" not in text
    assert "round trips avoided" not in text
    assert "tool schema bytes avoided" not in text
