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


def _delete_command(session_id: str, *, dry_run: bool = False) -> CoreCommand:
    payload: dict[str, object] = {
        "operations": [{"op": "delete", "path": "src/legacy.py"}]
    }
    if dry_run:
        payload["dry_run"] = True
    return CoreCommand(
        name="repo.command",
        arguments={"action": "batch", "payload": payload},
        session_id=session_id,
        origin=Origin(OriginKind.NATIVE_AGENT, "test-agent"),
    )


def _workspace(temporary: str) -> tuple[Path, SessionStore]:
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
    return repository, sessions


def _runtime(
    repository: Path,
    sessions: SessionStore,
    *,
    checkpoint_factory=None,
) -> CapabilityCoreRuntime:
    risk = RiskEngine()
    return CapabilityCoreRuntime(
        repository,
        CapabilityPolicy(AccessProfile.ELEVATED),
        sessions,
        repository.parent / "audit.jsonl",
        risk=risk,
        action_decisions=ActionDecisionEngine(risk),
        checkpoint_factory=checkpoint_factory,
    )


def test_checkpoint_factory_does_not_silently_dissolve_delete_confirmation() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        repository, sessions = _workspace(temporary)
        calls: list[str] = []

        def factory() -> str:
            calls.append("created")
            return "cp-on-demand"

        runtime = _runtime(repository, sessions, checkpoint_factory=factory)
        with pytest.raises(ActionConfirmationRequired):
            runtime._apply_smart_stop(_delete_command("session"))
        assert calls == []
        assert runtime._rollback_checkpoint_id is None


def test_factory_failure_keeps_the_confirmation_boundary() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        repository, sessions = _workspace(temporary)

        def factory() -> str:
            raise RuntimeError("checkpoint store unavailable")

        runtime = _runtime(repository, sessions, checkpoint_factory=factory)
        with pytest.raises(ActionConfirmationRequired):
            runtime._apply_smart_stop(_delete_command("session"))
        assert runtime._rollback_checkpoint_id is None


def test_without_factory_the_stop_is_unchanged() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        repository, sessions = _workspace(temporary)
        runtime = _runtime(repository, sessions)
        with pytest.raises(ActionConfirmationRequired):
            runtime._apply_smart_stop(_delete_command("session"))


def test_dry_run_batch_is_a_free_preview() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        repository, sessions = _workspace(temporary)
        calls: list[str] = []

        def factory() -> str:
            calls.append("created")
            return "cp-unused"

        runtime = _runtime(repository, sessions, checkpoint_factory=factory)
        runtime._apply_smart_stop(_delete_command("session", dry_run=True))
        assert calls == []
        assert runtime._rollback_checkpoint_id is None
