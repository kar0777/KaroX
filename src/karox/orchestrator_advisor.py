"""One-shot guarded worker-selection advice for KaroX orchestration.

The selected orchestrator should be able to choose workers without becoming a
second execution authority.  This module deliberately keeps that turn tiny:
API endpoints receive one tool-less provider request containing only the
secret-free delegation catalog; supported subscription CLIs reuse their existing
read-only KaroX adapter.  The resulting JSON is still advisory and must be
validated by :mod:`karox.orchestrator_delegation` before it can affect a plan.
"""

from __future__ import annotations

import dataclasses
import hashlib
import time
from pathlib import Path
from typing import Callable, Optional

from .context_bus import ContextDelta
from .effort import budget_for, normalize_effort
from .intelligence_pool import SOURCE_API, SOURCE_SUBSCRIPTION, IntelligenceEndpoint
from .orchestrator import WorkerExecutionRequest
from .orchestrator_delegation import DelegationProposal, DelegationProposalError
from .paths import config_dir
from .provider_factory import ProviderFactory
from .providers import ModelMessage, ModelRequest, ModelResponse
from .registry import ProviderRegistry
from .subscription_cli import SubscriptionCliExecutor


class OrchestratorAdviceError(RuntimeError):
    """The selected orchestrator could not produce a safe delegation proposal."""


@dataclasses.dataclass(frozen=True)
class OrchestratorAdvice:
    endpoint_id: str
    source_kind: str
    proposal: DelegationProposal
    total_tokens: int = 0
    cost_usd: Optional[float] = None
    latency_ms: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "endpoint_id": self.endpoint_id,
            "source_kind": self.source_kind,
            "proposal": self.proposal.to_dict(),
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "latency_ms": round(max(0.0, self.latency_ms), 3),
        }


def _usage_total(response: ModelResponse) -> int:
    usage = response.usage
    total = usage.get("total_tokens")
    if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
        return total
    values = 0
    for key in ("input_tokens", "prompt_tokens", "output_tokens", "completion_tokens"):
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            values += value
    return values


def _parse(endpoint: IntelligenceEndpoint, text: str) -> DelegationProposal:
    if not isinstance(text, str) or not text.strip():
        raise OrchestratorAdviceError(
            f"orchestrator {endpoint.endpoint_id} returned no delegation JSON"
        )
    if len(text) > 20_000:
        raise OrchestratorAdviceError("orchestrator delegation response is too large")
    try:
        return DelegationProposal.parse(text.strip())
    except DelegationProposalError as exc:
        raise OrchestratorAdviceError(
            f"orchestrator {endpoint.endpoint_id} returned invalid delegation JSON: {exc}"
        ) from exc


