"""Peer adversarial selections executed through the real bridge and worker.

No fake delegate: these tests catch disagreement between selector discovery
and the actual Python/Node runner, including a falsely green full fallback.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

from _support import initialize_git_repository
from karox.affected_checks import AffectedChecksEngine
from karox.artifacts import ArtifactStore
from karox.hosted_bridge import CoreToolBridge
from karox.models import AccessProfile
from karox.repo_context import RepositoryContextEngine
from karox.repository_lease import RepositoryLeaseStore
from karox.sessions import SessionStore
from karox.task_state import TaskStateStore


def _product(tmp_path: Path):
    repo = tmp_path / "repo"
    initialize_git_repository(repo)
    sessions = SessionStore(tmp_path / "sessions")
    session_id = "worker-integration-" + tmp_path.name
    sessions.create(repo, "Worker integration", AccessProfile.WORKSPACE_WRITE,
                    session_id=session_id)
    artifacts = ArtifactStore(session_id)
    context = RepositoryContextEngine(repo, artifacts, policy_profile="workspace_write")
    delegate = CoreToolBridge(repo, sessions, session_id, ["karox.tests.run"])
    engine = AffectedChecksEngine(
        repository=repo, session_id=session_id, connection_id="worker-local",
        delegate=delegate, verification_commands=(), artifacts=artifacts,
        repo_context=context, task_states=TaskStateStore(sessions),
        lease_store=RepositoryLeaseStore(tmp_path / "leases"),
    )
    return repo, engine, delegate


def _mixed_repo(repo: Path) -> None:
    (repo / "package.json").write_text(json.dumps({
        "scripts": {"test": 'node -e "process.exit(0)"'},
    }), encoding="utf-8")


def test_suffix_selection_executes_python_through_actual_worker(tmp_path: Path) -> None:
    repo, engine, delegate = _product(tmp_path)
    (repo / "tests").mkdir()
    (repo / "tests/widget_test.py").write_text(
        "def test_widget():\n    assert True\n", encoding="utf-8",
    )
    _mixed_repo(repo)
    selection = engine.select(["tests/widget_test.py"])
    assert selection[0]["arguments"] == {
        "suite": "focused", "targets": ["tests/widget_test.py"],
    }
    result = delegate.execute(selection[0]["tool"], selection[0]["arguments"],
                              idempotency_key="suffix-focused", deadline_seconds=60)
    assert result["ok"], result
    assert result["data"]["argv"][:3] == [sys.executable, "-m", "pytest"], result
    assert "1 passed" in result["data"]["stdout"], result


def test_suffix_budget_fallback_runs_all_python_tests_not_node(tmp_path: Path) -> None:
    repo, engine, delegate = _product(tmp_path)
    (repo / "tests").mkdir()
    (repo / "src").mkdir()
    (repo / "src/widget.py").write_text("VALUE = 1\n", encoding="utf-8")
    _mixed_repo(repo)
    for index in range(201):
        (repo / f"tests/widget_{index:03}_test.py").write_text(
            f"# widget\ndef test_widget():\n    assert {index != 200!r}\n", encoding="utf-8",
        )
    selection = engine.select(["src/widget.py"])
    assert selection[0]["arguments"] == {"suite": "full"}
    result = delegate.execute(selection[0]["tool"], selection[0]["arguments"],
                              idempotency_key="suffix-budget", deadline_seconds=60)
    assert result["data"]["argv"][:3] == [sys.executable, "-m", "pytest"], result
    assert result["data"]["exit_code"] == 1, result
    assert "1 failed, 200 passed" in result["data"]["stdout"], result


def test_suffix_split_discovery_runs_actual_pytest(tmp_path: Path) -> None:
    repo, _, delegate = _product(tmp_path)
    (repo / "tests/nested").mkdir(parents=True)
    (repo / "tests/nested/widget_test.py").write_text(
        "def test_widget():\n    assert True\n", encoding="utf-8",
    )
    _mixed_repo(repo)
    result = delegate.execute("karox.tests.run", {"suite": "split", "split": 2, "part": 1},
                              idempotency_key="suffix-split", deadline_seconds=60)
    assert result["ok"], result
    assert result["data"]["argv"][:3] == [sys.executable, "-m", "pytest"], result
    assert "tests/nested/widget_test.py" in result["data"]["argv"], result
    assert "1 passed" in result["data"]["stdout"], result
