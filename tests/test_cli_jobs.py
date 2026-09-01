from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _support import SRC  # noqa: F401

import karox.cli as cli


def _state(job_id: str, *, session_id: str, status: str, updated_at: float) -> SimpleNamespace:
    return SimpleNamespace(
        job_id=job_id,
        session_id=session_id,
        status=status,
        updated_at=updated_at,
        started_at=updated_at - 2.0,
        finished_at=(updated_at if status in {"passed", "failed", "cancelled", "timed_out"} else None),
        argv=("python", "-m", "pytest"),
        exit_code=(0 if status == "passed" else None),
        error_code=None,
        artifact_id=None,
        repository=".",
        worker_identity=None,
    )


def test_collect_durable_jobs_merges_check_and_command_stores(tmp_path: Path) -> None:
    check_base = tmp_path / "check-jobs"
    command_base = tmp_path / "dev-command-jobs"
    check_dir = check_base / "session-a"
    command_dir = command_base / "session-a"
    check_dir.mkdir(parents=True)
    command_dir.mkdir(parents=True)
    (check_dir / "job-aaaaaaaaaaaaaaaaaaaa.json").write_text("{}", encoding="utf-8")
    (command_dir / "job-bbbbbbbbbbbbbbbbbbbb.json").write_text("{}", encoding="utf-8")

    states = {
        "job-aaaaaaaaaaaaaaaaaaaa": _state(
            "job-aaaaaaaaaaaaaaaaaaaa", session_id="session-a", status="passed", updated_at=10.0
        ),
        "job-bbbbbbbbbbbbbbbbbbbb": _state(
            "job-bbbbbbbbbbbbbbbbbbbb", session_id="session-a", status="running", updated_at=20.0
        ),
    }

    class FakeStore:
        def __init__(self, session_id: str, *, root: Path):
            self.session_id = session_id
            self.root = root

        def get(self, job_id: str):
            return states[job_id]

        def get_scope(self, job_id: str):
            if job_id == "job-bbbbbbbbbbbbbbbbbbbb":
                return {"workstream_id": "frontend", "project_id": "project-a"}
            return None

    with (
        mock.patch.object(
            cli,
            "_durable_job_bases",
            return_value=(("check", check_base), ("command", command_base)),
        ),
        mock.patch("karox.check_jobs.CheckJobStore", FakeStore),
    ):
        report = cli._collect_durable_jobs(session_id="session-a", limit=10)

    assert report["status"] == "ok"
    assert report["total_count"] == 2
    assert report["active_count"] == 1
    assert report["stale_count"] == 0
    assert [item["job_id"] for item in report["recent"]] == [
        "job-bbbbbbbbbbbbbbbbbbbb",
        "job-aaaaaaaaaaaaaaaaaaaa",
    ]
    assert report["recent"][0]["kind"] == "command"
    assert report["recent"][0]["command"] == "python"
    assert report["recent"][0]["workstream_id"] == "frontend"
    assert report["recent"][0]["project_id"] == "project-a"
    assert report["recent"][1]["workstream_id"] == "legacy-unscoped"


def test_collect_durable_jobs_does_not_count_proven_dead_worker_as_active(tmp_path: Path) -> None:
    command_base = tmp_path / "dev-command-jobs"
    command_dir = command_base / "session-a"
    command_dir.mkdir(parents=True)
    job_id = "job-cccccccccccccccccccc"
    (command_dir / f"{job_id}.json").write_text("{}", encoding="utf-8")
    state = _state(job_id, session_id="session-a", status="running", updated_at=30.0)

    class FakeStore:
        def __init__(self, session_id: str, *, root: Path):
            self.session_id = session_id
            self.root = root

        def get(self, _job_id: str):
            return state

        def get_scope(self, _job_id: str):
            return {"workstream_id": "backend", "project_id": "project-a"}

    with (
        mock.patch.object(
            cli,
            "_durable_job_bases",
            return_value=(("command", command_base),),
        ),
        mock.patch("karox.check_jobs.CheckJobStore", FakeStore),
        mock.patch(
            "karox.check_jobs.effective_job_status",
            return_value=("failed", "worker_exited_without_final_state"),
        ),
    ):
        report = cli._collect_durable_jobs(session_id="session-a", limit=10)

    assert report["active_count"] == 0
    assert report["stale_count"] == 1
    assert report["recent"][0]["status"] == "failed"
    assert report["recent"][0]["persisted_status"] == "running"
    assert report["recent"][0]["error_code"] == "worker_exited_without_final_state"


def test_handle_cancel_delegates_to_owned_job_manager_without_pid_argument(tmp_path: Path) -> None:
    state = _state(
        "job-cccccccccccccccccccc",
        session_id="session-a",
        status="running",
        updated_at=30.0,
    )
    state.repository = str(tmp_path)
    manager = mock.Mock()
    manager.cancel.return_value = {
        "job_id": state.job_id,
        "status": "running",
        "cancel_requested": True,
    }
    emitted: dict[str, object] = {}

    with (
        mock.patch.object(
            cli,
            "_find_durable_job",
            return_value=("command", tmp_path / "dev-command-jobs", state),
        ),
        mock.patch("karox.check_jobs.CheckJobManager", return_value=manager) as manager_factory,
        mock.patch.object(
            cli,
            "_json",
            side_effect=lambda value: emitted.setdefault("payload", value),
        ),
    ):
        code = cli._handle_cancel(
            argparse.Namespace(job_id=state.job_id, session_id=None, json=True)
        )

    assert code == 0
    manager.cancel.assert_called_once_with(state.job_id)
    kwargs = manager_factory.call_args.kwargs
    assert kwargs["allow_developer_commands"] is True
    assert kwargs["wait_for_child"] is False
    assert emitted["payload"]["cancel_requested"] is True


def test_root_parser_exposes_jobs_and_cancel() -> None:
    jobs = cli._parser().parse_args(["jobs", "--limit", "5", "--json"])
    assert jobs.command == "jobs"
    assert jobs.limit == 5
    cancel = cli._parser().parse_args(["cancel", "job-aaaaaaaaaaaaaaaaaaaa", "--json"])
    assert cancel.command == "cancel"
    assert cancel.job_id == "job-aaaaaaaaaaaaaaaaaaaa"
