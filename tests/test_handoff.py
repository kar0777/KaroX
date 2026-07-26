"""Tests for the Phase 6 unified session handoff and mid-task model switch.

The unit tests exercise the structured handoff document in isolation.  The
end-to-end test runs two distinct ``AgentKernel`` instances (one per "model")
against a single leased session through the real ``CoreRuntime``: model A
changes a file and runs a check, stops at the step limit, then model B resumes
the same session, observes the preserved project state, and completes the
verification -- proving the session survives a provider switch without losing
real state or duplicating mutations.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Callable

from _support import SRC, initialize_git_repository  # noqa: F401
from karox.agent import AgentKernel, AgentLimits
from karox.core import CoreRuntime
from karox.handoff import build_handoff, handoff_digest
from karox.models import AccessProfile, Capability, CoreCommand, Origin, OriginKind
from karox.policy import CapabilityPolicy
from karox.providers import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ProviderError,
    ToolCall,
)
from karox.sessions import SessionBusy, SessionError, SessionStore


class QueueProvider:
    """Deterministic in-process provider used by the handoff E2E."""

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


def tool_call(call_id: str, alias: str, arguments: object) -> ToolCall:
    return ToolCall(call_id, alias, json.dumps(arguments, ensure_ascii=False))


def response(*calls: ToolCall, content: str | None = None) -> ModelResponse:
    return ModelResponse(
        content=content,
        tool_calls=tuple(calls),
        finish_reason=("tool_calls" if calls else "stop"),
        usage={"prompt_tokens": 2, "completion_tokens": 1},
        response_id=None,
    )


class HandoffDocumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        initialize_git_repository(self.repository)
        self.sessions = SessionStore(self.root / "sessions")
        self.record = self.sessions.create(
            self.repository, "fix the sample module",
            AccessProfile.WORKSPACE_WRITE, session_id="s",
        )
        self.origin = Origin(OriginKind.NATIVE_AGENT, "test")
        self.policy = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
        self.policy.set_grants(
            self.origin,
            {
                Capability.REPO_READ, Capability.REPO_WRITE, Capability.CHECKS_RUN,
                Capability.PROCESS_RUN, Capability.GIT_READ,
            },
        )
        self.core = CoreRuntime(
            self.repository, self.policy, self.sessions, self.root / "audit.jsonl",
            verification_commands=[
                [sys.executable, "-c", "print('ok')"],
            ],
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _mutate(self, command: CoreCommand) -> None:
        lease = self.sessions.acquire("s", "test", ttl_seconds=30.0)
        try:
            self.core.execute(command, lease=lease)
        finally:
            self.sessions.release(lease)

    def _populate_session(self) -> None:
        self._mutate(
            CoreCommand(
                "repo.write_file", {"path": "sample.txt", "content": "after\n"},
                "s", self.origin, idempotency_key="w",
            )
        )
        self._mutate(
            CoreCommand(
                "checks.run",
                {"argv": [sys.executable, "-c", "print('ok')"]},
                "s", self.origin, idempotency_key="c",
            )
        )
        with self.sessions.mutate("s", "populate") as record:
            record.summary = "The sample module was fixed."
            record.plan = [
                {"step": "reproduce", "status": "done"},
                {"step": "patch", "status": "done"},
                {"step": "verify on second model", "status": "pending"},
            ]
            record.decisions = [{"choice": "use strict json", "reason": "security"}]

    def _record(self) -> "SessionRecord":
        return self.sessions.load("s")

    def test_handoff_contains_structured_fields_without_chat_history(self) -> None:
        self._populate_session()
        document = build_handoff(self._record(), repository=self.repository)
        for key in (
            "schema_version", "session_id", "goal", "constraints", "summary",
            "done", "changed_files", "commands", "check_results", "errors",
            "remaining_steps", "git_state", "active_processes", "model_history",
            "usage", "unfinished_actions", "evidence", "document_sha256",
        ):
            self.assertIn(key, document)
        # Goal and remaining steps are preserved; full chat history is not.
        self.assertEqual(document["goal"], "fix the sample module")
        self.assertEqual(
            document["remaining_steps"],
            [{"step": "verify on second model", "status": "pending"}],
        )
        self.assertIn("sample.txt", document["changed_files"])

    def test_handoff_is_secret_free_and_strict_json(self) -> None:
        self._populate_session()
        secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
        with self.sessions.mutate("s", "secret") as record:
            record.failures = [{"message": secret}]
        document = build_handoff(self._record(), repository=self.repository)
        serialized = json.dumps(document, allow_nan=False)
        self.assertNotIn(secret, serialized)
        # No credential references leak into the handoff.
        self.assertNotIn("os-keyring:", serialized)
        self.assertNotIn("Bearer ", serialized)

    def test_handoff_digest_is_stable_and_verifiable(self) -> None:
        self._populate_session()
        first = build_handoff(self._record(), repository=self.repository)
        second = build_handoff(self._record(), repository=self.repository)
        self.assertEqual(handoff_digest(first), handoff_digest(second))
        # Tampering a stable field invalidates the digest on a rebuilt document.
        with self.sessions.mutate("s", "tamper") as record:
            record.summary = "different"
        tampered = build_handoff(self._record(), repository=self.repository)
        self.assertNotEqual(handoff_digest(first), handoff_digest(tampered))

    def test_handoff_model_history_summarizes_without_full_text(self) -> None:
        with self.sessions.mutate("s", "history") as record:
            record.provider_history = [
                {"role": "user", "model": "model-a", "content": "go"},
                {
                    "role": "assistant", "model": "model-a",
                    "content": "x" * 500,
                    "tool_calls": [{"id": "1", "name": "repo_write_file"}],
                },
                {
                    "role": "tool", "model": "model-a",
                    "tool_result": {"ok": True, "command": "repo.write_file"},
                },
            ]
        document = build_handoff(self._record(), repository=self.repository)
        history = document["model_history"]
        self.assertEqual([item["model"] for item in history], ["model-a"] * 3)
        # Assistant content is truncated to a preview, not the full 500 chars.
        assistant = next(item for item in history if item["role"] == "assistant")
        self.assertLess(len(assistant["content_preview"]), 500)
        self.assertEqual(assistant["tool_calls"][0]["name"], "repo_write_file")

    def test_handoff_uses_latest_history_and_real_agent_schema(self) -> None:
        with self.sessions.mutate("s", "history-contract") as record:
            record.provider_history = [
                {"role": "user", "model": "old", "content": str(index)}
                for index in range(130)
            ]
            record.provider_history.extend(
                [
                    {
                        "role": "tool",
                        "core_name": "checks.run",
                        "result": {"ok": True, "command": "checks.run"},
                    },
                    {
                        "role": "provider_audit",
                        "selected_provider": "fallback",
                        "selected_model": "model-b",
                    },
                ]
            )
        history = build_handoff(
            self.sessions.load("s"), repository=self.repository
        )["model_history"]
        self.assertEqual(len(history), 128)
        self.assertNotEqual(history[0].get("content_preview"), "0")
        self.assertEqual(history[-2]["tool_result"]["command"], "checks.run")
        self.assertEqual(history[-1]["route"]["selected_provider"], "fallback")


class SessionLockCliTests(unittest.TestCase):
    """The lock CLI reports the durable mutation lease state."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        self.repository.mkdir()
        self.runtime_dir = self.root / "runtime"
        self.sessions = SessionStore(self.runtime_dir / "vnext" / "sessions")
        self.sessions.create(
            self.repository, "task", AccessProfile.WORKSPACE_WRITE, session_id="s",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _cli(self, *args: str) -> tuple[int, str, str]:
        env = dict(os.environ)
        env.update(
            {
                "PYTHONPATH": str(SRC),
                "KAROX_CONFIG_DIR": str(self.root / "config"),
                "KAROX_RUNTIME_DIR": str(self.runtime_dir),
            }
        )
        proc = subprocess.run(
            [sys.executable, "-m", "karox.cli", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(self.repository),
        )
        return proc.returncode, proc.stdout, proc.stderr

    def test_lock_reports_unlocked_then_acquired(self) -> None:
        code, out, _ = self._cli("session", "lock", "s", "--json")
        self.assertEqual(code, 0, out)
        info = json.loads(out)
        self.assertFalse(info["locked"])
        # While a lease is held, the lock is visible.
        lease = self.sessions.acquire("s", "holder", ttl_seconds=30.0)
        try:
            code, out, _ = self._cli("session", "lock", "s", "--json")
            self.assertEqual(code, 0, out)
            info = json.loads(out)
            self.assertTrue(info["locked"])
            self.assertEqual(info["owner"], "holder")
        finally:
            self.sessions.release(lease)


class ModelSwitchEndToEndTests(unittest.TestCase):
    """Two models resume one session through the real Core + Kernel."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        initialize_git_repository(self.repository)
        (self.repository / "sample.txt").write_text("before\n", encoding="utf-8")
        self.sessions = SessionStore(self.root / "sessions")
        self.sessions.create(
            self.repository, "fix sample and verify",
            AccessProfile.WORKSPACE_WRITE, session_id="s",
        )
        self.origin = Origin(OriginKind.NATIVE_AGENT, "test")
        self.policy = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
        self.policy.set_grants(
            self.origin,
            {
                Capability.REPO_READ, Capability.REPO_WRITE, Capability.CHECKS_RUN,
                Capability.PROCESS_RUN, Capability.GIT_READ,
            },
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _kernel(self, model: str, responses: list, *, max_steps: int = 8) -> AgentKernel:
        core = CoreRuntime(
            self.repository, self.policy, self.sessions, self.root / "audit.jsonl",
            verification_commands=[
                [sys.executable, "-c", "print('ok')"],
            ],
        )
        return AgentKernel(
            provider=QueueProvider(responses),
            model=model,
            core=core,
            sessions=self.sessions,
            origin=self.origin,
            limits=AgentLimits(max_steps=max_steps, max_seconds=60.0),
        )

    def test_model_b_resumes_preserved_state_and_completes_verification(self) -> None:
        # Model A changes the file, runs a check, and stops at the step limit
        # (final answer, no verification yet -- so the session is not verified).
        model_a = self._kernel(
            "model-a",
            [
                response(tool_call("a1", "repo_write_file", {"path": "sample.txt", "content": "after\n"})),
                response(tool_call("a2", "checks_run", {"argv": [sys.executable, "-c", "print('ok')"]})),
                response(content="Done with the patch."),
            ],
            max_steps=3,
        )
        report_a = model_a.run("s")
        self.assertEqual(report_a.status, "stopped")
        self.assertFalse(report_a.verified)
        # The real file change and check are persisted by Core.
        self.assertEqual(
            (self.repository / "sample.txt").read_text(encoding="utf-8"), "after\n",
        )
        record = self.sessions.load("s")
        self.assertIn("sample.txt", record.changed_files)
        self.assertTrue(record.checks)
        self.assertTrue(record.evidence)

        # Model B resumes with a different model id. It must observe the
        # preserved state and complete verification without re-mutating.
        model_b = self._kernel(
            "model-b",
            [
                response(tool_call("b1", "git_status", {})),
                response(tool_call("b2", "git_diff", {})),
                response(content="Verification complete."),
            ],
            max_steps=8,
        )
        report_b = model_b.run("s")
        self.assertTrue(report_b.verified)
        self.assertEqual(report_b.status, "verified")

        # Both models are recorded in the durable provider history.
        history = self.sessions.load("s").provider_history
        models = {entry.get("model") for entry in history if isinstance(entry, dict)}
        self.assertIn("model-a", models)
        self.assertIn("model-b", models)
        # The handoff document records the cross-model transfer.
        document = build_handoff(self.sessions.load("s"), repository=self.repository)
        doc_models = {item["model"] for item in document["model_history"]}
        self.assertIn("model-a", doc_models)
        self.assertIn("model-b", doc_models)

    def test_concurrent_second_model_is_blocked_by_session_lock(self) -> None:
        # Hold the lease as if model A is mid-flight.
        lease = self.sessions.acquire("s", "model-a", ttl_seconds=30.0)
        try:
            model_b = self._kernel("model-b", [response(content="noop")])
            with self.assertRaises(SessionBusy):
                model_b.run("s")
        finally:
            self.sessions.release(lease)


if __name__ == "__main__":
    unittest.main()
