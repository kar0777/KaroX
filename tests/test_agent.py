from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from _support import SRC, initialize_git_repository
from karox.agent import AgentKernel, AgentLimits, ContextBudget, SYSTEM_PROMPT
from karox.core import CoreRuntime
from karox.core_tools import ExtendedCoreRuntime
from karox.models import AccessProfile, Capability, CoreCommand, Origin, OriginKind
from karox.policy import CapabilityPolicy
from karox.providers import (
    ModelRequest,
    ModelResponse,
    ProviderError,
    ProviderErrorKind,
    ToolCall,
)
from karox.sessions import SessionStore


class QueueProvider:
    provider_name = "test_provider"

    def __init__(
        self,
        responses: list[ModelResponse | ProviderError],
        on_complete: Callable[[], None] | None = None,
    ) -> None:
        self.responses = list(responses)
        self.requests: list[ModelRequest] = []
        self.on_complete = on_complete

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self.on_complete is not None:
            self.on_complete()
        if not self.responses:
            raise AssertionError("provider received an unexpected request")
        outcome = self.responses.pop(0)
        if isinstance(outcome, ProviderError):
            raise outcome
        return outcome


def call(call_id: str, name: str, arguments: object, *, raw: bool = False) -> ToolCall:
    encoded = str(arguments) if raw else json.dumps(arguments, ensure_ascii=False)
    return ToolCall(call_id, name, encoded)


def model_response(
    *calls: ToolCall,
    content: str | None = None,
    finish_reason: str | None = None,
) -> ModelResponse:
    return ModelResponse(
        content=content,
        tool_calls=tuple(calls),
        finish_reason=finish_reason or ("tool_calls" if calls else "stop"),
        usage={"prompt_tokens": 2, "completion_tokens": 1},
        response_id=None,
    )


class AgentKernelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        initialize_git_repository(self.repository)
        (self.repository / "sample.txt").write_text("before\n", encoding="utf-8")
        self.sessions = SessionStore(self.root / "sessions")
        self.sessions.create(
            self.repository,
            "change sample and verify it",
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
            verification_commands=[
                [sys.executable, "-c", "print('ok')"],
                [sys.executable, "-c", "raise SystemExit(9)"],
            ],
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def successful_responses(content: str = "after\n") -> list[ModelResponse]:
        return [
            model_response(
                call(
                    "write",
                    "repo_write_file",
                    {"path": "sample.txt", "content": content},
                )
            ),
            model_response(
                call(
                    "check",
                    "checks_run",
                    {"argv": [sys.executable, "-c", "print('ok')"]},
                )
            ),
            model_response(
                call("status", "git_status", {}),
                call("diff", "git_diff", {}),
            ),
            model_response(content="verified locally"),
        ]

    def kernel(
        self,
        provider: QueueProvider,
        limits: AgentLimits | None = None,
        monotonic: Callable[[], float] | None = None,
        *,
        origin: Origin | None = None,
        system_prompt: str = SYSTEM_PROMPT,
        require_change: bool = False,
    ) -> AgentKernel:
        kwargs: dict[str, object] = {}
        if require_change:
            kwargs["require_change"] = True
        if monotonic is not None:
            kwargs["monotonic"] = monotonic
        return AgentKernel(
            provider=provider,
            model="test-model",
            core=self.core,
            sessions=self.sessions,
            origin=origin or self.origin,
            limits=limits or AgentLimits(max_seconds=30),
            system_prompt=system_prompt,
            **kwargs,
        )

    def test_skill_prompt_is_request_only_and_native_history_stays_stable(self) -> None:
        enhanced_prompt = SYSTEM_PROMPT + "\nSKILL_INSTRUCTION_TOKEN"
        provider = QueueProvider([model_response(content="not finished")])

        report = self.kernel(
            provider,
            AgentLimits(max_steps=1, max_seconds=30),
            system_prompt=enhanced_prompt,
        ).run("session")

        self.assertEqual(report.reason, "step_limit")
        self.assertEqual(
            provider.requests[0].messages[0].content,
            self.kernel(
                QueueProvider([]),
                AgentLimits(max_steps=1, max_seconds=30),
                system_prompt=enhanced_prompt,
            ).system_prompt,
        )
        persisted = self.sessions.load("session")
        self.assertEqual(persisted.provider_history[0]["content"], SYSTEM_PROMPT)
        self.assertNotIn(
            "SKILL_INSTRUCTION_TOKEN",
            json.dumps(persisted.provider_history, ensure_ascii=False),
        )

    def test_skill_origin_is_persisted_on_assistant_and_tool_records(self) -> None:
        skill_origin = Origin(
            OriginKind.SKILL,
            "bounded@1.0.0",
            parent=self.origin.key,
        )
        self.policy.set_grants(skill_origin, {Capability.REPO_READ})
        provider = QueueProvider(
            [
                model_response(
                    call("read", "repo_read_file", {"path": "sample.txt"})
                ),
                model_response(content="read complete"),
            ]
        )

        self.kernel(
            provider,
            AgentLimits(max_steps=2, max_seconds=30),
            origin=skill_origin,
        ).run("session")

        history = self.sessions.load("session").provider_history
        generated = [
            item for item in history if item.get("role") in {"assistant", "tool"}
        ]
        self.assertGreaterEqual(len(generated), 3)
        self.assertTrue(
            all(item.get("origin") == skill_origin.key for item in generated)
        )

    def test_pending_call_from_other_origin_is_not_replayed(self) -> None:
        raw_arguments = json.dumps(
            {"path": "sample.txt", "content": "must not be replayed\n"},
            ensure_ascii=False,
            sort_keys=True,
        )
        lease = self.sessions.acquire("session", "origin-simulation")
        try:
            record = self.sessions.load("session")
            record.provider_history.extend(
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": record.task},
                    {
                        "role": "assistant",
                        "origin": self.origin.key,
                        "content": None,
                        "tool_calls": [
                            {
                                "call_id": "foreign-write",
                                "name": "repo_write_file",
                                "raw_arguments": raw_arguments,
                                "recoverable": True,
                            }
                        ],
                        "provider": "test_provider",
                        "model": "test-model",
                    },
                ]
            )
            self.sessions.save(record, record.revision, lease)
        finally:
            self.sessions.release(lease)

        skill_origin = Origin(
            OriginKind.SKILL,
            "bounded@1.0.0",
            parent=self.origin.key,
        )
        self.policy.set_grants(skill_origin, {Capability.REPO_WRITE})
        provider = QueueProvider([model_response(content="stopping")])

        self.kernel(
            provider,
            AgentLimits(max_steps=2, max_seconds=30),
            origin=skill_origin,
        ).run("session")

        self.assertEqual(
            (self.repository / "sample.txt").read_text(encoding="utf-8"),
            "before\n",
        )
        failure = next(
            item
            for item in self.sessions.load("session").provider_history
            if item.get("tool_call_id") == "foreign-write"
            and item.get("role") == "tool"
        )
        self.assertEqual(failure["error"]["type"], "origin_mismatch")
        self.assertEqual(failure["origin"], skill_origin.key)

    def test_denied_skill_write_leaves_repository_unchanged(self) -> None:
        skill_origin = Origin(
            OriginKind.SKILL,
            "bounded@1.0.0",
            parent=self.origin.key,
        )
        self.policy.set_denies(skill_origin, {Capability.REPO_WRITE})
        provider = QueueProvider(
            [
                model_response(
                    call(
                        "denied-write",
                        "repo_write_file",
                        {"path": "sample.txt", "content": "forbidden\n"},
                    )
                ),
                model_response(content="write denied"),
            ]
        )

        report = self.kernel(
            provider,
            AgentLimits(max_steps=2, max_seconds=30),
            origin=skill_origin,
        ).run("session")

        self.assertFalse(report.verified)
        self.assertEqual(
            (self.repository / "sample.txt").read_text(encoding="utf-8"),
            "before\n",
        )
        failure = next(
            item
            for item in self.sessions.load("session").provider_history
            if item.get("tool_call_id") == "denied-write"
            and item.get("role") == "tool"
        )
        self.assertEqual(failure["error"]["type"], "PolicyDenied")

    def test_success_requires_durable_write_check_status_and_diff_evidence(self) -> None:
        provider = QueueProvider(self.successful_responses())
        report = self.kernel(provider).run("session")

        self.assertTrue(report.verified)
        self.assertEqual(report.status, "verified")
        self.assertEqual(report.reason, "verified")
        self.assertEqual(report.changed_files, ("sample.txt",))
        self.assertTrue(report.checks[-1]["ok"])
        self.assertEqual(set(report.git_state), {"status", "diff"})
        self.assertTrue(report.git_state["status"]["ok"])
        self.assertTrue(report.git_state["diff"]["ok"])
        self.assertEqual(
            {item["kind"] for item in report.evidence},
            {"file_write", "check", "git_status", "git_diff"},
        )
        persisted = self.sessions.load("session")
        evidence_ids = {item["evidence_id"] for item in persisted.evidence}
        self.assertEqual(len(evidence_ids), 4)
        for entry in persisted.provider_history:
            result = entry.get("result")
            if isinstance(result, dict):
                for item in result.get("evidence", []):
                    self.assertIn(item["evidence_id"], evidence_ids)

    def test_malformed_arguments_are_reported_and_do_not_crash_loop(self) -> None:
        provider = QueueProvider(
            [
                model_response(
                    call("bad", "repo_write_file", "{not-json", raw=True)
                ),
                model_response(content="cannot continue"),
            ]
        )
        report = self.kernel(
            provider, AgentLimits(max_steps=2, max_seconds=30)
        ).run("session")
        self.assertFalse(report.verified)
        self.assertEqual(report.reason, "step_limit")
        record = self.sessions.load("session")
        error = next(
            item for item in record.provider_history if item.get("tool_call_id") == "bad"
        )
        self.assertEqual(error["error"]["type"], "malformed_arguments")
        self.assertEqual(record.changed_files, [])

    def test_a_repeated_call_is_refused_without_ending_the_session(self) -> None:
        arguments = {"path": "sample.txt", "content": "after\n"}
        provider = QueueProvider(
            [
                model_response(call("write-1", "repo_write_file", arguments)),
                model_response(call("write-2", "repo_write_file", arguments)),
                model_response(content="I will stop repeating that call."),
            ]
        )
        report = self.kernel(
            provider,
            AgentLimits(
                max_steps=5,
                max_seconds=30,
                max_identical_actions=1,
                max_repair_prompts=0,
            ),
        ).run("session")

        record = self.sessions.load("session")
        repeated = next(
            item
            for item in record.provider_history
            if item.get("tool_call_id") == "write-2"
        )
        self.assertEqual(repeated["error"]["type"], "repeated_action")
        # The refusal is a correction the model can act on, so the run keeps
        # going. Ending the whole session on the second identical call used to
        # throw away every result the run had already produced.
        self.assertNotEqual(report.reason, "repeated_action")
        self.assertEqual(len(provider.requests), 3)

    def test_repeating_forever_still_ends_the_session(self) -> None:
        arguments = {"path": "sample.txt"}
        provider = QueueProvider(
            [
                model_response(call(f"read-{index}", "repo_read_file", arguments))
                for index in range(6)
            ]
        )
        report = self.kernel(
            provider,
            AgentLimits(
                max_steps=10,
                max_seconds=30,
                max_identical_actions=1,
                max_repeated_action_errors=2,
            ),
        ).run("session")

        self.assertFalse(report.verified)
        self.assertEqual(report.reason, "repeated_action")
        # One refusal is a nudge; a model that ignores every nudge is not making
        # progress and is stopped rather than paid for.
        self.assertEqual(len(provider.requests), 3)

    def test_two_repair_rounds_do_not_exhaust_the_repeat_budget(self) -> None:
        # Verification demands git_status and git_diff after the successful
        # check, so a task whose first two checks fail legitimately issues the
        # same git_status a third time. At the old limit of two that killed the
        # session outright and threw away the work already done.
        failing = {"argv": [sys.executable, "-c", "raise SystemExit(9)"]}
        passing = {"argv": [sys.executable, "-c", "print('ok')"]}
        provider = QueueProvider(
            [
                model_response(
                    call(
                        "write",
                        "repo_write_file",
                        {"path": "sample.txt", "content": "after\n"},
                    )
                ),
                model_response(call("check-1", "checks_run", failing)),
                model_response(
                    call("status-1", "git_status", {}),
                    call("diff-1", "git_diff", {}),
                ),
                model_response(call("check-2", "checks_run", failing)),
                model_response(
                    call("status-2", "git_status", {}),
                    call("diff-2", "git_diff", {}),
                ),
                model_response(call("check-3", "checks_run", passing)),
                model_response(
                    call("status-3", "git_status", {}),
                    call("diff-3", "git_diff", {}),
                ),
                model_response(content="verified locally"),
            ]
        )

        report = self.kernel(
            provider, AgentLimits(max_steps=12, max_seconds=30)
        ).run("session")

        self.assertTrue(report.verified)
        self.assertEqual(report.reason, "verified")
        record = self.sessions.load("session")
        self.assertFalse(
            [
                item
                for item in record.provider_history
                if isinstance(item.get("error"), dict)
                and item["error"].get("type") == "repeated_action"
            ]
        )

    def test_step_and_wall_time_limits_stop_deterministically(self) -> None:
        provider = QueueProvider([model_response(content="not finished")])
        report = self.kernel(
            provider, AgentLimits(max_steps=1, max_seconds=30)
        ).run("session")
        self.assertEqual(report.reason, "step_limit")
        self.assertEqual(len(provider.requests), 1)

        other = self.sessions.create(
            self.repository,
            "wall time",
            AccessProfile.WORKSPACE_WRITE,
            session_id="wall-time",
        )
        self.assertEqual(other.session_id, "wall-time")

        class Clock:
            value = 0.0

            def now(self) -> float:
                return self.value

            def expire(self) -> None:
                self.value = 1.0

        clock = Clock()
        timed_provider = QueueProvider(
            [model_response(content="too late")], on_complete=clock.expire
        )
        report = self.kernel(
            timed_provider,
            AgentLimits(max_steps=2, max_seconds=0.1),
            monotonic=clock.now,
        ).run("wall-time")
        self.assertEqual(report.reason, "wall_time_limit")
        self.assertFalse(report.verified)

    def test_failed_check_cannot_verify_even_after_git_evidence(self) -> None:
        provider = QueueProvider(
            [
                model_response(
                    call(
                        "write",
                        "repo_write_file",
                        {"path": "sample.txt", "content": "after\n"},
                    )
                ),
                model_response(
                    call(
                        "check",
                        "checks_run",
                        {"argv": [sys.executable, "-c", "raise SystemExit(9)"]},
                    )
                ),
                model_response(
                    call("status", "git_status", {}),
                    call("diff", "git_diff", {}),
                ),
                model_response(content="claimed success"),
            ]
        )
        report = self.kernel(
            provider, AgentLimits(max_steps=4, max_seconds=30)
        ).run("session")
        self.assertFalse(report.verified)
        self.assertEqual(report.reason, "step_limit")
        self.assertFalse(report.checks[-1]["ok"])

    def test_noop_write_cannot_verify_or_enter_changed_files(self) -> None:
        existing = (self.repository / "sample.txt").read_bytes().decode("utf-8")
        provider = QueueProvider(self.successful_responses(content=existing))
        report = self.kernel(
            provider, AgentLimits(max_steps=4, max_seconds=30)
        ).run("session")
        self.assertFalse(report.verified)
        self.assertEqual(report.reason, "step_limit")
        self.assertEqual(report.changed_files, ())
        write_result = next(
            item["result"]
            for item in self.sessions.load("session").provider_history
            if item.get("core_name") == "repo.write_file"
        )
        self.assertFalse(write_result["data"]["changed"])

    def test_pending_mutation_recovers_through_core_idempotency(self) -> None:
        raw_arguments = json.dumps(
            {"path": "sample.txt", "content": "recovered\n"},
            ensure_ascii=False,
            sort_keys=True,
        )
        pending = call("pending-write", "repo_write_file", raw_arguments, raw=True)
        lease = self.sessions.acquire("session", "crash-simulation")
        try:
            record = self.sessions.load("session")
            record.provider_history.extend(
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": record.task},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "call_id": pending.call_id,
                                "name": pending.name,
                                "raw_arguments": pending.raw_arguments,
                                "arguments_sha256": hashlib.sha256(
                                    pending.raw_arguments.encode("utf-8")
                                ).hexdigest(),
                                "recoverable": True,
                            }
                        ],
                        "provider": "test_provider",
                        "model": "test-model",
                    },
                ]
            )
            self.sessions.save(record, record.revision, lease)
            identity = f"session\0{pending.call_id}\0repo.write_file".encode("utf-8")
            digest = hashlib.sha256(identity).hexdigest()
            result = self.core.execute(
                CoreCommand(
                    "repo.write_file",
                    json.loads(raw_arguments),
                    "session",
                    self.origin,
                    correlation_id=f"agent-{digest[:32]}",
                    idempotency_key=f"agent-{digest}",
                ),
                lease=lease,
            )
            self.assertFalse(result.idempotent_replay)
        finally:
            self.sessions.release(lease)

        provider = QueueProvider(
            [
                model_response(
                    call(
                        "check",
                        "checks_run",
                        {"argv": [sys.executable, "-c", "print('ok')"]},
                    )
                ),
                model_response(
                    call("status", "git_status", {}),
                    call("diff", "git_diff", {}),
                ),
                model_response(content="recovered and verified"),
            ]
        )
        report = self.kernel(provider).run("session")
        self.assertTrue(report.verified)
        recovered = next(
            item["result"]
            for item in self.sessions.load("session").provider_history
            if item.get("tool_call_id") == "pending-write"
            and item.get("role") == "tool"
        )
        self.assertTrue(recovered["idempotent_replay"])
        self.assertEqual(
            (self.repository / "sample.txt").read_text(encoding="utf-8"),
            "recovered\n",
        )

    def test_redacted_pending_arguments_fail_closed(self) -> None:
        lease = self.sessions.acquire("session", "redacted-simulation")
        try:
            record = self.sessions.load("session")
            record.provider_history.extend(
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": record.task},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "call_id": "redacted",
                                "name": "repo_write_file",
                                "raw_arguments": json.dumps(
                                    {
                                        "path": "should-not-exist.txt",
                                        "content": "[REDACTED]",
                                    }
                                ),
                                "arguments_sha256": "0" * 64,
                                "recoverable": False,
                            }
                        ],
                        "provider": "test_provider",
                        "model": "test-model",
                    },
                ]
            )
            self.sessions.save(record, record.revision, lease)
        finally:
            self.sessions.release(lease)

        provider = QueueProvider([model_response(content="stopping")])
        report = self.kernel(
            provider, AgentLimits(max_steps=2, max_seconds=30)
        ).run("session")
        self.assertFalse(report.verified)
        self.assertFalse((self.repository / "should-not-exist.txt").exists())
        failure = next(
            item
            for item in self.sessions.load("session").provider_history
            if item.get("tool_call_id") == "redacted" and item.get("role") == "tool"
        )
        self.assertEqual(failure["error"]["type"], "unrecoverable_arguments")

    def test_pending_recovery_matches_only_preceding_calls_in_order(self) -> None:
        def persisted_call(call_id: str, name: str) -> dict[str, object]:
            return {
                "call_id": call_id,
                "name": name,
                "raw_arguments": "{}",
                "recoverable": True,
            }

        history = [
            {"role": "tool", "tool_call_id": "shared", "content": "orphan"},
            {
                "role": "assistant",
                "tool_calls": [persisted_call("shared", "first")],
            },
            {"role": "tool", "tool_call_id": "shared", "content": "first-result"},
            {
                "role": "assistant",
                "tool_calls": [
                    persisted_call("shared", "second"),
                    persisted_call("other", "resolved"),
                    persisted_call("last", "last"),
                ],
            },
            {"role": "tool", "tool_call_id": "other", "content": "other-result"},
        ]

        pending = AgentKernel._pending_calls(history)

        self.assertEqual(
            [(item.call.call_id, item.call.name) for item in pending],
            [("shared", "second"), ("last", "last")],
        )

    def test_verified_rerun_does_not_call_provider(self) -> None:
        provider = QueueProvider(self.successful_responses())
        kernel = self.kernel(provider)
        first = kernel.run("session")
        request_count = len(provider.requests)
        second = kernel.run("session")
        self.assertTrue(first.verified)
        self.assertTrue(second.verified)
        self.assertEqual(second.reason, "already_verified")
        self.assertEqual(len(provider.requests), request_count)

    def test_later_failed_check_invalidates_previous_verification_chain(self) -> None:
        responses = self.successful_responses()
        responses.insert(
            3,
            model_response(
                call(
                    "late-failure",
                    "checks_run",
                    {"argv": [sys.executable, "-c", "raise SystemExit(9)"]},
                )
            ),
        )
        report = self.kernel(
            QueueProvider(responses), AgentLimits(max_steps=5, max_seconds=30)
        ).run("session")
        self.assertFalse(report.verified)
        self.assertEqual([item["ok"] for item in report.checks], [True, False])

    def test_unapproved_noop_check_cannot_verify(self) -> None:
        provider = QueueProvider(
            [
                model_response(
                    call(
                        "write",
                        "repo_write_file",
                        {"path": "sample.txt", "content": "after\n"},
                    )
                ),
                model_response(
                    call(
                        "noop",
                        "checks_run",
                        {"argv": [sys.executable, "-c", "pass"]},
                    )
                ),
                model_response(content="claimed success"),
            ]
        )
        report = self.kernel(
            provider, AgentLimits(max_steps=3, max_seconds=30)
        ).run("session")
        self.assertFalse(report.verified)
        failure = next(
            item
            for item in self.sessions.load("session").provider_history
            if item.get("tool_call_id") == "noop"
        )
        self.assertEqual(failure["error"]["type"], "InvalidCommand")

    def test_message_normalization_keeps_tool_results_adjacent(self) -> None:
        history = [
            {
                "role": "provider_audit",
                "kind": "route",
                "route_attempts": [{"provider_id": "ignored"}],
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"call_id": "one", "name": "git_status", "raw_arguments": "{}"},
                    {"call_id": "two", "name": "git_diff", "raw_arguments": "{}"},
                ],
            },
            {"role": "user", "content": "intervening"},
            {"role": "tool", "tool_call_id": "two", "content": "diff"},
            {"role": "tool", "tool_call_id": "orphan", "content": "ignored"},
            {"role": "tool", "tool_call_id": "one", "content": "status"},
        ]
        messages = list(AgentKernel._messages(history))
        self.assertEqual([item.role for item in messages], ["assistant", "tool", "tool", "user"])
        self.assertEqual(
            [item.tool_call_id for item in messages[1:3]], ["one", "two"]
        )

    def test_routed_response_persists_secret_safe_audit_and_cost_totals(self) -> None:
        routed_response = ModelResponse(
            content="not finished",
            tool_calls=(),
            finish_reason="stop",
            usage={"prompt_tokens": 4, "completion_tokens": 2},
            route_attempts=(
                {
                    "route_index": 0,
                    "provider_id": "private-provider",
                    "model": "model-a",
                    "status": "completed",
                },
            ),
            selected_provider="private-provider",
            selected_model="model-a",
            cost=0.25,
            currency="USD",
            pricing_version="2026-07",
            cumulative_usage={"prompt_tokens": 14, "completion_tokens": 6},
            cumulative_cost=1.25,
        )
        provider = QueueProvider([routed_response])

        report = self.kernel(
            provider, AgentLimits(max_steps=1, max_seconds=30)
        ).run("session")

        self.assertEqual(report.reason, "step_limit")
        record = self.sessions.load("session")
        audit = next(
            item
            for item in record.provider_history
            if item.get("role") == "provider_audit"
        )
        self.assertEqual(audit["selected_provider"], "private-provider")
        self.assertEqual(audit["selected_model"], "model-a")
        self.assertEqual(audit["pricing_version"], "2026-07")
        self.assertEqual(audit["cumulative_usage"]["prompt_tokens"], 14)
        self.assertEqual(audit["cumulative_cost"], 1.25)
        self.assertEqual(record.usage["costs"], {"USD": 1.25})
        assistant = next(
            item for item in record.provider_history if item.get("role") == "assistant"
        )
        self.assertEqual(assistant["provider"], "private-provider")
        self.assertEqual(assistant["model"], "model-a")
        self.assertEqual(report.steps, 1)
        self.assertNotIn(
            "provider_audit", [message.role for message in provider.requests[0].messages]
        )

    def test_route_failure_audit_is_durable_and_does_not_count_as_step(self) -> None:
        provider = QueueProvider(
            [
                ProviderError(
                    ProviderErrorKind.TRANSPORT,
                    "offline",
                    route_attempts=(
                        {
                            "route_index": 0,
                            "provider_id": "first",
                            "model": "model-a",
                            "status": "failed",
                            "error_kind": "transport",
                        },
                    ),
                )
            ]
        )

        report = self.kernel(provider).run("session")

        self.assertEqual(report.status, "failed")
        self.assertEqual(report.reason, "provider_error:transport")
        self.assertEqual(report.steps, 0)
        record = self.sessions.load("session")
        audit = next(
            item
            for item in record.provider_history
            if item.get("role") == "provider_audit"
        )
        self.assertEqual(audit["kind"], "route_failure")
        self.assertEqual(audit["route_attempts"][0]["provider_id"], "first")
        self.assertEqual(AgentKernel._pending_calls(record.provider_history), [])

    def test_post_response_budget_overrun_blocks_and_resolves_tool_calls(self) -> None:
        blocked = call(
            "blocked-write",
            "repo_write_file",
            {"path": "sample.txt", "content": "must not be written\n"},
        )
        provider = QueueProvider(
            [
                ModelResponse(
                    content="attempted write",
                    tool_calls=(blocked,),
                    finish_reason="tool_calls",
                    usage={"prompt_tokens": 8, "completion_tokens": 4},
                    route_attempts=(
                        {
                            "route_index": 0,
                            "provider_id": "priced",
                            "model": "model-a",
                            "status": "completed",
                        },
                    ),
                    selected_provider="priced",
                    selected_model="model-a",
                    cost=0.012,
                    currency="USD",
                    pricing_version="v1",
                    cumulative_usage={"prompt_tokens": 8, "completion_tokens": 4},
                    cumulative_cost=0.012,
                    budget_exceeded=True,
                    budget_reason="token_budget,cost_budget",
                )
            ]
        )

        report = self.kernel(provider).run("session")

        self.assertEqual(report.status, "stopped")
        self.assertEqual(report.phase, "budget")
        self.assertEqual(
            report.reason, "budget_exceeded:token_budget,cost_budget"
        )
        self.assertEqual(
            (self.repository / "sample.txt").read_text(encoding="utf-8"),
            "before\n",
        )
        record = self.sessions.load("session")
        blocked_result = next(
            item
            for item in record.provider_history
            if item.get("tool_call_id") == "blocked-write"
            and item.get("role") == "tool"
        )
        self.assertEqual(blocked_result["error"]["type"], "budget_exceeded")
        self.assertEqual(AgentKernel._pending_calls(record.provider_history), [])

    def test_preflight_budget_failure_stops_without_failing_session(self) -> None:
        provider = QueueProvider(
            [
                ProviderError(
                    ProviderErrorKind.BUDGET_EXCEEDED,
                    "token budget is already exhausted",
                    route_attempts=(
                        {
                            "route_index": 0,
                            "provider_id": "priced",
                            "model": "model-a",
                            "status": "rejected",
                            "error_kind": "budget_exceeded",
                        },
                    ),
                )
            ]
        )

        report = self.kernel(provider).run("session")

        self.assertEqual(report.status, "stopped")
        self.assertEqual(report.phase, "budget")
        self.assertEqual(report.reason, "budget_exceeded")
        self.assertEqual(report.steps, 0)

    def test_an_answer_backed_by_a_read_is_a_successful_outcome(self) -> None:
        provider = QueueProvider(
            [
                model_response(
                    call("read", "repo_read_file", {"path": "sample.txt"})
                ),
                model_response(content="the file says before"),
            ]
        )

        report = self.kernel(
            provider, AgentLimits(max_steps=24, max_seconds=30)
        ).run("session")

        # A question used to be structurally unanswerable: the only terminal
        # success required a file change, so the model answered, KaroX demanded
        # a write, and the run exited 1 with the answer discarded.
        self.assertTrue(report.verified)
        self.assertEqual(report.status, "verified")
        self.assertEqual(report.reason, "answer")
        self.assertEqual(report.provider_message, "the file says before")
        self.assertEqual(report.changed_files, ())
        # It costs two provider calls, and the answer names what it rests on.
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(
            [item["tool"] for item in report.answer_basis], ["repo.read_file"]
        )
        self.assertEqual(report.answer_basis[0]["path"], "sample.txt")

    def test_an_answer_that_looked_at_nothing_is_not_accepted(self) -> None:
        provider = QueueProvider(
            [
                model_response(content="I think it says before"),
                model_response(content="I still think it says before"),
            ]
        )

        report = self.kernel(
            provider, AgentLimits(max_steps=24, max_seconds=30)
        ).run("session")

        # Narrative is not evidence. A model that never looked at the repository
        # gets one nudge and then a non-zero exit, rather than having its guess
        # blessed as verified.
        self.assertFalse(report.verified)
        self.assertEqual(report.reason, "no_changes")
        self.assertEqual(report.provider_message, "I still think it says before")
        self.assertEqual(len(provider.requests), 2)

    def test_a_change_task_may_refuse_the_answer_outcome(self) -> None:
        provider = QueueProvider(
            [
                model_response(
                    call("read", "repo_read_file", {"path": "sample.txt"})
                ),
                model_response(content="the file says before"),
                model_response(content="the file says before"),
            ]
        )

        report = self.kernel(
            provider,
            AgentLimits(max_steps=24, max_seconds=30),
            require_change=True,
        ).run("session")

        self.assertFalse(report.verified)
        self.assertEqual(report.reason, "no_changes")
        self.assertEqual(report.answer_basis, ())

    def test_unverified_change_is_reported_separately_from_no_change(self) -> None:
        provider = QueueProvider(
            [
                model_response(
                    call(
                        "write",
                        "repo_write_file",
                        {"path": "sample.txt", "content": "after\n"},
                    )
                ),
                model_response(content="I changed it, trust me"),
                model_response(content="I changed it, trust me"),
                model_response(content="I changed it, trust me"),
            ]
        )

        report = self.kernel(
            provider, AgentLimits(max_steps=24, max_seconds=30)
        ).run("session")

        # A real change is worth re-prompting twice, because the model can still
        # run the check and the two Git reads that make it verifiable.
        self.assertEqual(len(provider.requests), 4)
        self.assertEqual(report.reason, "unverified_changes")
        self.assertFalse(report.verified)
        self.assertIn("sample.txt", report.changed_files)

    def test_offered_tools_exclude_capabilities_the_origin_lacks(self) -> None:
        provider = QueueProvider([model_response(content="stopping")])
        kernel = self.kernel(provider, AgentLimits(max_steps=1, max_seconds=30))

        offered = {item.name for item in kernel._provider_tools}

        # The session grants no GIT_COMMIT, so the tool is never advertised even
        # though Core implements it.
        self.assertNotIn("git_commit", offered)
        self.assertIn("repo_search", offered)
        self.assertIn("repo_read_file", offered)

    def test_unoffered_core_tool_still_reaches_core_for_an_audited_denial(
        self,
    ) -> None:
        provider = QueueProvider(
            [
                model_response(
                    call("commit", "git_commit", {"message": "m", "paths": ["a"]})
                ),
                model_response(content="denied"),
            ]
        )

        self.kernel(provider, AgentLimits(max_steps=2, max_seconds=30)).run(
            "session"
        )

        failure = next(
            item
            for item in self.sessions.load("session").provider_history
            if item.get("tool_call_id") == "commit" and item.get("role") == "tool"
        )
        self.assertEqual(failure["error"]["type"], "PolicyDenied")


