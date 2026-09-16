from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from _support import initialize_git_repository
from karox.action_execution import CapabilityCoreRuntime
from karox.action_policy import (
    ActionConfirmationRequired,
    ActionDecisionEngine,
    ActionHardBlocked,
)
from karox.models import AccessProfile, Capability, CoreCommand, Origin, OriginKind
from karox.policy import CapabilityPolicy
from karox.risk_engine import RiskEngine
from karox.sessions import SessionStore


class Harness:
    def __init__(self, task: str) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        initialize_git_repository(self.repo)
        self.sessions = SessionStore(self.root / "sessions")
        self.sessions.create(
            self.repo,
            task,
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
                Capability.PROCESS_RUN,
                Capability.DEV_COMMAND,
                Capability.CHECKS_RUN,
            },
        )
        risk = RiskEngine()
        self.decisions = ActionDecisionEngine(risk)
        self.runtime = CapabilityCoreRuntime(
            self.repo,
            self.policy,
            self.sessions,
            self.root / "audit.jsonl",
            risk=risk,
            action_decisions=self.decisions,
        )

    def enable_checkpoint(self) -> None:
        with self.sessions.mutate("session", "test-checkpoint", ttl_seconds=5.0) as record:
            record.checkpoints.append(
                {
                    "checkpoint_id": "cp-test",
                    "session_id": "session",
                    "created_at": 1.0,
                    "tracked_count": 1,
                    "untracked_count": 0,
                    "untracked_bytes": 0,
                }
            )
        # CapabilityCoreRuntime deliberately trusts only the checkpoint bound to
        # this build turn, never an arbitrary historical checkpoint found in the
        # session ledger. Native/TUI construction passes this id explicitly; the
        # test harness mirrors that production contract here.
        self.runtime._rollback_checkpoint_id = "cp-test"

    def close(self) -> None:
        self.temp.cleanup()

    def command(
        self,
        name: str,
        arguments: dict[str, object],
        *,
        key: str | None = None,
    ) -> CoreCommand:
        return CoreCommand(
            name,
            arguments,
            "session",
            self.origin,
            idempotency_key=key,
        )

    def mutate(self, command: CoreCommand):
        lease = self.sessions.acquire("session", "test")
        try:
            return self.runtime.execute(command, lease=lease)
        finally:
            self.sessions.release(lease)


def test_bulk_local_commit_no_longer_needs_a_human_round_trip() -> None:
    h = Harness("commit the generated fixture files")
    try:
        paths = []
        for index in range(12):
            relative = f"generated_{index}.txt"
            (h.repo / relative).write_text("x\n", encoding="utf-8")
            paths.append(relative)
        result = h.mutate(
            h.command(
                "git.commit",
                {"message": "add generated fixtures", "paths": paths},
                key="commit-1",
            )
        )
        assert result.ok
        assert "core.command.guarded_auto" in (h.root / "audit.jsonl").read_text(
            encoding="utf-8"
        )
    finally:
        h.close()


def test_rebuildable_batch_delete_is_guarded_auto() -> None:
    h = Harness("refactor the parser")
    try:
        target = h.repo / "build" / "cache.tmp"
        target.parent.mkdir()
        target.write_text("cache", encoding="utf-8")
        result = h.mutate(
            h.command(
                "repo.command",
                {
                    "action": "batch",
                    "payload": {
                        "operations": [
                            {"op": "delete", "path": "build/cache.tmp"}
                        ]
                    },
                },
                key="delete-cache",
            )
        )
        assert result.ok
        assert not target.exists()
    finally:
        h.close()


def test_mixed_batch_judges_only_the_actual_delete_targets() -> None:
    h = Harness("update the generated build output")
    try:
        source = h.repo / "src" / "index.py"
        source.parent.mkdir()
        source.write_text("OLD = True\n", encoding="utf-8")
        cache = h.repo / "build" / "cache.tmp"
        cache.parent.mkdir()
        cache.write_text("cache", encoding="utf-8")
        result = h.mutate(
            h.command(
                "repo.command",
                {
                    "action": "batch",
                    "payload": {
                        "operations": [
                            {"op": "write", "path": "src/index.py", "content": "OLD = False\n"},
                            {"op": "delete", "path": "build/cache.tmp"},
                        ]
                    },
                },
                key="mixed-rebuildable-delete",
            )
        )
        assert result.ok
        assert source.read_text(encoding="utf-8") == "OLD = False\n"
        assert not cache.exists()
    finally:
        h.close()


def test_source_delete_uses_authority_already_present_in_user_task() -> None:
    h = Harness("удали устаревший src/legacy.py и обнови тесты")
    try:
        target = h.repo / "src" / "legacy.py"
        target.parent.mkdir()
        target.write_text("OLD = True\n", encoding="utf-8")
        result = h.mutate(
            h.command(
                "repo.command",
                {
                    "action": "batch",
                    "payload": {
                        "operations": [
                            {"op": "delete", "path": "src/legacy.py"}
                        ]
                    },
                },
                key="delete-legacy",
            )
        )
        assert result.ok
        assert not target.exists()
    finally:
        h.close()


def test_unrequested_source_delete_still_stops_without_checkpoint() -> None:
    h = Harness("refactor the parser without changing public behavior")
    try:
        target = h.repo / "src" / "legacy.py"
        target.parent.mkdir()
        target.write_text("OLD = True\n", encoding="utf-8")
        with pytest.raises(ActionConfirmationRequired):
            h.mutate(
                h.command(
                    "repo.command",
                    {
                        "action": "batch",
                        "payload": {
                            "operations": [
                                {"op": "delete", "path": "src/legacy.py"}
                            ]
                        },
                    },
                    key="unexpected-delete",
                )
            )
        assert target.exists()
    finally:
        h.close()


def test_pre_turn_checkpoint_allows_recoverable_source_delete() -> None:
    h = Harness("refactor the parser without changing public behavior")
    try:
        target = h.repo / "src" / "legacy.py"
        target.parent.mkdir()
        target.write_text("OLD = True\n", encoding="utf-8")
        h.enable_checkpoint()
        result = h.mutate(
            h.command(
                "repo.command",
                {
                    "action": "batch",
                    "payload": {
                        "operations": [
                            {"op": "delete", "path": "src/legacy.py"}
                        ]
                    },
                },
                key="checkpoint-delete",
            )
        )
        assert result.ok
        assert not target.exists()
        audit = (h.root / "audit.jsonl").read_text(encoding="utf-8")
        assert "rollback_checkpoint_available" in audit
    finally:
        h.close()


def test_system_path_remains_a_hard_boundary() -> None:
    h = Harness("delete the hosts file")
    try:
        with pytest.raises(ActionHardBlocked):
            h.mutate(
                h.command(
                    "repo.write_file",
                    {
                        "path": r"C:\Windows\System32\drivers\etc\hosts",
                        "content": "x",
                    },
                    key="system-write",
                )
            )
    finally:
        h.close()
