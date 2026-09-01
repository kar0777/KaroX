"""Evidence-driven routing for the KaroX 5 orchestration layer.

Automatic model selection is allowed to learn only from KaroX-observed,
accepted-and-verified outcomes.  Marketing rankings, model-name heuristics and
unverified benchmark claims never enter the score.  Before enough local evidence
exists, explicit role assignments and capabilities are the quality authority.

Routing also understands marginal cost (already-paid subscriptions are zero
marginal cost), quota reserve, task risk, and deterministic provider pricing.
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Optional

from .intelligence_pool import IntelligenceEndpoint, IntelligencePool
from .paths import config_dir
from .quota_brain import QuotaBrain
from .registry import ProviderRegistry
from .risk_engine import RiskLevel


TASK_DISCOVERY = "repo_discovery"
TASK_ARCHITECTURE = "architecture"
TASK_IMPLEMENTATION = "implementation"
TASK_REVIEW = "review"
TASK_UI = "ui_verification"
TASK_SECURITY = "security"
TASK_SUMMARY = "summarization"
TASK_TESTING = "testing"
TASK_GENERAL = "general"
TASK_CLASSES = frozenset(
    {
        TASK_DISCOVERY,
        TASK_ARCHITECTURE,
        TASK_IMPLEMENTATION,
        TASK_REVIEW,
        TASK_UI,
        TASK_SECURITY,
        TASK_SUMMARY,
        TASK_TESTING,
        TASK_GENERAL,
    }
)
_SCHEMA_VERSION = 1


class RoutingError(RuntimeError):
    pass


def _finite_nonnegative(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite non-negative number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{label} must be a finite non-negative number")
    return number


@dataclasses.dataclass(frozen=True)
class RoutingObservation:
    endpoint_id: str
    task_class: str
    accepted: bool
    verified: bool
    latency_ms: float = 0.0
    cost_usd: Optional[float] = None
    total_tokens: int = 0
    timestamp: float = 0.0

    def __post_init__(self) -> None:
        if self.task_class not in TASK_CLASSES:
            raise ValueError(f"unsupported task class: {self.task_class}")
        if not isinstance(self.accepted, bool) or not isinstance(self.verified, bool):
            raise ValueError("routing outcome flags must be booleans")
        _finite_nonnegative(self.latency_ms, "latency_ms")
        if self.cost_usd is not None:
            _finite_nonnegative(self.cost_usd, "cost_usd")
        if isinstance(self.total_tokens, bool) or not isinstance(self.total_tokens, int) or self.total_tokens < 0:
            raise ValueError("total_tokens must be a non-negative integer")
        _finite_nonnegative(self.timestamp, "timestamp")

    @property
    def success(self) -> bool:
        return self.accepted and self.verified

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RoutingObservation":
        return cls(
            endpoint_id=str(value["endpoint_id"]),
            task_class=str(value["task_class"]),
            accepted=value["accepted"],
            verified=value["verified"],
            latency_ms=value.get("latency_ms", 0.0),
            cost_usd=value.get("cost_usd"),
            total_tokens=value.get("total_tokens", 0),
            timestamp=value.get("timestamp", 0.0),
        )


@dataclasses.dataclass(frozen=True)
class EndpointPerformance:
    endpoint_id: str
    task_class: str
    samples: int
    successes: int
    success_rate: Optional[float]
    average_cost_usd: Optional[float]
    average_latency_ms: Optional[float]

    @property
    def verified_quality_available(self) -> bool:
        return self.samples > 0 and self.success_rate is not None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class RoutingTelemetry:
    """Bounded, secret-free durable routing evidence."""

    def __init__(self, path: Optional[Path] = None, *, limit: int = 5000) -> None:
        self.path = (path or (config_dir() / "vnext" / "routing-evidence.json")).expanduser().resolve()
        if limit <= 0:
            raise ValueError("routing telemetry limit must be positive")
        self.limit = int(limit)

    def _load(self) -> list[RoutingObservation]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RoutingError(f"cannot read routing telemetry: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != _SCHEMA_VERSION:
            raise RoutingError("routing telemetry has unsupported schema")
        raw = payload.get("observations")
        if not isinstance(raw, list):
            raise RoutingError("routing telemetry observations must be an array")
        try:
            return [RoutingObservation.from_dict(item) for item in raw if isinstance(item, Mapping)]
        except (KeyError, TypeError, ValueError) as exc:
            raise RoutingError(f"routing telemetry is invalid: {exc}") from exc

    def _save(self, observations: list[RoutingObservation]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        observations = observations[-self.limit :]
        fd, raw = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        temp = Path(raw)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(
                    {
                        "schema_version": _SCHEMA_VERSION,
                        "observations": [item.to_dict() for item in observations],
                    },
                    handle,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                    allow_nan=False,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    def record(self, observation: RoutingObservation) -> RoutingObservation:
        observations = self._load()
        observations.append(observation)
        self._save(observations)
        return observation

    def observe(
        self,
        *,
        endpoint_id: str,
        task_class: str,
        accepted: bool,
        verified: bool,
        latency_ms: float = 0.0,
        cost_usd: Optional[float] = None,
        total_tokens: int = 0,
        timestamp: Optional[float] = None,
    ) -> RoutingObservation:
        return self.record(
            RoutingObservation(
                endpoint_id=endpoint_id,
                task_class=task_class,
                accepted=accepted,
                verified=verified,
                latency_ms=latency_ms,
                cost_usd=cost_usd,
                total_tokens=total_tokens,
                timestamp=time.time() if timestamp is None else timestamp,
            )
        )

    def performance(self, endpoint_id: str, task_class: str) -> EndpointPerformance:
        selected = [
            item
            for item in self._load()
            if item.endpoint_id == endpoint_id and item.task_class == task_class
        ]
        samples = len(selected)
        successes = sum(1 for item in selected if item.success)
        costs = [item.cost_usd for item in selected if item.cost_usd is not None]
        latencies = [item.latency_ms for item in selected if item.latency_ms > 0]
        return EndpointPerformance(
            endpoint_id=endpoint_id,
            task_class=task_class,
            samples=samples,
            successes=successes,
            success_rate=(successes / samples if samples else None),
            average_cost_usd=(sum(costs) / len(costs) if costs else None),
            average_latency_ms=(sum(latencies) / len(latencies) if latencies else None),
        )

    def all_performance(self, task_class: str) -> list[EndpointPerformance]:
        endpoints = sorted({item.endpoint_id for item in self._load() if item.task_class == task_class})
        return [self.performance(endpoint_id, task_class) for endpoint_id in endpoints]


@dataclasses.dataclass(frozen=True)
class RouteRequest:
    task_class: str
    role: str
    required_capabilities: tuple[str, ...] = ()
    risk_level: RiskLevel = RiskLevel.MEDIUM
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0
    preferred_endpoint_id: Optional[str] = None
    preferred_is_advisory: bool = False
    exclude_endpoint_ids: tuple[str, ...] = ()
    minimum_verified_samples: int = 3
    minimum_verified_success_rate: float = 0.75
    reserve_quota_fraction: float = 0.15
    prefer_already_paid: bool = True

    def __post_init__(self) -> None:
        if self.task_class not in TASK_CLASSES:
            raise ValueError(f"unsupported task class: {self.task_class}")
        if isinstance(self.estimated_input_tokens, bool) or self.estimated_input_tokens < 0:
            raise ValueError("estimated_input_tokens must be non-negative")
        if isinstance(self.estimated_output_tokens, bool) or self.estimated_output_tokens < 0:
            raise ValueError("estimated_output_tokens must be non-negative")
        if self.minimum_verified_samples < 0:
            raise ValueError("minimum_verified_samples must be non-negative")
        if not 0 <= self.minimum_verified_success_rate <= 1:
            raise ValueError("minimum_verified_success_rate must be between 0 and 1")
        if not 0 <= self.reserve_quota_fraction <= 1:
            raise ValueError("reserve_quota_fraction must be between 0 and 1")
        if not isinstance(self.preferred_is_advisory, bool):
            raise ValueError("preferred_is_advisory must be boolean")


@dataclasses.dataclass(frozen=True)
class RouteCandidate:
    endpoint: IntelligenceEndpoint
    performance: EndpointPerformance
    estimated_marginal_cost_usd: Optional[float]
    score: float
    eligible: bool
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint.to_dict(),
            "performance": self.performance.to_dict(),
            "estimated_marginal_cost_usd": self.estimated_marginal_cost_usd,
            "score": round(self.score, 6),
            "eligible": self.eligible,
            "reasons": list(self.reasons),
        }


@dataclasses.dataclass(frozen=True)
class RouteDecision:
    endpoint: IntelligenceEndpoint
    reasons: tuple[str, ...]
    candidates: tuple[RouteCandidate, ...]
    verified_quality_used: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint.to_dict(),
            "reasons": list(self.reasons),
            "verified_quality_used": self.verified_quality_used,
            "candidates": [item.to_dict() for item in self.candidates],
        }


class VerifiedSmartRouter:
    def __init__(
        self,
        *,
        pool: Optional[IntelligencePool] = None,
        telemetry: Optional[RoutingTelemetry] = None,
        provider_registry: Optional[ProviderRegistry] = None,
        quota_brain: Optional[QuotaBrain] = None,
    ) -> None:
        self.pool = pool or IntelligencePool(provider_registry=provider_registry)
        self.telemetry = telemetry or RoutingTelemetry()
        self.provider_registry = provider_registry or self.pool.provider_registry
        self.quota_brain = quota_brain or QuotaBrain()

    def _marginal_cost(self, endpoint: IntelligenceEndpoint, request: RouteRequest) -> Optional[float]:
        if endpoint.already_paid or endpoint.source_kind in {"subscription", "local"}:
            return 0.0
        if endpoint.provider_id is None or endpoint.model_id is None:
            return None
        try:
            model = self.provider_registry.model(endpoint.provider_id, endpoint.model_id)
        except Exception:
            return None
        if model.pricing is None:
            return None
        usage = {
            "prompt_tokens": request.estimated_input_tokens,
            "completion_tokens": request.estimated_output_tokens,
        }
        return float(model.pricing.estimate(usage))

    @staticmethod
    def _risk_requires_proven_quality(risk: RiskLevel) -> bool:
        return risk.rank >= RiskLevel.HIGH.rank

    def _candidate(self, endpoint: IntelligenceEndpoint, request: RouteRequest) -> RouteCandidate:
        reasons: list[str] = []
        eligible = True
        score = 0.0
        performance = self.telemetry.performance(endpoint.endpoint_id, request.task_class)
        cost = self._marginal_cost(endpoint, request)

        if not endpoint.enabled:
            eligible = False
            reasons.append("endpoint_disabled")
        if endpoint.endpoint_id in request.exclude_endpoint_ids:
            eligible = False
            reasons.append("explicitly_excluded")
        if not endpoint.supports(request.required_capabilities):
            eligible = False
            reasons.append("missing_required_capability")
        if endpoint.roles and request.role not in endpoint.roles:
            # Explicit roles are a user configuration boundary, not a quality guess.
            eligible = False
            reasons.append("role_not_assigned")

        quota = self.quota_brain.effective(endpoint)
        if quota.remaining_fraction is not None:
            if quota.remaining_fraction <= 0:
                eligible = False
                reasons.append("quota_exhausted")
            elif quota.remaining_fraction < request.reserve_quota_fraction:
                # Preserve scarce subscription capacity for explicit/critical use.
                score -= 35.0
                reasons.append("quota_below_reserve")
            else:
                score += min(10.0, quota.remaining_fraction * 10.0)
                reasons.append("quota_available")

        proven = performance.samples >= request.minimum_verified_samples
        if proven and performance.success_rate is not None:
            score += performance.success_rate * 100.0
            reasons.append(f"verified_success_rate:{performance.success_rate:.3f}/{performance.samples}")
            if performance.success_rate < request.minimum_verified_success_rate:
                score -= 50.0
                reasons.append("verified_quality_below_floor")
                if self._risk_requires_proven_quality(request.risk_level):
                    eligible = False
        elif self._risk_requires_proven_quality(request.risk_level):
            # High-risk automatic routing never invents quality. Endpoint role metadata
            # is often populated automatically by discovery, so only a real explicit
            # preference may authorize first-use exploration. Model-authored advisory
            # preferences never become that authorization.
            explicit = (
                endpoint.endpoint_id == request.preferred_endpoint_id
                and not request.preferred_is_advisory
            )
            if not explicit:
                eligible = False
                reasons.append("high_risk_requires_verified_or_explicit_route")
            else:
                reasons.append("high_risk_explicit_first_use")
        else:
            reasons.append("verified_quality_unavailable")

        if endpoint.already_paid or endpoint.source_kind == "subscription":
            score += 22.0 if request.prefer_already_paid else 3.0
            reasons.append("already_paid_capacity")
        elif endpoint.source_kind == "local":
            score += 18.0 if request.prefer_already_paid else 3.0
            reasons.append("local_zero_marginal_cost")

        if cost is not None:
            # Cost matters only after eligibility/quality. Smooth bounded preference,
            # never enough to turn a failed quality floor into a win.
            score += max(-20.0, 12.0 - min(32.0, cost * 8.0))
            reasons.append(f"estimated_marginal_cost_usd:{cost:.6f}")
        else:
            reasons.append("marginal_cost_unknown")

        if performance.average_latency_ms is not None:
            score += max(-8.0, 5.0 - performance.average_latency_ms / 10_000.0)
            reasons.append(f"measured_latency_ms:{performance.average_latency_ms:.1f}")

        if endpoint.endpoint_id == request.preferred_endpoint_id:
            if request.preferred_is_advisory:
                # Orchestrator advice is useful as a tie-breaker, not an authority
                # capable of overriding verified quality, risk or quota policy.
                score += 8.0
                reasons.append("advisory_preference")
            else:
                score += 1_000.0
                reasons.append("explicit_preference")

        return RouteCandidate(
            endpoint=endpoint,
            performance=performance,
            estimated_marginal_cost_usd=cost,
            score=score,
            eligible=eligible,
            reasons=tuple(reasons),
        )

    def decide(self, request: RouteRequest) -> RouteDecision:
        candidates = tuple(self._candidate(endpoint, request) for endpoint in self.pool.list(include_disabled=True))
        eligible = [item for item in candidates if item.eligible]
        if not eligible:
            raise RoutingError(
                "no intelligence endpoint satisfies the requested role/capabilities/risk/quota policy"
            )
        eligible.sort(key=lambda item: (-item.score, item.endpoint.endpoint_id))
        winner = eligible[0]
        verified_used = winner.performance.samples >= request.minimum_verified_samples
        return RouteDecision(
            endpoint=winner.endpoint,
            reasons=winner.reasons,
            candidates=tuple(sorted(candidates, key=lambda item: (-item.score, item.endpoint.endpoint_id))),
            verified_quality_used=verified_used,
        )


__all__ = [
    "EndpointPerformance",
    "RouteCandidate",
    "RouteDecision",
    "RouteRequest",
    "RoutingError",
    "RoutingObservation",
    "RoutingTelemetry",
    "TASK_ARCHITECTURE",
    "TASK_CLASSES",
    "TASK_DISCOVERY",
    "TASK_GENERAL",
    "TASK_IMPLEMENTATION",
    "TASK_REVIEW",
    "TASK_SECURITY",
    "TASK_SUMMARY",
    "TASK_TESTING",
    "TASK_UI",
    "VerifiedSmartRouter",
]
