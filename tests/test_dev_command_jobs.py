from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

from _support import SRC  # noqa: F401 - inserts src on sys.path

from karox.check_jobs import CheckJobError, CheckJobManager, developer_worker_launcher
from karox.process_identity import process_is_running
from karox.repository_lease import RepositoryLeaseStore


class _DummyWorker:
    def poll(self):
        return None


def _await_child_exit(status: dict, deadline_seconds: float = 5.0) -> None:
    """Await the child's real exit before TemporaryDirectory cleanup.

    Windows keeps the child's current-directory handles alive until the process
    is gone; a parallel worker can reach rmtree a few milliseconds before the
    detached developer worker finishes exiting.
    """
    child_pid = status.get("child_pid") if isinstance(status, dict) else None
    if not isinstance(child_pid, int) or child_pid <= 0:
        return
    gone_by = time.monotonic() + deadline_seconds
    while process_is_running(child_pid) and time.monotonic() < gone_by:
        time.sleep(0.02)


class DeveloperCommandJobTests(unittest.TestCase):
    def test_default_check_manager_rejects_developer_kind(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manager = CheckJobManager(root, "session-default")
            with self.assertRaisesRegex(CheckJobError, "developer commands are not enabled"):
                manager.start(
                    {"kind": "dev", "argv": [sys.executable, "-c", "print('x')"]},
                    idempotency_key="dev-disabled",
                )

    def test_detached_developer_job_is_fast_and_workspace_insensitive(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state_root = root / "runtime" / "dev-command-jobs"
            launched: list[Path] = []

            def launcher(path: Path):
                launched.append(path)
                return _DummyWorker()

            manager = CheckJobManager(
                root,
                "session-dev",
                (),
                root=state_root,
                worker_launcher=launcher,
                allow_developer_commands=True,
                workspace_sensitive=False,
                wait_for_child=False,
            )
            args = {"kind": "dev", "argv": [sys.executable, "-c", "print('ok')"]}
            first = manager.start(args, idempotency_key="stable-request")
            self.assertEqual(first["status"], "queued")
            self.assertFalse(first["idempotent_replay"])
            self.assertEqual(len(launched), 1)

            # A command may mutate its own workspace before the hosted client
            # retries after a network error. The same request must still replay
            # the original durable job rather than start the command twice.
            (root / "changed.txt").write_text("changed", encoding="utf-8")
            second = manager.start(args, idempotency_key="stable-request")
            self.assertEqual(second["job_id"], first["job_id"])
            self.assertTrue(second["idempotent_replay"])
            self.assertEqual(len(launched), 1)

    def test_developer_job_rejects_credential_shaped_argv(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manager = CheckJobManager(
                root,
                "session-secret",
                (),
                root=root / "runtime" / "dev-command-jobs",
                worker_launcher=lambda _path: _DummyWorker(),
                allow_developer_commands=True,
                workspace_sensitive=False,
                wait_for_child=False,
            )
            with self.assertRaisesRegex(CheckJobError, "credential-shaped argv"):
                manager.start(
                    {"kind": "dev", "argv": ["tool", "Be" + "arer " + ("F" * 16)]},
                    idempotency_key="secret-request",
                )

    def test_real_developer_worker_runs_detached_and_persists_logs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manager = CheckJobManager(
                root,
                "session-real-worker",
                (),
                root=root / "runtime" / "dev-command-jobs",
                worker_launcher=developer_worker_launcher,
                allow_developer_commands=True,
                workspace_sensitive=False,
                wait_for_child=False,
            )
            started = time.monotonic()
            result = manager.start(
                {
                    "kind": "dev",
                    "argv": [
                        sys.executable,
                        "-c",
                        "import time; print('DURABLE_DEV_JOB_OK', flush=True); time.sleep(0.7)",
                    ],
                    "timeout_seconds": 10,
                },
                idempotency_key="real-worker-detached",
            )
            self.assertLess(time.monotonic() - started, 1.5)
            job_id = result["job_id"]
            deadline = time.monotonic() + 10
            status = result
            while time.monotonic() < deadline:
                status = manager.status(job_id)
                if status["status"] in {"passed", "failed", "cancelled", "timed_out"}:
                    break
                time.sleep(0.1)
            self.assertEqual(status["status"], "passed", status)
            self.assertEqual(status["exit_code"], 0)
            _await_child_exit(status)
            log = manager.logs(job_id)["log"]["text"]
            self.assertIn("DURABLE_DEV_JOB_OK", log)

    def test_real_developer_worker_holds_repository_lease_for_child_lifetime(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manager = CheckJobManager(
                root,
                "session-lease-worker",
                (),
                root=root / "runtime" / "dev-command-jobs",
                worker_launcher=developer_worker_launcher,
                allow_developer_commands=True,
                workspace_sensitive=False,
                wait_for_child=False,
            )
            result = manager.start(
                {
                    "kind": "dev",
                    "argv": [
                        sys.executable,
                        "-c",
                        "import time; print('LEASE_HELD', flush=True); time.sleep(1.5)",
                    ],
                    "timeout_seconds": 10,
                },
                idempotency_key="lease-held-real-worker",
            )
            job_id = result["job_id"]
            leases = RepositoryLeaseStore()
            deadline = time.monotonic() + 8
            lease = None
            status = result
            while time.monotonic() < deadline:
                status = manager.status(job_id)
                lease = leases.load(root)
                if lease is not None and status["status"] == "running":
                    break
                time.sleep(0.05)
            self.assertIsNotNone(lease, status)
            assert lease is not None
            self.assertEqual(lease.session_id, "session-lease-worker")
            self.assertEqual(lease.task_id, job_id)
            self.assertEqual(lease.current_operation, "durable_developer_command")

            while time.monotonic() < deadline:
                status = manager.status(job_id)
                if status["status"] in {"passed", "failed", "cancelled", "timed_out"}:
                    break
                time.sleep(0.05)
            self.assertEqual(status["status"], "passed", status)
            release_deadline = time.monotonic() + 2
            while time.monotonic() < release_deadline and leases.load(root) is not None:
                time.sleep(0.05)
            self.assertIsNone(leases.load(root))
            _await_child_exit(manager.status(job_id))

    def test_real_developer_worker_launcher_is_exported(self) -> None:
        self.assertTrue(callable(developer_worker_launcher))


if __name__ == "__main__":
    unittest.main()
