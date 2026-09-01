from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from karox.saved_bridge_supervisor import (
    _force_stop_proven_owner,
    _force_stop_proven_supervisor,
    _write_supervisor_heartbeat,
    ensure_saved_bridge_supervisor,
    record_saved_bridge_restart_recovery,
    request_saved_bridge_restart_migration,
    saved_bridge_restart_recovery_status,
    saved_bridge_supervisor_status,
    set_saved_bridge_desired_running,
    supervisor_heartbeat_path,
    supervisor_restart_migration_path,
    supervisor_restart_recovery_path,
    supervisor_state_path,
    supervisor_tick,
)
from karox.process_identity import process_is_running, read_process_create_time_ns
from karox.web_bridge_launcher import stop_saved_bridge


class SavedBridgeSupervisorStateTests(unittest.TestCase):
    def test_desired_state_is_persistent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "karox.saved_bridge_supervisor.runtime_dir", return_value=Path(tmp)
        ):
            set_saved_bridge_desired_running("hyperagent-auto", True)
            status = saved_bridge_supervisor_status("hyperagent-auto")
            self.assertTrue(status["desired_running"])
            self.assertFalse(status["supervisor_alive"])

            set_saved_bridge_desired_running("hyperagent-auto", False)
            status = saved_bridge_supervisor_status("hyperagent-auto")
            self.assertFalse(status["desired_running"])

            telemetry_path = supervisor_state_path("hyperagent-auto")
            telemetry = json.loads(telemetry_path.read_text(encoding="utf-8"))
            telemetry["desired_running"] = True
            telemetry_path.write_text(json.dumps(telemetry), encoding="utf-8")
            status = saved_bridge_supervisor_status("hyperagent-auto")
            self.assertFalse(
                status["desired_running"],
                "telemetry must never override an explicit persisted Stop",
            )

    def test_restart_recovery_postmortem_survives_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "karox.saved_bridge_supervisor.runtime_dir", return_value=Path(tmp)
        ):
            armed = record_saved_bridge_restart_recovery(
                "hyperagent-auto",
                phase="armed",
            )
            requested = record_saved_bridge_restart_recovery(
                "hyperagent-auto",
                phase="shutdown_requested",
                owner_pid=111,
                stop_protocol="request-v2",
            )
            recovered = record_saved_bridge_restart_recovery(
                "hyperagent-auto",
                phase="recovered",
                owner_pid=111,
                new_owner_pid=222,
            )

            self.assertEqual(armed["restart_id"], requested["restart_id"])
            self.assertEqual(requested["restart_id"], recovered["restart_id"])
            self.assertEqual(recovered["phase"], "recovered")
            self.assertEqual(recovered["owner_pid"], 111)
            self.assertEqual(recovered["new_owner_pid"], 222)
            self.assertEqual(recovered["stop_protocol"], "request-v2")
            self.assertIsInstance(recovered["completed_at"], float)
            self.assertTrue(supervisor_restart_recovery_path("hyperagent-auto").exists())
            self.assertEqual(
                saved_bridge_restart_recovery_status("hyperagent-auto"),
                recovered,
            )
            self.assertEqual(
                saved_bridge_supervisor_status("hyperagent-auto")["restart_recovery"],
                recovered,
            )

    def test_ensure_reuses_proven_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("karox.saved_bridge_supervisor.runtime_dir", return_value=Path(tmp)),
                patch(
                    "karox.saved_bridge_supervisor.read_process_create_time_ns",
                    return_value=456,
                ),
                patch(
                    "karox.saved_bridge_supervisor.process_is_running",
                    return_value=True,
                ),
                patch("karox.saved_bridge_supervisor.subprocess.Popen") as popen,
                patch(
                    "karox.saved_bridge_supervisor._force_stop_proven_supervisor",
                    return_value=True,
                ) as force_stop,
            ):
                set_saved_bridge_desired_running("hyperagent-auto", True)
                process = MagicMock()
                process.pid = 123
                popen.return_value = process

                self.assertEqual(ensure_saved_bridge_supervisor("hyperagent-auto"), 123)
                _write_supervisor_heartbeat(
                    "hyperagent-auto", pid=123, create_time_ns=456
                )
                status = saved_bridge_supervisor_status("hyperagent-auto")
                self.assertTrue(status["supervisor_process_alive"])
                self.assertTrue(status["supervisor_heartbeat_fresh"])
                self.assertTrue(status["supervisor_alive"])
                self.assertEqual(ensure_saved_bridge_supervisor("hyperagent-auto"), 123)
                self.assertEqual(popen.call_count, 1)
                force_stop.assert_not_called()

                heartbeat_path = supervisor_heartbeat_path("hyperagent-auto")
                heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
                heartbeat["protocol_version"] = 1
                heartbeat_path.write_text(json.dumps(heartbeat), encoding="utf-8")
                status = saved_bridge_supervisor_status("hyperagent-auto")
                self.assertTrue(status["supervisor_process_alive"])
                self.assertFalse(status["supervisor_protocol_compatible"])
                self.assertFalse(status["supervisor_alive"])
                process.pid = 124

                self.assertEqual(ensure_saved_bridge_supervisor("hyperagent-auto"), 124)
                force_stop.assert_called_once_with(123, 456)
                self.assertEqual(popen.call_count, 2)

                heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
                heartbeat["main_progress_at"] = 1.0
                heartbeat["main_busy_until"] = time.time() + 60.0
                heartbeat_path.write_text(json.dumps(heartbeat), encoding="utf-8")
                status = saved_bridge_supervisor_status("hyperagent-auto")
                self.assertTrue(status["supervisor_heartbeat_fresh"])
                self.assertTrue(status["supervisor_main_progress_fresh"])
                self.assertTrue(status["supervisor_alive"])

                heartbeat["main_busy_until"] = 1.0
                heartbeat_path.write_text(json.dumps(heartbeat), encoding="utf-8")
                status = saved_bridge_supervisor_status("hyperagent-auto")
                self.assertFalse(status["supervisor_main_progress_fresh"])
                self.assertFalse(status["supervisor_alive"])
                process.pid = 125

                self.assertEqual(ensure_saved_bridge_supervisor("hyperagent-auto"), 125)
                self.assertEqual(force_stop.call_count, 2)
                force_stop.assert_called_with(124, 456)
                self.assertEqual(popen.call_count, 3)

    def test_ensure_removes_legacy_os_autostart_and_never_registers_it(self) -> None:
        process = SimpleNamespace(pid=321)
        with tempfile.TemporaryDirectory() as tmp, patch(
            "karox.saved_bridge_supervisor.runtime_dir", return_value=Path(tmp)
        ), patch(
            "karox.saved_bridge_supervisor.read_process_create_time_ns",
            return_value=999,
        ), patch(
            "karox.saved_bridge_supervisor._spawn_detached",
            return_value=(process, "detached"),
        ), patch(
            "karox.saved_bridge_autostart.remove_saved_bridge_autostart"
        ) as remove_autostart, patch(
            "karox.saved_bridge_autostart.ensure_saved_bridge_autostart"
        ) as register_autostart:
            pid = ensure_saved_bridge_supervisor(
                "hyperagent-auto", desired_running=True
            )

        self.assertEqual(pid, 321)
        remove_autostart.assert_called_once_with("hyperagent-auto")
        register_autostart.assert_not_called()

    def test_ensure_spawns_credential_free_detached_module(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("karox.saved_bridge_supervisor.runtime_dir", return_value=Path(tmp)),
                patch(
                    "karox.saved_bridge_supervisor.read_process_create_time_ns",
                    return_value=999,
                ),
                patch("karox.saved_bridge_supervisor.subprocess.Popen") as popen,
            ):
                process = MagicMock()
                process.pid = 321
                popen.return_value = process

                pid = ensure_saved_bridge_supervisor(
                    "hyperagent-auto", desired_running=True
                )

                self.assertEqual(pid, 321)
                argv = popen.call_args.args[0]
                self.assertEqual(argv[1:3], ["-m", "karox.saved_bridge_supervisor"])
                self.assertEqual(argv[-2:], ["--saved", "hyperagent-auto"])
                self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
                self.assertEqual(popen.call_args.kwargs["stdout"], subprocess.DEVNULL)
                self.assertEqual(popen.call_args.kwargs["stderr"], subprocess.DEVNULL)
                if os.name == "nt":
                    flags = popen.call_args.kwargs["creationflags"]
                    self.assertTrue(flags & subprocess.DETACHED_PROCESS)
                    self.assertTrue(flags & subprocess.CREATE_NO_WINDOW)
                    self.assertTrue(flags & subprocess.CREATE_NEW_PROCESS_GROUP)
                    self.assertTrue(flags & subprocess.CREATE_BREAKAWAY_FROM_JOB)

        with self.subTest("proven supervisor PID is stopped without a tree kill"):
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
                start_new_session=(os.name != "nt"),
            )
            try:
                deadline = time.monotonic() + 2.0
                create_time = read_process_create_time_ns(process.pid)
                while create_time is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                    create_time = read_process_create_time_ns(process.pid)
                self.assertIsNotNone(create_time)
                assert create_time is not None
                self.assertFalse(
                    _force_stop_proven_supervisor(process.pid, create_time + 1),
                    "a mismatched creation time must never terminate a process",
                )
                self.assertIsNone(process.poll())
                self.assertTrue(_force_stop_proven_supervisor(process.pid, create_time))
                process.wait(timeout=5)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)

        with self.subTest("proven owner tree is terminated as one unit"):
            parent = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    (
                        "import subprocess,sys,time; "
                        "c=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
                        "print(c.pid, flush=True); time.sleep(30)"
                    ),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                creationflags=flags,
                start_new_session=(os.name != "nt"),
            )
            child_pid: int | None = None
            try:
                assert parent.stdout is not None
                child_pid = int(parent.stdout.readline().strip())
                self.assertTrue(_force_stop_proven_owner(parent.pid))
                parent.wait(timeout=5)
                deadline = time.monotonic() + 2.0
                while process_is_running(child_pid) and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertFalse(process_is_running(child_pid))
            finally:
                if parent.poll() is None:
                    parent.kill()
                if parent.stdout is not None:
                    try:
                        parent.stdout.close()
                    except OSError:
                        pass
                parent.wait(timeout=5)


