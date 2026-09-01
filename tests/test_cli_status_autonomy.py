from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _support import SRC  # noqa: F401

import karox.cli as cli


def _fact(value: object) -> SimpleNamespace:
    return SimpleNamespace(value=value)


def _state(task_id: str, updated_at: float, objective: str, *, phase: str, next_action: str):
    return SimpleNamespace(
        task_id=task_id,
        revision=1,
        updated_at=updated_at,
        facts={
            "objective": _fact(objective),
            "current_phase": _fact(phase),
            "next_safe_action": _fact(next_action),
            "current_blockers": _fact([]),
        },
    )


def test_status_prefers_most_recent_workstream_over_stale_default(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    record = SimpleNamespace(
        repository=str(repository),
        revoked=False,
        updated_at=100.0,
        session_id="session-a",
        task="legacy default task",
        phase="planning",
        status="active",
        changed_files=[],
        jobs=[],
        failures=[],
    )
    default = _state(
        "task-default",
        10.0,
        "stale default objective",
        phase="old",
        next_action="old next",
    )
    active = _state(
        "task-active",
        30.0,
        "current reliability objective",
        phase="verification",
        next_action="run live restart smoke",
    )
    tasks = mock.Mock()
    tasks.list_workstreams.return_value = ("reliability",)
    tasks.load_optional.side_effect = lambda _session, workstream_id=None: (
        default if workstream_id is None else active
    )
    sessions = mock.Mock()
    sessions.list.return_value = [record]
    registry = mock.Mock()
    registry.selected_model.return_value = None
    emitted: dict[str, object] = {}

    with (
        mock.patch.object(cli, "SessionStore", return_value=sessions),
        mock.patch.object(cli, "session_dir", return_value=tmp_path / "sessions"),
        mock.patch.object(cli, "_registry", return_value=registry),
        mock.patch("karox.task_state.TaskStateStore", return_value=tasks),
        mock.patch("karox.tui._load_agent_mode", return_value="build"),
        mock.patch("karox.tui._load_effort_level", return_value="high"),
        mock.patch.object(cli, "WebBridgeProfileStore") as bridge_profiles,
        mock.patch.object(cli, "RepositoryLeaseStore") as lease_store,
        mock.patch.object(
            cli,
            "_emit",
            side_effect=lambda value, *, json_output: emitted.setdefault("payload", value),
        ),
    ):
        bridge_profiles.return_value.list.return_value = []
        lease_store.return_value.doctor.return_value = {"leases": []}
        exit_code = cli._handle_status(
            argparse.Namespace(repository=repository, json=True)
        )

    assert exit_code == 0
    payload = emitted["payload"]
    assert isinstance(payload, dict)
    assert payload["status"] == "ok"
    assert payload["task"]["workstream_id"] == "reliability"
    assert payload["task"]["task_id"] == "task-active"
    assert payload["task"]["objective"] == "current reliability objective"
    assert payload["next"] == "run live restart smoke"
    assert payload["workstreams"]["recent"][0]["workstream_id"] == "reliability"
