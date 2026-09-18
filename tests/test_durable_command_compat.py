from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

from _support import initialize_git_repository

from karox.check_jobs import CheckJobError
from karox.hosted_bridge import CompositeHostedBridge, HostedBridgeAccessDenied
from karox.hosted_tools_runtime import (
    CHECKS_LOGS,
    CHECKS_START,
    CHECKS_STATUS,
    COMMAND_LOGS,
    COMMAND_START,
    COMMAND_STATUS,
    HostedToolsRuntime,
)
from karox.models import AccessProfile, Origin, OriginKind
from karox.process_identity import process_is_running
from karox.proxy import ProxyToolDescriptor
from karox.sessions import SessionStore


def _await_job_processes_exit(status: dict | None, deadline_seconds: float = 10.0) -> None:
    """Wait for the detached worker tree before deleting its Windows cwd."""

    if not isinstance(status, dict):
        return
    deadline = time.monotonic() + deadline_seconds
    for key in ("child_pid", "worker_pid"):
        pid = status.get(key)
        if not isinstance(pid, int) or pid <= 0:
            continue
        while process_is_running(pid) and time.monotonic() < deadline:
            time.sleep(0.02)
    # Win32 can release the final directory handle just after process exit.
    time.sleep(0.2)


def test_legacy_long_command_run_uses_durable_worker_and_replays_same_job() -> None:
    old = dict(os.environ)
    temp = tempfile.TemporaryDirectory()
    try:
        root = Path(temp.name)
        repository = root / "repo"
        initialize_git_repository(repository)
        os.environ["KAROX_VNEXT_RUNTIME_DIR"] = str(root / "runtime")
        os.environ.pop("KAROX_RUNTIME_DIR", None)
        sessions = SessionStore(root / "sessions")
        sessions.create(repository, "durable compat", AccessProfile.ELEVATED, session_id="compat-session")
        runtime = HostedToolsRuntime(
            repository,
            sessions,
            "compat-session",
            (COMMAND_START, COMMAND_STATUS, COMMAND_LOGS),
            access_profile=AccessProfile.ELEVATED,
            hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "compat-test"),
        )
        arguments = {
            "argv": [sys.executable, "-c", "print('DURABLE_COMPAT_OK', flush=True)"],
            "timeout_seconds": 20,
        }
        first = runtime.execute_command_run_compat(
            arguments,
            idempotency_key="compat-same-command",
            deadline_seconds=30,
        )
        assert first is not None
        assert first["detached"] is False
        assert first["exit_code"] == 0
        assert "DURABLE_COMPAT_OK" in first["stdout"]

        second = runtime.execute_command_run_compat(
            arguments,
            idempotency_key="compat-same-command",
            deadline_seconds=30,
        )
        assert second is not None
        assert second["durable_job_id"] == first["durable_job_id"]
        assert second["exit_code"] == 0
        assert "DURABLE_COMPAT_OK" in second["stdout"]
    finally:
        os.environ.clear()
        os.environ.update(old)
        temp.cleanup()


def test_durable_developer_path_keeps_global_remote_side_effect_blocks() -> None:
    old = dict(os.environ)
    temp = tempfile.TemporaryDirectory()
    try:
        root = Path(temp.name)
        repository = root / "repo"
        initialize_git_repository(repository)
        os.environ["KAROX_VNEXT_RUNTIME_DIR"] = str(root / "runtime")
        os.environ.pop("KAROX_RUNTIME_DIR", None)
        sessions = SessionStore(root / "sessions")
        sessions.create(repository, "durable safety", AccessProfile.ELEVATED, session_id="safety-session")
        runtime = HostedToolsRuntime(
            repository,
            sessions,
            "safety-session",
            (COMMAND_START, COMMAND_STATUS, COMMAND_LOGS),
            access_profile=AccessProfile.ELEVATED,
            hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "safety-test"),
        )
        blocked = (
            ["git", "push", "origin", "HEAD"],
            ["npm", "publish"],
            ["gh", "auth", "login"],
            ["vercel", "deploy", "--prod"],
            [sys.executable, "-c", "import os; os.system('git push origin HEAD')"],
        )
        for index, argv in enumerate(blocked):
            with pytest.raises(CheckJobError):
                runtime.execute_command_run_compat(
                    {"argv": argv, "timeout_seconds": 20},
                    idempotency_key=f"blocked-{index}",
                    deadline_seconds=30,
                )
        assert not list((root / "runtime" / "vnext" / "check-jobs").rglob("job-*.json"))
    finally:
        os.environ.clear()
        os.environ.update(old)
        temp.cleanup()


