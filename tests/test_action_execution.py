from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from _support import initialize_git_repository
from karox.action_execution import CapabilityCoreRuntime
from karox.action_policy import ActionConfirmationRequired, ActionDecisionEngine
from karox.models import AccessProfile, CoreCommand, Origin, OriginKind
from karox.policy import CapabilityPolicy
from karox.risk_engine import RiskEngine
from karox.sessions import SessionStore


def _delete_command(session_id: str) -> CoreCommand:
    return CoreCommand(
        name="repo.command",
        arguments={
            "action": "batch",
            "payload": {"operations": [{"op": "delete", "path": "src/legacy.py"}]},
        },
        session_id=session_id,
        origin=Origin(OriginKind.NATIVE_AGENT, "test-agent"),
    )


def _runtime(
    repository: Path,
    sessions: SessionStore,
    *,
    rollback_checkpoint_id: str | None = None,
) -> CapabilityCoreRuntime:
    risk = RiskEngine()
    return CapabilityCoreRuntime(
        repository,
        CapabilityPolicy(AccessProfile.ELEVATED),
        sessions,
        repository.parent / "audit.jsonl",
        risk=risk,
        action_decisions=ActionDecisionEngine(risk),
        rollback_checkpoint_id=rollback_checkpoint_id,
    )


def test_stale_session_checkpoint_does_not_authorize_current_delete() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        repository = Path(temporary) / "repo"
        initialize_git_repository(repository)
        (repository / "src").mkdir()
        (repository / "src" / "legacy.py").write_text("OLD = True\n", encoding="utf-8")
        sessions = SessionStore(Path(temporary) / "sessions")
        sessions.create(
            repository,
            "refactor the parser",
            AccessProfile.ELEVATED,
            session_id="session",
        )
        with sessions.mutate("session", "stale-checkpoint-fixture") as record:
            record.checkpoints.append({"checkpoint_id": "cp-from-an-older-turn"})

        with pytest.raises(ActionConfirmationRequired):
            _runtime(repository, sessions)._apply_smart_stop(_delete_command("session"))


def test_current_turn_checkpoint_makes_workspace_delete_guarded_auto() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        repository = Path(temporary) / "repo"
        initialize_git_repository(repository)
        (repository / "src").mkdir()
        (repository / "src" / "legacy.py").write_text("OLD = True\n", encoding="utf-8")
        sessions = SessionStore(Path(temporary) / "sessions")
        sessions.create(
            repository,
            "refactor the parser",
            AccessProfile.ELEVATED,
            session_id="session",
        )

        runtime = _runtime(
            repository,
            sessions,
            rollback_checkpoint_id="cp-current-turn",
        )
        runtime._apply_smart_stop(_delete_command("session"))