class ExtendedToolAgentTests(unittest.TestCase):
    """The native agent runs on the same extended Core as the hosted bridge."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        initialize_git_repository(self.repository)
        (self.repository / "sample.txt").write_text(
            "alpha\nbeta\ngamma\n", encoding="utf-8"
        )
        self.sessions = SessionStore(self.root / "sessions")
        self.sessions.create(
            self.repository,
            "edit sample and verify it",
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

    def kernel(self, provider: QueueProvider, limits: AgentLimits) -> AgentKernel:
        return AgentKernel(
            provider=provider,
            model="test-model",
            core=self.core,
            sessions=self.sessions,
            origin=self.origin,
            limits=limits,
        )

    def test_extended_tools_are_offered_to_the_model(self) -> None:
        provider = QueueProvider([model_response(content="stopping")])
        kernel = self.kernel(provider, AgentLimits(max_steps=1, max_seconds=30))

        offered = {item.name for item in kernel._provider_tools}

        self.assertLessEqual(
            {
                "repo_edit_file",
                "repo_read_lines",
                "repo_search",
                "git_log",
                "repo_read_file",
                "repo_write_file",
                "repo_list_files",
                "checks_run",
                "git_status",
                "git_diff",
            },
            offered,
        )

    def test_exact_string_edit_satisfies_verification(self) -> None:
        provider = QueueProvider(
            [
                model_response(
                    call(
                        "edit",
                        "repo_edit_file",
                        {
                            "path": "sample.txt",
                            "old_string": "beta",
                            "new_string": "delta",
                        },
                    )
                ),
                model_response(
                    call(
                        "check",
                        "checks_run",
                        {"argv": [sys.executable, "-c", "print('ok')"]},
                    )
                ),
                model_response(
                    call("status", "git_status", {}),
                    call("diff", "git_diff", {}),
                ),
                model_response(content="verified locally"),
            ]
        )

        report = self.kernel(provider, AgentLimits(max_seconds=30)).run("session")

        self.assertTrue(report.verified)
        self.assertEqual(report.reason, "verified")
        self.assertIn("sample.txt", report.changed_files)
        self.assertEqual(
            (self.repository / "sample.txt").read_text(encoding="utf-8"),
            "alpha\ndelta\ngamma\n",
        )
        self.assertIn(
            "file_edit", {item.get("kind") for item in report.evidence}
        )


class ContextCompactionTests(unittest.TestCase):
    """History is bounded before it reaches the provider, not after it fails."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        initialize_git_repository(self.repository)
        (self.repository / "sample.txt").write_text("before\n", encoding="utf-8")
        self.sessions = SessionStore(self.root / "sessions")
        self.sessions.create(
            self.repository,
            "work through a long task",
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

    def kernel(self, context: ContextBudget) -> AgentKernel:
        return AgentKernel(
            provider=QueueProvider([]),
            model="test-model",
            core=self.core,
            sessions=self.sessions,
            origin=self.origin,
            limits=AgentLimits(max_seconds=30),
            context=context,
        )

    @staticmethod
    def history(turns: int, payload_chars: int) -> list[dict[str, object]]:
        entries: list[dict[str, object]] = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "the original task"},
        ]
        for index in range(turns):
            entries.append(
                {
                    "role": "assistant",
                    "content": None,
                    "provider": "test_provider",
                    "tool_calls": [
                        {
                            "call_id": f"c{index}",
                            "name": "repo_read_file",
                            "raw_arguments": '{"path": "sample.txt"}',
                            "recoverable": True,
                        }
                    ],
                }
            )
            entries.append(
                {
                    "role": "tool",
                    "tool_call_id": f"c{index}",
                    "tool_name": "repo_read_file",
                    "core_name": "repo.read_file",
                    "content": "x" * payload_chars,
                    "result": {"ok": True, "data": {"path": f"file{index}.txt"}},
                }
            )
        return entries

    def test_history_under_the_ceiling_is_untouched(self) -> None:
        kernel = self.kernel(ContextBudget(max_input_tokens=200_000))
        entries = self.history(4, 200)

        self.assertEqual(kernel._compact(entries), entries)

    def test_oldest_turns_are_replaced_by_a_summary(self) -> None:
        kernel = self.kernel(
            ContextBudget(max_input_tokens=8_000, keep_recent_groups=2)
        )
        entries = self.history(12, 3_000)

        compacted = kernel._compact(entries)

        # The system prompt and the original task are never candidates.
        self.assertEqual(compacted[0]["content"], "system prompt")
        self.assertEqual(compacted[1]["content"], "the original task")
        summary = compacted[2]
        self.assertEqual(summary["kind"], "context_summary")
        self.assertIn("repo.read_file", str(summary["content"]))
        self.assertIn("file0.txt", str(summary["content"]))
        self.assertLess(len(compacted), len(entries))
        self.assertLessEqual(
            kernel._estimated_tokens(compacted), kernel.context.token_ceiling
        )

    def test_compaction_never_splits_a_tool_call_from_its_result(self) -> None:
        kernel = self.kernel(
            ContextBudget(max_input_tokens=8_000, keep_recent_groups=2)
        )

        compacted = kernel._compact(self.history(12, 3_000))

        pending = [
            str(raw.get("call_id"))
            for entry in compacted
            if entry.get("role") == "assistant"
            for raw in entry.get("tool_calls", [])
        ]
        answered = [
            str(entry.get("tool_call_id"))
            for entry in compacted
            if entry.get("role") == "tool"
        ]
        # Every wire format rejects an assistant tool call with no matching
        # result, and a result with no matching call.
        self.assertEqual(sorted(pending), sorted(answered))

    def test_recent_turns_survive_even_when_they_exceed_the_ceiling(self) -> None:
        kernel = self.kernel(
            ContextBudget(max_input_tokens=2_000, keep_recent_groups=4)
        )
        entries = self.history(10, 20_000)

        compacted = kernel._compact(entries)

        # The floor of recent turns is kept even though it is over budget:
        # dropping the freshest evidence would be worse than a large request,
        # and clipping still bounds each individual result.
        self.assertEqual(
            sum(1 for entry in compacted if entry.get("role") == "assistant"), 4
        )
        self.assertEqual(compacted[2]["kind"], "context_summary")

    def test_oversized_tool_results_are_clipped_in_the_request_only(self) -> None:
        kernel = self.kernel(
            ContextBudget(max_input_tokens=200_000, max_tool_result_chars=1_000)
        )
        entries = self.history(1, 50_000)

        messages = list(kernel._request_messages(entries))

        tool_message = next(item for item in messages if item.role == "tool")
        self.assertLess(len(tool_message.content or ""), 1_400)
        self.assertIn("KaroX omitted", tool_message.content or "")
        self.assertIn("repo_read_lines", tool_message.content or "")
        # The durable record is untouched; only the outbound view is bounded.
        self.assertEqual(len(str(entries[3]["content"])), 50_000)

    def test_unknown_window_still_applies_a_safety_ceiling(self) -> None:
        kernel = self.kernel(ContextBudget(keep_recent_groups=2))
        self.assertFalse(kernel.context.window_known)
        entries = self.history(400, 3_000)

        compacted = kernel._compact(entries)

        self.assertLess(len(compacted), len(entries))
        summary = next(
            item for item in compacted if item.get("kind") == "context_summary"
        )
        self.assertIn("does not advertise an input window", str(summary["content"]))


class ScriptedChatHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        server = self.server
        server.requests.append(payload)  # type: ignore[attr-defined]
        index = len(server.requests) - 1  # type: ignore[attr-defined]
        scripted = server.responses[index]  # type: ignore[attr-defined]
        body = "".join(
            [f"data: {json.dumps(event)}\n\n" for event in scripted]
            + ["data: [DONE]\n\n"]
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return None


class AgentCliEndToEndTests(unittest.TestCase):
    def test_subprocess_cli_runs_openai_compatible_agent_to_verified_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repo"
            initialize_git_repository(repository)
            (repository / "sample.txt").write_text("before\n", encoding="utf-8")
            (repository / "AGENTS.md").write_text(
                "PROJECT_RULE_TOKEN: this repository formats with ruff.\n",
                encoding="utf-8",
            )
            git_environment = dict(os.environ)
            git_environment.update(
                {
                    "GIT_AUTHOR_NAME": "KaroX Test",
                    "GIT_AUTHOR_EMAIL": "karox@example.invalid",
                    "GIT_COMMITTER_NAME": "KaroX Test",
                    "GIT_COMMITTER_EMAIL": "karox@example.invalid",
                }
            )
            subprocess.run(
                ["git", "add", "sample.txt", "AGENTS.md"],
                cwd=repository,
                env=git_environment,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "--quiet", "-m", "initial"],
                cwd=repository,
                env=git_environment,
                check=True,
                capture_output=True,
            )

            def tool_delta(
                index: int,
                identifier: str,
                name: str,
                arguments: str,
            ) -> dict[str, object]:
                return {
                    "index": index,
                    "id": identifier,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": arguments,
                    },
                }

            def choice_chunk(
                response_id: str,
                *,
                content: str | None = None,
                tool_calls: list[dict[str, object]] | None = None,
                finish_reason: str | None = None,
            ) -> dict[str, object]:
                delta: dict[str, object] = {}
                if content is not None:
                    delta["content"] = content
                if tool_calls is not None:
                    delta["tool_calls"] = tool_calls
                return {
                    "id": response_id,
                    "choices": [
                        {"delta": delta, "finish_reason": finish_reason}
                    ],
                }

            def usage_chunk(response_id: str) -> dict[str, object]:
                return {
                    "id": response_id,
                    "choices": [],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }

            read_arguments = json.dumps({"path": "sample.txt"})
            write_arguments = json.dumps(
                {"path": "sample.txt", "content": "after\n"}
            )
            check_arguments = json.dumps(
                {"argv": [sys.executable, "-c", "print('e2e-ok')"]}
            )
            split = len(write_arguments) // 2
            responses = [
                [
                    choice_chunk(
                        "response-read",
                        tool_calls=[
                            tool_delta(0, "read", "repo_read_file", read_arguments)
                        ],
                    ),
                    choice_chunk("response-read", finish_reason="tool_calls"),
                    usage_chunk("response-read"),
                ],
                [
                    choice_chunk(
                        "response-write",
                        tool_calls=[
                            tool_delta(
                                0,
                                "write",
                                "repo_write_file",
                                write_arguments[:split],
                            )
                        ],
                    ),
                    choice_chunk(
                        "response-write",
                        tool_calls=[
                            tool_delta(0, "", "", write_arguments[split:])
                        ],
                    ),
                    choice_chunk("response-write", finish_reason="tool_calls"),
                    usage_chunk("response-write"),
                ],
                [
                    choice_chunk(
                        "response-check",
                        tool_calls=[
                            tool_delta(0, "check", "checks_run", check_arguments)
                        ],
                    ),
                    choice_chunk("response-check", finish_reason="tool_calls"),
                    usage_chunk("response-check"),
                ],
                [
                    choice_chunk(
                        "response-git",
                        tool_calls=[
                            tool_delta(0, "status", "git_status", "{}"),
                            tool_delta(1, "diff", "git_diff", "{}"),
                        ],
                    ),
                    choice_chunk("response-git", finish_reason="tool_calls"),
                    usage_chunk("response-git"),
                ],
                [
                    choice_chunk(
                        "response-final",
                        content="All local evidence is complete.",
                    ),
                    choice_chunk("response-final", finish_reason="stop"),
                    usage_chunk("response-final"),
                ],
            ]
            server = ThreadingHTTPServer(("127.0.0.1", 0), ScriptedChatHandler)
            server.requests = []  # type: ignore[attr-defined]
            server.responses = responses  # type: ignore[attr-defined]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                environment = dict(os.environ)
                environment.update(
                    {
                        "PYTHONPATH": str(SRC)
                        + (os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else ""),
                        "KAROX_VNEXT_CONFIG_DIR": str(root / "config"),
                        "KAROX_VNEXT_RUNTIME_DIR": str(root / "runtime"),
                    }
                )
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "karox.cli",
                        "agent",
                        "run",
                        "--repository",
                        str(repository),
                        "--task",
                        "Change sample.txt and verify the result",
                        "--model",
                        "local-test-model",
                        "--base-url",
                        f"http://127.0.0.1:{server.server_port}/v1",
                        "--session-id",
                        "cli-e2e",
                        "--max-seconds",
                        "30",
                        "--verification-command",
                        json.dumps([sys.executable, "-c", "print('e2e-ok')"]),
                        "--json",
                    ],
                    cwd=repository,
                    env=environment,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=60,
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(completed.stdout)
            self.assertTrue(report["verified"])
            self.assertEqual(report["status"], "verified")
            self.assertEqual(report["reason"], "verified")
            self.assertEqual(report["changed_files"], ["sample.txt"])
            self.assertTrue(report["checks"][-1]["ok"])
            self.assertEqual(set(report["git_state"]), {"status", "diff"})
            self.assertIn("sample.txt", report["git_state"]["status"]["stdout"])
            self.assertIn("after", report["git_state"]["diff"]["stdout"])
            self.assertEqual(
                {item["kind"] for item in report["evidence"]},
                {"file_write", "check", "git_status", "git_diff"},
            )
            self.assertEqual(
                (repository / "sample.txt").read_text(encoding="utf-8"), "after\n"
            )
            self.assertEqual(len(server.requests), 5)  # type: ignore[attr-defined]
            system_message = server.requests[0]["messages"][0]  # type: ignore[attr-defined]
            self.assertEqual(system_message["role"], "system")
            # The repository's own instructions reach the model, labelled as
            # untrusted, together with the facts it would otherwise spend tool
            # calls discovering.
            self.assertIn("PROJECT_RULE_TOKEN", system_message["content"])
            self.assertIn("untrusted", system_message["content"])
            self.assertIn("<environment>", system_message["content"])
            self.assertIn("e2e-ok", system_message["content"])
            # Untrusted text that steered the run is named in the report, so it
            # is never adopted invisibly.
            self.assertTrue(report["project_context"]["enabled"])
            self.assertEqual(
                [item["path"] for item in report["project_context"]["sources"]],
                ["AGENTS.md"],
            )
            # Third-party text stays request-only: an edited AGENTS.md must not
            # retroactively change what a past run was told, and the handoff
            # document must not carry it.
            persisted = json.loads(
                (
                    root
                    / "runtime"
                    / "vnext"
                    / "sessions"
                    / "cli-e2e"
                    / "session.json"
                ).read_text(encoding="utf-8")
            )
            self.assertNotIn(
                "PROJECT_RULE_TOKEN",
                json.dumps(persisted["provider_history"], ensure_ascii=False),
            )
            for request_payload in server.requests:  # type: ignore[attr-defined]
                self.assertIs(request_payload["stream"], True)
                self.assertEqual(
                    request_payload["stream_options"], {"include_usage": True}
                )
            for request_payload in server.requests[1:]:  # type: ignore[attr-defined]
                messages = request_payload["messages"]
                for index, message in enumerate(messages):
                    calls = message.get("tool_calls", [])
                    if not calls:
                        continue
                    following = messages[index + 1 : index + 1 + len(calls)]
                    self.assertEqual(
                        [item["tool_call_id"] for item in following],
                        [item["id"] for item in calls],
                    )

            persisted = SessionStore(root / "runtime" / "vnext" / "sessions").load(
                "cli-e2e"
            )
            self.assertEqual(persisted.status, "verified")
            self.assertEqual(len(persisted.evidence), 4)


if __name__ == "__main__":
    unittest.main()