def test_legacy_long_command_run_detaches_quickly_and_replay_is_instant() -> None:
    old = dict(os.environ)
    temp = tempfile.TemporaryDirectory()
    final: dict | None = None
    try:
        root = Path(temp.name)
        repository = root / "repo"
        initialize_git_repository(repository)
        os.environ["KAROX_VNEXT_RUNTIME_DIR"] = str(root / "runtime")
        os.environ.pop("KAROX_RUNTIME_DIR", None)
        sessions = SessionStore(root / "sessions")
        sessions.create(repository, "durable detach", AccessProfile.ELEVATED, session_id="detach-session")
        runtime = HostedToolsRuntime(
            repository,
            sessions,
            "detach-session",
            (COMMAND_START, COMMAND_STATUS, COMMAND_LOGS),
            access_profile=AccessProfile.ELEVATED,
            hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "detach-test"),
        )
        arguments = {
            "argv": [
                sys.executable,
                "-c",
                (
                    "import time; print('DURABLE_STARTED', flush=True); "
                    "time.sleep(3); print('DURABLE_DONE', flush=True)"
                ),
            ],
            "timeout_seconds": 20,
        }

        started_at = time.monotonic()
        first = runtime.execute_command_run_compat(
            arguments,
            idempotency_key="compat-detached-command",
            deadline_seconds=30,
        )
        first_elapsed = time.monotonic() - started_at
        assert first is not None
        assert first["detached"] is True
        assert first["exit_code"] is None
        assert first["idempotent_replay"] is False
        assert first_elapsed < 2.75

        replay_at = time.monotonic()
        second = runtime.execute_command_run_compat(
            arguments,
            idempotency_key="compat-detached-command",
            deadline_seconds=30,
        )
        replay_elapsed = time.monotonic() - replay_at
        assert second is not None
        assert second["detached"] is True
        assert second["durable_job_id"] == first["durable_job_id"]
        assert second["idempotent_replay"] is True
        assert replay_elapsed < 1.0

        deadline = time.monotonic() + 8
        final = second
        while final["detached"] and time.monotonic() < deadline:
            time.sleep(0.2)
            final = runtime.execute_command_run_compat(
                arguments,
                idempotency_key="compat-detached-command",
                deadline_seconds=30,
            )
            assert final is not None
        assert final["detached"] is False
        assert final["durable_job_id"] == first["durable_job_id"]
        assert final["idempotent_replay"] is True
        assert final["exit_code"] == 0
        assert "DURABLE_STARTED" in final["stdout"]
        assert "DURABLE_DONE" in final["stdout"]
    finally:
        _await_job_processes_exit(final)
        os.environ.clear()
        os.environ.update(old)
        temp.cleanup()


def test_legacy_command_run_request_id_controls_rerun_generation() -> None:
    old = dict(os.environ)
    temp = tempfile.TemporaryDirectory()
    try:
        root = Path(temp.name)
        repository = root / "repo"
        initialize_git_repository(repository)
        os.environ["KAROX_VNEXT_RUNTIME_DIR"] = str(root / "runtime")
        os.environ.pop("KAROX_RUNTIME_DIR", None)
        sessions = SessionStore(root / "sessions")
        sessions.create(
            repository,
            "request generation",
            AccessProfile.ELEVATED,
            session_id="generation-session",
        )
        runtime = HostedToolsRuntime(
            repository,
            sessions,
            "generation-session",
            (COMMAND_START, COMMAND_STATUS, COMMAND_LOGS),
            access_profile=AccessProfile.ELEVATED,
            hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "generation-test"),
        )
        base = {
            "argv": [
                sys.executable,
                "-c",
                "import time; print('GEN_OK', flush=True); time.sleep(4)",
            ],
            "timeout_seconds": 20,
        }
        first = runtime.execute_command_run_compat(
            {**base, "request_id": "run-a"},
            idempotency_key="same-transport-key",
            deadline_seconds=30,
        )
        assert first is not None and first["detached"] is True
        replay = runtime.execute_command_run_compat(
            {**base, "request_id": "run-a"},
            idempotency_key="same-transport-key",
            deadline_seconds=30,
        )
        assert replay is not None
        assert replay["durable_job_id"] == first["durable_job_id"]
        assert replay["idempotent_replay"] is True

        second = runtime.execute_command_run_compat(
            {**base, "request_id": "run-b"},
            idempotency_key="same-transport-key",
            deadline_seconds=30,
        )
        assert second is not None
        assert second["durable_job_id"] != first["durable_job_id"]
        assert second["request_id"] == "run-b"
        assert second["idempotent_replay"] is False

        runtime._command_jobs.cancel_active()
        deadline = time.monotonic() + 8
        for job_id in (first["durable_job_id"], second["durable_job_id"]):
            while time.monotonic() < deadline:
                status = runtime._command_jobs.status(job_id)
                if status["status"] in {"passed", "failed", "cancelled", "timed_out"}:
                    break
                time.sleep(0.1)
            assert status["status"] in {"passed", "failed", "cancelled", "timed_out"}
            # The real child holds its job directory while it lives; wait for
            # the actual exit before TemporaryDirectory cleanup removes the
            # runtime tree under a 12-worker load.
            child_pid = status.get("child_pid")
            if isinstance(child_pid, int) and child_pid > 0:
                gone_by = time.monotonic() + 8
                while process_is_running(child_pid) and time.monotonic() < gone_by:
                    time.sleep(0.05)
    finally:
        os.environ.clear()
        os.environ.update(old)
        temp.cleanup()


