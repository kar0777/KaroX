"""Persistent usage and cost analytics derived from KaroX session records.

The native agent already persists authoritative cumulative usage in
``SessionRecord.usage``.  This module adds a bounded per-request event stream
inside that same field so the TUI can answer time-scoped questions (today,
this session, provider/model breakdown) without creating a second database or
re-pricing historical requests.

No credential, prompt, completion text, tool arguments, URL, or repository
content is stored here.  Events contain only numeric usage/cost facts and safe
provider/model identifiers already present in the provider audit.
"""

from __future__ import annotations

import dataclasses
import math
import time
from collections import defaultdict
from typing import Any, Iterable, Mapping, Optional

from .providers import ModelResponse
from .sessions import SessionRecord, SessionStore

USAGE_EVENTS_KEY = "events"
MAX_USAGE_EVENTS_PER_SESSION = 1_000


def _count(value: Any) -> int:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _usage_count(usage: Mapping[str, Any], *names: str) -> int:
    for name in names:
        if name in usage:
            return _count(usage.get(name))
    return 0


def _route_retries(response: ModelResponse) -> int:
    total = 0
    for attempt in response.route_attempts:
        total += _count(attempt.get("retries"))
    return total


def usage_event_from_response(
    *,
    session_id: str,
    step: int,
    response: ModelResponse,
    timestamp: Optional[float] = None,
) -> dict[str, Any]:
    """Build one bounded, secret-free usage event from a completed response."""

    usage = response.usage
    prompt_tokens = _usage_count(usage, "prompt_tokens", "input_tokens")
    completion_tokens = _usage_count(usage, "completion_tokens", "output_tokens")
    total_tokens = _usage_count(usage, "total_tokens") or (
        prompt_tokens + completion_tokens
    )
    event: dict[str, Any] = {
        "timestamp": float(time.time() if timestamp is None else timestamp),
        "session_id": str(session_id),
        "step": max(0, int(step)),
        "provider": str(response.selected_provider or ""),
        "model": str(response.selected_model or ""),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cache_read_tokens": _usage_count(usage, "cache_read_tokens"),
        "cache_write_tokens": _usage_count(usage, "cache_write_tokens"),
        "reasoning_tokens": _usage_count(usage, "reasoning_tokens"),
        "transport_attempts": max(1, int(response.transport_attempts)),
        "route_retries": _route_retries(response),
    }
    for name, value in (
        ("cost", response.cost),
        ("uncached_cost", response.uncached_cost),
        ("cache_savings", response.cache_savings),
    ):
        number = _number(value)
        if number is not None:
            event[name] = round(number, 12)
    if response.currency:
        event["currency"] = str(response.currency)
    if response.pricing_version:
        event["pricing_version"] = str(response.pricing_version)
    return event


def merge_response_usage(
    aggregate: Mapping[str, Any],
    response: ModelResponse,
    *,
    event: Mapping[str, Any],
    model_fallback: str = "",
) -> dict[str, Any]:
    """Merge one provider response into the authoritative session aggregate.

    Native-agent and read-only subagent calls must use the same accounting path:
    otherwise Recursive Context can spend real tokens while the Usage screen
    claims they never happened. ``event`` is supplied by the caller so it may add
    a safe source label (for example ``research_subagent``) without duplicating
    the cumulative token/cost rules here.
    """

    result = dict(aggregate)
    result["requests"] = _count(result.get("requests")) + 1
    result["transport_attempts"] = _count(result.get("transport_attempts")) + max(
        1, int(response.transport_attempts)
    )

    previous_total = _usage_count(result, "total_tokens")
    if previous_total <= 0:
        previous_total = _usage_count(result, "prompt_tokens", "input_tokens") + _usage_count(
            result, "completion_tokens", "output_tokens"
        )
    addition_total = _usage_count(response.usage, "total_tokens")
    if addition_total <= 0:
        addition_total = _usage_count(response.usage, "prompt_tokens", "input_tokens") + _usage_count(
            response.usage, "completion_tokens", "output_tokens"
        )

    for name, count in response.usage.items():
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            result[name] = _count(result.get(name)) + count
    result["total_tokens"] = previous_total + addition_total

    if response.currency is not None and response.cumulative_cost is not None:
        raw_costs = result.get("costs")
        costs = dict(raw_costs) if isinstance(raw_costs, dict) else {}
        costs[response.currency] = response.cumulative_cost
        result["costs"] = costs

    latest_prompt = response.usage.get(
        "prompt_tokens", response.usage.get("input_tokens")
    )
    if isinstance(latest_prompt, bool) or not isinstance(latest_prompt, (int, float)):
        latest_prompt = None
    result["last_request"] = {
        "prompt_tokens": None if latest_prompt is None else int(latest_prompt),
        "model": response.selected_model or model_fallback,
    }
    return append_usage_event(result, event)


