from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from _support import ROOT  # noqa: F401 - inserts src on sys.path

from karox import runtime_restart


class RuntimeRestartTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.env = patch.dict(
            os.environ,
            {
                "KAROX_RUNTIME_DIR": str(self.root),
                "KAROX_VNEXT_RUNTIME_DIR": str(self.root),
            },
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.temporary.cleanup()

    def _watchdog(
        self,
        *,
        session_id: str = "restart-session",
        saved_profile: str = "chatgpt-dev",
        bridge_pid: int = 4101,
        owner_pid: int = 4100,
    ) -> Path:
        path = self.root / "web-bridge" / f"{session_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "saved_profile": saved_profile,
                    "persistent_session": True,
                    "bridge_pid": bridge_pid,
                    "owner_pid": owner_pid,
                    "public_url": "https://stable.example.invalid",
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_current_child_validation_requires_exact_profile_pid_and_parent(self) -> None:
        self._watchdog()
        with (
            patch("karox.runtime_restart.os.getpid", return_value=4101),
            patch("karox.runtime_restart.os.getppid", return_value=4100),
            patch("karox.runtime_restart.process_is_running", return_value=True),
        ):
            value = runtime_restart._validate_current_saved_child(
                "restart-session", "chatgpt-dev"
            )
            self.assertEqual(value["bridge_pid"], 4101)
            with self.assertRaises(runtime_restart.RuntimeRestartError):
                runtime_restart._validate_current_saved_child(
                    "restart-session", "claude-dev"
                )

        with (
            patch("karox.runtime_restart.os.getpid", return_value=9999),
            patch("karox.runtime_restart.os.getppid", return_value=4100),
            patch("karox.runtime_restart.process_is_running", return_value=True),
        ):
            with self.assertRaises(runtime_restart.RuntimeRestartError):
                runtime_restart._validate_current_saved_child(
                    "restart-session", "chatgpt-dev"
                )

        with (
            patch("karox.runtime_restart.os.getpid", return_value=4101),
            patch("karox.runtime_restart.os.getppid", return_value=9998),
            patch("karox.runtime_restart.process_is_running", return_value=True),
        ):
            with self.assertRaises(runtime_restart.RuntimeRestartError):
                runtime_restart._validate_current_saved_child(
                    "restart-session", "chatgpt-dev"
                )

    def test_current_child_validation_accepts_only_proven_venv_launcher_chain(self) -> None:
        self._watchdog(bridge_pid=4101, owner_pid=4100)
        with (
            patch("karox.runtime_restart.os.getpid", return_value=4102),
            patch("karox.runtime_restart.os.getppid", return_value=4101),
            patch("karox.runtime_restart.process_is_running", return_value=True),
            patch(
                "karox.runtime_restart.prove_bridge_process_identity",
                side_effect=lambda pid, sessions: "restart-session"
                if pid in {4101, 4102} and "restart-session" in tuple(sessions)
                else None,
            ),
            patch("karox.runtime_restart._process_parent_pid", return_value=4100),
            patch("karox.runtime_restart.prove_saved_bridge_owner_identity", return_value=True),
        ):
            value = runtime_restart._validate_current_saved_child(
                "restart-session", "chatgpt-dev"
            )
            self.assertEqual(value["bridge_pid"], 4101)

        with (
            patch("karox.runtime_restart.os.getpid", return_value=4102),
            patch("karox.runtime_restart.os.getppid", return_value=4101),
            patch("karox.runtime_restart.process_is_running", return_value=True),
            patch("karox.runtime_restart.prove_bridge_process_identity", return_value="restart-session"),
            patch("karox.runtime_restart._process_parent_pid", return_value=4100),
            patch("karox.runtime_restart.prove_saved_bridge_owner_identity", return_value=False),
        ):
            with self.assertRaisesRegex(
                runtime_restart.RuntimeRestartError, "owner identity cannot be proven"
            ):
                runtime_restart._validate_current_saved_child(
                    "restart-session", "chatgpt-dev"
                )

    def test_waiter_exits_only_after_new_response_completed_and_transport_idle(self) -> None:
        receipt = self.root / "receipt.json"
        receipt.write_text(json.dumps({"status": "scheduled"}), encoding="utf-8")
        snapshots = iter(
            [
                {"responses_completed": 10, "active_requests": 1},
                {"responses_completed": 11, "active_requests": 1},
                {"responses_completed": 11, "active_requests": 0},
            ]
        )
        exited: list[int] = []
        clock = {"value": 0.0}

        def monotonic() -> float:
            clock["value"] += 0.05
            return clock["value"]

        runtime_restart._await_transport_idle_then_exit(
            receipt,
            baseline_completed=10,
            activity_snapshot=lambda: next(snapshots),
            exit_process=exited.append,
            timeout_seconds=1.0,
            poll_seconds=0.01,
            sleep=lambda _seconds: None,
            monotonic=monotonic,
        )

        self.assertEqual(exited, [runtime_restart.SELF_RESTART_EXIT_CODE])
        stored = json.loads(receipt.read_text(encoding="utf-8"))
        self.assertEqual(stored["status"], "triggered")

    def test_waiter_times_out_without_exiting_when_response_never_completes(self) -> None:
        receipt = self.root / "receipt-timeout.json"
        receipt.write_text(json.dumps({"status": "scheduled"}), encoding="utf-8")
        exited: list[int] = []
        clock = {"value": 0.0}

        def monotonic() -> float:
            clock["value"] += 0.2
            return clock["value"]

        runtime_restart._await_transport_idle_then_exit(
            receipt,
            baseline_completed=4,
            activity_snapshot=lambda: {
                "responses_completed": 4,
                "active_requests": 0,
            },
            exit_process=exited.append,
            timeout_seconds=0.5,
            poll_seconds=0.01,
            sleep=lambda _seconds: None,
            monotonic=monotonic,
        )

        self.assertEqual(exited, [])
        stored = json.loads(receipt.read_text(encoding="utf-8"))
        self.assertEqual(stored["status"], "transport_busy_timeout")

    def test_schedule_is_idempotent_and_never_persists_raw_reason_or_key(self) -> None:
        secret_reason = "restart after local test account password rotation"
        raw_key = "restart-idempotency-key-123"
        fake_thread = MagicMock()
        with (
            patch(
                "karox.runtime_restart._validate_current_saved_child",
                return_value={
                    "owner_pid": 5000,
                    "public_url": "https://stable.example.invalid",
                },
            ),
            patch("karox.runtime_restart.threading.Thread", return_value=fake_thread) as thread,
        ):
            first = runtime_restart.schedule_saved_bridge_child_restart(
                session_id="restart-session",
                saved_profile="chatgpt-dev",
                idempotency_key=raw_key,
                reason=secret_reason,
                activity_snapshot=lambda: {
                    "responses_completed": 7,
                    "active_requests": 1,
                },
                exit_process=lambda _code: None,
            )
            replay = runtime_restart.schedule_saved_bridge_child_restart(
                session_id="restart-session",
                saved_profile="chatgpt-dev",
                idempotency_key=raw_key,
                reason=secret_reason,
                activity_snapshot=lambda: {
                    "responses_completed": 7,
                    "active_requests": 1,
                },
                exit_process=lambda _code: None,
            )
            with self.assertRaises(runtime_restart.RuntimeRestartError):
                runtime_restart.schedule_saved_bridge_child_restart(
                    session_id="restart-session",
                    saved_profile="chatgpt-dev",
                    idempotency_key=raw_key,
                    reason="different request",
                )

        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(replay["idempotent_replay"])
        thread.assert_called_once()
        fake_thread.start.assert_called_once_with()
        receipt = runtime_restart._receipt_path("restart-session", raw_key)
        rendered = receipt.read_text(encoding="utf-8")
        self.assertNotIn(raw_key, rendered)
        self.assertNotIn(secret_reason, rendered)
        self.assertNotIn("password rotation", rendered)

    def test_timed_out_restart_rearms_once_on_same_idempotent_retry(self) -> None:
        raw_key = "restart-timeout-retry-key"
        fake_thread = MagicMock()
        validation = {
            "owner_pid": 5000,
            "public_url": "https://stable.example.invalid",
        }
        snapshot = lambda: {"responses_completed": 12, "active_requests": 1}
        with (
            patch(
                "karox.runtime_restart._validate_current_saved_child",
                return_value=validation,
            ),
            patch("karox.runtime_restart.threading.Thread", return_value=fake_thread) as thread,
        ):
            first = runtime_restart.schedule_saved_bridge_child_restart(
                session_id="restart-session",
                saved_profile="chatgpt-dev",
                idempotency_key=raw_key,
                reason="retry after transport timeout",
                activity_snapshot=snapshot,
                exit_process=lambda _code: None,
            )
            receipt = runtime_restart._receipt_path("restart-session", raw_key)
            stored = json.loads(receipt.read_text(encoding="utf-8"))
            stored["status"] = "transport_busy_timeout"
            receipt.write_text(json.dumps(stored), encoding="utf-8")

            rearmed = runtime_restart.schedule_saved_bridge_child_restart(
                session_id="restart-session",
                saved_profile="chatgpt-dev",
                idempotency_key=raw_key,
                reason="retry after transport timeout",
                activity_snapshot=snapshot,
                exit_process=lambda _code: None,
            )
            replay = runtime_restart.schedule_saved_bridge_child_restart(
                session_id="restart-session",
                saved_profile="chatgpt-dev",
                idempotency_key=raw_key,
                reason="retry after transport timeout",
                activity_snapshot=snapshot,
                exit_process=lambda _code: None,
            )

        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(rearmed["idempotent_replay"])
        self.assertTrue(rearmed["rearmed_after_timeout"])
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(thread.call_count, 2)
        self.assertEqual(fake_thread.start.call_count, 2)
        stored = json.loads(receipt.read_text(encoding="utf-8"))
        self.assertEqual(stored["status"], "scheduled")
        self.assertEqual(stored["attempt"], 2)

    def test_tailscale_schedule_uses_rolling_handoff_worker(self) -> None:
        fake_thread = MagicMock()
        validation = {
            "owner_pid": 5000,
            "bridge_pid": 5001,
            "public_url": "https://stable.example.invalid",
            "tunnel": "tailscale",
        }
        with (
            patch(
                "karox.runtime_restart._validate_current_saved_child",
                return_value=validation,
            ),
            patch("karox.runtime_restart.threading.Thread", return_value=fake_thread) as thread,
        ):
            result = runtime_restart.schedule_saved_bridge_child_restart(
                session_id="rolling-session",
                saved_profile="chatgpt-dev",
                idempotency_key="rolling-worker-1",
                reason="load new runtime code",
                activity_snapshot=lambda: {"responses_completed": 7, "active_requests": 1},
                exit_process=lambda _code: None,
            )

        self.assertEqual(result["restart_mode"], "rolling_tailscale")
        kwargs = thread.call_args.kwargs
        self.assertIs(kwargs["target"], runtime_restart._await_transport_idle_then_handoff)
        self.assertEqual(kwargs["kwargs"]["owner_pid"], 5000)
        self.assertEqual(kwargs["kwargs"]["bridge_pid"], 5001)
        self.assertEqual(kwargs["kwargs"]["session_id"], "rolling-session")
        fake_thread.start.assert_called_once_with()

    def test_rolling_waiter_exits_only_after_route_switch(self) -> None:
        receipt = self.root / "rolling-receipt.json"
        receipt.write_text(json.dumps({"status": "scheduled"}), encoding="utf-8")
        exited: list[int] = []
        snapshots = iter(
            [
                {"responses_completed": 11, "active_requests": 0},
                {"responses_completed": 11, "active_requests": 0},
            ]
        )
        clock = {"value": 0.0}

        def monotonic() -> float:
            clock["value"] += 0.05
            return clock["value"]

        with (
            patch("karox.runtime_restart.request_rolling_restart") as requested,
            patch(
                "karox.runtime_restart.read_rolling_restart_request",
                return_value={"status": "route_switched"},
            ),
        ):
            runtime_restart._await_transport_idle_then_handoff(
                receipt,
                session_id="rolling-session",
                owner_pid=5000,
                bridge_pid=5001,
                request_key_sha256="a" * 64,
                baseline_completed=10,
                activity_snapshot=lambda: next(snapshots),
                exit_process=exited.append,
                timeout_seconds=1.0,
                handoff_timeout_seconds=1.0,
                poll_seconds=0.01,
                sleep=lambda _seconds: None,
                monotonic=monotonic,
            )

        requested.assert_called_once_with(
            session_id="rolling-session",
            owner_pid=5000,
            bridge_pid=5001,
            request_key_sha256="a" * 64,
        )
        self.assertEqual(exited, [runtime_restart.SELF_RESTART_EXIT_CODE])
        stored = json.loads(receipt.read_text(encoding="utf-8"))
        self.assertEqual(stored["status"], "triggered")
        self.assertTrue(stored["rolling_handoff"])


if __name__ == "__main__":
    unittest.main()