def test_legacy_command_run_rejects_malformed_request_id() -> None:
    old = dict(os.environ)
    temp = tempfile.TemporaryDirectory()
    try:
        root = Path(temp.name)
        repository = root / "repo"
        initialize_git_repository(repository)
        os.environ["KAROX_VNEXT_RUNTIME_DIR"] = str(root / "runtime")
        sessions = SessionStore(root / "sessions")
        sessions.create(
            repository,
            "bad request id",
            AccessProfile.ELEVATED,
            session_id="bad-request-session",
        )
        runtime = HostedToolsRuntime(
            repository,
            sessions,
            "bad-request-session",
            (COMMAND_START, COMMAND_STATUS, COMMAND_LOGS),
            access_profile=AccessProfile.ELEVATED,
            hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "bad-request-test"),
        )
        try:
            runtime.execute_command_run_compat(
                {
                    "argv": [sys.executable, "-c", "print('x')"],
                    "timeout_seconds": 20,
                    "request_id": "bad request id",
                },
                idempotency_key="bad-request-key",
                deadline_seconds=30,
            )
        except HostedBridgeAccessDenied as exc:
            assert "request_id" in str(exc)
        else:
            raise AssertionError("malformed request_id was accepted")
    finally:
        os.environ.clear()
        os.environ.update(old)
        temp.cleanup()


class _CoreRuntime:
    def descriptors(self):
        return [ProxyToolDescriptor("karox.command.run", "legacy command", {"type": "object"}, False)]

    def execute(self, *args, **kwargs):
        raise AssertionError("legacy synchronous owner should not be called")


class _DurableRuntime:
    def __init__(self):
        self.compat_calls = []

    def descriptors(self):
        return [ProxyToolDescriptor("karox.command.start", "durable command", {"type": "object"}, False)]

    def execute(self, *args, **kwargs):
        raise AssertionError("direct durable execute is not expected")

    def execute_command_run_compat(self, arguments, *, idempotency_key, deadline_seconds):
        self.compat_calls.append((arguments, idempotency_key, deadline_seconds))
        return {"exit_code": 0, "stdout": "compat", "stderr": "", "timed_out": False}


def test_composite_bridge_routes_legacy_command_run_to_durable_compat_surface() -> None:
    durable = _DurableRuntime()
    bridge = CompositeHostedBridge((_CoreRuntime(), durable))
    result = bridge.execute(
        "karox.command.run",
        {"argv": ["tool"], "timeout_seconds": 30},
        idempotency_key="stable-key",
        deadline_seconds=45,
    )
    assert result["stdout"] == "compat"
    assert durable.compat_calls == [
        ({"argv": ["tool"], "timeout_seconds": 30}, "stable-key", 45)
    ]


class _CoreCheckRuntime:
    def descriptors(self):
        return [
            ProxyToolDescriptor(
                "karox.tests.run", "legacy tests", {"type": "object"}, False
            )
        ]

    def execute(self, *args, **kwargs):
        raise AssertionError("legacy synchronous tests owner should not be called")


