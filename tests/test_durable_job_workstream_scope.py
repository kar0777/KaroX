from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from _support import SRC, initialize_git_repository  # noqa: F401

from karox.autonomy_runtime import TASK_STATUS, AutonomyRuntime
from karox.check_jobs import CheckJobState, CheckJobStore
from karox.hosted_bridge import HostedBridgeAccessDenied
from karox.hosted_tools_runtime import (
    CHECKS_START,
    COMMAND_CANCEL,
    COMMAND_LOGS,
    COMMAND_START,
    COMMAND_STATUS,
    HostedToolsRuntime,
)
from karox.models import AccessProfile, Origin, OriginKind
from karox.sessions import SessionStore


class DurableJobWorkstreamScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_env = dict(os.environ)
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        initialize_git_repository(self.repository)
        os.environ["KAROX_RUNTIME_DIR"] = str(self.root / "runtime")
        os.environ["KAROX_VNEXT_RUNTIME_DIR"] = str(self.root / "runtime")
        self.sessions = SessionStore(self.root / "sessions")
        self.sessions.create(
            self.repository,
            "durable job workstream scope",
            AccessProfile.ELEVATED,
            session_id="session-scope",
        )

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._old_env)
        self.temporary.cleanup()

    def _runtime(self, tools: tuple[str, ...]) -> HostedToolsRuntime:
        return HostedToolsRuntime(
            self.repository,
            self.sessions,
            "session-scope",
            tools,
            access_profile=AccessProfile.ELEVATED,
            hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "test-job-scope"),
        )

    def _queued_state(self, job_id: str) -> CheckJobState:
        now = time.time()
        return CheckJobState(
            schema_version=1,
            job_id=job_id,
            session_id="session-scope",
            repository=str(self.repository),
            argv=("python", "-c", "print('ok')"),
            command_sha256="a" * 64,
            idempotency_sha256="b" * 64,
            status="queued",
            queued_at=now,
            started_at=None,
            updated_at=now,
            finished_at=None,
            timeout_seconds=60.0,
            bridge_pid=os.getpid(),
            bridge_identity=None,
            worker_identity=None,
            child_identity=None,
            process_group="pending",
            cancellation_source=None,
            signal_sent=None,
            exit_code=None,
            error_code=None,
            error=None,
            log_path=str(self.root / f"{job_id}.log"),
            artifact_id=None,
            log_truncated=False,
        )

    def test_command_control_schema_accepts_returned_workstream_scope(self) -> None:
        runtime = self._runtime((COMMAND_STATUS, COMMAND_LOGS, COMMAND_CANCEL))
        descriptors = {item.name: item for item in runtime.descriptors()}
        for name in (COMMAND_STATUS, COMMAND_LOGS, COMMAND_CANCEL):
            self.assertIn("workstream_id", descriptors[name].input_schema["properties"])

    def test_command_start_persists_scope_without_changing_job_state_schema(self) -> None:
        runtime = self._runtime((COMMAND_START, COMMAND_STATUS, COMMAND_CANCEL))
        job_id = "job-" + "a" * 20
        started = {
            "job_id": job_id,
            "status": "queued",
            "idempotent_replay": False,
        }
        with mock.patch(
            "karox.hosted_tools_runtime.CheckJobManager.start",
            return_value=started,
        ):
            result = runtime._command_start(
                {
                    "argv": ["python", "-c", "print('ok')"],
                    "request_id": "frontend-run-1",
                    "workstream_id": "frontend",
                },
                10.0,
            )

        self.assertEqual(result["workstream_id"], "frontend")
        self.assertEqual(
            runtime._command_jobs.store.get_scope(job_id),
            {
                "workstream_id": "frontend",
                "project_id": runtime._anchor_project_id,
            },
        )

    def test_sibling_lane_cannot_accidentally_cancel_new_scoped_command(self) -> None:
        runtime = self._runtime((COMMAND_CANCEL, COMMAND_STATUS, COMMAND_LOGS))
        job_id = "job-" + "b" * 20
        runtime._command_jobs.store.put_scope(
            job_id,
            workstream_id="frontend",
            project_id=runtime._anchor_project_id,
        )
        with mock.patch.object(runtime._command_jobs, "cancel") as cancel:
            with self.assertRaisesRegex(HostedBridgeAccessDenied, "another workstream"):
                runtime._command_cancel(
                    {"job_id": job_id, "workstream_id": "backend"},
                    10.0,
                )
        cancel.assert_not_called()

    def test_idempotent_job_replay_cannot_be_claimed_by_sibling_lane(self) -> None:
        runtime = self._runtime((COMMAND_START,))
        job_id = "job-" + "c" * 20
        runtime._command_jobs.store.put_scope(
            job_id,
            workstream_id="frontend",
            project_id=runtime._anchor_project_id,
        )
        replay = {
            "job_id": job_id,
            "status": "running",
            "idempotent_replay": True,
        }
        with mock.patch(
            "karox.hosted_tools_runtime.CheckJobManager.start",
            return_value=replay,
        ):
            with self.assertRaisesRegex(HostedBridgeAccessDenied, "another workstream"):
                runtime._command_start(
                    {
                        "argv": ["python", "-c", "print('ok')"],
                        "request_id": "same-client-request",
                        "workstream_id": "backend",
                    },
                    10.0,
                )

    def test_new_check_job_is_explicitly_default_scoped(self) -> None:
        runtime = self._runtime((CHECKS_START,))
        job_id = "job-" + "d" * 20
        with mock.patch.object(
            runtime._check_jobs,
            "start",
            return_value={
                "job_id": job_id,
                "status": "queued",
                "idempotent_replay": False,
            },
        ):
            result = runtime._checks_start(
                {"kind": "pytest", "_idempotency_key": "checks-scope-key"},
                10.0,
            )
        self.assertEqual(result["workstream_id"], "default")
        self.assertEqual(
            runtime._check_jobs.store.get_scope(job_id),
            {
                "workstream_id": "default",
                "project_id": runtime._anchor_project_id,
            },
        )

    def test_task_continuation_filters_jobs_to_its_workstream(self) -> None:
        command_root = self.root / "runtime" / "vnext" / "dev-command-jobs"
        store = CheckJobStore("session-scope", root=command_root)
        frontend_id = "job-" + "e" * 20
        backend_id = "job-" + "f" * 20
        legacy_id = "job-" + "1" * 20
        for job_id in (frontend_id, backend_id, legacy_id):
            store.put(self._queued_state(job_id))
        store.put_scope(frontend_id, workstream_id="frontend", project_id="project-a")
        store.put_scope(backend_id, workstream_id="backend", project_id="project-a")

        runtime = AutonomyRuntime(
            self.repository,
            self.sessions,
            "session-scope",
            (TASK_STATUS,),
            access_profile=AccessProfile.ELEVATED,
            hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "test-active-job-scope"),
            connection_profile="chatgpt-web",
        )
        try:
            frontend = runtime._active_durable_jobs(workstream_id="frontend")
            backend = runtime._active_durable_jobs(workstream_id="backend")
            default = runtime._active_durable_jobs(workstream_id="default")
            all_jobs = runtime._active_durable_jobs()
        finally:
            runtime.close()

        self.assertEqual([item["job_id"] for item in frontend["jobs"]], [frontend_id])
        self.assertEqual([item["job_id"] for item in backend["jobs"]], [backend_id])
        self.assertEqual([item["job_id"] for item in default["jobs"]], [legacy_id])
        self.assertEqual(
            {item["job_id"] for item in all_jobs["jobs"]},
            {frontend_id, backend_id, legacy_id},
        )
        self.assertEqual(frontend["jobs"][0]["workstream_id"], "frontend")


if __name__ == "__main__":
    unittest.main()
