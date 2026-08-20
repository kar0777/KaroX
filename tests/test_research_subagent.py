from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from _support import SRC, initialize_git_repository  # noqa: F401
from karox.core_tools import ExtendedCoreRuntime
from karox.models import AccessProfile, Capability, Origin, OriginKind
from karox.policy import CapabilityPolicy
from karox.providers import ModelRequest, ModelResponse, ToolCall
from karox.research_subagent import ResearchLimits, ResearchSubagent
from karox.sessions import SessionStore


class QueueProvider:
    provider_name = "research-test"

    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("research provider received an unexpected request")
        return self.responses.pop(0)


def call(call_id: str, name: str, arguments: object) -> ToolCall:
    return ToolCall(call_id, name, json.dumps(arguments, ensure_ascii=False))


def response(*calls: ToolCall, content: str | None = None) -> ModelResponse:
    return ModelResponse(
        content=content,
        tool_calls=tuple(calls),
        finish_reason="tool_calls" if calls else "stop",
        usage={"prompt_tokens": 3, "completion_tokens": 2},
    )


class ResearchSubagentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.repository = root / "repo"
        initialize_git_repository(self.repository)
        (self.repository / "src").mkdir()
        (self.repository / "src" / "service.py").write_text(
            "def route_request():\n    return 'primary'\n", encoding="utf-8"
        )
        self.sessions = SessionStore(root / "sessions")
        self.sessions.create(
            self.repository,
            "understand routing",
            AccessProfile.WORKSPACE_WRITE,
            session_id="research-session",
        )
        self.parent = Origin(OriginKind.NATIVE_AGENT, "root-agent")
        self.policy = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
        self.policy.set_grants(
            self.parent,
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
            root / "audit.jsonl",
            verification_commands=[],
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_agent(
        self,
        provider: QueueProvider,
        *,
        limits: ResearchLimits = ResearchLimits(),
        focus: str = "find the routing implementation",
    ):
        agent = ResearchSubagent(
            provider=provider,
            model="research-model",
            core=self.core,
            limits=limits,
        )
        return agent.run(
            session_id="research-session",
            goal="understand how request routing works",
            focus=focus,
            parent_origin=self.parent,
        )

    def test_research_answer_requires_real_read_evidence(self) -> None:
        provider = QueueProvider(
            [
                response(
                    call(
                        "search",
                        "repo_search",
                        {"query": "route_request", "pattern": "src/*.py"},
                    )
                ),
                response(content="Routing starts in src/service.py at route_request."),
            ]
        )

        report = self.run_agent(provider)

        self.assertTrue(report.evidence_backed)
        self.assertEqual(report.status, "answered")
        self.assertEqual(report.tool_calls, 1)
        self.assertTrue(report.basis)
        self.assertIn("src/service.py", report.basis[0].get("paths", []))
        offered = {tool.name for tool in provider.requests[0].tools}
        self.assertIn("repo_search", offered)
        self.assertIn("repo_read_file", offered)
        self.assertNotIn("repo_write_file", offered)
        self.assertNotIn("checks_run", offered)
        self.assertNotIn("tests_run", offered)

    def test_unadvertised_write_call_is_denied_and_cannot_change_file(self) -> None:
        path = self.repository / "src" / "service.py"
        before = path.read_text(encoding="utf-8")
        provider = QueueProvider(
            [
                response(
                    call(
                        "write",
                        "repo_write_file",
                        {"path": "src/service.py", "content": "pwned\n"},
                    )
                ),
                response(call("read", "repo_read_file", {"path": "src/service.py"})),
                response(content="The implementation returns the primary route."),
            ]
        )

        report = self.run_agent(provider)

        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertEqual(report.denied_tool_calls, 1)
        self.assertTrue(report.evidence_backed)
        second_request = provider.requests[1]
        self.assertEqual(second_request.messages[-1].role, "tool")
        self.assertIn("tool_not_allowed", second_request.messages[-1].content or "")
        child_grants = [
            grants
            for key, grants in self.policy.origin_grants.items()
            if key.startswith("subagent:")
        ]
        self.assertEqual(len(child_grants), 1)
        self.assertEqual(
            child_grants[0], {Capability.REPO_READ, Capability.GIT_READ}
        )

    def test_child_never_inherits_capabilities_the_parent_does_not_have(self) -> None:
        self.policy.set_grants(self.parent, {Capability.REPO_READ})
        provider = QueueProvider(
            [
                response(call("read", "repo_read_file", {"path": "src/service.py"})),
                response(content="The service file contains route_request."),
            ]
        )

        report = self.run_agent(provider)

        self.assertTrue(report.evidence_backed)
        offered = {tool.name for tool in provider.requests[0].tools}
        self.assertIn("repo_read_file", offered)
        self.assertNotIn("git_status", offered)
        self.assertNotIn("git_diff", offered)
        self.assertNotIn("git_log", offered)
        child_grants = [
            grants
            for key, grants in self.policy.origin_grants.items()
            if key.startswith("subagent:")
        ]
        self.assertEqual(child_grants, [{Capability.REPO_READ}])

    def test_budget_exceeded_response_stops_before_any_tool_execution(self) -> None:
        provider = QueueProvider(
            [
                ModelResponse(
                    content="do not continue",
                    tool_calls=(call("read", "repo_read_file", {"path": "src/service.py"}),),
                    finish_reason="tool_calls",
                    usage={"prompt_tokens": 3, "completion_tokens": 2},
                    budget_exceeded=True,
                    budget_reason="max_cost",
                )
            ]
        )

        report = self.run_agent(provider)
        record = self.sessions.load("research-session")

        self.assertEqual(report.status, "budget_exceeded")
        self.assertEqual(report.tool_calls, 0)
        self.assertEqual(report.provider_error, "max_cost")
        self.assertEqual(record.usage["requests"], 1)
        self.assertEqual(record.provider_history, [])

    def test_narrative_without_repository_evidence_is_not_accepted(self) -> None:
        provider = QueueProvider([response(content="I think it is in src/service.py.")])

        report = self.run_agent(
            provider,
            limits=ResearchLimits(max_steps=1, max_seconds=10.0),
        )

        self.assertEqual(report.status, "evidence_missing")
        self.assertIsNone(report.answer)
        self.assertFalse(report.evidence_backed)
        self.assertEqual(report.tool_calls, 0)

    def test_research_usage_is_accounted_without_child_chat_history(self) -> None:
        provider = QueueProvider(
            [
                response(call("read", "repo_read_file", {"path": "src/service.py"})),
                response(content="The route returns primary."),
            ]
        )

        report = self.run_agent(provider)
        record = self.sessions.load("research-session")

        self.assertTrue(report.evidence_backed)
        self.assertEqual(record.usage["requests"], 2)
        self.assertEqual(record.usage["prompt_tokens"], 6)
        self.assertEqual(record.usage["completion_tokens"], 4)
        self.assertEqual(record.usage["total_tokens"], 10)
        events = record.usage.get("events")
        self.assertIsInstance(events, list)
        assert isinstance(events, list)
        self.assertEqual(len(events), 2)
        self.assertTrue(all(item.get("source") == "research_subagent" for item in events))
        branches = {str(item.get("branch")) for item in events}
        self.assertEqual(len(branches), 1)
        self.assertTrue(next(iter(branches)).startswith("research-"))
        # Research context is request-only. It must not leak child prompts or
        # answers into the root agent transcript/TUI history.
        self.assertEqual(record.provider_history, [])

    def test_cache_key_is_stable_within_branch_and_distinct_across_focuses(self) -> None:
        provider_a = QueueProvider(
            [
                response(call("read-a", "repo_read_file", {"path": "src/service.py"})),
                response(content="First focus result."),
            ]
        )
        self.run_agent(provider_a, focus="first focus")
        keys_a = {request.cache_key for request in provider_a.requests}

        provider_b = QueueProvider(
            [
                response(call("read-b", "repo_read_file", {"path": "src/service.py"})),
                response(content="Second focus result."),
            ]
        )
        self.run_agent(provider_b, focus="second focus")
        keys_b = {request.cache_key for request in provider_b.requests}

        self.assertEqual(len(keys_a), 1)
        self.assertEqual(len(keys_b), 1)
        self.assertNotEqual(keys_a, keys_b)


if __name__ == "__main__":
    unittest.main()