class OrchestratorAdvisor:
    """Generate a proposal through one selected Intelligence Pool endpoint."""

    def __init__(
        self,
        repository: Path,
        *,
        provider_registry: Optional[ProviderRegistry] = None,
        provider_factory: Optional[ProviderFactory] = None,
        subscription_factory: Optional[
            Callable[[Path], SubscriptionCliExecutor]
        ] = None,
        deadline_seconds: float = 120.0,
    ) -> None:
        self.repository = repository.expanduser().resolve(strict=True)
        if not self.repository.is_dir():
            raise ValueError("orchestrator advisor repository must be a directory")
        if not 1.0 <= float(deadline_seconds) <= 600.0:
            raise ValueError("orchestrator advisor deadline must be between 1 and 600 seconds")
        self.deadline_seconds = float(deadline_seconds)
        self.registry = provider_registry or ProviderRegistry(
            config_dir() / "vnext" / "providers.json"
        )
        self.provider_factory = provider_factory or ProviderFactory()
        self.subscription_factory = subscription_factory

    def _api(
        self,
        endpoint: IntelligenceEndpoint,
        *,
        prompt: str,
        effort_level: str,
    ) -> OrchestratorAdvice:
        if endpoint.provider_id is None or endpoint.model_id is None:
            raise OrchestratorAdviceError(
                f"API orchestrator {endpoint.endpoint_id} has no provider/model identity"
            )
        provider_record = self.registry.provider(endpoint.provider_id)
        model_record = self.registry.model(endpoint.provider_id, endpoint.model_id)
        if not provider_record.enabled:
            raise OrchestratorAdviceError(
                f"orchestrator provider is disabled: {provider_record.provider_id}"
            )
        effort = budget_for(effort_level)
        max_output = model_record.max_output_tokens
        if max_output is None:
            max_output = 2048
        else:
            max_output = max(256, min(int(max_output), 4096))
        request = ModelRequest(
            model=model_record.model_id,
            messages=(
                ModelMessage(
                    role="system",
                    content=(
                        "You are KaroX's worker-selection advisor. Return only the strict JSON "
                        "object requested by the user message. Do not call tools, request secrets, "
                        "change permissions, or add prose/code fences. KaroX independently validates "
                        "every assignment before execution."
                    ),
                ),
                ModelMessage(role="user", content=prompt),
            ),
            tools=(),
            max_output_tokens=max_output,
            deadline_seconds=self.deadline_seconds,
            reasoning_effort=effort.reasoning_effort,
            cache_key="karox-orchestrator-delegation-v1",
        )
        started = time.monotonic()
        response = self.provider_factory.create(provider_record).complete(request)
        latency_ms = max(0.0, (time.monotonic() - started) * 1000.0)
        if response.tool_calls:
            raise OrchestratorAdviceError(
                "tool calls are forbidden in orchestrator delegation advice"
            )
        proposal = _parse(endpoint, response.content or "")
        return OrchestratorAdvice(
            endpoint_id=endpoint.endpoint_id,
            source_kind=endpoint.source_kind,
            proposal=proposal,
            total_tokens=_usage_total(response),
            cost_usd=response.cost,
            latency_ms=latency_ms,
        )

    def _subscription(
        self,
        endpoint: IntelligenceEndpoint,
        *,
        prompt: str,
        effort_level: str,
    ) -> OrchestratorAdvice:
        factory = self.subscription_factory
        if factory is None:
            # The normal executor requires a non-empty verification list even
            # though orchestrator/read roles never execute it. Keep the sentinel
            # deterministic and harmless; it is not invoked on this path.
            factory = lambda repository: SubscriptionCliExecutor(
                repository,
                verification_commands=(("python", "-m", "pytest", "-q"),),
                timeout_seconds=self.deadline_seconds,
            )
        executor = factory(self.repository)
        if not SubscriptionCliExecutor.supports(endpoint):
            raise OrchestratorAdviceError(
                f"subscription orchestrator has no guarded built-in adapter: {endpoint.endpoint_id}"
            )
        digest = hashlib.sha256(
            f"{endpoint.endpoint_id}\0{prompt}".encode("utf-8")
        ).hexdigest()
        request = WorkerExecutionRequest(
            run_id=f"advice-{digest[:24]}",
            task_id=f"advice-task-{digest[:24]}",
            step_id="orchestrator-delegation",
            idempotency_key=f"advice:{digest}",
            role="orchestrator",
            task_class="general",
            objective=prompt,
            endpoint=endpoint,
            context_delta=ContextDelta(
                role="orchestrator",
                changed=(),
                unchanged_ids=(),
                removed_ids=(),
                sent_chars=0,
                reused_chars=0,
            ),
            upstream_messages=(),
            max_cost_usd=0.0,
            effort_level=normalize_effort(effort_level),
            workspace_path=None,
        )
        result = executor(request)
        if not result.ok or not result.accepted:
            raise OrchestratorAdviceError(
                f"subscription orchestrator failed to produce delegation advice: {result.summary[:300]}"
            )
        proposal = _parse(endpoint, result.summary)
        return OrchestratorAdvice(
            endpoint_id=endpoint.endpoint_id,
            source_kind=endpoint.source_kind,
            proposal=proposal,
            total_tokens=max(0, result.total_tokens),
            cost_usd=max(0.0, result.cost_usd),
            latency_ms=max(0.0, result.latency_ms),
        )

    def generate(
        self,
        endpoint: IntelligenceEndpoint,
        *,
        prompt: str,
        effort_level: str = "high",
    ) -> OrchestratorAdvice:
        effort = normalize_effort(effort_level)
        if endpoint.source_kind == SOURCE_API:
            return self._api(endpoint, prompt=prompt, effort_level=effort)
        if endpoint.source_kind == SOURCE_SUBSCRIPTION:
            return self._subscription(endpoint, prompt=prompt, effort_level=effort)
        raise OrchestratorAdviceError(
            "automatic delegation currently requires an API or guarded subscription orchestrator"
        )


__all__ = [
    "OrchestratorAdvice",
    "OrchestratorAdviceError",
    "OrchestratorAdvisor",
]
