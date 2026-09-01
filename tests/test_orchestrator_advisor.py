from __future__ import annotations

from pathlib import Path

import pytest

from karox.intelligence_pool import (
    CAP_REASONING,
    SOURCE_API,
    SOURCE_SUBSCRIPTION,
    IntelligenceEndpoint,
)
from karox.orchestrator import WorkerExecutionResult
from karox.orchestrator_advisor import OrchestratorAdviceError, OrchestratorAdvisor
from karox.providers import ModelResponse, ToolCall
from karox.registry import ModelRecord, ProviderRecord, ProviderRegistry
from karox.subscription_cli import TARGET_CODEX


class _Provider:
    def __init__(self, response: ModelResponse) -> None:
        self.response = response
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        return self.response


class _ProviderFactory:
    def __init__(self, provider: _Provider) -> None:
        self.provider = provider

    def create(self, _record):
        return self.provider


class _SubscriptionExecutor:
    def __init__(self, result: WorkerExecutionResult) -> None:
        self.result = result
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return self.result


def _registry(tmp_path: Path) -> ProviderRegistry:
    registry = ProviderRegistry(tmp_path / "providers.json")
    registry.put_provider(
        ProviderRecord(
            provider_id="openai",
            adapter_kind="openai_responses",
            base_url="https://api.example.test/v1",
        )
    )
    registry.put_model(
        ModelRecord(
            provider_id="openai",
            model_id="orchestrator",
            tools="true",
            structured_output="true",
            max_output_tokens=8192,
        )
    )
    return registry


def _api_endpoint() -> IntelligenceEndpoint:
    return IntelligenceEndpoint(
        endpoint_id="api:openai:orchestrator",
        display_name="API orchestrator",
        source_kind=SOURCE_API,
        capabilities=(CAP_REASONING,),
        roles=("orchestrator",),
        provider_id="openai",
        model_id="orchestrator",
    )


def _subscription_endpoint() -> IntelligenceEndpoint:
    return IntelligenceEndpoint(
        endpoint_id="sub:codex",
        display_name="Codex subscription",
        source_kind=SOURCE_SUBSCRIPTION,
        capabilities=(CAP_REASONING,),
        roles=("orchestrator",),
        target_id=TARGET_CODEX,
        already_paid=True,
    )


def test_api_advisor_is_one_toolless_request_and_parses_strict_json(tmp_path: Path) -> None:
    provider = _Provider(
        ModelResponse(
            content='{"assignments":{"implementer":"sub:codex"},"effort_assignments":{"reviewer":"high"},"rationale":"Use paid capacity."}',
            tool_calls=(),
            finish_reason="stop",
            usage={"input_tokens": 100, "output_tokens": 20},
            cost=0.01,
        )
    )
    advisor = OrchestratorAdvisor(
        tmp_path,
        provider_registry=_registry(tmp_path),
        provider_factory=_ProviderFactory(provider),
    )
    result = advisor.generate(_api_endpoint(), prompt="choose workers", effort_level="high")
    assert result.proposal.assignments == {"implementer": "sub:codex"}
    assert result.total_tokens == 120
    assert result.cost_usd == 0.01
    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert request.tools == ()
    assert request.temperature is None
    assert request.cache_key == "karox-orchestrator-delegation-v1"
    assert len(request.messages) == 2


def test_api_advisor_rejects_tool_calls_even_when_content_is_json(tmp_path: Path) -> None:
    provider = _Provider(
        ModelResponse(
            content='{"assignments":{}}',
            tool_calls=(ToolCall("call-1", "repo.read", "{}"),),
            finish_reason="tool_calls",
            usage={},
        )
    )
    advisor = OrchestratorAdvisor(
        tmp_path,
        provider_registry=_registry(tmp_path),
        provider_factory=_ProviderFactory(provider),
    )
    with pytest.raises(OrchestratorAdviceError, match="tool calls are forbidden"):
        advisor.generate(_api_endpoint(), prompt="choose workers")


def test_api_advisor_rejects_markdown_wrapped_json(tmp_path: Path) -> None:
    provider = _Provider(
        ModelResponse(
            content='```json\n{"assignments":{}}\n```',
            tool_calls=(),
            finish_reason="stop",
            usage={},
        )
    )
    advisor = OrchestratorAdvisor(
        tmp_path,
        provider_registry=_registry(tmp_path),
        provider_factory=_ProviderFactory(provider),
    )
    with pytest.raises(OrchestratorAdviceError, match="invalid delegation JSON"):
        advisor.generate(_api_endpoint(), prompt="choose workers")


def test_subscription_advisor_reuses_guarded_read_only_executor(tmp_path: Path) -> None:
    executor = _SubscriptionExecutor(
        WorkerExecutionResult(
            ok=True,
            summary='{"assignments":{"reviewer":"api:openai:reviewer"},"rationale":"Independent provider."}',
            accepted=True,
            verified=True,
            total_tokens=44,
            latency_ms=12.5,
        )
    )
    advisor = OrchestratorAdvisor(
        tmp_path,
        provider_registry=_registry(tmp_path),
        subscription_factory=lambda _repo: executor,  # type: ignore[arg-type]
    )
    result = advisor.generate(
        _subscription_endpoint(), prompt="choose workers", effort_level="extra-high"
    )
    assert result.proposal.assignments == {"reviewer": "api:openai:reviewer"}
    assert result.total_tokens == 44
    assert len(executor.requests) == 1
    request = executor.requests[0]
    assert request.role == "orchestrator"
    assert request.workspace_path is None
    assert request.context_delta.changed == ()
    assert request.effort_level == "extra-high"


def test_subscription_advisor_rejects_failed_executor(tmp_path: Path) -> None:
    executor = _SubscriptionExecutor(
        WorkerExecutionResult(
            ok=False,
            summary="failed",
            accepted=False,
            verified=False,
        )
    )
    advisor = OrchestratorAdvisor(
        tmp_path,
        provider_registry=_registry(tmp_path),
        subscription_factory=lambda _repo: executor,  # type: ignore[arg-type]
    )
    with pytest.raises(OrchestratorAdviceError, match="failed to produce"):
        advisor.generate(_subscription_endpoint(), prompt="choose workers")
