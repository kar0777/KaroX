"""P0.4 acceptance: the production agent turn drives the quality-economy stack.

These tests exist to prevent the "class file equals feature" trap: they assert
that a normal AgentKernel run actually invokes StablePrefixCache,
ToolSchemaDeduplicator, ReadCache, CostLedger and CostGovernor, with counters
that move for real work. Everything measured here is a proxy metric produced
by this process; no provider-side token savings are asserted or invented.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from _support import SRC, initialize_git_repository  # noqa: F401
from karox.agent import AgentKernel, AgentLimits, SYSTEM_PROMPT
from karox.core import CoreRuntime
from karox.models import AccessProfile, Capability, Origin, OriginKind
from karox.policy import CapabilityPolicy
from karox.providers import ModelRequest, ModelResponse, ToolCall
from karox.sessions import SessionStore


class _EconomyQueueProvider:
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


def _response(*calls: ToolCall, content: str | None = None) -> ModelResponse:
    return ModelResponse(
        content=content,
        tool_calls=tuple(calls),
        finish_reason="tool_calls" if calls else "stop",
        usage={"prompt_tokens": 2, "completion_tokens": 1},
        response_id=None,
    )


class QualityEconomyWiringTests(unittest.TestCase):
    """A real repository and Core, a scripted provider, and moving counters."""

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

    def _run_repeated_read_session(self) -> tuple[AgentKernel, _EconomyQueueProvider]:
        provider = _EconomyQueueProvider(
            [
                _response(
                    _call("read-1", "repo_read_file", {"path": "sample.txt"})
                ),
                _response(
                    _call("read-2", "repo_read_file", {"path": "sample.txt"})
                ),
                _response(content="sample.txt contains the word before"),
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
        )
        kernel.run("session")
        return kernel, provider

    def test_read_cache_counts_repeated_unchanged_read(self) -> None:
        kernel, _provider = self._run_repeated_read_session()
        self.assertEqual(kernel._read_cache.misses, 1)
        self.assertEqual(kernel._read_cache.hits, 1)

    def test_prefix_cache_keys_every_request_and_stays_stable(self) -> None:
        kernel, provider = self._run_repeated_read_session()
        self.assertEqual(len(provider.requests), 3)
        keys = {request.cache_key for request in provider.requests}
        self.assertEqual(len(keys), 1)
        (key,) = keys
        self.assertTrue(key.startswith("karox-session-session-"))
        self.assertEqual(kernel._prefix_total_steps, 3)
        self.assertEqual(kernel._prefix_stable_steps, 2)

    def test_cost_ledger_records_every_provider_round_trip(self) -> None:
        kernel, _provider = self._run_repeated_read_session()
        self.assertEqual(kernel._cost_ledger.round_trips("session"), 3)
        self.assertEqual(kernel._cost_ledger.total_tokens("session"), 9)

    def test_tool_schema_economy_is_measured_at_construction(self) -> None:
        kernel, _provider = self._run_repeated_read_session()
        self.assertGreater(kernel._tool_schema_bytes_advertised, 0)
        self.assertLessEqual(
            kernel._tool_schema_bytes_unique,
            kernel._tool_schema_bytes_advertised,
        )

    def test_usage_events_carry_economy_counters(self) -> None:
        self._run_repeated_read_session()
        record = self.sessions.load("session")
        usage = record.usage if isinstance(record.usage, dict) else {}
        rendered = json.dumps(usage, ensure_ascii=False, sort_keys=True)
        self.assertIn("economy_prefix_total_steps", rendered)
        self.assertIn("economy_read_cache_hits", rendered)
        self.assertIn("economy_tool_schema_bytes_advertised", rendered)

    def test_governor_stays_advisory_and_never_blocks(self) -> None:
        kernel, _provider = self._run_repeated_read_session()
        decision = kernel._cost_governor.evaluate(current_cost_usd=999.0)
        self.assertTrue(decision.allowed)
        self.assertTrue(decision.shadow_mode)

    def _duplicate_history(self, big: str) -> list[dict[str, object]]:
        return [
            {"role": "system", "content": "stale prompt"},
            {"role": "user", "content": "task"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "call_id": "dup-1",
                        "name": "repo_read_file",
                        "raw_arguments": "{\"path\": \"a.txt\"}",
                    },
                    {
                        "call_id": "dup-2",
                        "name": "repo_read_file",
                        "raw_arguments": "{\"path\": \"a.txt\"}",
                    },
                ],
            },
            {"role": "tool", "content": big, "tool_call_id": "dup-1"},
            {"role": "tool", "content": big, "tool_call_id": "dup-2"},
        ]

    def _bare_kernel(
        self, economy: bool, *, lossless_context_rewrite: bool = True
    ) -> AgentKernel:
        return AgentKernel(
            provider=_EconomyQueueProvider([]),
            model="test-model",
            core=self.core,
            sessions=self.sessions,
            origin=self.origin,
            limits=AgentLimits(max_seconds=30),
            system_prompt=SYSTEM_PROMPT,
            economy_mode=economy,
            lossless_context_rewrite=lossless_context_rewrite,
        )

    def test_context_compiler_rewrites_exact_duplicates_in_normal_mode(self) -> None:
        kernel = self._bare_kernel(economy=False)
        big = "y" * 1_500
        messages = list(kernel._request_messages(self._duplicate_history(big)))
        tool_messages = [m for m in messages if m.role == "tool"]
        self.assertEqual(tool_messages[0].content, big)
        replaced = tool_messages[1].content or ""
        self.assertIn("identical_tool_result_reused", replaced)
        self.assertIn("dup-1", replaced)
        compiled = kernel._context_compilation
        self.assertIsNotNone(compiled)
        assert compiled is not None
        self.assertEqual(compiled.stats.referenced, 1)
        self.assertGreater(kernel._economy_reused_chars_pending, 0)

    def test_lossless_rewrite_has_an_explicit_diagnostic_escape_hatch(self) -> None:
        kernel = self._bare_kernel(
            economy=False, lossless_context_rewrite=False
        )
        big = "y" * 1_500
        messages = list(kernel._request_messages(self._duplicate_history(big)))
        tool_contents = [m.content for m in messages if m.role == "tool"]
        self.assertEqual(tool_contents, [big, big])
        compiled = kernel._context_compilation
        self.assertIsNotNone(compiled)
        assert compiled is not None
        self.assertEqual(compiled.stats.referenced, 1)
        self.assertEqual(kernel._economy_reused_chars_pending, 0)

    def test_economy_mode_keeps_the_same_lossless_context_rewrite(self) -> None:
        kernel = self._bare_kernel(economy=True)
        big = "y" * 1_500
        messages = list(kernel._request_messages(self._duplicate_history(big)))
        tool_messages = [m for m in messages if m.role == "tool"]
        self.assertEqual(tool_messages[0].content, big)
        replaced = tool_messages[1].content or ""
        self.assertIn("identical_tool_result_reused", replaced)
        self.assertIn("dup-1", replaced)
        self.assertGreater(kernel._economy_reused_chars_pending, 0)

    def test_usage_events_carry_context_compiler_counters(self) -> None:
        self._run_repeated_read_session()
        record = self.sessions.load("session")
        usage = record.usage if isinstance(record.usage, dict) else {}
        rendered = json.dumps(usage, ensure_ascii=False, sort_keys=True)
        self.assertIn("economy_context_items", rendered)
        self.assertIn("economy_context_applied", rendered)
        self.assertIn("lossless_context_rewrite_applied", rendered)
        self.assertIn('"lossless_context_rewrite_applied": true', rendered)
        self.assertIn('"economy_context_applied": false', rendered)


if __name__ == "__main__":
    unittest.main()
