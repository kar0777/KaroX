from __future__ import annotations

from karox.providers import ModelResponse
from karox.sessions import SessionRecord
from karox.usage_analytics import (
    MAX_USAGE_EVENTS_PER_SESSION,
    append_usage_event,
    summarize_records,
    summarize_events,
    usage_event_from_response,
)


def _record(session_id: str, usage: dict) -> SessionRecord:
    return SessionRecord(
        session_id=session_id,
        repository="C:/repo",
        repo_fingerprint="fixture",
        branch="main",
        access_profile="test",
        task="work",
        created_at=100.0,
        updated_at=200.0,
        usage=usage,
    )


def test_response_event_contains_only_usage_route_and_cost_facts() -> None:
    response = ModelResponse(
        content="secret-looking answer must not be copied into analytics",
        tool_calls=(),
        finish_reason="stop",
        usage={
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "cache_read_tokens": 80,
            "reasoning_tokens": 7,
        },
        selected_provider="provider-a",
        selected_model="model-a",
        cost=0.02,
        uncached_cost=0.10,
        cache_savings=0.08,
        currency="USD",
        pricing_version="v1",
        transport_attempts=2,
        route_attempts=({"provider_id": "provider-a", "status": "completed", "retries": 1},),
    )

    event = usage_event_from_response(
        session_id="session-a", step=3, response=response, timestamp=123.0
    )

    assert event["session_id"] == "session-a"
    assert event["prompt_tokens"] == 100
    assert event["cache_read_tokens"] == 80
    assert event["cost"] == 0.02
    assert event["cache_savings"] == 0.08
    assert event["route_retries"] == 1
    assert "content" not in event
    assert "tool_calls" not in event


def test_event_history_is_bounded() -> None:
    aggregate: dict = {}
    for index in range(MAX_USAGE_EVENTS_PER_SESSION + 3):
        aggregate = append_usage_event(
            aggregate,
            {"timestamp": float(index), "session_id": "s", "total_tokens": 1},
        )

    assert len(aggregate["events"]) == MAX_USAGE_EVENTS_PER_SESSION
    assert aggregate["events"][0]["timestamp"] == 3.0
    assert aggregate["events_dropped"] == 3


def test_summary_is_time_scoped_and_cache_aware() -> None:
    events = [
        {
            "timestamp": 100.0,
            "session_id": "old",
            "provider": "p",
            "model": "m",
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "total_tokens": 110,
            "cache_read_tokens": 0,
            "transport_attempts": 1,
            "route_retries": 0,
            "currency": "USD",
            "cost": 0.10,
            "uncached_cost": 0.10,
            "cache_savings": 0.0,
        },
        {
            "timestamp": 200.0,
            "session_id": "new",
            "provider": "p",
            "model": "m",
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "cache_read_tokens": 80,
            "transport_attempts": 2,
            "route_retries": 1,
            "currency": "USD",
            "cost": 0.02,
            "uncached_cost": 0.10,
            "cache_savings": 0.08,
        },
    ]

    summary = summarize_events(events, since=150.0)

    assert summary.requests == 1
    assert summary.total_tokens == 120
    assert summary.cache_hit_rate == 0.8
    assert summary.retries == 2
    assert summary.costs == {"USD": 0.02}
    assert summary.uncached_costs == {"USD": 0.10}
    assert summary.cache_savings == {"USD": 0.08}
    assert summary.by_provider_model["p/m"]["requests"] == 1


def test_time_scoped_summary_does_not_guess_when_legacy_session_was_spent() -> None:
    legacy = _record(
        "legacy",
        {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "costs": {"USD": 0.5},
        },
    )

    today = summarize_records([legacy], since=150.0)
    all_time = summarize_records([legacy])

    assert today.total_tokens == 0
    assert today.costs == {}
    assert today.legacy_unattributed_sessions == 1
    assert all_time.total_tokens == 120
    assert all_time.costs == {"USD": 0.5}