class SavedBridgeSupervisorRecoveryTests(unittest.TestCase):
    def test_request_v1_migration_rearms_desired_state_and_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch("karox.saved_bridge_supervisor.runtime_dir", return_value=root),
                patch("karox.web_bridge_profiles.WebBridgeProfileStore") as profile_store,
                patch("karox.port_ownership.check_port_ownership") as ownership,
                patch("karox.web_bridge_launcher.start_saved_bridge") as start,
            ):
                set_saved_bridge_desired_running("hyperagent-auto", False)
                request_saved_bridge_restart_migration("hyperagent-auto", 4242)
                profile_store.return_value.get.return_value = SimpleNamespace(port=8768)
                ownership.return_value = SimpleNamespace(
                    verdict="free",
                    reason="old request-v1 owner exited",
                    metadata=SimpleNamespace(pid=None, pid_proven=False),
                )
                start.return_value = {
                    "action": "started",
                    "owner_pid": 5001,
                    "bridge_pid": 5002,
                }

                result = supervisor_tick("hyperagent-auto")
                status = saved_bridge_supervisor_status("hyperagent-auto")

            self.assertEqual(result["status"], "recovered")
            self.assertTrue(status["desired_running"])
            self.assertFalse(status["restart_migration_pending"])
            self.assertFalse(supervisor_restart_migration_path("hyperagent-auto").exists())
            start.assert_called_once_with("hyperagent-auto", timeout_seconds=120.0)

    def test_request_v1_migration_waits_for_exact_old_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch("karox.saved_bridge_supervisor.runtime_dir", return_value=root),
                patch("karox.web_bridge_profiles.WebBridgeProfileStore") as profile_store,
                patch("karox.port_ownership.check_port_ownership") as ownership,
                patch("karox.web_bridge_launcher.start_saved_bridge") as start,
            ):
                set_saved_bridge_desired_running("hyperagent-auto", True)
                request_saved_bridge_restart_migration("hyperagent-auto", 4242)
                profile_store.return_value.get.return_value = SimpleNamespace(port=8768)
                ownership.return_value = SimpleNamespace(
                    verdict="reuse_same_profile",
                    reason="old owner still serving",
                    metadata=SimpleNamespace(
                        pid=4242,
                        pid_proven=True,
                        watchdog_path=None,
                    ),
                )

                result = supervisor_tick("hyperagent-auto")
                marker = supervisor_restart_migration_path("hyperagent-auto")

            self.assertEqual(result["status"], "migration_waiting")
            self.assertEqual(result["owner_pid"], 4242)
            self.assertTrue(marker.exists())
            start.assert_not_called()

    def test_tick_recovers_missing_owner_through_canonical_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("karox.saved_bridge_supervisor.runtime_dir", return_value=Path(tmp)),
                patch("karox.web_bridge_profiles.WebBridgeProfileStore") as profile_store,
                patch("karox.port_ownership.check_port_ownership") as ownership,
                patch("karox.web_bridge_launcher.start_saved_bridge") as start,
            ):
                set_saved_bridge_desired_running("hyperagent-auto", True)
                profile_store.return_value.get.return_value = SimpleNamespace(port=8768)
                ownership.return_value = SimpleNamespace(
                    verdict="free",
                    reason="listener is free",
                    metadata=SimpleNamespace(pid=None, pid_proven=False),
                )
                start.return_value = {
                    "action": "started",
                    "owner_pid": 4001,
                    "bridge_pid": 4002,
                }

                result = supervisor_tick("hyperagent-auto")

                self.assertEqual(result["status"], "recovered")
                self.assertEqual(result["owner_pid"], 4001)
                start.assert_called_once_with("hyperagent-auto", timeout_seconds=120.0)

        # A live PID is not enough: a stale owner heartbeat means the supervision
        # loop itself is wedged. Recycle only the ownership-proven tree, then use
        # the same canonical saved-profile start path.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            watchdog = root / "owner.json"
            watchdog.write_text('{"owner_heartbeat_at": 1.0}', encoding="utf-8")
            owned = SimpleNamespace(
                verdict="reuse_same_profile",
                reason="owned saved bridge",
                metadata=SimpleNamespace(
                    pid=4001,
                    pid_proven=True,
                    watchdog_path=str(watchdog),
                ),
            )
            free = SimpleNamespace(
                verdict="free",
                reason="listener is free",
                metadata=SimpleNamespace(pid=None, pid_proven=False),
            )
            with (
                patch("karox.saved_bridge_supervisor.runtime_dir", return_value=root),
                patch("karox.web_bridge_profiles.WebBridgeProfileStore") as profile_store,
                patch(
                    "karox.port_ownership.check_port_ownership",
                    side_effect=[owned, free],
                ),
                patch(
                    "karox.saved_bridge_supervisor._force_stop_proven_owner",
                    return_value=True,
                ) as force_stop,
                patch("karox.web_bridge_launcher.start_saved_bridge") as start,
                patch("karox.saved_bridge_supervisor.time.time", return_value=100.0),
            ):
                set_saved_bridge_desired_running("hyperagent-auto", True)
                profile_store.return_value.get.return_value = SimpleNamespace(port=8768)
                start.return_value = {
                    "action": "started",
                    "owner_pid": 5001,
                    "bridge_pid": 5002,
                }

                result = supervisor_tick("hyperagent-auto")

            self.assertEqual(result["status"], "recovered")
            self.assertEqual(result["owner_pid"], 5001)
            force_stop.assert_called_once_with(4001)
            start.assert_called_once_with("hyperagent-auto", timeout_seconds=120.0)

    def test_supervisor_preserves_last_failure_after_owner_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            thread = MagicMock()
            with (
                patch("karox.saved_bridge_supervisor.runtime_dir", return_value=root),
                patch("karox.saved_bridge_supervisor._claim_leadership", return_value=True),
                patch("karox.saved_bridge_supervisor.threading.Thread", return_value=thread),
                patch(
                    "karox.saved_bridge_supervisor.supervisor_tick",
                    side_effect=[
                        {"status": "recovery_failed", "error": "owner exited with code 9"},
                        {"status": "healthy", "owner_pid": 5001},
                        {"status": "disabled"},
                    ],
                ),
                patch("karox.saved_bridge_supervisor.time.sleep"),
            ):
                set_saved_bridge_desired_running("hyperagent-auto", True)

                from karox.saved_bridge_supervisor import run_saved_bridge_supervisor

                self.assertEqual(run_saved_bridge_supervisor("hyperagent-auto"), 0)
                state = json.loads(
                    supervisor_state_path("hyperagent-auto").read_text(encoding="utf-8")
                )

            self.assertEqual(state["last_failure_status"], "recovery_failed")
            self.assertEqual(state["last_failure_error"], "owner exited with code 9")
            self.assertIsInstance(state["last_failure_at"], float)
            self.assertIsNone(state["last_error"])

    def test_tick_never_replaces_foreign_port_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("karox.saved_bridge_supervisor.runtime_dir", return_value=Path(tmp)),
                patch("karox.web_bridge_profiles.WebBridgeProfileStore") as profile_store,
                patch("karox.port_ownership.check_port_ownership") as ownership,
                patch("karox.web_bridge_launcher.start_saved_bridge") as start,
            ):
                set_saved_bridge_desired_running("hyperagent-auto", True)
                profile_store.return_value.get.return_value = SimpleNamespace(port=8768)
                ownership.return_value = SimpleNamespace(
                    verdict="unrelated_process",
                    reason="port belongs to another process",
                    metadata=SimpleNamespace(pid=9999, pid_proven=False),
                )

                result = supervisor_tick("hyperagent-auto")

                self.assertEqual(result["status"], "blocked_foreign_process")
                start.assert_not_called()

    def test_tick_recovers_when_a_dead_owner_left_its_own_listener_behind(self) -> None:
        # The port holder proves it is this profile's own bridge child, so the
        # supervisor must recover through the canonical start path (which
        # reclaims the port) instead of reporting a foreign holder forever.
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("karox.saved_bridge_supervisor.runtime_dir", return_value=Path(tmp)),
                patch("karox.web_bridge_profiles.WebBridgeProfileStore") as profile_store,
                patch("karox.port_ownership.check_port_ownership") as ownership,
                patch("karox.web_bridge_launcher.start_saved_bridge") as start,
            ):
                set_saved_bridge_desired_running("hyperagent-auto", True)
                profile_store.return_value.get.return_value = SimpleNamespace(port=8768)
                ownership.return_value = SimpleNamespace(
                    verdict="stale_owned_process",
                    reason="our own bridge child outlived its owner",
                    metadata=SimpleNamespace(pid=None, pid_proven=False),
                    owned_orphan_pid=4242,
                )
                start.return_value = {
                    "action": "started",
                    "owner_pid": 6001,
                    "bridge_pid": 6002,
                }

                result = supervisor_tick("hyperagent-auto")

                self.assertEqual(result["status"], "recovered")
                start.assert_called_once_with("hyperagent-auto", timeout_seconds=120.0)

    def test_explicit_stop_disables_auto_restart_even_when_already_stopped(self) -> None:
        profile = SimpleNamespace(port=8768)
        ownership = SimpleNamespace(
            verdict="free",
            reason="listener is free",
            metadata=SimpleNamespace(pid=None, pid_proven=False),
        )
        with (
            patch("karox.web_bridge_profiles.WebBridgeProfileStore") as profile_store,
            patch("karox.port_ownership.check_port_ownership", return_value=ownership),
            patch(
                "karox.saved_bridge_supervisor.clear_saved_bridge_restart_migration"
            ) as clear_migration,
            patch(
                "karox.saved_bridge_supervisor.set_saved_bridge_desired_running"
            ) as desired,
        ):
            profile_store.return_value.get.return_value = profile
            result = stop_saved_bridge("hyperagent-auto")

        self.assertEqual(result["action"], "no_action")
        clear_migration.assert_called_once_with("hyperagent-auto")
        desired.assert_called_once_with("hyperagent-auto", False)


