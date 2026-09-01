from __future__ import annotations

from pathlib import Path

from karox.agent import AgentReport
from karox.context_bus import ContextBus
from karox.intelligence_pool import CAP_REASONING, IntelligenceEndpoint
from karox.orchestrator import WorkerExecutionRequest
from karox.worker_adapters import NativeAgentExecutor


class _Kernel:
    def __init__(self, report: AgentReport) -> None:
        self.report = report

    def run(self, session_id: str) -> AgentReport:
        assert session_id == "native-session"
        return self.report


def _request(tmp_path: Path) -> WorkerExecutionRequest:
    endpoint = IntelligenceEndpoint(
        endpoint_id="api:test:model",
        display_name="Test API model",
        source_kind="api",
        capabilities=(CAP_REASONING,),
        roles=("reviewer",),
        provider_id="test",
        model_id="model",
    )
    bus = ContextBus("worker-usage", path=tmp_path / "context.json")
    bus.put_text(
        item_id="task",
        kind="task",
        content="Review the change",
        priority=100,
    )
    return WorkerExecutionRequest(
        run_id="run-worker-usage",
        task_id="task-worker-usage",
        step_id="review",
        idempotency_key="run-worker-usage:review:1",
        role="reviewer",
        task_class="review",
        objective="Review the change",
        endpoint=endpoint,
        context_delta=bus.delta(role="reviewer", known_hashes={}),
        upstream_messages=(),
        max_cost_usd=2.0,
    )


def _report(usage: dict) -> AgentReport:
    return AgentReport(
        session_id="native-session",
        status="completed",
        phase="done",
        verified=True,
        reason="completed",
        steps=1,
        changed_files=(),
        checks=(),
        git_state={},
        evidence=(),
        usage=usage,
        provider_message="review complete",
    )


def test_native_worker_preserves_provider_reported_cache_usage(tmp_path: Path) -> None:
    report = _report(
        {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "cache_read_tokens": 80,
            "cache_write_tokens": 5,
            "costs": {"USD": 0.25},
            "events": [
                {
                    "cache_savings": 0.1,
                    "currency": "USD",
                }
            ],
        }
    )
    executor = NativeAgentExecutor(lambda _request: (_Kernel(report), "native-session"))

    result = executor(_request(tmp_path))

    assert result.ok and result.accepted and result.verified
    assert result.prompt_tokens == 100
    assert result.completion_tokens == 20
    assert result.total_tokens == 120
    assert result.cache_read_tokens == 80
    assert result.cache_write_tokens == 5
    assert result.cache_metrics_reported is True
    assert result.cache_savings_usd == 0.1
    assert result.cost_usd == 0.25


def test_native_worker_does_not_invent_cache_metrics(tmp_path: Path) -> None:
    report = _report(
        {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "costs": {"USD": 0.25},
            "events": [],
        }
    )
    executor = NativeAgentExecutor(lambda _request: (_Kernel(report), "native-session"))

    result = executor(_request(tmp_path))

    assert result.cache_read_tokens == 0
    assert result.cache_write_tokens == 0
    assert result.cache_metrics_reported is False
    assert result.cache_savings_usd is None
