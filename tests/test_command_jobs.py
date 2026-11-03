"""Background command jobs: durable, pollable, cancellable, never persisting argv secrets."""

from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from karox import command_jobs
from karox.command_jobs import CommandJobError, CommandJobStore, _JobPaths, _RotatingLog
from karox.hosted_core import CoreToolBridge, HostedBridgeAccessDenied
from karox.models import AccessProfile
from karox.sessions import SessionStore

from test_bridge_core import initialize_git_repository


def _script(code: str) -> list[str]:
    return [sys.executable, "-c", code]


class CommandJobStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.store = CommandJobStore(self.base / "jobs")
        self.started: list[str] = []
        self.addCleanup(self._cancel_leftovers)

    def _cancel_leftovers(self) -> None:
        for job_id in self.started:
            try:
                if not self.store.status(job_id)["done"]:
                    self.store.cancel(job_id, wait_seconds=5.0)
            except CommandJobError:
                pass

    def _start(self, request_id: str, code: str, *, timeout: float = 30.0, argv: Any = None, kind: str = "dev") -> dict[str, Any]:
        payload = self.store.start(
            kind=kind,
            request_id=request_id,
            argv=argv if argv is not None else ["python", "-c", code],
            launch_argv=_script(code),
            cwd=self.base,
            timeout_seconds=timeout,
            environment=dict(os.environ),
        )
        self.started.append(payload["job_id"])
        return payload

    def _wait(self, job_id: str, seconds: float = 20.0) -> dict[str, Any]:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            status = self.store.status(job_id)
            if status["done"]:
                return status
            time.sleep(0.05)
        self.fail(f"job {job_id} did not finish: {self.store.status(job_id)}")

    def test_job_runs_in_the_background_and_its_log_is_paged(self) -> None:
        started = self._start("build-1", "import time\nfor i in range(3):\n    print('line', i, flush=True)\n")
        self.assertIn(started["status"], {"starting", "running", "passed"})
        self.assertFalse(started.get("replayed", False))
        finished = self._wait(started["job_id"])
        self.assertEqual((finished["status"], finished["exit_code"]), ("passed", 0))
        whole = self.store.logs(started["job_id"])
        self.assertEqual(whole["text"].replace("\r\n", "\n"), "line 0\nline 1\nline 2\n")
        self.assertFalse(whole["truncated"])
        first = self.store.logs(started["job_id"], limit=7, offset=0)
        self.assertEqual(first["text"].replace("\r\n", "\n"), "line 0\n"[: first["next_offset"]])
        rest = self.store.logs(started["job_id"], offset=first["next_offset"])
        self.assertEqual(first["next_offset"] + len(rest["text"].encode()), whole["bytes_total"])
        self.assertTrue(first["truncated"])

    def test_the_same_request_id_replays_instead_of_running_twice(self) -> None:
        marker = self.base / "runs.txt"
        code = f"open({str(marker)!r}, 'a').write('x')"
        first = self._start("once", code)
        self._wait(first["job_id"])
        again = self._start("once", code)
        self.assertTrue(again["replayed"])
        self.assertEqual(again["job_id"], first["job_id"])
        self.assertEqual(marker.read_text(), "x")

    def test_exit_code_timeout_and_failed_start_are_reported(self) -> None:
        failed = self._wait(self._start("fails", "import sys; sys.exit(3)")["job_id"])
        self.assertEqual((failed["status"], failed["exit_code"]), ("failed", 3))

        slow = self._start("slow", "import time; print('go', flush=True); time.sleep(60)", timeout=1.0)
        timed_out = self._wait(slow["job_id"])
        self.assertEqual(timed_out["status"], "timed_out")
        self.assertIn("timeout", self.store.logs(slow["job_id"])["text"])

        missing = self.store.start(
            kind="dev",
            request_id="missing",
            argv=["does-not-exist"],
            launch_argv=[str(self.base / "does-not-exist")],
            cwd=self.base,
            timeout_seconds=10,
            environment=dict(os.environ),
        )
        self.assertEqual(self._wait(missing["job_id"])["status"], "failed_to_start")

    def test_cancel_stops_a_running_job_and_is_idempotent(self) -> None:
        job = self._start("server", "import time; print('up', flush=True); time.sleep(60)", kind="check")
        deadline = time.monotonic() + 10
        while self.store.status(job["job_id"])["status"] != "running" and time.monotonic() < deadline:
            time.sleep(0.05)
        cancelled = self.store.cancel(job["job_id"], kind="check", wait_seconds=10.0)
        self.assertTrue(cancelled["cancelled"])
        self.assertEqual(cancelled["status"], "cancelled")
        again = self.store.cancel(job["job_id"], kind="check")
        self.assertTrue(again["already_finished"])
        with self.assertRaisesRegex(CommandJobError, "unknown job"):
            self.store.status(job["job_id"], kind="dev")

    def test_a_lost_runner_is_reported_and_cancel_still_stops_the_command(self) -> None:
        job = self._start("orphan", "import time; print('up', flush=True); time.sleep(60)")
        paths = _JobPaths(self.store.root / job["job_id"])
        deadline = time.monotonic() + 10
        while not paths.state.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        spawn = json.loads(paths.spawn.read_text(encoding="utf-8"))
        state = json.loads(paths.state.read_text(encoding="utf-8"))
        os.kill(spawn["runner_pid"], signal.SIGTERM if os.name == "nt" else signal.SIGKILL)
        deadline = time.monotonic() + 10
        while self.store.status(job["job_id"])["status"] != "lost" and time.monotonic() < deadline:
            time.sleep(0.05)
        lost = self.store.status(job["job_id"])
        self.assertEqual(lost["status"], "lost")
        self.assertIn("hint", lost)
        cancelled = self.store.cancel(job["job_id"], wait_seconds=0.1)
        self.assertEqual(cancelled["status"], "cancelled")
        deadline = time.monotonic() + 10
        while command_jobs._alive(state["child_pid"], state["child_create_time_ns"]) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(command_jobs._alive(state["child_pid"], state["child_create_time_ns"]))

    def test_argv_secrets_are_not_persisted_and_logs_are_redacted(self) -> None:
        token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
        job = self._start(
            "secret",
            f"print('token={token}')",
            argv=["tool", "--token", token],
        )
        self._wait(job["job_id"])
        directory = self.store.root / job["job_id"]
        for path in directory.iterdir():
            self.assertNotIn(token, path.read_text(encoding="utf-8", errors="replace").replace("token=" + token, ""), path.name)
        spec = json.loads((directory / "job.json").read_text(encoding="utf-8"))
        self.assertNotIn(token, json.dumps(spec))
        self.assertNotIn(token, self.store.logs(job["job_id"])["text"])
        self.assertNotIn(token, json.dumps(self.store.status(job["job_id"])))

    def test_requests_are_validated(self) -> None:
        with self.assertRaisesRegex(CommandJobError, "request_id"):
            self._start("../escape", "pass")
        with self.assertRaisesRegex(CommandJobError, "job_id"):
            self.store.status("../x")
        with self.assertRaisesRegex(CommandJobError, "unknown job"):
            self.store.status("dev-0000")
        job = self._start("limits", "pass")
        with self.assertRaisesRegex(CommandJobError, "limit"):
            self.store.logs(job["job_id"], limit=0)
        with self.assertRaisesRegex(CommandJobError, "offset"):
            self.store.logs(job["job_id"], offset=-1)
        with self.assertRaisesRegex(CommandJobError, "timeout_seconds"):
            command_jobs.job_timeout(0)
        self.assertEqual(command_jobs.job_timeout(None), command_jobs.DEFAULT_TIMEOUT_SECONDS)

    def test_rotated_logs_are_read_across_the_boundary(self) -> None:
        directory = self.store.root / "dev-rotated"
        directory.mkdir(parents=True)
        paths = _JobPaths(directory)
        paths.spec.write_text(json.dumps({"job_id": "dev-rotated", "kind": "dev", "created_at": time.time()}), encoding="utf-8")
        paths.result.write_text(json.dumps({"status": "passed", "exit_code": 0, "ended_at": time.time()}), encoding="utf-8")
        with patch.object(command_jobs, "LOG_ROTATE_BYTES", 10):
            log = _RotatingLog(paths)
            for chunk in (b"aaaaaaaa", b"bbbbbbbb", b"cccc"):
                log.write(chunk)
            log.close()
        whole = self.store.logs("dev-rotated", offset=0)
        self.assertEqual(whole["bytes_total"], 20)
        # The oldest part is gone; what remains is read in order.
        self.assertEqual(whole["unavailable_bytes"], 8)
        self.assertEqual(whole["text"], "bbbbbbbbcccc")
        tail = self.store.logs("dev-rotated", limit=6)
        self.assertEqual(tail["text"], "bbcccc")

    def test_only_the_most_recent_finished_jobs_are_kept(self) -> None:
        root = self.store.root
        for index in range(command_jobs.MAX_FINISHED_JOBS_KEPT + 5):
            directory = root / f"dev-{index:04d}"
            directory.mkdir(parents=True)
            (directory / "job.json").write_text(json.dumps({"job_id": directory.name, "kind": "dev", "created_at": 1000.0 + index}), encoding="utf-8")
            (directory / "result.json").write_text(json.dumps({"status": "passed"}), encoding="utf-8")
        self.store._prune(keep="none")
        remaining = sorted(item.name for item in root.iterdir())
        self.assertEqual(len(remaining), command_jobs.MAX_FINISHED_JOBS_KEPT)
        self.assertNotIn("dev-0000", remaining)


class HostedCommandJobTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {"KAROX_RUNTIME_DIR": str(self.root / "runtime")})
        environment.start()
        self.addCleanup(environment.stop)
        self.repository = self.root / "repo"
        initialize_git_repository(self.repository)
        (self.repository / "check.py").write_text("print('check ran')\n", encoding="utf-8")
        self.sessions = SessionStore(self.root / "sessions")
        self.sessions.create(self.repository, "elevated", AccessProfile.ELEVATED, session_id="elevated")
        self.sessions.create(self.repository, "write", AccessProfile.WORKSPACE_WRITE, session_id="write")

    def _wait(self, bridge: CoreToolBridge, tool: str, job_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            status = bridge.execute(tool, {"job_id": job_id})["data"]
            if status["done"]:
                return status
            time.sleep(0.05)
        self.fail("job did not finish")

    def test_elevated_session_starts_polls_and_reads_a_developer_job(self) -> None:
        tools = ["karox.command.start", "karox.command.status", "karox.command.logs", "karox.command.cancel"]
        bridge = CoreToolBridge(self.repository, self.sessions, "elevated", tools, audit_path=self.root / "audit.jsonl")
        self.assertEqual([item.name for item in bridge.descriptors()], tools)
        started = bridge.execute(
            "karox.command.start",
            {"argv": [sys.executable, "-c", "print('hello from a job')"], "request_id": "hello"},
            idempotency_key="start-hello",
        )["data"]
        finished = self._wait(bridge, "karox.command.status", started["job_id"])
        self.assertEqual(finished["status"], "passed")
        logs = bridge.execute("karox.command.logs", {"job_id": started["job_id"]})["data"]
        self.assertIn("hello from a job", logs["text"])
        listed = bridge.execute("karox.command.status", {})["data"]["jobs"]
        self.assertEqual([item["job_id"] for item in listed], [started["job_id"]])

    def test_checks_start_runs_only_the_approved_set(self) -> None:
        tools = ["karox.checks.start", "karox.checks.status", "karox.checks.logs"]
        bridge = CoreToolBridge(
            self.repository,
            self.sessions,
            "write",
            tools,
            audit_path=self.root / "audit.jsonl",
            verification_commands=[[sys.executable, "check.py"]],
        )
        started = bridge.execute(
            "karox.checks.start", {"argv": [sys.executable, "check.py"], "request_id": "suite"}, idempotency_key="suite-1"
        )["data"]
        self.assertEqual(self._wait(bridge, "karox.checks.status", started["job_id"])["status"], "passed")
        self.assertIn("check ran", bridge.execute("karox.checks.logs", {"job_id": started["job_id"]})["data"]["text"])
        with self.assertRaisesRegex(Exception, "user-approved"):
            bridge.execute(
                "karox.checks.start", {"argv": [sys.executable, "other.py"], "request_id": "other"}, idempotency_key="other-1"
            )

    def test_a_workspace_write_session_cannot_start_developer_jobs(self) -> None:
        with self.assertRaisesRegex(HostedBridgeAccessDenied, "does not allow"):
            CoreToolBridge(self.repository, self.sessions, "write", ["karox.command.start"], audit_path=self.root / "a.jsonl")


if __name__ == "__main__":
    unittest.main()