def append_usage_event(
    aggregate: Mapping[str, Any],
    event: Mapping[str, Any],
    *,
    limit: int = MAX_USAGE_EVENTS_PER_SESSION,
) -> dict[str, Any]:
    """Return ``aggregate`` with one event appended and the history bounded."""

    if limit <= 0:
        raise ValueError("usage event limit must be positive")
    result = dict(aggregate)
    raw_events = result.get(USAGE_EVENTS_KEY)
    events = [dict(item) for item in raw_events if isinstance(item, dict)] if isinstance(raw_events, list) else []
    events.append(dict(event))
    if len(events) > limit:
        dropped = len(events) - limit
        events = events[dropped:]
        result["events_dropped"] = _count(result.get("events_dropped")) + dropped
    result[USAGE_EVENTS_KEY] = events
    return result


@dataclasses.dataclass(frozen=True)
class UsageSummary:
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    transport_retries: int = 0
    route_retries: int = 0
    costs: dict[str, float] = dataclasses.field(default_factory=dict)
    uncached_costs: dict[str, float] = dataclasses.field(default_factory=dict)
    cache_savings: dict[str, float] = dataclasses.field(default_factory=dict)
    by_provider_model: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)
    legacy_unattributed_sessions: int = 0

    @property
    def cache_hit_rate(self) -> float:
        if self.prompt_tokens <= 0:
            return 0.0
        return min(1.0, self.cache_read_tokens / self.prompt_tokens)

    @property
    def retries(self) -> int:
        return self.transport_retries + self.route_retries

    def to_dict(self) -> dict[str, Any]:
        value = dataclasses.asdict(self)
        value["cache_hit_rate"] = round(self.cache_hit_rate, 6)
        value["retries"] = self.retries
        return value


def _event_timestamp(event: Mapping[str, Any]) -> Optional[float]:
    value = _number(event.get("timestamp"))
    return value if value is not None and value >= 0 else None