class SupervisorSpawnMechanismTests(unittest.TestCase):
    """How the supervisor gets started, and what it records about that.

    ``CREATE_BREAKAWAY_FROM_JOB`` is an attempt, not a requirement: a caller whose
    own job forbids breakaway -- every Task Scheduler action -- gets ``WinError 5``
    from ``CreateProcess``. The mechanism that actually worked is written to the
    state file because an unattended start has no other way to explain itself.
    """

    def test_the_mechanism_that_worked_is_recorded_next_to_the_pid(self) -> None:
        spawned = SimpleNamespace(pid=os.getpid())
        with tempfile.TemporaryDirectory() as tmp, patch(
            "karox.saved_bridge_supervisor.runtime_dir", return_value=Path(tmp)
        ), patch(
            "karox.saved_bridge_supervisor._spawn_detached",
            return_value=(spawned, "in_caller_job after breakaway:5"),
        ):
            pid = ensure_saved_bridge_supervisor("chatgpt-dev", desired_running=True)
            state = json.loads(supervisor_state_path("chatgpt-dev").read_text(encoding="utf-8"))

        self.assertEqual(pid, os.getpid())
        self.assertEqual(state["supervisor_spawn"], "in_caller_job after breakaway:5")
        self.assertIsNone(state["last_error"])

    def test_a_start_that_windows_refused_says_why_in_the_state_file(self) -> None:
        refusal = "spawn_failed:breakaway:5,in_caller_job:5"
        with tempfile.TemporaryDirectory() as tmp, patch(
            "karox.saved_bridge_supervisor.runtime_dir", return_value=Path(tmp)
        ), patch(
            "karox.saved_bridge_supervisor._spawn_detached", return_value=(None, refusal)
        ):
            pid = ensure_saved_bridge_supervisor("chatgpt-dev", desired_running=True)
            state = json.loads(supervisor_state_path("chatgpt-dev").read_text(encoding="utf-8"))

        # Without this the watchdog fails every five minutes in silence: the Task
        # Scheduler keeps one integer, and the supervisor never got far enough to
        # write a log of its own.
        self.assertIsNone(pid)
        self.assertEqual(state["last_error"], refusal)
        self.assertIsInstance(state["last_spawn_attempt_at"], float)
        self.assertTrue(state["desired_running"])


if __name__ == "__main__":
    unittest.main()
