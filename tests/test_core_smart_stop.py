"""Smart Stop enforced at the one boundary every agent passes through.

``CoreRuntime`` is where an OpenAI model, a sponsor API, ChatGPT Web, an MCP
client and a subagent all end up. These tests pin that the gate lives there,
that it is opt-in so existing embedders are unaffected, and that a model cannot
talk its way past it.

Every command below uses a real Core tool schema, so a test cannot pass against
a shape the runtime would have rejected anyway.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from _support import initialize_git_repository
from karox.core import CoreRuntime
from karox.event_bus import EventBus, EventKind
from karox.models import (
    AccessProfile,
    Capability,
    CoreCommand,
    Origin,
    OriginKind,
)
from karox.policy import CapabilityPolicy
from karox.risk_engine import (
    ConfirmationRejected,
    RiskEngine,
    RiskLevel,
    SmartStopRequired,
)
from karox.risk_mapping import action_for_command
from karox.sessions import SessionStore

BULK = 12


class _Base(unittest.TestCase):
    def build(self, *, risk: RiskEngine | None, events: EventBus | None) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        initialize_git_repository(self.repository)
        self.tracked = [f"generated_{index}.txt" for index in range(BULK)]
        for name in self.tracked:
            (self.repository / name).write_text("x\n", encoding="utf-8")
        self.sessions = SessionStore(self.root / "sessions")
        self.sessions.create(
            self.repository,
            "bulk change",
            AccessProfile.ELEVATED,
            session_id="session",
        )
        self.origin = Origin(OriginKind.NATIVE_AGENT, "test-agent")
        self.policy = CapabilityPolicy(AccessProfile.ELEVATED)
        self.policy.set_grants(
            self.origin,
            {
                Capability.REPO_READ,
                Capability.REPO_WRITE,
                Capability.GIT_READ,
                Capability.GIT_COMMIT,
            },
        )
        self.runtime = CoreRuntime(
            self.repository,
            self.policy,
            self.sessions,
            self.root / "audit.jsonl",
            risk=risk,
            events=events,
        )

    def command(
        self,
        name: str,
        arguments: dict[str, object],
        *,
        key: str | None = None,
        token: str | None = None,
    ) -> CoreCommand:
        return CoreCommand(
            name,
            arguments,
            "session",
            self.origin,
            idempotency_key=key,
            confirmation_token=token,
        )

    def bulk_commit(self, *, key: str, token: str | None = None) -> CoreCommand:
        return self.command(
            "git.commit",
            {"message": "add generated files", "paths": list(self.tracked)},
            key=key,
            token=token,
        )

    def run_mutation(self, command: CoreCommand):
        lease = self.sessions.acquire(command.session_id, "test")
        try:
            return self.runtime.execute(command, lease=lease)
        finally:
            self.sessions.release(lease)


class CoreSmartStopTests(_Base):
    def setUp(self) -> None:
        self.risk = RiskEngine()
        self.events = EventBus(capacity=50)
        self.build(risk=self.risk, events=self.events)

    def test_a_read_passes_the_gate_untouched(self) -> None:
        result = self.runtime.execute(
            self.command("repo.read_file", {"path": "generated_0.txt"})
        )
        self.assertIn("x", json.dumps(result.to_dict()))

    def test_a_bounded_write_still_proceeds_without_a_human(self) -> None:
        result = self.run_mutation(
            self.command(
                "repo.write_file",
                {"path": "generated_0.txt", "content": "after\n"},
                key="write-1",
            )
        )
        self.assertTrue(result.ok)
        self.assertEqual(
            (self.repository / "generated_0.txt").read_text(encoding="utf-8"),
            "after\n",
        )

    def test_a_bulk_commit_is_stopped_before_it_runs(self) -> None:
        with self.assertRaises(SmartStopRequired) as stop:
            self.run_mutation(self.bulk_commit(key="batch-1"))
        assessment = stop.exception.assessment
        self.assertIn("bulk_mutation", assessment.reasons)
        self.assertEqual(assessment.level, RiskLevel.HIGH)
        self.assertEqual(assessment.preview["file_count"], BULK)

    def test_a_write_outside_the_repository_is_critical(self) -> None:
        # The path guard would refuse this too, but Smart Stop must see it
        # first: a critical action is never a silent library error.
        command = self.command(
            "repo.write_file",
            {"path": "C:/Windows/System32/drivers/etc/hosts", "content": "x"},
            key="system-1",
        )
        with self.assertRaises(SmartStopRequired) as stop:
            self.run_mutation(command)
        self.assertEqual(stop.exception.assessment.level, RiskLevel.CRITICAL)
        self.assertIn("system_path", stop.exception.assessment.reasons)

    def test_a_human_confirmation_lets_the_same_action_through_once(self) -> None:
        pending = self.bulk_commit(key="batch-2")
        grant = self.risk.ledger.issue(
            self.risk.assess(action_for_command(pending))
        )
        result = self.run_mutation(
            self.bulk_commit(key="batch-2", token=grant.token)
        )
        self.assertTrue(result.ok)

        with self.assertRaises(ConfirmationRejected):
            self.run_mutation(self.bulk_commit(key="batch-3", token=grant.token))

    def test_a_model_invented_token_is_refused(self) -> None:
        with self.assertRaises(ConfirmationRejected):
            self.run_mutation(
                self.bulk_commit(key="batch-4", token="i-approve-this-myself")
            )

    def test_the_decision_reaches_the_event_stream_without_the_token(self) -> None:
        pending = self.bulk_commit(key="batch-5")
        grant = self.risk.ledger.issue(
            self.risk.assess(action_for_command(pending))
        )
        self.run_mutation(self.bulk_commit(key="batch-5", token=grant.token))

        decisions = self.events.snapshot(kinds=(EventKind.RISK_DECISION,))
        self.assertTrue(decisions)
        blob = json.dumps([item.to_dict() for item in decisions])
        self.assertNotIn(grant.token, blob)
        self.assertIn("bulk_mutation", blob)
        self.assertTrue(decisions[-1].data["allowed"])

    def test_a_refusal_is_recorded_as_a_warning_event(self) -> None:
        with self.assertRaises(SmartStopRequired):
            self.run_mutation(self.bulk_commit(key="batch-6"))
        decision = self.events.snapshot(kinds=(EventKind.RISK_DECISION,))[-1]
        self.assertFalse(decision.data["allowed"])
        self.assertEqual(decision.data["reason"], "confirmation_required")

    def test_the_stop_is_recorded_in_the_audit_log(self) -> None:
        with self.assertRaises(SmartStopRequired):
            self.run_mutation(self.bulk_commit(key="batch-7"))
        audit = (self.root / "audit.jsonl").read_text(encoding="utf-8")
        self.assertIn("core.command.stopped", audit)

    def test_a_rejected_confirmation_is_audited_with_its_reason(self) -> None:
        with self.assertRaises(ConfirmationRejected):
            self.run_mutation(self.bulk_commit(key="batch-8", token="forged"))
        audit = (self.root / "audit.jsonl").read_text(encoding="utf-8")
        self.assertIn("core.command.confirmation_rejected", audit)
        self.assertIn("confirmation_unknown", audit)
        self.assertNotIn("forged", audit)

    def test_the_origin_does_not_change_the_verdict(self) -> None:
        for identity in ("chatgpt-web", "openai-api", "clickup-mcp"):
            with self.subTest(identity=identity):
                origin = Origin(OriginKind.HOSTED_CLIENT, identity)
                self.policy.set_grants(origin, {Capability.GIT_COMMIT})
                command = CoreCommand(
                    "git.commit",
                    {"message": "m", "paths": list(self.tracked)},
                    "session",
                    origin,
                    idempotency_key=f"origin-{identity}",
                )
                with self.assertRaises(SmartStopRequired):
                    self.run_mutation(command)


class CoreWithoutRiskEngineTests(_Base):
    """An embedder that passes no engine keeps its previous behaviour exactly."""

    def setUp(self) -> None:
        self.build(risk=None, events=None)

    def test_no_engine_means_no_gate(self) -> None:
        result = self.run_mutation(self.bulk_commit(key="batch-9"))
        self.assertTrue(result.ok)


if __name__ == "__main__":
    unittest.main()
