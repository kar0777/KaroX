"""Lifecycle ownership tests for saved connection runtimes."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from _support import SRC  # noqa: F401 - inserts src on sys.path

from karox.connection_runtime import (
    ConnectionRuntimeError,
    ConnectionRuntimeManager,
    ConnectionRuntimeRecord,
    ConnectionRuntimeStore,
)


class ConnectionRuntimeManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "runtimes.json"
        self.store = ConnectionRuntimeStore(self.path)

    def test_managed_runtime_survives_card_close_and_stops_idempotently(self) -> None:
        alive = {101: True, 202: True}
        stopped: list[str] = []

        def stop() -> None:
            stopped.append("called")
            alive[101] = False
            alive[202] = False

        manager = ConnectionRuntimeManager(
            self.store, pid_alive=lambda pid: alive.get(pid, False)
        )
        record = manager.register(
            connection_id="c-1234567890abcdef",
            session_id="clickup-runtime-test",
            tunnel="cloudflare",
            local_endpoint="http://127.0.0.1:8765/mcp",
            public_endpoint="https://example.test/mcp",
            bridge_pid=101,
            tunnel_pid=202,
            stop=stop,
        )
        self.assertTrue(record.runtime_id.startswith("rt-"))
        self.assertEqual(manager.status(record.connection_id)["state"], "running")

        result = manager.stop(record.connection_id)
        self.assertEqual(result["state"], "stopped")
        self.assertEqual(stopped, ["called"])
        # Stopping a persisted stopped record again is a safe no-op.
        again = manager.stop(record.connection_id)
        self.assertEqual(again["state"], "stopped")
        self.assertEqual(stopped, ["called"])

        payload = json.loads(self.path.read_text(encoding="utf-8"))
        serialized = json.dumps(payload, sort_keys=True)
        self.assertNotIn("secret", serialized.lower())
        self.assertNotIn("token", serialized.lower())

    def test_restart_reports_live_pid_as_unmanaged_and_refuses_unsafe_stop(self) -> None:
        record = ConnectionRuntimeRecord(
            runtime_id="rt-1234567890abcdef",
            connection_id="c-1234567890abcdef",
            session_id="clickup-runtime-test",
            tunnel="tailscale",
            local_endpoint="http://127.0.0.1:8765/mcp",
            public_endpoint="https://device.example.ts.net/mcp",
            bridge_pid=303,
            tunnel_pid=404,
            started_at=1.0,
        )
        self.store.put(record)
        restarted = ConnectionRuntimeManager(
            self.store, pid_alive=lambda pid: pid in {303, 404}
        )
        status = restarted.status(record.connection_id)
        self.assertEqual(status["state"], "unmanaged_running")
        self.assertFalse(status["managed"])
        with self.assertRaises(ConnectionRuntimeError):
            restarted.stop(record.connection_id)

    def test_dead_persisted_process_reconciles_to_stopped_and_can_be_forgotten(self) -> None:
        record = ConnectionRuntimeRecord(
            runtime_id="rt-1234567890abcdef",
            connection_id="c-1234567890abcdef",
            session_id="clickup-runtime-test",
            tunnel="cloudflare",
            local_endpoint="http://127.0.0.1:8765/mcp",
            public_endpoint="https://example.test/mcp",
            bridge_pid=505,
            tunnel_pid=606,
            started_at=1.0,
        )
        self.store.put(record)
        manager = ConnectionRuntimeManager(self.store, pid_alive=lambda _pid: False)
        self.assertEqual(manager.status(record.connection_id)["state"], "stopped")
        manager.forget(record.connection_id)
        self.assertIsNone(self.store.get(record.connection_id))

    def test_restart_adopts_a_proven_runtime_and_can_stop_it(self) -> None:
        alive = {101: True, 202: True}
        created = {101: 111_000_000, 202: 222_000_000}
        manager = ConnectionRuntimeManager(
            self.store,
            pid_alive=lambda pid: alive.get(pid, False),
            create_time_reader=lambda pid: created.get(pid),
        )
        record = manager.register(
            connection_id="c-1234567890abcdef",
            session_id="clickup-runtime-test",
            tunnel="cloudflare",
            local_endpoint="http://127.0.0.1:8765/mcp",
            public_endpoint="https://example.test/mcp",
            bridge_pid=101,
            tunnel_pid=202,
            stop=lambda: None,
            bridge_executable="C:/tools/karox-bridge.exe",
            bridge_argv=["karox", "bridge", "serve"],
        )
        self.assertIsNotNone(record.bridge_identity)
        self.assertIsNotNone(record.tunnel_identity)

        killed: list[int] = []

        def terminate(pid: int) -> None:
            killed.append(pid)
            alive[pid] = False

        # A fresh manager over the same store is exactly what a KaroX restart
        # looks like: no live stop callback, only the persisted record.
        restarted = ConnectionRuntimeManager(
            self.store,
            pid_alive=lambda pid: alive.get(pid, False),
            create_time_reader=lambda pid: created.get(pid),
            terminate=terminate,
            sleep=lambda _seconds: None,
        )
        status = restarted.status(record.connection_id)
        self.assertEqual(status["state"], "running")
        self.assertFalse(status["managed"])
        self.assertTrue(status["adopted"])
        self.assertTrue(status["identity_verified"])
        self.assertEqual(status["bridge_identity_state"], "verified")

        result = restarted.stop(record.connection_id)
        self.assertEqual(result["state"], "stopped")
        self.assertEqual(sorted(killed), [101, 202])
        restarted.forget(record.connection_id)
        self.assertIsNone(self.store.get(record.connection_id))

        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertNotIn("karox-bridge", json.dumps(payload).lower())

    def test_restart_refuses_a_recycled_pid(self) -> None:
        alive = {101: True}
        manager = ConnectionRuntimeManager(
            self.store,
            pid_alive=lambda pid: alive.get(pid, False),
            create_time_reader=lambda _pid: 111_000_000,
        )
        record = manager.register(
            connection_id="c-1234567890abcdef",
            session_id="clickup-runtime-test",
            tunnel="local",
            local_endpoint="http://127.0.0.1:8765/mcp",
            public_endpoint="http://127.0.0.1:8765/mcp",
            bridge_pid=101,
            tunnel_pid=None,
            stop=lambda: None,
        )

        def never(pid: int) -> None:
            self.fail(f"a recycled PID must never be signalled: {pid}")

        recycled = ConnectionRuntimeManager(
            self.store,
            pid_alive=lambda pid: alive.get(pid, False),
            create_time_reader=lambda _pid: 999_000_000,
            terminate=never,
        )
        status = recycled.status(record.connection_id)
        self.assertEqual(status["state"], "unmanaged_running")
        self.assertFalse(status["identity_verified"])
        self.assertEqual(status["bridge_identity_state"], "create_time_mismatch")
        with self.assertRaises(ConnectionRuntimeError):
            recycled.stop(record.connection_id)

    def test_version_1_registry_reads_forward_but_stays_unproven(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "runtimes": [
                        {
                            "runtime_id": "rt-1234567890abcdef",
                            "connection_id": "c-1234567890abcdef",
                            "session_id": "clickup-runtime-test",
                            "tunnel": "cloudflare",
                            "local_endpoint": "http://127.0.0.1:8765/mcp",
                            "public_endpoint": "https://example.test/mcp",
                            "bridge_pid": 303,
                            "tunnel_pid": 404,
                            "started_at": 1.0,
                            "status": "running",
                            "stopped_at": None,
                        }
                    ],
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        def never(pid: int) -> None:
            self.fail(f"a legacy record must never authorise a signal: {pid}")

        manager = ConnectionRuntimeManager(
            self.store,
            pid_alive=lambda pid: pid in {303, 404},
            create_time_reader=lambda _pid: 111_000_000,
            terminate=never,
        )
        status = manager.status("c-1234567890abcdef")
        self.assertEqual(status["state"], "unmanaged_running")
        self.assertEqual(status["bridge_identity_state"], "identity_not_recorded")
        with self.assertRaises(ConnectionRuntimeError):
            manager.stop("c-1234567890abcdef")

    def test_identity_pid_must_match_the_recorded_pid(self) -> None:
        with self.assertRaises(ConnectionRuntimeError):
            ConnectionRuntimeRecord(
                runtime_id="rt-1234567890abcdef",
                connection_id="c-1234567890abcdef",
                session_id="clickup-runtime-test",
                tunnel="local",
                local_endpoint="http://127.0.0.1:8765/mcp",
                public_endpoint="http://127.0.0.1:8765/mcp",
                bridge_pid=101,
                tunnel_pid=None,
                started_at=1.0,
                bridge_identity={"pid": 999, "create_time_ns": 5},
            )


if __name__ == "__main__":
    unittest.main()
