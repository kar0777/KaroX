"""Process acceptance matrix for the generic service supervisor.

Mandate section 24 rows proven here, stack-independent and deterministic:
start own / status / logs / restart / stop / already dead / startup crash /
health timeout / foreign PID / PID reuse / wrong project / wrong workstream /
concurrent restart / attempt to kill the control plane / protected
executables. PID alone is never accepted as identity anywhere.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from _support import SRC  # noqa: F401
from karox.service_supervisor import (
    IdentityVerdict,
    ManagedService,
    ProcessIdentity,
    ServiceLease,
    ServiceLeaseError,
    ServiceSupervisor,
    env_fingerprint,
    verify_identity,
)

_SLEEP_ARGV = [sys.executable, "-c", "import time; time.sleep(60)"]


def _wait_dead(pid: int, timeout: float = 10.0) -> bool:
    from karox.remote_tools import _pid_alive

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.1)
    return False


class IdentityTests(unittest.TestCase):
    def test_capture_of_own_process_finds_positive_identity(self) -> None:
        identity = ProcessIdentity.capture(os.getpid())
        self.assertEqual(identity.pid, os.getpid())
        # psutil is installed in the dev environment; at least one field
        # beyond the PID must be provable for the running interpreter.
        self.assertTrue(
            identity.created_at is not None
            or identity.executable is not None
            or identity.cmdline_digest is not None
        )

    def test_created_at_mismatch_refuses_as_pid_reuse(self) -> None:
        stored = ProcessIdentity(pid=1234, created_at=1000.0, executable=None, cmdline_digest=None)
        live = ProcessIdentity(pid=1234, created_at=5000.0, executable=None, cmdline_digest=None)
        verdict = verify_identity(stored, live, alive=True)
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, IdentityVerdict.REFUSE_CREATED_AT)

    def test_executable_mismatch_refuses_as_foreign(self) -> None:
        stored = ProcessIdentity(pid=1, created_at=None, executable="C:/x/node.exe", cmdline_digest=None)
        live = ProcessIdentity(pid=1, created_at=None, executable="C:/y/java.exe", cmdline_digest=None)
        verdict = verify_identity(stored, live, alive=True)
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, IdentityVerdict.REFUSE_EXECUTABLE)

    def test_unprovable_identity_refuses_rather_than_guessing(self) -> None:
        stored = ProcessIdentity(pid=1, created_at=None, executable=None, cmdline_digest=None)
        live = ProcessIdentity(pid=1, created_at=None, executable=None, cmdline_digest=None)
        verdict = verify_identity(stored, live, alive=True)
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, IdentityVerdict.REFUSE_UNPROVABLE)

    def test_dead_process_refuses(self) -> None:
        stored = ProcessIdentity(pid=1, created_at=1.0, executable=None, cmdline_digest=None)
        verdict = verify_identity(stored, stored, alive=False)
        self.assertFalse(verdict.ok)

    def test_small_created_at_jitter_is_tolerated(self) -> None:
        stored = ProcessIdentity(pid=7, created_at=1000.0, executable=None, cmdline_digest=None)
        live = ProcessIdentity(pid=7, created_at=1000.4, executable=None, cmdline_digest=None)
        self.assertTrue(verify_identity(stored, live, alive=True).ok)


class SupervisorMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.supervisor = ServiceSupervisor(
            self.root / "services",
            project_id="proj-a",
            workstream_id="ws-a",
            owner="test-owner",
        )

    def tearDown(self) -> None:
        for record in self.supervisor.list():
            try:
                self.supervisor.stop(record.service_id)
            except Exception:
                pass
        self.temporary.cleanup()

    def _start_sleeper(self, service_id: str = "svc"):
        result = self.supervisor.start(
            service_id=service_id,
            argv=_SLEEP_ARGV,
            cwd=Path(self.temporary.name),
        )
        self.assertTrue(result.ok, result.reason)
        return result

    def test_start_status_logs_stop_own_service(self) -> None:
        started = self._start_sleeper()
        status = self.supervisor.status("svc")
        self.assertTrue(status.ok)
        self.assertEqual(status.reason, "running")
        logs = self.supervisor.logs("svc")
        self.assertTrue(logs.ok)
        self.assertIsNotNone(logs.stdout_tail)
        stopped = self.supervisor.stop("svc")
        self.assertTrue(stopped.ok, stopped.reason)
        self.assertTrue(stopped.exit_confirmed)
        self.assertTrue(_wait_dead(started.pid or -1))

    def test_double_start_is_refused_while_running(self) -> None:
        self._start_sleeper()
        second = self.supervisor.start(
            service_id="svc",
            argv=_SLEEP_ARGV,
            cwd=Path(self.temporary.name),
        )
        self.assertFalse(second.ok)
        self.assertIn("already running", second.reason)
        self.supervisor.stop("svc")

    def test_stop_already_dead_service_reports_cleanly(self) -> None:
        started = self._start_sleeper()
        self.supervisor.stop("svc")
        self.assertTrue(_wait_dead(started.pid or -1))
        again = self.supervisor.stop("svc")
        self.assertTrue(again.ok)
        self.assertEqual(again.reason, "already stopped")

    def test_startup_crash_is_reported_not_hidden(self) -> None:
        result = self.supervisor.start(
            service_id="crash",
            argv=["definitely-not-a-real-binary-xyz"],
            cwd=Path(self.temporary.name),
        )
        self.assertFalse(result.ok)
        self.assertIn("launch failed", result.reason)

    def test_unknown_service_actions_fail_honestly(self) -> None:
        for action in ("status", "logs", "stop", "restart"):
            result = getattr(self.supervisor, action)("ghost")
            self.assertFalse(result.ok)
            self.assertEqual(result.reason, "unknown service")

    def test_restart_transaction_produces_new_live_pid(self) -> None:
        started = self._start_sleeper()
        result = self.supervisor.restart("svc")
        self.assertTrue(result.ok, result.reason)
        self.assertTrue(result.exit_confirmed)
        self.assertIsNotNone(result.pid)
        self.assertNotEqual(result.pid, started.pid)
        status = self.supervisor.status("svc")
        self.assertEqual(status.reason, "running")
        self.supervisor.stop("svc")

    def test_restart_health_timeout_fails_with_evidence(self) -> None:
        self._start_sleeper()
        result = self.supervisor.restart(
            "svc", readiness_probe=lambda record: False
        )
        self.assertFalse(result.ok)
        self.assertIn("readiness", result.reason)
        self.assertIsNotNone(result.stdout_tail)
        self.supervisor.stop("svc")

    def test_pid_reuse_is_refused_on_stop(self) -> None:
        # A recorded service whose PID the OS handed to another live process:
        # stored identity carries a creation time that cannot match.
        import dataclasses as dc

        started = self._start_sleeper()
        record = self.supervisor.load("svc")
        assert record is not None
        forged = dc.replace(
            record,
            identity=dc.replace(record.identity, created_at=1.0),
        )
        self.supervisor._save(forged)
        result = self.supervisor.stop("svc")
        self.assertFalse(result.ok)
        self.assertIn("REFUSED", result.reason)
        # The real process is untouched by the refusal.
        status_pid = started.pid or -1
        from karox.remote_tools import _pid_alive

        self.assertTrue(_pid_alive(status_pid))
        restored = dc.replace(forged, identity=record.identity)
        self.supervisor._save(restored)
        self.supervisor.stop("svc")

    def test_wrong_project_and_workstream_are_blocked(self) -> None:
        self._start_sleeper()
        foreign = ServiceSupervisor(
            self.root / "services",
            project_id="proj-b",
            workstream_id="ws-a",
            owner="other",
        )
        result = foreign.stop("svc")
        self.assertFalse(result.ok)
        self.assertIn("BLOCKED", result.reason)
        other_ws = ServiceSupervisor(
            self.root / "services",
            project_id="proj-a",
            workstream_id="ws-b",
            owner="other",
        )
        result = other_ws.restart("svc")
        self.assertFalse(result.ok)
        self.assertIn("BLOCKED", result.reason)
        self.supervisor.stop("svc")

    def test_control_plane_pid_is_blocked(self) -> None:
        import dataclasses as dc

        self._start_sleeper()
        record = self.supervisor.load("svc")
        assert record is not None
        forged = dc.replace(
            record,
            identity=dc.replace(record.identity, pid=os.getpid()),
        )
        self.supervisor._save(forged)
        result = self.supervisor.stop("svc")
        self.assertFalse(result.ok)
        self.assertIn("BLOCKED", result.reason)
        self.assertIn("control plane", result.reason)
        restored = dc.replace(forged, identity=record.identity)
        self.supervisor._save(restored)
        self.supervisor.stop("svc")

    def test_protected_executable_is_blocked(self) -> None:
        import dataclasses as dc

        self._start_sleeper()
        record = self.supervisor.load("svc")
        assert record is not None
        forged = dc.replace(
            record,
            identity=dc.replace(
                record.identity, executable="C:/Program Files/Tailscale/tailscale.exe"
            ),
        )
        self.supervisor._save(forged)
        result = self.supervisor.stop("svc")
        self.assertFalse(result.ok)
        self.assertIn("BLOCKED", result.reason)
        self.assertIn("tailscale", result.reason)
        restored = dc.replace(forged, identity=record.identity)
        self.supervisor._save(restored)
        self.supervisor.stop("svc")

    def test_concurrent_mutation_is_serialized_by_lease(self) -> None:
        self._start_sleeper()
        lease = ServiceLease(
            self.supervisor.root, "svc", owner="agent-B", ttl=60.0
        )
        lease.acquire()
        try:
            with self.assertRaises(ServiceLeaseError):
                self.supervisor.stop("svc")
        finally:
            lease.release()
        stopped = self.supervisor.stop("svc")
        self.assertTrue(stopped.ok, stopped.reason)

    def test_expired_lease_is_reclaimed(self) -> None:
        stale = ServiceLease(self.supervisor.root, "svc2", owner="dead", ttl=0.0)
        stale.acquire()
        fresh = ServiceLease(self.supervisor.root, "svc2", owner="live", ttl=60.0)
        fresh.acquire()
        fresh.release()

    def test_env_fingerprint_hashes_names_never_values(self) -> None:
        first = env_fingerprint(["PORT", "NODE_ENV"])
        second = env_fingerprint(["NODE_ENV", "PORT"])
        self.assertEqual(first, second)
        self.assertNotIn("PORT", first)
        self.assertEqual(len(first), 64)


if __name__ == "__main__":
    unittest.main()
