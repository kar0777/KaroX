"""Cache-scheduler wiring: a production kernel turn measures and reports.

The unit suite proves the scheduler's logic; this suite proves the seam:
AgentKernel drives the scheduler on every step, feeds provider-reported
cached counts back as capability evidence, and surfaces economy_cache_*
counters in the session usage view. Money appears only when a pricing
registry actually knows the rates -- absent registry, absent field.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from _support import SRC, initialize_git_repository  # noqa: F401
from karox.agent import AgentKernel, AgentLimits, SYSTEM_PROMPT
from karox.cache_scheduler import CacheDecision
from karox.core import CoreRuntime
from karox.models import AccessProfile, Capability, Origin, OriginKind
from karox.policy import CapabilityPolicy
from karox.provider_pricing import PricingRegistry
from karox.providers import ModelRequest, ModelResponse, ToolCall
from karox.sessions import SessionStore


class _Provider:
    provider_name = "test_provider"

    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("provider received an unexpected request")
        return self.responses.pop(0)


def _call(call_id: str, name: str, arguments: object) -> ToolCall:
    return ToolCall(call_id, name, json.dumps(arguments, ensure_ascii=False))


def _response(
    *calls: ToolCall,
    content: str | None = None,
    cached: int = 0,
) -> ModelResponse:
    usage: dict[str, object] = {"prompt_tokens": 20, "completion_tokens": 1}
    if cached:
        usage["cache_read_tokens"] = cached
    return ModelResponse(
        content=content,
        tool_calls=tuple(calls),
        finish_reason="tool_calls" if calls else "stop",
        usage=usage,
        response_id=None,
    )


def _registry() -> PricingRegistry:
    return PricingRegistry.from_document(
        {
            "models": [
                {
                    "provider": "test_provider",
                    "model": "test-model",
                    "provenance": {
                        "source": "test-document",
                        "recorded_at": "2026-08-22",
                    },
                    "input_per_mtok": 10.0,
                    "cached_input_per_mtok": 1.0,
                    "cache_write_per_mtok": 12.5,
                    "output_per_mtok": 30.0,
                }
            ]
        }
    )


class CacheSchedulerWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        initialize_git_repository(self.repository)
        (self.repository / "sample.txt").write_text("before\n", encoding="utf-8")
        self.sessions = SessionStore(self.root / "sessions")
        self.sessions.create(
            self.repository,
            "what does sample.txt contain?",
            AccessProfile.WORKSPACE_WRITE,
            session_id="session",
        )
        self.origin = Origin(OriginKind.NATIVE_AGENT, "test-agent")
        self.policy = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
        self.policy.set_grants(
            self.origin,
            {
                Capability.REPO_READ,
                Capability.REPO_WRITE,
                Capability.PROCESS_RUN,
                Capability.CHECKS_RUN,
                Capability.GIT_READ,
            },
        )
        self.core = CoreRuntime(
            self.repository,
            self.policy,
            self.sessions,
            self.root / "audit.jsonl",
            verification_commands=[[sys.executable, "-c", "print('ok')"]],
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run(
        self, *, registry: PricingRegistry | None, cached_second: int = 0
    ) -> tuple[AgentKernel, _Provider]:
        provider = _Provider(
            [
                _response(_call("read-1", "repo_read_file", {"path": "sample.txt"})),
                _response(content="it says before", cached=cached_second),
            ]
        )
        kernel = AgentKernel(
            provider=provider,
            model="test-model",
            core=self.core,
            sessions=self.sessions,
            origin=self.origin,
            limits=AgentLimits(max_seconds=30),
            system_prompt=SYSTEM_PROMPT,
            pricing_registry=registry,
        )
        kernel.run("session")
        return kernel, provider

    def test_every_step_gets_a_decision_and_second_step_reuses(self) -> None:
        kernel, provider = self._run(registry=None)
        self.assertEqual(len(provider.requests), 2)
        decision = kernel._cache_decision
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.verdict, CacheDecision.VERDICT_REUSE)
        self.assertEqual(kernel._cache_scheduler.decisions, 2)
        self.assertEqual(kernel._cache_scheduler.invalidations, 0)

    def test_usage_view_carries_cache_counters(self) -> None:
        self._run(registry=None)
        record = self.sessions.load("session")
        usage = record.usage if isinstance(record.usage, dict) else {}
        rendered = json.dumps(usage, ensure_ascii=False, sort_keys=True)
        self.assertIn("economy_cache_verdict", rendered)
        self.assertIn("economy_cache_prefix_sha", rendered)
        self.assertIn("economy_cache_capability", rendered)

    def test_capability_stays_unknown_without_provider_evidence(self) -> None:
        kernel, _ = self._run(registry=None)
        self.assertIsNone(
            kernel._cache_scheduler.capability.supports_prompt_caching
        )
        self.assertEqual(kernel._cache_scheduler.capability.label(), "UNKNOWN")

    def test_provider_reported_reads_confirm_capability(self) -> None:
        kernel, _ = self._run(registry=None, cached_second=500)
        capability = kernel._cache_scheduler.capability
        self.assertTrue(capability.supports_prompt_caching)
        self.assertTrue(capability.reports_cache_reads)
        self.assertIsNone(capability.reports_cache_writes)
        self.assertEqual(kernel._cache_scheduler.cache_read_tokens_total, 500)

    def test_no_registry_means_no_money_field(self) -> None:
        self._run(registry=None, cached_second=500)
        record = self.sessions.load("session")
        rendered = json.dumps(record.usage, ensure_ascii=False)
        self.assertNotIn("economy_cache_saving_estimated_usd", rendered)

    def test_full_rates_and_measured_reads_yield_estimate(self) -> None:
        kernel, _ = self._run(registry=_registry(), cached_second=1_000_000)
        saving = kernel._cache_scheduler.estimated_reuse_saving_usd()
        self.assertIsNotNone(saving)
        assert saving is not None
        self.assertAlmostEqual(saving, 9.0)
        record = self.sessions.load("session")
        rendered = json.dumps(record.usage, ensure_ascii=False)
        self.assertIn("economy_cache_saving_estimated_usd", rendered)

    def test_registry_without_reads_still_shows_no_money(self) -> None:
        kernel, _ = self._run(registry=_registry(), cached_second=0)
        self.assertIsNone(kernel._cache_scheduler.estimated_reuse_saving_usd())
        record = self.sessions.load("session")
        rendered = json.dumps(record.usage, ensure_ascii=False)
        self.assertNotIn("economy_cache_saving_estimated_usd", rendered)


if __name__ == "__main__":
    unittest.main()
