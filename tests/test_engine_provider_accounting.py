"""Engine assistant messages must carry the measured cost back to the session.

``_persist_engine_assistant`` rebuilds a ``ModelResponse`` from the persisted
message and feeds it to ``merge_response_usage``. Before these tests the
rebuild dropped cost, cumulative cost and currency, so an engine run on a priced
model recorded per-step events but no session total, and an unpriced response's
"unknown" marker disappeared on the way to the report.
"""

from __future__ import annotations

from karox.engine_provider_bridge import _response_from_engine_message
from karox.usage_analytics import merge_response_usage


def _message(**extra: object) -> dict:
    payload: dict = {
        "role": "assistant",
        "content": "answer",
        "extra": {
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "finish_reason": "stop",
            "provider": "demo",
            "model": "model-1",
            "transport_attempts": 1,
        },
    }
    payload["extra"].update(extra)
    return payload


def test_priced_engine_message_keeps_its_cost_fields() -> None:
    response = _response_from_engine_message(
        _message(
            cost=0.001,
            cumulative_cost=0.003,
            uncached_cost=0.004,
            cache_savings=0.001,
            currency="USD",
            pricing_version="2026-07",
        )
    )

    assert response.cost == 0.001
    assert response.cumulative_cost == 0.003
    assert response.uncached_cost == 0.004
    assert response.cache_savings == 0.001
    assert response.currency == "USD"
    assert response.pricing_version == "2026-07"
    assert response.cost_unknown_reason is None

    aggregate = merge_response_usage({}, response, event={"step": 1})
    assert aggregate["costs"] == {"USD": 0.003}
    assert aggregate["cost_status"] == "measured"


def test_unknown_cost_reason_survives_the_engine_message() -> None:
    response = _response_from_engine_message(
        _message(cost_unknown_reason="usage_missing", usage={})
    )

    assert response.cost is None
    assert response.cost_unknown_reason == "usage_missing"

    aggregate = merge_response_usage({}, response, event={"step": 1})
    assert aggregate["cost_status"] == "unknown"
    assert aggregate["cost_unknown_reason"] == "usage_missing"
    assert "costs" not in aggregate