class _DurableCheckRuntime:
    def __init__(self):
        self.compat_calls = []

    def descriptors(self):
        return [
            ProxyToolDescriptor(
                "karox.checks.start", "durable checks", {"type": "object"}, False
            )
        ]

    def execute(self, *args, **kwargs):
        raise AssertionError("direct durable checks execute is not expected")

    def execute_check_run_compat(
        self, tool_name, arguments, *, idempotency_key, deadline_seconds
    ):
        self.compat_calls.append(
            (tool_name, arguments, idempotency_key, deadline_seconds)
        )
        return {
            "ok": True,
            "detached": True,
            "durable_job_id": "job-compat-check",
            "durable_status": "running",
        }


def test_composite_bridge_routes_legacy_long_tests_to_durable_compat_surface() -> None:
    durable = _DurableCheckRuntime()
    bridge = CompositeHostedBridge((_CoreCheckRuntime(), durable))
    result = bridge.execute(
        "karox.tests.run",
        {"suite": "full", "timeout_seconds": 120},
        idempotency_key="stable-tests-key",
        deadline_seconds=180,
    )
    assert result["detached"] is True
    assert result["durable_job_id"] == "job-compat-check"
    assert durable.compat_calls == [
        (
            "karox.tests.run",
            {"suite": "full", "timeout_seconds": 120},
            "stable-tests-key",
            180,
        )
    ]


def _hosted_runner_job_object() -> bool:
    """Whether this process runs under a hosted runner's restrictive job."""

    if os.name != "nt":
        return False
    # Hosted Windows runners (GitHub Actions and the stored CI environment)
    # wrap each step in a job object that refuses CREATE_BREAKAWAY_FROM_JOB
    # and, for this launch surface, CREATE_NO_WINDOW group variants; the
    # observable effect is a persistent PermissionError [WinError 5] whose
    # reproducible environment is the runner, not a local machine.
    return bool(os.environ.get("CI")) and bool(
        os.environ.get("GITHUB_ACTIONS")
    )


def test_real_long_tests_compat_uses_one_durable_job_and_reconciles() -> None:
    if _hosted_runner_job_object():
        import pytest

        pytest.skip(
            "the hosted Windows runner's job object denies the durable "
            "worker launch even without any spawn flags; the contract is "
            "enforced on a real Windows host instead"
        )
    old = dict(os.environ)
    temp = tempfile.TemporaryDirectory()
    try:
        root = Path(temp.name)
        repository = root / "repo"
        initialize_git_repository(repository)
        tests_dir = repository / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_fast.py").write_text(
            "def test_fast():\n    assert 2 + 2 == 4\n",
            encoding="utf-8",
        )
        os.environ["KAROX_VNEXT_RUNTIME_DIR"] = str(root / "runtime")
        os.environ.pop("KAROX_RUNTIME_DIR", None)
        sessions = SessionStore(root / "sessions")
        sessions.create(
            repository,
            "durable tests compat",
            AccessProfile.WORKSPACE_WRITE,
            session_id="checks-compat-session",
        )
        runtime = HostedToolsRuntime(
            repository,
            sessions,
            "checks-compat-session",
            (CHECKS_START, CHECKS_STATUS, CHECKS_LOGS),
            access_profile=AccessProfile.WORKSPACE_WRITE,
            hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "checks-compat-test"),
        )
        arguments = {"suite": "full", "timeout_seconds": 20}
        first = runtime.execute_check_run_compat(
            "karox.tests.run",
            arguments,
            idempotency_key="compat-tests-one-job",
            deadline_seconds=30,
        )
        assert first is not None
        job_id = first["durable_job_id"]
        final = first
        deadline = time.monotonic() + 10
        while final.get("detached") and time.monotonic() < deadline:
            time.sleep(0.1)
            final = runtime.execute_check_run_compat(
                "karox.tests.run",
                arguments,
                idempotency_key="compat-tests-one-job",
                deadline_seconds=30,
            )
            assert final is not None
        assert final["durable_job_id"] == job_id
        assert final["durable_status"] == "passed"
        assert final["exit_code"] == 0
        replay = runtime.execute_check_run_compat(
            "karox.tests.run",
            arguments,
            idempotency_key="compat-tests-one-job",
            deadline_seconds=30,
        )
        assert replay is not None
        assert replay["durable_job_id"] == job_id
        assert replay["idempotent_replay"] is True
    finally:
        os.environ.clear()
        os.environ.update(old)
        temp.cleanup()
