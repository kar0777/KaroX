"""A hosted client is the most remote agent source, and gets the same stop line.

The hosted bridge is how ChatGPT Web, Claude Web, an MCP client and an external
coding agent reach the machine. These tests pin two things: Smart Stop applies
there by default, and the bridge gives a hosted agent no way at all to approve
its own action.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _support import initialize_git_repository
from karox.event_bus import EventBus, EventKind
from karox.hosted_bridge import CoreToolBridge, HostedApprovalRequired
from karox.action_policy import ActionDisposition
from karox.models import AccessProfile, CoreResult
from karox.risk_engine import RiskEngine, RiskLevel, SmartStopRequired
from karox.sessions import SessionStore
from karox.task_state import FactOrigin, TaskFact, TaskStateStore

BULK = 12
TOOLS = (
    "karox.repo.read_file",
    "karox.repo.write_file",
    "karox.git.commit",
    "karox.git.push",
    "karox.command.run",
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

    def test_workstream_objective_is_carried_as_out_of_band_user_intent(self) -> None:
        TaskStateStore(self.sessions).bootstrap(
            "hosted-session",
            {
                "objective": TaskFact(
                    "delete the obsolete generated file",
                    FactOrigin.REPORTED_BY_AGENT,
                )
            },
            workstream_id="cleanup",
        )
        bridge = self.bridge()
        captured: dict[str, str] = {}

        class StubRuntime:
            def execute(self, command, *, lease=None):  # type: ignore[no-untyped-def]
                del lease
                captured["intent"] = command.user_intent
                captured["arguments"] = repr(command.arguments)
                return CoreResult(
                    ok=True,
                    command=command.name,
                    data={},
                    correlation_id=command.correlation_id,
                )

        with patch.object(bridge, "_core", return_value=StubRuntime()):
            result = bridge.execute(
                "karox.repo.read_file",
                {"path": "generated_0.txt", "workstream_id": "cleanup"},
            )
        self.assertTrue(result["ok"])
        self.assertEqual(captured["intent"], "delete the obsolete generated file")
        self.assertNotIn("workstream_id", captured["arguments"])
        self.assertNotIn("delete the obsolete generated file", captured["arguments"])

    def test_a_bulk_local_commit_is_guarded_auto_not_a_user_prompt(self) -> None:
        result = self.bridge().execute(
            "karox.git.commit",
            {"message": "bulk", "paths": list(self.tracked)},
            idempotency_key="bulk-1",
        )
        self.assertTrue(result["ok"])
        decisions = self.events.snapshot(kinds=(EventKind.RISK_DECISION,))
        self.assertTrue(decisions)
        self.assertTrue(decisions[-1].data["allowed"])
        self.assertEqual(decisions[-1].data["risk"], RiskLevel.HIGH.value)
        self.assertEqual(decisions[-1].data["reason"], ActionDisposition.GUARDED_AUTO.value)

    def test_git_push_requires_protocol_approval_and_executes_only_exact_retry(self) -> None:
        remote = self.root / "remote.git"
        subprocess.run(
            ["git", "init", "--bare", str(remote)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        subprocess.run(
            ["git", "remote", "add", "origin", str(remote)],
            cwd=self.repository,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=self.repository,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        self.assertTrue(branch)
        TaskStateStore(self.sessions).bootstrap(
            "hosted-session",
            {
                "objective": TaskFact(
                    "push the verified branch to origin",
                    FactOrigin.REPORTED_BY_AGENT,
                )
            },
            workstream_id="ship",
        )
        bridge = self.bridge()
        committed = bridge.execute(
            "karox.git.commit",
            {"message": "baseline for push", "paths": list(self.tracked)},
            idempotency_key="push-baseline-commit",
        )
        self.assertTrue(committed["ok"], committed)
        arguments = {"remote": "origin", "branch": branch, "workstream_id": "ship"}
        with self.assertRaises(HostedApprovalRequired) as stopped:
            bridge.execute(
                "karox.git.push",
                arguments,
                idempotency_key="push-approved-1",
            )
        self.assertEqual(stopped.exception.action_kind, "git.push")
        self.assertEqual(stopped.exception.preview["remote"], "origin")
        self.assertEqual(stopped.exception.preview["branch"], branch)

        with self.assertRaisesRegex(Exception, "no longer matches"):
            bridge.execute_approved(
                "karox.git.push",
                arguments,
                expected_action_digest="0" * 64,
                idempotency_key="push-approved-1",
            )

        result = bridge.execute_approved(
            "karox.git.push",
            arguments,
            expected_action_digest=stopped.exception.action_digest,
            idempotency_key="push-approved-1",
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["data"]["pushed"])
        remote_head = subprocess.run(
            ["git", "--git-dir", str(remote), "rev-parse", f"refs/heads/{branch}"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        self.assertEqual(remote_head, result["data"]["commit_sha"])

    def test_cached_command_run_push_routes_to_the_same_guarded_approval(self) -> None:
        bridge = self.bridge()
        with self.assertRaises(HostedApprovalRequired) as stopped:
            bridge.execute(
                "karox.command.run",
                {"argv": ["git", "push", "origin", "main"]},
                idempotency_key="cached-push-compat",
            )
        self.assertEqual(stopped.exception.tool_name, "karox.git.push")
        self.assertEqual(stopped.exception.action_kind, "git.push")
        self.assertEqual(stopped.exception.preview["remote"], "origin")
        self.assertEqual(stopped.exception.preview["branch"], "main")

        # Flags, force variants, or any more complex Git spelling never enter the
        # compatibility shim; the ordinary developer-command guard still denies
        # them before a process can start.
        with self.assertRaises(Exception):
            bridge.execute(
                "karox.command.run",
                {"argv": ["git", "push", "--force", "origin", "main"]},
                idempotency_key="cached-force-push-blocked",
            )

    def test_a_hosted_agent_has_no_channel_to_confirm_its_own_action(self) -> None:
        # A local commit no longer needs a prompt, so use a real external side
        # effect for the exact-human-boundary contract. The bridge still never
        # accepts a confirmation token from model-visible tool arguments.
        bridge = self.bridge()
        with self.assertRaises(HostedApprovalRequired):
            bridge.execute(
                "karox.command.run",
                {"argv": ["git", "push"]},
                idempotency_key="push-1",
            )
        with self.assertRaises(Exception) as smuggled:
            bridge.execute(
                "karox.command.run",
                {
                    "argv": ["git", "push"],
                    "confirmation_token": "model-visible-self-approval-token",
                },
                idempotency_key="push-2",
            )
        self.assertNotIsInstance(smuggled.exception, type(None))
        self.assertIn("confirmation_token", str(smuggled.exception))

    def test_a_real_confirmation_boundary_is_visible_on_the_event_stream(self) -> None:
        with self.assertRaises(HostedApprovalRequired):
            self.bridge().execute(
                "karox.command.run",
                {"argv": ["git", "push"]},
                idempotency_key="push-3",
            )
        decisions = self.events.snapshot(kinds=(EventKind.RISK_DECISION,))
        self.assertTrue(decisions)
        self.assertFalse(decisions[-1].data["allowed"])
        self.assertEqual(decisions[-1].data["reason"], "confirmation_required")

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
