"""Deferred tool universe wiring: the production agent turn actually defers.

Shadow first: with economy off, every schema is still advertised while the
would-be savings are measured. With economy on, conditional families leave
the request, the discovery note names them, an omitted tool called by exact
name still executes, and its family is attached from the next step. No
counter asserted here is invented; each one moves because of a real request
this process built.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from _support import SRC, initialize_git_repository  # noqa: F401
from karox.agent import AgentKernel, AgentLimits, SYSTEM_PROMPT
from karox.core_tools import ExtendedCoreRuntime
from karox.models import AccessProfile, Capability, Origin, OriginKind
from karox.policy import CapabilityPolicy
from karox.providers import ModelRequest, ModelResponse, ToolCall
from karox.sessions import SessionStore
from karox.tool_universe import family_of


class _QueueProvider:
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


class ToolUniverseWiringTests(unittest.TestCase):
    """A real repository and Core, a scripted provider, a moving universe."""

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
        self.core = ExtendedCoreRuntime(
            self.repository,
            self.policy,
            self.sessions,
            self.root / "audit.jsonl",
            verification_commands=[[sys.executable, "-c", "print('ok')"]],
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _kernel(self, provider: _QueueProvider, economy: bool) -> AgentKernel:
        return AgentKernel(
            provider=provider,
            model="test-model",
            core=self.core,
            sessions=self.sessions,
            origin=self.origin,
            limits=AgentLimits(max_seconds=30),
            system_prompt=SYSTEM_PROMPT,
            economy_mode=economy,
        )

    def _read_then_answer_provider(self) -> _QueueProvider:
        return _QueueProvider(
            [
                _response(
                    _call("read-1", "repo_read_file", {"path": "sample.txt"})
                ),
                _response(content="it contains the word before"),
            ]
        )

    def _conditional_aliases(self, kernel: AgentKernel) -> set[str]:
        return {
            alias
            for alias, core_name in kernel._permitted_tool_pairs
            if family_of(core_name) not in ("core", "task", "memory")
        }

    def test_fixture_has_a_conditional_tool_to_defer(self) -> None:
        kernel = self._kernel(self._read_then_answer_provider(), economy=False)
        self.assertIn("runtime_status", self._conditional_aliases(kernel))

    def test_shadow_mode_measures_but_advertises_everything(self) -> None:
        provider = self._read_then_answer_provider()
        kernel = self._kernel(provider, economy=False)
        kernel.run("session")
        self.assertEqual(len(provider.requests), 2)
        request = provider.requests[0]
        self.assertEqual(len(request.tools), len(kernel._provider_tools))
        self.assertIsNotNone(kernel._universe_selection)
        self.assertGreater(kernel._tool_schemas_omitted, 0)
        self.assertGreater(kernel._tool_schema_bytes_avoided, 0)
        self.assertEqual(kernel._universe_note, "")
        system = request.messages[0]
        self.assertEqual(system.role, "system")
        self.assertNotIn("Deferred tool schemas", system.content or "")
        usage = self.sessions.load("session").usage
        rendered = json.dumps(usage, ensure_ascii=False, sort_keys=True)
        self.assertIn('"economy_tool_universe_applied": false', rendered)
        self.assertIn('"economy_tool_groups_selected": "core,task,memory"', rendered)

    def test_economy_mode_defers_conditional_families(self) -> None:
        provider = self._read_then_answer_provider()
        kernel = self._kernel(provider, economy=True)
        kernel.run("session")
        request = provider.requests[0]
        advertised = {tool.name for tool in request.tools}
        conditional = self._conditional_aliases(kernel)
        self.assertTrue(conditional.isdisjoint(advertised))
        self.assertLess(len(request.tools), len(kernel._provider_tools))
        system = request.messages[0]
        self.assertIn("Deferred tool schemas", system.content or "")
        for alias in conditional:
            self.assertIn(alias, system.content or "")
        self.assertGreater(kernel._tool_schema_bytes_avoided, 0)
        usage = self.sessions.load("session").usage
        rendered = json.dumps(usage, ensure_ascii=False, sort_keys=True)
        self.assertIn('"economy_tool_universe_applied": true', rendered)

    def test_omitted_tool_call_executes_and_expands_its_family(self) -> None:
        provider = _QueueProvider(
            [
                _response(_call("st-1", "runtime_status", {})),
                _response(
                    _call("read-1", "repo_read_file", {"path": "sample.txt"})
                ),
                _response(content="runtime looks healthy"),
            ]
        )
        kernel = self._kernel(provider, economy=True)
        kernel.run("session")
        self.assertEqual(len(provider.requests), 3)
        first = {tool.name for tool in provider.requests[0].tools}
        second = {tool.name for tool in provider.requests[1].tools}
        self.assertNotIn("runtime_status", first)
        self.assertIn("runtime_status", second)
        self.assertEqual(kernel._expanded_families, {"admin"})
        # The omitted call executed for real instead of dead-ending.
        history = self.sessions.load("session").provider_history
        rendered = json.dumps(history, ensure_ascii=False)
        self.assertNotIn("unknown_tool", rendered)
        # The prefix honestly changed when the universe widened.
        self.assertNotEqual(
            provider.requests[0].cache_key, provider.requests[1].cache_key
        )
        usage = self.sessions.load("session").usage
        rendered_usage = json.dumps(usage, ensure_ascii=False, sort_keys=True)
        self.assertIn('"economy_tool_groups_expanded": "admin"', rendered_usage)

    def test_selection_and_digest_are_deterministic(self) -> None:
        kernel_a = self._kernel(self._read_then_answer_provider(), economy=True)
        kernel_b = self._kernel(self._read_then_answer_provider(), economy=True)
        kernel_a._select_tool_universe("what does sample.txt contain?")
        kernel_b._select_tool_universe("what does sample.txt contain?")
        self.assertEqual(kernel_a._universe_selection, kernel_b._universe_selection)
        self.assertEqual(kernel_a._tool_schema_digest, kernel_b._tool_schema_digest)
        self.assertEqual(kernel_a._universe_note, kernel_b._universe_note)

    def test_resolution_never_shrinks_with_deferral(self) -> None:
        kernel = self._kernel(self._read_then_answer_provider(), economy=True)
        kernel._select_tool_universe("what does sample.txt contain?")
        advertised = {tool.name for tool in kernel._advertised_provider_tools}
        self.assertLess(len(advertised), len(kernel._tool_aliases))
        for alias, _core_name in kernel._permitted_tool_pairs:
            self.assertIn(alias, kernel._tool_aliases)


if __name__ == "__main__":
    unittest.main()