def summarize_events(
    events: Iterable[Mapping[str, Any]],
    *,
    since: Optional[float] = None,
    session_id: Optional[str] = None,
) -> UsageSummary:
    """Aggregate already-persisted usage events without re-pricing them."""

    requests = prompt = completion = total = cache_read = cache_write = reasoning = 0
    transport_retries = route_retries = 0
    costs: dict[str, float] = defaultdict(float)
    uncached: dict[str, float] = defaultdict(float)
    savings: dict[str, float] = defaultdict(float)
    breakdown: dict[str, dict[str, Any]] = {}

    for event in events:
        if not isinstance(event, Mapping):
            continue
        if session_id is not None and str(event.get("session_id") or "") != session_id:
            continue
        timestamp = _event_timestamp(event)
        if since is not None and (timestamp is None or timestamp < since):
            continue
        requests += 1
        prompt_value = _count(event.get("prompt_tokens"))
        completion_value = _count(event.get("completion_tokens"))
        total_value = _count(event.get("total_tokens")) or prompt_value + completion_value
        cache_read_value = _count(event.get("cache_read_tokens"))
        cache_write_value = _count(event.get("cache_write_tokens"))
        reasoning_value = _count(event.get("reasoning_tokens"))
        prompt += prompt_value
        completion += completion_value
        total += total_value
        cache_read += cache_read_value
        cache_write += cache_write_value
        reasoning += reasoning_value
        transport_retries += max(0, _count(event.get("transport_attempts")) - 1)
        route_retries += _count(event.get("route_retries"))

        currency = str(event.get("currency") or "")
        cost = _number(event.get("cost"))
        uncached_cost = _number(event.get("uncached_cost"))
        saved = _number(event.get("cache_savings"))
        if currency:
            if cost is not None:
                costs[currency] += cost
            if uncached_cost is not None:
                uncached[currency] += uncached_cost
            if saved is not None:
                savings[currency] += saved

        provider = str(event.get("provider") or "unknown")
        model = str(event.get("model") or "unknown")
        key = f"{provider}/{model}"
        item = breakdown.setdefault(
            key,
            {
                "provider": provider,
                "model": model,
                "requests": 0,
                "total_tokens": 0,
                "cache_read_tokens": 0,
                "costs": {},
            },
        )
        item["requests"] += 1
        item["total_tokens"] += total_value
        item["cache_read_tokens"] += cache_read_value
        if currency and cost is not None:
            item_costs = item["costs"]
            item_costs[currency] = round(float(item_costs.get(currency, 0.0)) + cost, 12)

    return UsageSummary(
        requests=requests,
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        reasoning_tokens=reasoning,
        transport_retries=transport_retries,
        route_retries=route_retries,
        costs={name: round(value, 12) for name, value in sorted(costs.items())},
        uncached_costs={name: round(value, 12) for name, value in sorted(uncached.items())},
        cache_savings={name: round(value, 12) for name, value in sorted(savings.items())},
        by_provider_model=dict(sorted(breakdown.items())),
    )


def summarize_records(
    records: Iterable[SessionRecord],
    *,
    since: Optional[float] = None,
    session_id: Optional[str] = None,
) -> UsageSummary:
    """Aggregate session records; time-scoped summaries use only exact events.

    Old sessions written before per-request events existed still have valid
    cumulative usage, but they cannot be assigned to a day honestly.  They are
    therefore counted as ``legacy_unattributed_sessions`` instead of silently
    inflating today's number.
    """

    selected = [record for record in records if session_id is None or record.session_id == session_id]
    events: list[Mapping[str, Any]] = []
    legacy = 0
    for record in selected:
        raw_events = record.usage.get(USAGE_EVENTS_KEY) if isinstance(record.usage, dict) else None
        if isinstance(raw_events, list) and raw_events:
            events.extend(item for item in raw_events if isinstance(item, Mapping))
        elif since is not None and isinstance(record.usage, dict) and record.usage:
            legacy += 1

    summary = summarize_events(events, since=since, session_id=session_id)
    if since is None:
        # If a legacy record has no events, its cumulative usage is still the
        # authoritative all-time value for that session. Fold only those records
        # so event-backed sessions are never double-counted.
        legacy_events: list[dict[str, Any]] = []
        for record in selected:
            raw_events = record.usage.get(USAGE_EVENTS_KEY) if isinstance(record.usage, dict) else None
            if isinstance(raw_events, list) and raw_events:
                continue
            usage = record.usage if isinstance(record.usage, dict) else {}
            if not usage:
                continue
            raw_costs = usage.get("costs")
            costs: dict[Any, Any] = raw_costs if isinstance(raw_costs, dict) else {}
            event: dict[str, Any] = {
                "timestamp": float(record.updated_at),
                "session_id": record.session_id,
                "step": 0,
                "provider": "legacy",
                "model": "legacy",
                "prompt_tokens": _usage_count(usage, "prompt_tokens", "input_tokens"),
                "completion_tokens": _usage_count(usage, "completion_tokens", "output_tokens"),
                "total_tokens": _usage_count(usage, "total_tokens"),
                "cache_read_tokens": _usage_count(usage, "cache_read_tokens"),
                "cache_write_tokens": _usage_count(usage, "cache_write_tokens"),
                "reasoning_tokens": _usage_count(usage, "reasoning_tokens"),
                "transport_attempts": _count(usage.get("transport_attempts")) or _count(usage.get("requests")) or 1,
                "route_retries": 0,
            }
            if len(costs) == 1:
                currency, raw_cost = next(iter(costs.items()))
                number = _number(raw_cost)
                if isinstance(currency, str) and number is not None:
                    event["currency"] = currency
                    event["cost"] = number
            legacy_events.append(event)
        if legacy_events:
            legacy_summary = summarize_events(legacy_events)
            summary = merge_summaries(summary, legacy_summary)

    return dataclasses.replace(summary, legacy_unattributed_sessions=legacy)


