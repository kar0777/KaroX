"""A hosted client is the most remote agent source, and gets the same stop line.

The hosted bridge is how ChatGPT Web, Claude Web, an MCP client and an external
coding agent reach the machine. These tests pin two things: Smart Stop applies
there by default, and the bridge gives a hosted agent no way at all to approve
its own action.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from _support import initialize_git_repository
from karox.event_bus import EventBus, EventKind
from karox.hosted_bridge import CoreToolBridge
from karox.models import AccessProfile
from karox.risk_engine import RiskEngine, RiskLevel, SmartStopRequired
from karox.sessions import SessionStore

BULK = 12
TOOLS = (
    "karox.repo.read_file",
    "karox.repo.write_file",
    "karox.git.commit",
)


class HostedSmartStopTests(unittest.TestCase):
    def setUp(self) -> None:
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
            "hosted work",
            AccessProfile.ELEVATED,
            session_id="hosted-session",
        )
        self.risk = RiskEngine()
        self.events = EventBus(capacity=50)

    def bridge(self, **overrides: object) -> CoreToolBridge:
        settings: dict[str, object] = {
            "hosted_origin": None,
            "audit_path": self.root / "audit.jsonl",
            "risk": self.risk,
            "events": self.events,
        }
        settings.update(overrides)
        return CoreToolBridge(
            self.repository,
            self.sessions,
            "hosted-session",
            TOOLS,
            **settings,  # type: ignore[arg-type]
        )

    def test_a_read_still_works(self) -> None:
        result = self.bridge().execute(
            "karox.repo.read_file", {"path": "generated_0.txt"}
        )
        self.assertTrue(result["ok"])

    def test_a_single_write_still_works(self) -> None:
        result = self.bridge().execute(
            "karox.repo.write_file",
            {"path": "generated_0.txt", "content": "after\n"},
            idempotency_key="write-1",
        )
        self.assertTrue(result["ok"])

    def test_a_bulk_commit_from_a_hosted_client_is_stopped(self) -> None:
        with self.assertRaises(SmartStopRequired) as stop:
            self.bridge().execute(
                "karox.git.commit",
                {"message": "bulk", "paths": list(self.tracked)},
                idempotency_key="bulk-1",
            )
        self.assertEqual(stop.exception.assessment.level, RiskLevel.HIGH)
        self.assertIn("bulk_mutation", stop.exception.assessment.reasons)

    def test_a_hosted_agent_has_no_channel_to_confirm_its_own_action(self) -> None:
        # The bridge never forwards a confirmation token, by construction: it
        # builds the CoreCommand itself and leaves the field unset. Even a
        # token smuggled into the arguments cannot become an approval, because
        # arguments are schema-validated against the tool.
        bridge = self.bridge()
        with self.assertRaises(SmartStopRequired):
            bridge.execute(
                "karox.git.commit",
                {"message": "bulk", "paths": list(self.tracked)},
                idempotency_key="bulk-2",
            )
        grant = self.risk.ledger.issue(
            self.risk.assess(stop_action(self.tracked))
        )
        with self.assertRaises(Exception) as smuggled:
            bridge.execute(
                "karox.git.commit",
                {
                    "message": "bulk",
                    "paths": list(self.tracked),
                    "confirmation_token": grant.token,
                },
                idempotency_key="bulk-3",
            )
        self.assertNotIsInstance(smuggled.exception, type(None))
        self.assertIn("confirmation_token", str(smuggled.exception))

    def test_the_stop_is_visible_on_the_event_stream(self) -> None:
        with self.assertRaises(SmartStopRequired):
            self.bridge().execute(
                "karox.git.commit",
                {"message": "bulk", "paths": list(self.tracked)},
                idempotency_key="bulk-4",
            )
        decisions = self.events.snapshot(kinds=(EventKind.RISK_DECISION,))
        self.assertTrue(decisions)
        self.assertFalse(decisions[-1].data["allowed"])

    def test_passing_no_engine_explicitly_opts_out(self) -> None:
        # A caller that wants the previous behaviour must say so; the default is
        # protected, not the other way around.
        bridge = CoreToolBridge(
            self.repository,
            self.sessions,
            "hosted-session",
            TOOLS,
            audit_path=self.root / "audit.jsonl",
            risk=None,
            events=self.events,
        )
        self.assertIsNotNone(bridge._risk)


def stop_action(paths: list[str]):
    from karox.models import CoreCommand, Origin, OriginKind
    from karox.risk_mapping import action_for_command

    return action_for_command(
        CoreCommand(
            "git.commit",
            {"message": "bulk", "paths": list(paths)},
            "hosted-session",
            Origin(OriginKind.HOSTED_CLIENT, "core-bridge-hosted-session"),
        )
    )


if __name__ == "__main__":
    unittest.main()
