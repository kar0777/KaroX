"""ToolVM measurement: model turns avoided are counted, never invented.

The plan executor and the native kernel share one honesty rule: a model
round trip counts as avoided only when work really executed in its place.
Gated dependents that never ran are excluded, a blocked plan still reports
what it did execute before halting, and a response carrying several tool
calls counts the turns classic one-call-per-turn execution would have paid.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from _support import SRC, initialize_git_repository  # noqa: F401
from karox.agent import AgentKernel, AgentLimits, SYSTEM_PROMPT
from karox.artifacts import ArtifactStore
from karox.core_tools import ExtendedCoreRuntime
from karox.models import AccessProfile, Capability, Origin, OriginKind
from karox.plan_executor import PlanExecutionError, PlanExecutor
from karox.policy import CapabilityPolicy
from karox.providers import ModelRequest, ModelResponse, ToolCall
from karox.repo_context import RepositoryContextEngine
from karox.repository_lease import RepositoryLeaseStore
from karox.sessions import SessionStore
from karox.task_state import TaskStateStore

from test_plan_executor import FakeDelegate


class PlanTurnEconomyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.repo = root / "repo"
        initialize_git_repository(self.repo)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "value.txt").write_text("before\n", encoding="utf-8")
        self.previous_runtime = os.environ.get("KAROX_RUNTIME_DIR")
        os.environ["KAROX_RUNTIME_DIR"] = str(root / "runtime")
        self.sessions = SessionStore(root / "sessions")
        self.sessions.create(
            self.repo,
            "Execute bounded plan",
            AccessProfile.WORKSPACE_WRITE,
            branch="main",
            session_id="session-a",
        )
        self.artifacts = ArtifactStore("session-a")
        self.context = RepositoryContextEngine(
            self.repo,
            self.artifacts,
            policy_profile="workspace_write",
        )
        self.delegate = FakeDelegate(self.repo)
        self.executor = PlanExecutor(
            repository=self.repo,
            session_id="session-a",
            connection_id="chat-a",
            delegate=self.delegate,
            repo_context=self.context,
            task_states=TaskStateStore(self.sessions),
            artifacts=self.artifacts,
            lease_store=RepositoryLeaseStore(root / "repository-leases"),
            session_directory=self.sessions.session_dir("session-a"),
        )

    def tearDown(self) -> None:
        if self.previous_runtime is None:
            os.environ.pop("KAROX_RUNTIME_DIR", None)
        else:
            os.environ["KAROX_RUNTIME_DIR"] = self.previous_runtime
        self.temp.cleanup()

    def test_complete_plan_counts_executed_operations(self) -> None:
        plan: dict[str, Any] = {
            "operations": [
                {
                    "operation_id": "read-value",
                    "action": "read",
                    "inputs": {"path": "src/value.txt"},
                },
                {
                    "operation_id": "search-value",
                    "action": "search",
                    "depends_on": ["read-value"],
                    "inputs": {"query": "before"},
                },
            ]
        }
        result = self.executor.execute(plan, "plan-toolvm-complete")
        economy = result["economy"]
        self.assertEqual(economy["operations_executed"], 2)
        self.assertEqual(economy["model_round_trips_avoided"], 1)

    def test_gated_dependent_is_not_counted_as_avoided(self) -> None:
        self.delegate.fail_always.add("karox.repo.search")
        plan: dict[str, Any] = {
            "stop_on_error": False,
            "operations": [
                {
                    "operation_id": "search-fails",
                    "action": "search",
                    "inputs": {"query": "before"},
                    "on_failure": "continue",
                },
                {
                    "operation_id": "gated-read",
                    "action": "read",
                    "depends_on": ["search-fails"],
                    "inputs": {"path": "src/value.txt"},
                    "on_failure": "continue",
                },
                {
                    "operation_id": "free-read",
                    "action": "read",
                    "inputs": {"path": "src/value.txt"},
                },
            ],
        }
        result = self.executor.execute(plan, "plan-toolvm-gated")
        economy = result["economy"]
        # search-fails ran and failed; free-read ran; gated-read never ran.
        self.assertEqual(economy["operations_executed"], 2)
        self.assertEqual(economy["model_round_trips_avoided"], 1)
        self.assertEqual(result["operations_succeeded"], 1)
        statuses = {
            item["operation_id"]: item["status"]
            for item in result["operation_summaries"]
        }
        self.assertEqual(statuses["gated-read"], "failed")

    def test_blocked_plan_records_economy_in_journal(self) -> None:
        self.delegate.fail_always.add("karox.repo.search")
        plan: dict[str, Any] = {
            "operations": [
                {
                    "operation_id": "read-value",
                    "action": "read",
                    "inputs": {"path": "src/value.txt"},
                },
                {
                    "operation_id": "search-halts",
                    "action": "search",
                    "depends_on": ["read-value"],
                    "inputs": {"query": "before"},
                },
            ]
        }
        with self.assertRaises(PlanExecutionError):
            self.executor.execute(plan, "plan-toolvm-blocked")
        journal = self.executor.journals.load("plan-toolvm-blocked")
        assert journal is not None
        self.assertEqual(journal["status"], "blocked")
        economy = journal["economy"]
        # The read executed and the search really ran before failing.
        self.assertEqual(economy["operations_executed"], 2)
        self.assertEqual(economy["model_round_trips_avoided"], 1)


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


class BatchedTurnsTests(unittest.TestCase):
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

    def test_batched_tool_calls_count_avoided_turns(self) -> None:
        provider = _QueueProvider(
            [
                _response(
                    _call("read-1", "repo_read_file", {"path": "sample.txt"}),
                    _call("status-1", "git_status", {}),
                ),
                _response(content="it contains the word before"),
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
        self.assertEqual(kernel._batched_turns_avoided, 1)
        usage = self.sessions.load("session").usage
        rendered = json.dumps(usage, ensure_ascii=False, sort_keys=True)
        self.assertIn('"economy_batched_turns_avoided": 1', rendered)

    def test_single_call_steps_avoid_nothing(self) -> None:
        provider = _QueueProvider(
            [
                _response(
                    _call("read-1", "repo_read_file", {"path": "sample.txt"})
                ),
                _response(content="it contains the word before"),
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
        self.assertEqual(kernel._batched_turns_avoided, 0)


if __name__ == "__main__":
    unittest.main()
