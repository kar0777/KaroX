from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from _support import initialize_git_repository

from karox.hosted_bridge import CoreToolBridge, HostedBridgeAccessDenied
from karox.models import AccessProfile
from karox.repository_lease import RepositoryLeaseStore
from karox.sessions import SessionStore
from karox.task_state import FactOrigin, TaskStateStore, fact


def _bridge(root: Path, repository: Path, sessions: SessionStore) -> CoreToolBridge:
    bridge = CoreToolBridge(
        repository,
        sessions,
        "session-a",
        ["karox.repo.read_file", "karox.repo.write_file"],
        audit_path=root / "audit.jsonl",
    )
    bridge.repository_leases = RepositoryLeaseStore(root / "repository-leases")
    return bridge


def test_direct_hosted_mutation_respects_live_repository_lease_but_reads_continue() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repository = root / "repo"
        initialize_git_repository(repository)
        (repository / "before.txt").write_text("before\n", encoding="utf-8")
        sessions = SessionStore(root / "sessions")
        sessions.create(
            repository,
            "direct lease guard",
            AccessProfile.WORKSPACE_WRITE,
            branch="main",
            session_id="session-a",
        )
        bridge = _bridge(root, repository, sessions)
        leases = bridge.repository_leases
        held, _ = leases.acquire(
            repository,
            session_id="session-b",
            task_id="task-b",
            connection_id="other-agent",
            current_operation="long verification",
            ttl_seconds=30,
        )
        try:
            read = bridge.execute("karox.repo.read_file", {"path": "before.txt"})
            assert read["ok"] is True
            assert "before" in read["data"]["content"]

            with pytest.raises(HostedBridgeAccessDenied, match="repository is busy"):
                bridge.execute(
                    "karox.repo.write_file",
                    {"path": "blocked.txt", "content": "must-not-land\n"},
                    idempotency_key="blocked-write",
                    deadline_seconds=1,
                )
            assert not (repository / "blocked.txt").exists()
        finally:
            leases.release(repository, held)

        written = bridge.execute(
            "karox.repo.write_file",
            {"path": "after.txt", "content": "after\n"},
            idempotency_key="after-release-write",
            deadline_seconds=5,
        )
        assert written["ok"] is True
        assert (repository / "after.txt").read_text(encoding="utf-8") == "after\n"


def test_nested_high_level_lease_is_reused_for_same_session_and_task() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repository = root / "repo"
        initialize_git_repository(repository)
        sessions = SessionStore(root / "sessions")
        sessions.create(
            repository,
            "nested lease guard",
            AccessProfile.WORKSPACE_WRITE,
            branch="main",
            session_id="session-a",
        )
        task = TaskStateStore(sessions).bootstrap(
            "session-a",
            {"objective": fact("nested plan", FactOrigin.VERIFIED, "test")},
        )
        bridge = _bridge(root, repository, sessions)
        leases = bridge.repository_leases
        held, _ = leases.acquire(
            repository,
            session_id="session-a",
            task_id=task.task_id,
            connection_id="plan-executor",
            current_operation="task.execute_plan",
            ttl_seconds=30,
        )
        try:
            result = bridge.execute(
                "karox.repo.write_file",
                {"path": "nested.txt", "content": "nested\n"},
                idempotency_key="nested-write",
                deadline_seconds=5,
            )
            assert result["ok"] is True
            assert leases.load(repository) is not None
            assert leases.load(repository).lease_id == held.lease_id
        finally:
            leases.release(repository, held)

        assert (repository / "nested.txt").read_text(encoding="utf-8") == "nested\n"
