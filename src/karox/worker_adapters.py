"""Guarded worker adapter contracts for KaroX orchestration."""

from __future__ import annotations

import dataclasses
from typing import Any, Callable, Mapping, Optional, Protocol

from .agent import AgentKernel, AgentReport
from .agent_protocol import EvidenceReference, ReviewFinding
from .orchestrator import WorkerExecutionRequest, WorkerExecutionResult


class WorkerAdapterError(RuntimeError):
    pass


def _usage_int(usage: Mapping[str, Any], *names: str) -> int:
    for name in names:
        value = usage.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return 0


def _usage_cache_savings_usd(usage: Mapping[str, Any]) -> Optional[float]:
    events = usage.get("events")
    if not isinstance(events, list):
        return None
    total = 0.0
    saw_value = False
    for event in events:
        if not isinstance(event, Mapping):
            continue
        value = event.get("cache_savings")
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            continue
        if event.get("currency") != "USD":
            return None
        total += float(value)
        saw_value = True
    return round(total, 12) if saw_value else None


def _usage_cost(usage: Mapping[str, Any]) -> float:
    costs = usage.get("costs")
    if not isinstance(costs, Mapping):
        return 0.0
    values = [float(value) for value in costs.values() if isinstance(value, (int, float)) and not isinstance(value, bool)]
    # A worker result exposes one scalar for the run budget. Multiple currencies
    # cannot honestly be summed; return zero and let the session Usage screen
    # retain the authoritative per-currency accounting.
    return values[0] if len(values) == 1 and values[0] >= 0 else 0.0


class AgentKernelFactory(Protocol):
    def __call__(self, request: WorkerExecutionRequest) -> tuple[AgentKernel, str]: ...


class NativeAgentExecutor:
    """Adapt the existing KaroX AgentKernel to an orchestration worker.

    The factory owns Core/session construction and therefore all existing policy,
    repository leases, verification allowlists and provider credentials.
    """

    # Native orchestration uses a step-scoped durable session id. Different DAG
    # steps routed to the same endpoint do not share a provider conversation.
    reuses_context_between_requests = False

    def __init__(self, factory: AgentKernelFactory) -> None:
        self.factory = factory

    def __call__(self, request: WorkerExecutionRequest) -> WorkerExecutionResult:
        kernel, session_id = self.factory(request)
        report: AgentReport = kernel.run(session_id)
        prompt_tokens = _usage_int(report.usage, "prompt_tokens", "input_tokens")
        completion_tokens = _usage_int(
            report.usage, "completion_tokens", "output_tokens"
        )
        cache_read_tokens = _usage_int(report.usage, "cache_read_tokens")
        cache_write_tokens = _usage_int(report.usage, "cache_write_tokens")
        cache_metrics_reported = (
            "cache_read_tokens" in report.usage or "cache_write_tokens" in report.usage
        )
        total_tokens = _usage_int(report.usage, "total_tokens")
        if total_tokens <= 0:
            total_tokens = prompt_tokens + completion_tokens
        evidence: list[EvidenceReference] = []
        for item in report.evidence:
            if not isinstance(item, Mapping):
                continue
            evidence_id = item.get("evidence_id") or item.get("id")
            if not isinstance(evidence_id, str) or not evidence_id:
                continue
            evidence.append(
                EvidenceReference(
                    artifact_id=evidence_id,
                    kind=str(item.get("kind") or "agent_evidence"),
                    summary=str(item.get("summary") or "")[:500],
                )
            )
        accepted = report.status in {"completed", "answered"} and report.reason not in {"failed", "blocked"}
        return WorkerExecutionResult(
            ok=accepted,
            summary=report.provider_message or report.reason or report.status,
            accepted=accepted,
            verified=bool(report.verified),
            cost_usd=_usage_cost(report.usage),
            total_tokens=total_tokens,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            cache_metrics_reported=cache_metrics_reported,
            cache_savings_usd=_usage_cache_savings_usd(report.usage),
            changeset_ref=None,
            evidence=tuple(evidence),
        )


@dataclasses.dataclass(frozen=True)
class PromptTargetResult:
    ok: bool
    summary: str
    accepted: bool
    verified: bool
    cost_usd: float = 0.0
    total_tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cache_metrics_reported: bool = False
    cache_savings_usd: Optional[float] = None
    latency_ms: float = 0.0
    changeset_ref: Optional[str] = None
    evidence: tuple[EvidenceReference, ...] = ()
    findings: tuple[ReviewFinding, ...] = ()
    context_updates: tuple[dict[str, Any], ...] = ()


class PromptTarget(Protocol):
    def __call__(
        self,
        *,
        endpoint_id: str,
        role: str,
        objective: str,
        context: Mapping[str, Any],
        upstream: tuple[Mapping[str, Any], ...],
        idempotency_key: str,
        workspace_path: Optional[str],
    ) -> PromptTargetResult: ...


class GuardedPromptExecutor:
    """Adapter for an already-guarded MCP/CLI/desktop subscription target.

    ``target`` must itself be a KaroX-controlled transport. This class does not
    accept argv, shell strings, browser cookies or credentials, so it cannot be
    used to bypass the control plane accidentally.
    """

    # Generic guarded targets are stateless unless a dedicated adapter proves
    # otherwise. Never infer remote memory merely from endpoint identity.
    reuses_context_between_requests = False

    def __init__(self, target: PromptTarget) -> None:
        self.target = target

    def __call__(self, request: WorkerExecutionRequest) -> WorkerExecutionResult:
        context = request.context_delta.to_dict(include_content=True)
        context["orchestration"] = {
            "effort_level": request.effort_level,
            "max_cost_usd": request.max_cost_usd,
            "step_id": request.step_id,
        }
        upstream = tuple(message.to_dict() for message in request.upstream_messages)
        result = self.target(
            endpoint_id=request.endpoint.endpoint_id,
            role=request.role,
            objective=request.objective,
            context=context,
            upstream=upstream,
            idempotency_key=request.idempotency_key,
            workspace_path=request.workspace_path,
        )
        return WorkerExecutionResult(
            ok=result.ok,
            summary=result.summary,
            accepted=result.accepted,
            verified=result.verified,
            cost_usd=result.cost_usd,
            total_tokens=result.total_tokens,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            cache_read_tokens=result.cache_read_tokens,
            cache_write_tokens=result.cache_write_tokens,
            cache_metrics_reported=result.cache_metrics_reported,
            cache_savings_usd=result.cache_savings_usd,
            latency_ms=result.latency_ms,
            changeset_ref=result.changeset_ref,
            evidence=result.evidence,
            findings=result.findings,
            context_updates=result.context_updates,
        )


class CallbackWorkerExecutor:
    """Minimal adapter for local deterministic workers and tests."""

    def __init__(
        self,
        callback: Callable[[WorkerExecutionRequest], WorkerExecutionResult],
        *,
        reuses_context_between_requests: bool = False,
    ) -> None:
        self.callback = callback
        self.reuses_context_between_requests = bool(reuses_context_between_requests)

    def __call__(self, request: WorkerExecutionRequest) -> WorkerExecutionResult:
        result = self.callback(request)
        if not isinstance(result, WorkerExecutionResult):
            raise WorkerAdapterError("worker callback must return WorkerExecutionResult")
        return result


__all__ = [
    "AgentKernelFactory",
    "CallbackWorkerExecutor",
    "GuardedPromptExecutor",
    "NativeAgentExecutor",
    "PromptTarget",
    "PromptTargetResult",
    "WorkerAdapterError",
]
