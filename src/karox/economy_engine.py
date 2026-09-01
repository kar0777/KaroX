"""Measured economy reporting for KaroX orchestration.

The module is intentionally conservative: a Savings Receipt distinguishes
provider-measured actuals, deterministic pricing estimates, and true OFF-vs-ON
baselines.  Missing data stays unavailable instead of being replaced with a
flattering guess.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Iterable, Mapping, Optional

from .context_bus import ContextDelta
from .orchestration_routing import RouteRequest, VerifiedSmartRouter
from .usage_analytics import UsageSummary


MEASURED = "measured"
ESTIMATED = "estimated"
UNAVAILABLE = "unavailable"


@dataclasses.dataclass(frozen=True)
class EconomyCounters:
    context_chars_reused: int = 0
    context_chars_sent: int = 0
    tool_schema_bytes_avoided: int = 0
    round_trips_avoided: int = 0
    cache_read_tokens: int = 0
    premium_calls_avoided: int = 0

    def plus(self, other: "EconomyCounters") -> "EconomyCounters":
        return EconomyCounters(
            context_chars_reused=self.context_chars_reused + other.context_chars_reused,
            context_chars_sent=self.context_chars_sent + other.context_chars_sent,
            tool_schema_bytes_avoided=self.tool_schema_bytes_avoided + other.tool_schema_bytes_avoided,
            round_trips_avoided=self.round_trips_avoided + other.round_trips_avoided,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            premium_calls_avoided=self.premium_calls_avoided + other.premium_calls_avoided,
        )

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class MoneyMetric:
    amount: Optional[float]
    currency: str = "USD"
    evidence: str = UNAVAILABLE

    def __post_init__(self) -> None:
        if self.evidence not in {MEASURED, ESTIMATED, UNAVAILABLE}:
            raise ValueError("unsupported money metric evidence label")
        if self.amount is None and self.evidence != UNAVAILABLE:
            raise ValueError("money metric without amount must be unavailable")
        if self.amount is not None and self.amount < 0:
            raise ValueError("money amount must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class SavingsReceipt:
    actual_cost: MoneyMetric
    baseline_cost: MoneyMetric
    savings: MoneyMetric
    savings_percent: Optional[float]
    cache_savings: MoneyMetric
    counters: EconomyCounters
    quality_gates: tuple[str, ...]
    accepted: bool
    verified: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "actual_cost": self.actual_cost.to_dict(),
            "baseline_cost": self.baseline_cost.to_dict(),
            "savings": self.savings.to_dict(),
            "savings_percent": self.savings_percent,
            "cache_savings": self.cache_savings.to_dict(),
            "counters": self.counters.to_dict(),
            "quality_gates": list(self.quality_gates),
            "accepted": self.accepted,
            "verified": self.verified,
        }

    def render(self) -> list[str]:
        def money(metric: MoneyMetric) -> str:
            if metric.amount is None:
                return "UNAVAILABLE"
            return f"{metric.currency} {metric.amount:.4f} ({metric.evidence})"

        lines = [
            "KaroX Savings Receipt",
            f"actual cost: {money(self.actual_cost)}",
            f"baseline cost: {money(self.baseline_cost)}",
            f"savings: {money(self.savings)}",
            "savings percent: " + (
                "UNAVAILABLE"
                if self.savings_percent is None
                else f"{self.savings_percent:.1f}% ({self.savings.evidence})"
            ),
            f"cache savings: {money(self.cache_savings)}",
            f"context reused locally: {self.counters.context_chars_reused} chars",
            f"context sent: {self.counters.context_chars_sent} chars",
            # Tool-schema, round-trip and premium-call avoidance counters stay in
            # the machine payload for future instrumentation, but are deliberately
            # not rendered as user-facing savings until KaroX has an authoritative
            # measured source for them. Zero is not evidence of a measurement.
            f"quality: {'accepted' if self.accepted else 'not accepted'} / {'verified' if self.verified else 'not verified'}",
        ]
        lines.extend(f"gate: {gate}" for gate in self.quality_gates)
        return lines


def counters_from_delta(delta: ContextDelta) -> EconomyCounters:
    return EconomyCounters(
        context_chars_reused=delta.reused_chars,
        context_chars_sent=delta.sent_chars,
    )


def _single_currency(value: Mapping[str, float]) -> tuple[Optional[float], str]:
    nonzero = [(name, float(amount)) for name, amount in value.items() if amount is not None]
    if len(nonzero) != 1:
        return None, "USD"
    return nonzero[0][1], nonzero[0][0]


def build_savings_receipt(
    *,
    usage: UsageSummary,
    counters: EconomyCounters = EconomyCounters(),
    measured_baseline_cost: Optional[float] = None,
    baseline_currency: str = "USD",
    actual_cost_evidence: str = ESTIMATED,
    cache_savings_evidence: str = ESTIMATED,
    accepted: bool,
    verified: bool,
    quality_gates: Iterable[str] = (),
) -> SavingsReceipt:
    actual_amount, currency = _single_currency(usage.costs)
    actual = MoneyMetric(
        actual_amount,
        currency=currency,
        evidence=actual_cost_evidence if actual_amount is not None else UNAVAILABLE,
    )
    cache_amount, cache_currency = _single_currency(usage.cache_savings)
    cache = MoneyMetric(
        cache_amount,
        currency=cache_currency,
        evidence=cache_savings_evidence if cache_amount is not None else UNAVAILABLE,
    )

    baseline: MoneyMetric
    savings: MoneyMetric
    percent: Optional[float] = None
    if measured_baseline_cost is not None:
        if measured_baseline_cost < 0:
            raise ValueError("measured baseline cost must be non-negative")
        baseline = MoneyMetric(measured_baseline_cost, baseline_currency, MEASURED)
        if actual.amount is not None and actual.currency == baseline.currency:
            saved = max(0.0, baseline.amount - actual.amount)  # type: ignore[operator]
            savings_evidence = (
                MEASURED
                if actual.evidence == MEASURED and baseline.evidence == MEASURED
                else ESTIMATED
            )
            savings = MoneyMetric(saved, baseline.currency, savings_evidence)
            if baseline.amount and baseline.amount > 0:
                percent = round(saved / baseline.amount * 100.0, 3)
        else:
            savings = MoneyMetric(None)
    else:
        baseline = MoneyMetric(None)
        savings = MoneyMetric(None)

    return SavingsReceipt(
        actual_cost=actual,
        baseline_cost=baseline,
        savings=savings,
        savings_percent=percent,
        cache_savings=cache,
        counters=dataclasses.replace(counters, cache_read_tokens=usage.cache_read_tokens),
        quality_gates=tuple(quality_gates),
        accepted=accepted,
        verified=verified,
    )


@dataclasses.dataclass(frozen=True)
class ShadowRouteReport:
    actual_endpoint_id: str
    recommended_endpoint_id: str
    changed: bool
    actual_estimated_cost_usd: Optional[float]
    recommended_estimated_cost_usd: Optional[float]
    projected_cost_delta_usd: Optional[float]
    recommendation_uses_verified_quality: bool
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def shadow_route(
    *,
    router: VerifiedSmartRouter,
    request: RouteRequest,
    actual_endpoint_id: str,
) -> ShadowRouteReport:
    decision = router.decide(request)
    actual_candidate = next(
        (item for item in decision.candidates if item.endpoint.endpoint_id == actual_endpoint_id),
        None,
    )
    actual_cost = None if actual_candidate is None else actual_candidate.estimated_marginal_cost_usd
    recommended_cost = next(
        (
            item.estimated_marginal_cost_usd
            for item in decision.candidates
            if item.endpoint.endpoint_id == decision.endpoint.endpoint_id
        ),
        None,
    )
    delta = None
    if actual_cost is not None and recommended_cost is not None:
        delta = round(actual_cost - recommended_cost, 12)
    return ShadowRouteReport(
        actual_endpoint_id=actual_endpoint_id,
        recommended_endpoint_id=decision.endpoint.endpoint_id,
        changed=actual_endpoint_id != decision.endpoint.endpoint_id,
        actual_estimated_cost_usd=actual_cost,
        recommended_estimated_cost_usd=recommended_cost,
        projected_cost_delta_usd=delta,
        recommendation_uses_verified_quality=decision.verified_quality_used,
        reasons=decision.reasons,
    )


@dataclasses.dataclass(frozen=True)
class ReplayCase:
    name: str
    request: RouteRequest
    actual_endpoint_id: str


@dataclasses.dataclass(frozen=True)
class ReplayResult:
    cases: int
    route_changes: int
    comparable_cost_cases: int
    projected_cost_delta_usd: float
    reports: tuple[ShadowRouteReport, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "cases": self.cases,
            "route_changes": self.route_changes,
            "comparable_cost_cases": self.comparable_cost_cases,
            "projected_cost_delta_usd": self.projected_cost_delta_usd,
            "reports": [item.to_dict() for item in self.reports],
            "warning": (
                "Cost delta is a routing projection from known deterministic prices; "
                "Replay Lab does not claim the counterfactual worker would have succeeded."
            ),
        }


class ReplayLab:
    def __init__(self, router: VerifiedSmartRouter) -> None:
        self.router = router

    def replay(self, cases: Iterable[ReplayCase]) -> ReplayResult:
        reports = tuple(
            shadow_route(router=self.router, request=case.request, actual_endpoint_id=case.actual_endpoint_id)
            for case in cases
        )
        deltas = [item.projected_cost_delta_usd for item in reports if item.projected_cost_delta_usd is not None]
        return ReplayResult(
            cases=len(reports),
            route_changes=sum(1 for item in reports if item.changed),
            comparable_cost_cases=len(deltas),
            projected_cost_delta_usd=round(sum(deltas), 12),
            reports=reports,
        )


__all__ = [
    "ESTIMATED",
    "EconomyCounters",
    "MEASURED",
    "MoneyMetric",
    "ReplayCase",
    "ReplayLab",
    "ReplayResult",
    "SavingsReceipt",
    "ShadowRouteReport",
    "UNAVAILABLE",
    "build_savings_receipt",
    "counters_from_delta",
    "shadow_route",
]
