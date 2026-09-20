"""Lifecycle and process-isolation contracts for durable verification jobs."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from _support import ROOT, SRC, initialize_git_repository  # noqa: F401

import karox
import karox.check_jobs as check_jobs
import karox.core as core_module
from karox.check_jobs import (
    CheckJobError,
    CheckJobManager,
    CheckJobState,
    CheckJobStore,
    build_job_argv,
    effective_job_status,
)
from karox.core import CoreRuntime
from karox.hosted_tools_runtime import (
    CHECKS_CANCEL,
    CHECKS_LOGS,
    CHECKS_START,
    CHECKS_STATUS,
    HostedToolsRuntime,
)
from karox.models import AccessProfile, Origin, OriginKind
from karox.policy import CapabilityPolicy
from karox.process_identity import ProcessIdentity, capture_process_identity
from karox.sessions import SessionStore


def _wait_status(manager: CheckJobManager, job_id: str, expected: set[str], timeout: float = 8.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = manager.status(job_id)
        if last["status"] in expected:
            return last
        time.sleep(0.05)
    raise AssertionError(f"job did not reach {expected}: {last}")


class CheckJobSourceContractTests(unittest.TestCase):
    def test_source_suite_imports_repository_src(self) -> None:
        imported = Path(karox.__file__).resolve()
        self.assertTrue(
            imported.is_relative_to(SRC.resolve()),
            f"source suite imported {imported}, expected {SRC.resolve()}",
        )

    def test_source_worker_environment_points_only_at_src(self) -> None:
        environment = check_jobs._worker_environment()
        module = Path(check_jobs.__file__).resolve()
        if module.parents[1].name == "src":
            self.assertEqual(Path(environment["PYTHONPATH"]).resolve(), SRC.resolve())
        else:
            self.assertNotIn(str(SRC.resolve()), environment.get("PYTHONPATH", ""))

    def test_windows_worker_flags_are_console_and_job_isolated(self) -> None:
        with mock.patch.object(check_jobs.os, "name", "nt"):
            flags = check_jobs._worker_creationflags()
        self.assertTrue(flags & 0x00000200, "CREATE_NEW_PROCESS_GROUP missing")
        self.assertTrue(flags & 0x01000000, "CREATE_BREAKAWAY_FROM_JOB missing")
        self.assertTrue(flags & 0x08000000, "CREATE_NO_WINDOW missing")

    def test_legacy_windows_child_gets_new_process_group(self) -> None:
        with mock.patch.object(core_module.os, "name", "nt"):
            options = core_module._new_process_group_kwargs()
        flags = int(options["creationflags"])
        self.assertTrue(flags & 0x00000200)
        self.assertTrue(flags & 0x08000000)


class CheckJobStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        self.repository.mkdir()
        (self.repository / "tests").mkdir()
        (self.repository / "tests" / "test_a.py").write_text("def test_a(): pass\n", encoding="utf-8")
        (self.repository / "tests" / "test_b.py").write_text("def test_b(): pass\n", encoding="utf-8")
        self.store = CheckJobStore("session-a", root=self.root / "jobs")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _state(self) -> CheckJobState:
        now = time.time()
        return CheckJobState(
            schema_version=1,
            job_id="job-" + "a" * 20,
            session_id="session-a",
            repository=str(self.repository),
            argv=(sys.executable, "-c", "print('ok')"),
            command_sha256="b" * 64,
            idempotency_sha256="c" * 64,
            status="queued",
            queued_at=now,
            started_at=None,
            updated_at=now,
            finished_at=None,
            timeout_seconds=30.0,
            bridge_pid=os.getpid(),
            bridge_identity=capture_process_identity(os.getpid()),
            worker_identity=None,
            child_identity=None,
            process_group="pending",
            cancellation_source=None,
            signal_sent=None,
            exit_code=None,
            error_code=None,
            error=None,
            log_path=str(self.root / "job.log"),
            artifact_id=None,
            log_truncated=False,
        )

    def test_effective_status_drops_dead_worker_from_running_state(self) -> None:
        state = replace(
            self._state(),
            status="running",
            started_at=time.time(),
            worker_identity=capture_process_identity(os.getpid()),
        )
        status, error_code = effective_job_status(
            state,
            pid_alive=lambda _pid: False,
        )
        self.assertEqual(status, "failed")
        self.assertEqual(error_code, "worker_exited_without_final_state")

    def test_public_status_uses_one_worker_liveness_snapshot(self) -> None:
        manager = CheckJobManager(self.repository, "session-a", root=self.root / "jobs")
        identity = capture_process_identity(os.getpid())
        state = replace(
            self._state(), status="running", worker_identity=identity, child_identity=identity
        )
        alive = mock.Mock(alive=True)
        alive.to_dict.return_value = {"alive": True}
        dead = mock.Mock(alive=False)
        dead.to_dict.return_value = {"alive": False}
        with mock.patch.object(
            check_jobs, "verify_process_identity", side_effect=[alive, alive, dead]
        ) as verify:
            result = manager.public_status(state)
        self.assertEqual(result["status"], "running")
        self.assertTrue(result["ownership"]["worker"]["alive"])
        self.assertEqual(verify.call_count, 2, "probe each identity once, not the worker twice")

    def test_logs_read_once_and_reconcile_dead_worker_without_status_packet(self) -> None:
        manager = CheckJobManager(self.repository, "session-a", root=self.root / "jobs")
        state = replace(
            self._state(),
            status="running",
            worker_identity=capture_process_identity(os.getpid()),
        )
        self.store.put(state)
        Path(state.log_path).write_bytes(b"earlier\nlast line\n")
        with (
            mock.patch.object(check_jobs, "read_job_log", wraps=check_jobs.read_job_log) as read,
            mock.patch.object(
                check_jobs, "verify_process_identity", return_value=mock.Mock(alive=False)
            ) as verify,
            mock.patch.object(manager, "public_status", side_effect=AssertionError("unused")),
        ):
            result = manager.logs(state.job_id, limit=10)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["log"]["text"], "last line\n")
        read.assert_called_once_with(Path(state.log_path), limit=10)
        verify.assert_called_once()

    def test_logs_terminal_status_does_not_probe_processes(self) -> None:
        manager = CheckJobManager(self.repository, "session-a", root=self.root / "jobs")
        state = replace(self._state(), status="passed", exit_code=0)
        self.store.put(state)
        with (
            mock.patch.object(check_jobs, "verify_process_identity") as verify,
            mock.patch.object(manager, "public_status", side_effect=AssertionError("unused")),
        ):
            result = manager.logs(state.job_id)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["log"]["bytes"], 0)
        verify.assert_not_called()

    def test_log_tail_measures_opened_file_after_concurrent_trim(self) -> None:
        path = self.root / "trimmed.log"
        path.write_bytes(b"old log\n" * 100)
        actual_open = Path.open

        def trim_then_open(target: Path, *args: Any, **kwargs: Any) -> Any:
            if target == path:
                # Use builtin open to avoid re-entering the patched Path.open.
                with open(path, "wb") as writer:
                    writer.write(b"new log\n")
            return actual_open(target, *args, **kwargs)

        with mock.patch.object(Path, "open", autospec=True, side_effect=trim_then_open):
            result = check_jobs.read_job_log(path, limit=16)
        self.assertEqual(result["text"], "new log\n")
        self.assertEqual(result["bytes"], 8)
        self.assertFalse(result["truncated"])

    def test_log_reader_batches_writes_without_changing_redaction_boundaries(self) -> None:
        path = self.root / "batched.log"
        text = "SENSITIVE line\n" * 200 + "unfinished tail"
        with (
            mock.patch.object(
                check_jobs, "redact", side_effect=lambda line: line.replace("SENSITIVE", "[redacted]")
            ) as redact_line,
            mock.patch.object(
                check_jobs, "_write_capped_log", wraps=check_jobs._write_capped_log
            ) as write,
        ):
            check_jobs._append_redacted(io.StringIO(text), path, threading.Event(), [False])
        self.assertEqual(path.read_text(encoding="utf-8"), text.replace("SENSITIVE", "[redacted]"))
        self.assertEqual(
            redact_line.call_args_list,
            [mock.call("SENSITIVE line\n")] * 200 + [mock.call("unfinished tail")],
        )
        self.assertEqual(write.call_count, 2, "one chunk append plus the EOF partial line")

    def test_batched_log_reader_remains_capped_and_keeps_final_summary(self) -> None:
        path = self.root / "capped.log"
        text = "a complete line\n" * 1000 + "1 passed in 0.01s\n"
        truncated = [False]
        with (
            mock.patch.object(check_jobs, "_MAX_LOG_BYTES", 128),
            mock.patch.object(check_jobs, "_LOG_TRIM_TO_BYTES", 64),
        ):
            check_jobs._append_redacted(io.StringIO(text), path, threading.Event(), truncated)
        self.assertTrue(truncated[0])
        self.assertLessEqual(path.stat().st_size, 128)
        self.assertTrue(path.read_bytes().endswith(b"1 passed in 0.01s\n"))

    def test_checksum_mismatch_is_fail_safe(self) -> None:
        state = self._state()
        self.store.put(state)
        path = self.store.state_path(state.job_id)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["status"] = "passed"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(CheckJobError, "checksum mismatch"):
            self.store.get(state.job_id)

    def test_truncated_state_is_diagnostic(self) -> None:
        state = self._state()
        self.store.state_path(state.job_id).write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(CheckJobError, "unreadable"):
            self.store.get(state.job_id)

    def test_job_scope_sidecar_round_trips_without_changing_state_schema(self) -> None:
        state = self._state()
        self.store.put(state)
        self.store.put_scope(
            state.job_id,
            workstream_id="frontend",
            project_id="project-a",
        )
        self.assertEqual(
            self.store.get_scope(state.job_id),
            {"workstream_id": "frontend", "project_id": "project-a"},
        )
        self.assertEqual(self.store.get(state.job_id).schema_version, 1)
        self.assertTrue(self.store.scope_path(state.job_id).is_file())

    def test_state_read_retries_transient_sharing_error(self) -> None:
        state = self._state()
        self.store.put(state)
        target = self.store.state_path(state.job_id)
        actual_read_text = Path.read_text
        attempts = 0

        def flaky_read_text(path: Path, *args: Any, **kwargs: Any) -> str:
            nonlocal attempts
            if path == target:
                attempts += 1
                if attempts < 3:
                    raise PermissionError("synthetic sharing violation")
            return actual_read_text(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", autospec=True, side_effect=flaky_read_text):
            loaded = self.store.get(state.job_id)

        self.assertEqual(loaded.job_id, state.job_id)
        self.assertEqual(attempts, 3)

    def test_atomic_state_replace_retries_windows_sharing_violation(self) -> None:
        state = self._state()
        actual_replace = os.replace
        attempts = 0

        def flaky_replace(source: Any, destination: Any) -> None:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                error = PermissionError("synthetic sharing violation")
                error.winerror = 32  # type: ignore[attr-defined]
                raise error
            actual_replace(source, destination)

        with (
            mock.patch.object(check_jobs.os, "replace", side_effect=flaky_replace),
            mock.patch.object(
                check_jobs,
                "_retryable_atomic_replace_error",
                return_value=True,
            ),
        ):
            self.store.put(state)

        self.assertEqual(attempts, 3)
        self.assertEqual(self.store.get(state.job_id), state)

    def test_pytest_argv_selection_is_deterministic_and_confined(self) -> None:
        full = build_job_argv(self.repository, {"kind": "pytest", "suite": "full"}, ())
        self.assertEqual(full[:3], (sys.executable, "-m", "pytest"))
        focused = build_job_argv(
            self.repository,
            {"kind": "pytest", "suite": "focused", "targets": ["tests/test_b.py"]},
            (),
        )
        self.assertEqual(focused[-1], "tests/test_b.py")
        split = build_job_argv(
            self.repository,
            {"kind": "pytest", "suite": "split", "split": 2, "part": 1},
            (),
        )
        self.assertEqual(split[-1], "tests/test_a.py")
        with self.assertRaises(CheckJobError):
            build_job_argv(
                self.repository,
                {"kind": "pytest", "suite": "focused", "targets": ["../outside.py"]},
                (),
            )

    def test_check_argv_requires_exact_allowlist(self) -> None:
        argv = (sys.executable, "-c", "print('ok')")
        self.assertEqual(
            build_job_argv(
                self.repository,
                {"kind": "check", "argv": list(argv)},
                (argv,),
            ),
            argv,
        )
        with self.assertRaisesRegex(CheckJobError, "allowlist"):
            build_job_argv(
                self.repository,
                {"kind": "check", "argv": [sys.executable, "-c", "print('other')"]},
                (argv,),
            )


class CheckJobManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_env = dict(os.environ)
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        self.repository.mkdir()
        os.environ["KAROX_VNEXT_RUNTIME_DIR"] = str(self.root / "runtime")
        self.jobs_root = self.root / "jobs"
        self.allowed = (sys.executable, "-c", "print('ok')")

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.old_env)
        self.temporary.cleanup()

    def _completed_launcher(self, *, log_text: str = "1 passed in 0.01s\n"):
        def launch(state_path: Path) -> Any:
            store = CheckJobStore("session-a", root=self.jobs_root)
            state = store.get(state_path.stem)
            Path(state.log_path).write_text(log_text, encoding="utf-8")
            identity = capture_process_identity(os.getpid())
            store.put(
                replace(
                    state,
                    status="passed",
                    started_at=time.time(),
                    updated_at=time.time(),
                    finished_at=time.time(),
                    worker_identity=identity,
                    child_identity=identity,
                    exit_code=0,
                    process_group="synthetic-test-worker",
                )
            )
            return mock.Mock(pid=os.getpid())

        return launch

    def test_start_returns_fast_and_replay_does_not_duplicate(self) -> None:
        launcher = mock.Mock(side_effect=self._completed_launcher())
        manager = CheckJobManager(
            self.repository,
            "session-a",
            (self.allowed,),
            root=self.jobs_root,
            worker_launcher=launcher,
        )
        arguments = {"kind": "check", "argv": list(self.allowed), "timeout_seconds": 30}
        first = manager.start(arguments, idempotency_key="same-key")
        second = manager.start(arguments, idempotency_key="same-key")
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(launcher.call_count, 1)

    def test_same_check_after_workspace_change_starts_a_fresh_job(self) -> None:
        launcher = mock.Mock(side_effect=self._completed_launcher())
        manager = CheckJobManager(
            self.repository,
            "session-a",
            (self.allowed,),
            root=self.jobs_root,
            worker_launcher=launcher,
        )
        arguments = {"kind": "check", "argv": list(self.allowed), "timeout_seconds": 30}

        first = manager.start(arguments, idempotency_key="workspace-aware")
        retry = manager.start(arguments, idempotency_key="workspace-aware")
        self.assertEqual(first["job_id"], retry["job_id"])
        self.assertTrue(retry["idempotent_replay"])

        (self.repository / "changed.txt").write_text("new state\n", encoding="utf-8")
        fresh = manager.start(arguments, idempotency_key="workspace-aware")
        fresh_retry = manager.start(arguments, idempotency_key="workspace-aware")

        self.assertNotEqual(first["job_id"], fresh["job_id"])
        self.assertFalse(fresh["idempotent_replay"])
        self.assertEqual(fresh["job_id"], fresh_retry["job_id"])
        self.assertTrue(fresh_retry["idempotent_replay"])
        self.assertEqual(launcher.call_count, 2)

    def test_revisited_workspace_does_not_overwrite_an_earlier_job(self) -> None:
        launcher = mock.Mock(return_value=mock.Mock(pid=os.getpid()))
        manager = CheckJobManager(
            self.repository,
            "session-a",
            (self.allowed,),
            root=self.jobs_root,
            worker_launcher=launcher,
            wait_for_child=False,
        )
        arguments = {"kind": "check", "argv": list(self.allowed)}
        with mock.patch.object(
            check_jobs, "_workspace_state_fingerprint", side_effect=["a" * 64, "b" * 64, "a" * 64, "a" * 64]
        ):
            first = manager.start(arguments, idempotency_key="revisited-workspace")
            original = manager.store.get(first["job_id"])
            manager.store.request_cancel(original.job_id)
            Path(original.log_path).write_text("earlier worker output\n", encoding="utf-8")
            original_bytes = manager.store.state_path(original.job_id).read_bytes()
            second = manager.start(arguments, idempotency_key="revisited-workspace")
            third = manager.start(arguments, idempotency_key="revisited-workspace")
            replay = manager.start(arguments, idempotency_key="revisited-workspace")
        self.assertEqual(len({first["job_id"], second["job_id"], third["job_id"]}), 3)
        self.assertEqual(manager.store.state_path(original.job_id).read_bytes(), original_bytes)
        self.assertEqual(Path(original.log_path).read_text(encoding="utf-8"), "earlier worker output\n")
        self.assertTrue(manager.store.cancel_path(original.job_id).exists())
        self.assertFalse(manager.store.cancel_path(third["job_id"]).exists())
        self.assertEqual(replay["job_id"], third["job_id"])
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(launcher.call_count, 3)

    def test_idempotency_key_cannot_change_command(self) -> None:
        manager = CheckJobManager(
            self.repository,
            "session-a",
            (self.allowed, (sys.executable, "-c", "print('two')")),
            root=self.jobs_root,
            worker_launcher=self._completed_launcher(),
        )
        manager.start(
            {"kind": "check", "argv": list(self.allowed)},
            idempotency_key="fixed",
        )
        with self.assertRaisesRegex(CheckJobError, "different command"):
            manager.start(
                {"kind": "check", "argv": [sys.executable, "-c", "print('two')"]},
                idempotency_key="fixed",
            )

    def test_status_and_logs_survive_new_manager(self) -> None:
        manager = CheckJobManager(
            self.repository,
            "session-a",
            (self.allowed,),
            root=self.jobs_root,
            worker_launcher=self._completed_launcher(log_text="collected 1 item\n1 passed\n"),
        )
        started = manager.start(
            {"kind": "check", "argv": list(self.allowed)},
            idempotency_key="durable",
        )
        restored = CheckJobManager(
            self.repository,
            "session-a",
            (self.allowed,),
            root=self.jobs_root,
        )
        status = restored.status(started["job_id"])
        logs = restored.logs(started["job_id"], limit=1024)
        self.assertEqual(status["status"], "passed")
        self.assertEqual(status["exit_code"], 0)
        self.assertIn("1 passed", logs["log"]["text"])

    def test_cancel_completed_job_is_idempotent(self) -> None:
        manager = CheckJobManager(
            self.repository,
            "session-a",
            (self.allowed,),
            root=self.jobs_root,
            worker_launcher=self._completed_launcher(),
        )
        started = manager.start(
            {"kind": "check", "argv": list(self.allowed)},
            idempotency_key="done",
        )
        cancelled = manager.cancel(started["job_id"])
        self.assertTrue(cancelled["idempotent"])
        self.assertFalse(cancelled["cancel_requested"])

    def test_worker_exit_before_child_identity_becomes_terminal_failure(self) -> None:
        class ExitedWorker:
            pid = 999999

            def poll(self) -> int:
                return 7

        manager = CheckJobManager(
            self.repository,
            "session-a",
            (self.allowed,),
            root=self.jobs_root,
            worker_launcher=lambda _path: ExitedWorker(),
        )
        with mock.patch.object(check_jobs, "_WORKER_EXIT_STATE_GRACE_SECONDS", 0.0):
            result = manager.start(
                {"kind": "check", "argv": list(self.allowed)},
                idempotency_key="worker-exit",
            )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            result["diagnostics"]["error_code"],
            "worker_exited_before_child_identity",
        )
        self.assertEqual(result["exit_code"], 7)

    def test_delayed_terminal_state_wins_over_observed_worker_exit(self) -> None:
        class ExitedWorker:
            pid = 999998

            def poll(self) -> int:
                return 1

        writers: list[threading.Thread] = []

        def launch(state_path: Path) -> Any:
            def publish() -> None:
                time.sleep(0.2)
                store = CheckJobStore("session-a", root=self.jobs_root)
                state = store.get(state_path.stem)
                identity = capture_process_identity(os.getpid())
                store.put(
                    replace(
                        state,
                        status="passed",
                        started_at=time.time(),
                        updated_at=time.time(),
                        finished_at=time.time(),
                        worker_identity=identity,
                        child_identity=identity,
                        exit_code=0,
                    )
                )

            writer = threading.Thread(target=publish)
            writer.start()
            writers.append(writer)
            return ExitedWorker()

        manager = CheckJobManager(
            self.repository,
            "session-a",
            (self.allowed,),
            root=self.jobs_root,
            worker_launcher=launch,
        )
        result = manager.start(
            {"kind": "check", "argv": list(self.allowed)},
            idempotency_key="delayed-terminal-state",
        )
        for writer in writers:
            writer.join(timeout=2)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["exit_code"], 0)

    def test_live_log_is_redacted_and_size_bounded(self) -> None:
        path = self.root / "bounded.log"
        with (
            mock.patch.object(check_jobs, "_MAX_LOG_BYTES", 64),
            mock.patch.object(check_jobs, "_LOG_TRIM_TO_BYTES", 32),
        ):
            with path.open("w+b") as handle:
                first = check_jobs._write_capped_log(handle, b"a" * 48)
                second = check_jobs._write_capped_log(handle, b"b" * 48)
        self.assertFalse(first)
        self.assertTrue(second)
        payload = path.read_bytes()
        self.assertLessEqual(len(payload), 64)
        self.assertTrue(payload.endswith(b"b" * 48))

    def test_worker_bootstrap_exception_publishes_terminal_state(self) -> None:
        store = CheckJobStore("session-a", root=self.jobs_root)
        state = CheckJobState(
            schema_version=1,
            job_id="job-" + "d" * 20,
            session_id="session-a",
            repository=str(self.repository),
            argv=self.allowed,
            command_sha256="e" * 64,
            idempotency_sha256="f" * 64,
            status="queued",
            queued_at=time.time(),
            started_at=None,
            updated_at=time.time(),
            finished_at=None,
            timeout_seconds=30.0,
            bridge_pid=os.getpid(),
            bridge_identity=capture_process_identity(os.getpid()),
            worker_identity=None,
            child_identity=None,
            process_group="pending",
            cancellation_source=None,
            signal_sent=None,
            exit_code=None,
            error_code=None,
            error=None,
            log_path=str(store.root / "bootstrap.log"),
            artifact_id=None,
            log_truncated=False,
        )
        store.put(state)
        with mock.patch.object(
            check_jobs.subprocess,
            "Popen",
            side_effect=OSError("synthetic bootstrap failure"),
        ):
            self.assertEqual(check_jobs.run_worker(store.state_path(state.job_id)), 1)
        failed = store.get(state.job_id)
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.error_code, "worker_exception")
        self.assertIn("synthetic bootstrap failure", failed.error or "")

    def test_pid_reuse_or_foreign_pid_is_never_signalled(self) -> None:
        manager = CheckJobManager(
            self.repository,
            "session-a",
            (self.allowed,),
            root=self.jobs_root,
            worker_launcher=self._completed_launcher(),
            pid_alive=lambda _pid: True,
        )
        state = manager.store.get(
            manager.start(
                {"kind": "check", "argv": list(self.allowed)},
                idempotency_key="pid-reuse",
            )["job_id"]
        )
        reused = replace(
            state,
            status="running",
            finished_at=None,
            exit_code=None,
            worker_identity=ProcessIdentity(pid=os.getpid(), create_time_ns=1),
        )
        manager.store.put(reused)
        with self.assertRaisesRegex(CheckJobError, "unproven"):
            manager.cancel(reused.job_id)
        self.assertFalse(manager.store.cancel_path(reused.job_id).exists())


class CheckJobRuntimeSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_env = dict(os.environ)
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        initialize_git_repository(self.repository)
        os.environ["KAROX_VNEXT_RUNTIME_DIR"] = str(self.root / "runtime")
        self.sessions = SessionStore(self.root / "sessions")
        self.sessions.create(
            self.repository,
            "check job runtime",
            AccessProfile.ELEVATED,
            session_id="session-a",
        )
        self.runtime = HostedToolsRuntime(
            self.repository,
            self.sessions,
            "session-a",
            (CHECKS_START, CHECKS_STATUS, CHECKS_LOGS, CHECKS_CANCEL),
            access_profile=AccessProfile.ELEVATED,
            hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "checks-test"),
        )

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.old_env)
        self.temporary.cleanup()

    def test_tool_descriptors_have_correct_mutation_contract(self) -> None:
        descriptors = {item.name: item for item in self.runtime.descriptors()}
        self.assertFalse(descriptors[CHECKS_START].read_only)
        self.assertTrue(descriptors[CHECKS_STATUS].read_only)
        self.assertTrue(descriptors[CHECKS_LOGS].read_only)
        self.assertFalse(descriptors[CHECKS_CANCEL].read_only)

    def test_start_passes_mcp_idempotency_key_to_manager(self) -> None:
        fake = mock.Mock()
        fake.store = self.runtime._check_jobs.store
        fake.start.return_value = {"job_id": "job-" + "a" * 20, "status": "running"}
        self.runtime._check_jobs = fake
        result = self.runtime.execute(
            CHECKS_START,
            {"kind": "pytest", "suite": "full"},
            idempotency_key="wire-key",
            deadline_seconds=1,
        )
        payload = result if isinstance(result, dict) else result.structuredContent
        self.assertTrue(payload["ok"])
        fake.start.assert_called_once()
        self.assertEqual(fake.start.call_args.kwargs["idempotency_key"], "wire-key")


class LegacyInterruptContainmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        initialize_git_repository(self.repository)
        self.sessions = SessionStore(self.root / "sessions")
        self.runtime = CoreRuntime(
            self.repository,
            CapabilityPolicy(AccessProfile.ELEVATED),
            self.sessions,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_keyboard_interrupt_is_contained_to_child_tree(self) -> None:
        class FakeProcess:
            pid = 987654

            def wait(self, timeout: float | None = None) -> int:
                raise KeyboardInterrupt

        class FakeTree:
            instances: list["FakeTree"] = []

            def __init__(self, process: Any) -> None:
                self.process = process
                self.terminated = False
                self.closed = False
                self.instances.append(self)

            def terminate(self) -> None:
                self.terminated = True

            def close(self) -> None:
                self.closed = True

        with (
            mock.patch.object(core_module.subprocess, "Popen", return_value=FakeProcess()),
            mock.patch.object(core_module, "ProcessTree", FakeTree),
            mock.patch.object(core_module, "_resolve_executable", side_effect=lambda argv: argv),
        ):
            result = self.runtime._run([sys.executable, "-c", "pass"], 1.0)

        self.assertTrue(result["interrupted"])
        self.assertFalse(result["timed_out"])
        self.assertEqual(result["cancellation_source"], "request_interrupt")
        self.assertEqual(result["signal_sent"], "terminate_owned_child_tree")
        self.assertTrue(FakeTree.instances[0].terminated)
        self.assertTrue(FakeTree.instances[0].closed)


if __name__ == "__main__":
    unittest.main()