def merge_summaries(*summaries: UsageSummary) -> UsageSummary:
    # This helper is intentionally arithmetic rather than reconstructing fake
    # events, because provider/model breakdowns and currencies may differ.
    if not summaries:
        return UsageSummary()
    requests = sum(item.requests for item in summaries)
    prompt = sum(item.prompt_tokens for item in summaries)
    completion = sum(item.completion_tokens for item in summaries)
    total = sum(item.total_tokens for item in summaries)
    cache_read = sum(item.cache_read_tokens for item in summaries)
    cache_write = sum(item.cache_write_tokens for item in summaries)
    reasoning = sum(item.reasoning_tokens for item in summaries)
    transport = sum(item.transport_retries for item in summaries)
    route = sum(item.route_retries for item in summaries)
    legacy = sum(item.legacy_unattributed_sessions for item in summaries)
    costs: dict[str, float] = defaultdict(float)
    uncached: dict[str, float] = defaultdict(float)
    savings: dict[str, float] = defaultdict(float)
    breakdown: dict[str, dict[str, Any]] = {}
    for summary in summaries:
        for name, value in summary.costs.items():
            costs[name] += value
        for name, value in summary.uncached_costs.items():
            uncached[name] += value
        for name, value in summary.cache_savings.items():
            savings[name] += value
        for key, source in summary.by_provider_model.items():
            target = breakdown.setdefault(
                key,
                {
                    "provider": source.get("provider", "unknown"),
                    "model": source.get("model", "unknown"),
                    "requests": 0,
                    "total_tokens": 0,
                    "cache_read_tokens": 0,
                    "costs": {},
                },
            )
            target["requests"] += _count(source.get("requests"))
            target["total_tokens"] += _count(source.get("total_tokens"))
            target["cache_read_tokens"] += _count(source.get("cache_read_tokens"))
            for currency, raw_cost in (source.get("costs") or {}).items():
                number = _number(raw_cost)
                if isinstance(currency, str) and number is not None:
                    target["costs"][currency] = round(
                        float(target["costs"].get(currency, 0.0)) + number, 12
                    )
    return UsageSummary(
        requests=requests,
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        reasoning_tokens=reasoning,
        transport_retries=transport,
        route_retries=route,
        costs={name: round(value, 12) for name, value in sorted(costs.items())},
        uncached_costs={name: round(value, 12) for name, value in sorted(uncached.items())},
        cache_savings={name: round(value, 12) for name, value in sorted(savings.items())},
        by_provider_model=dict(sorted(breakdown.items())),
        legacy_unattributed_sessions=legacy,
    )


def summarize_store(
    store: SessionStore,
    *,
    since: Optional[float] = None,
    session_id: Optional[str] = None,
) -> UsageSummary:
    try:
        records = store.list()
    except Exception:
        return UsageSummary()
    return summarize_records(records, since=since, session_id=session_id)


__all__ = [
    "MAX_USAGE_EVENTS_PER_SESSION",
    "USAGE_EVENTS_KEY",
    "UsageSummary",
    "append_usage_event",
    "merge_summaries",
    "summarize_events",
    "summarize_records",
    "summarize_store",
    "usage_event_from_response",
]
