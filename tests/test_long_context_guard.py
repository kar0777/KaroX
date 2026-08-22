"""Long-context guard: advisory tiers from pricing, never a block.

Pins the Part 2 mandate: no model name hard-coded in core (the boundary
comes from the per-deployment pricing document), UNKNOWN without a tier,
reductions advised before the boundary, and every path allowed because
required evidence outranks the long-context premium.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from _support import SRC, initialize_git_repository  # noqa: F401
from karox.agent import AgentKernel, AgentLimits, SYSTEM_PROMPT
from karox.cost_intelligence import (
    CONTEXT_REDUCTIONS,
    ContextTierDecision,
    CostGovernor,
)
from karox.models import AccessProfile, Capability, Origin, OriginKind
from karox.policy import CapabilityPolicy
from karox.core import CoreRuntime
from karox.provider_pricing import (
    ModelPricing,
    PricingProvenance,
    PricingRegistry,
)
from karox.providers import ModelRequest, ModelResponse
from karox.sessions import SessionStore


def _pricing(**overrides: object) -> ModelPricing:
    values: dict[str, object] = {
        "provider": "p",
        "model": "m",
        "provenance": PricingProvenance(source="test", recorded_at="2026-08-22"),
    }
    values.update(overrides)
    return ModelPricing(**values)  # type: ignore[arg-type]


class EvaluateContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.governor = CostGovernor(shadow_mode=True)

    def test_no_pricing_is_unknown_and_allowed(self) -> None:
        decision = self.governor.evaluate_context(estimated_input_tokens=5_000)
        self.assertEqual(decision.tier, ContextTierDecision.TIER_UNKNOWN)
        self.assertTrue(decision.allowed)
        self.assertIsNone(decision.threshold_tokens)
        self.assertEqual(decision.suggested_reductions, ())

    def test_pricing_without_tier_stays_unknown(self) -> None:
        decision = self.governor.evaluate_context(
            estimated_input_tokens=5_000,
            pricing=_pricing(input_per_mtok=10.0),
        )
        self.assertEqual(decision.tier, ContextTierDecision.TIER_UNKNOWN)
        self.assertTrue(decision.allowed)

    def test_standard_below_approach_ratio(self) -> None:
        decision = self.governor.evaluate_context(
            estimated_input_tokens=79_999,
            pricing=_pricing(long_context_threshold_tokens=100_000),
        )
        self.assertEqual(decision.tier, ContextTierDecision.TIER_STANDARD)
        self.assertEqual(decision.suggested_reductions, ())
        self.assertTrue(decision.allowed)

    def test_approaching_at_eighty_percent_advises_reductions(self) -> None:
        decision = self.governor.evaluate_context(
            estimated_input_tokens=80_000,
            pricing=_pricing(long_context_threshold_tokens=100_000),
        )
        self.assertEqual(decision.tier, ContextTierDecision.TIER_APPROACHING)
        self.assertEqual(decision.suggested_reductions, CONTEXT_REDUCTIONS)
        self.assertTrue(decision.allowed)
        self.assertIn("context_compiler", decision.suggested_reductions)

    def test_long_context_at_threshold_is_still_allowed(self) -> None:
        decision = self.governor.evaluate_context(
            estimated_input_tokens=100_000,
            pricing=_pricing(
                long_context_threshold_tokens=100_000,
                input_per_mtok=10.0,
                long_context_input_per_mtok=20.0,
            ),
        )
        self.assertEqual(decision.tier, ContextTierDecision.TIER_LONG)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.standard_input_per_mtok, 10.0)
        self.assertEqual(decision.long_context_input_per_mtok, 20.0)
        self.assertIn("required evidence outranks price", decision.reason)

    def test_negative_estimate_is_clamped_not_rejected(self) -> None:
        decision = self.governor.evaluate_context(
            estimated_input_tokens=-5,
            pricing=_pricing(long_context_threshold_tokens=100),
        )
        self.assertEqual(decision.estimated_input_tokens, 0)
        self.assertEqual(decision.tier, ContextTierDecision.TIER_STANDARD)

    def test_every_tier_is_allowed_by_contract(self) -> None:
        for tokens in (0, 80_000, 100_000, 10_000_000):
            decision = self.governor.evaluate_context(
                estimated_input_tokens=tokens,
                pricing=_pricing(long_context_threshold_tokens=100_000),
            )
            self.assertTrue(decision.allowed, f"tokens={tokens}")


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


class GuardWiringTests(unittest.TestCase):
    """A tiny threshold in the pricing document must light up the tier."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        initialize_git_repository(self.repository)
        self.sessions = SessionStore(self.root / "sessions")
        self.sessions.create(
            self.repository,
            "what does sample.txt contain?",
            AccessProfile.WORKSPACE_WRITE,
            session_id="session",
        )
        (self.repository / "sample.txt").write_text("before\n", encoding="utf-8")
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

    def _run(self, registry: PricingRegistry | None) -> AgentKernel:
        import json as json_module

        from karox.providers import ToolCall

        provider = _Provider(
            [
                ModelResponse(
                    content=None,
                    tool_calls=(
                        ToolCall(
                            "read-1",
                            "repo_read_file",
                            json_module.dumps({"path": "sample.txt"}),
                        ),
                    ),
                    finish_reason="tool_calls",
                    usage={"prompt_tokens": 5, "completion_tokens": 1},
                    response_id=None,
                ),
                ModelResponse(
                    content="it says before",
                    tool_calls=(),
                    finish_reason="stop",
                    usage={"prompt_tokens": 5, "completion_tokens": 1},
                    response_id=None,
                ),
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
        return kernel

    def _registry(self, threshold: int) -> PricingRegistry:
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
                        "long_context_threshold_tokens": threshold,
                    }
                ]
            }
        )

    def test_without_registry_tier_is_unknown_in_usage(self) -> None:
        self._run(None)
        record = self.sessions.load("session")
        usage = record.usage if isinstance(record.usage, dict) else {}
        rendered = json.dumps(usage, ensure_ascii=False)
        self.assertIn('"economy_context_tier": "UNKNOWN"', rendered)
        self.assertNotIn("economy_context_tier_threshold", rendered)

    def test_tiny_threshold_reports_long_context_and_never_blocks(self) -> None:
        kernel = self._run(self._registry(threshold=10))
        record = self.sessions.load("session")
        usage = record.usage if isinstance(record.usage, dict) else {}
        rendered = json.dumps(usage, ensure_ascii=False)
        self.assertIn('"economy_context_tier": "LONG_CONTEXT"', rendered)
        self.assertIn("economy_context_tier_reason", rendered)
        decision = kernel._context_tier
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertTrue(decision.allowed)


if __name__ == "__main__":
    unittest.main()
