"""Passive Shadow Economy observations for KaroX.

Shadow mode never changes the actual route. It records what happened, asks the
verified router what it would have chosen, and stores only the cost comparison
that deterministic current pricing can support. Quality remains the actual
accepted/verified outcome; a counterfactual success is never invented.
"""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from .economy_engine import ShadowRouteReport, shadow_route
from .orchestration_routing import RouteRequest, VerifiedSmartRouter
from .paths import runtime_dir


_SCHEMA_VERSION = 1


class ShadowEconomyError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class ShadowEconomyEvent:
    task_id: str
    actual_endpoint_id: str
    task_class: str
    accepted: bool
    verified: bool
    actual_cost_usd: Optional[float]
    route_report: ShadowRouteReport
    timestamp: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "actual_endpoint_id": self.actual_endpoint_id,
            "task_class": self.task_class,
            "accepted": self.accepted,
            "verified": self.verified,
            "actual_cost_usd": self.actual_cost_usd,
            "route_report": self.route_report.to_dict(),
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ShadowEconomyEvent":
        report = value.get("route_report")
        if not isinstance(report, Mapping):
            raise ValueError("shadow event requires route_report")
        return cls(
            task_id=str(value["task_id"]),
            actual_endpoint_id=str(value["actual_endpoint_id"]),
            task_class=str(value["task_class"]),
            accepted=bool(value["accepted"]),
            verified=bool(value["verified"]),
            actual_cost_usd=(
                float(value["actual_cost_usd"])
                if value.get("actual_cost_usd") is not None
                else None
            ),
            route_report=ShadowRouteReport(
                actual_endpoint_id=str(report["actual_endpoint_id"]),
                recommended_endpoint_id=str(report["recommended_endpoint_id"]),
                changed=bool(report["changed"]),
                actual_estimated_cost_usd=(
                    float(report["actual_estimated_cost_usd"])
                    if report.get("actual_estimated_cost_usd") is not None
                    else None
                ),
                recommended_estimated_cost_usd=(
                    float(report["recommended_estimated_cost_usd"])
                    if report.get("recommended_estimated_cost_usd") is not None
                    else None
                ),
                projected_cost_delta_usd=(
                    float(report["projected_cost_delta_usd"])
                    if report.get("projected_cost_delta_usd") is not None
                    else None
                ),
                recommendation_uses_verified_quality=bool(
                    report.get("recommendation_uses_verified_quality", False)
                ),
                reasons=tuple(str(item) for item in report.get("reasons", [])),
            ),
            timestamp=float(value.get("timestamp", 0.0)),
        )


@dataclasses.dataclass(frozen=True)
class ShadowEconomySummary:
    events: int
    accepted_verified_events: int
    route_changes: int
    comparable_cost_events: int
    projected_avoidable_cost_usd: float
    verified_quality_recommendations: int

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class ShadowEconomyLedger:
    def __init__(self, path: Optional[Path] = None, *, limit: int = 5000) -> None:
        self.path = (path or (runtime_dir() / "vnext" / "shadow-economy.json")).expanduser().resolve()
        if limit <= 0:
            raise ValueError("shadow economy event limit must be positive")
        self.limit = int(limit)

    def _load(self) -> list[ShadowEconomyEvent]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ShadowEconomyError(f"cannot read shadow economy ledger: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != _SCHEMA_VERSION:
            raise ShadowEconomyError("shadow economy ledger has unsupported schema")
        raw = payload.get("events")
        if not isinstance(raw, list):
            raise ShadowEconomyError("shadow economy events must be an array")
        try:
            return [ShadowEconomyEvent.from_dict(item) for item in raw if isinstance(item, Mapping)]
        except (KeyError, TypeError, ValueError) as exc:
            raise ShadowEconomyError(f"shadow economy ledger is invalid: {exc}") from exc

    def _save(self, events: list[ShadowEconomyEvent]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        events = events[-self.limit :]
        fd, raw = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        temp = Path(raw)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(
                    {
                        "schema_version": _SCHEMA_VERSION,
                        "events": [item.to_dict() for item in events],
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

    def observe(
        self,
        *,
        router: VerifiedSmartRouter,
        task_id: str,
        request: RouteRequest,
        actual_endpoint_id: str,
        accepted: bool,
        verified: bool,
        actual_cost_usd: Optional[float] = None,
        timestamp: Optional[float] = None,
    ) -> ShadowEconomyEvent:
        if actual_cost_usd is not None and actual_cost_usd < 0:
            raise ValueError("actual shadow-mode cost must be non-negative")
        report = shadow_route(
            router=router,
            request=request,
            actual_endpoint_id=actual_endpoint_id,
        )
        event = ShadowEconomyEvent(
            task_id=task_id,
            actual_endpoint_id=actual_endpoint_id,
            task_class=request.task_class,
            accepted=accepted,
            verified=verified,
            actual_cost_usd=actual_cost_usd,
            route_report=report,
            timestamp=time.time() if timestamp is None else timestamp,
        )
        events = self._load()
        events.append(event)
        self._save(events)
        return event

    def list(self) -> list[ShadowEconomyEvent]:
        return self._load()

    def summary(self, events: Optional[Iterable[ShadowEconomyEvent]] = None) -> ShadowEconomySummary:
        rows = tuple(self._load() if events is None else events)
        deltas = [
            row.route_report.projected_cost_delta_usd
            for row in rows
            if row.route_report.projected_cost_delta_usd is not None
            and row.route_report.projected_cost_delta_usd > 0
        ]
        return ShadowEconomySummary(
            events=len(rows),
            accepted_verified_events=sum(1 for row in rows if row.accepted and row.verified),
            route_changes=sum(1 for row in rows if row.route_report.changed),
            comparable_cost_events=sum(
                1 for row in rows if row.route_report.projected_cost_delta_usd is not None
            ),
            projected_avoidable_cost_usd=round(sum(deltas), 12),
            verified_quality_recommendations=sum(
                1 for row in rows if row.route_report.recommendation_uses_verified_quality
            ),
        )


__all__ = [
    "ShadowEconomyError",
    "ShadowEconomyEvent",
    "ShadowEconomyLedger",
    "ShadowEconomySummary",
]
