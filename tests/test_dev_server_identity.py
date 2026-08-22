"""Hosted dev_server stop re-verifies process identity before any signal.

Mandate sections 15-16 applied to the hosted tool path: the identity
sidecar is written at start, a provable mismatch (PID reuse forgery)
REFUSES without touching the process, and a verified stop reports how it
was verified. Uses a real child process so psutil facts exist.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from _support import SRC, initialize_git_repository  # noqa: F401
from karox.hosted_tools_runtime import (
    DEV_SERVER_START,
    DEV_SERVER_STOP,
    HostedToolsRuntime,
    ManagedServerProfile,
)
from karox.models import AccessProfile, Origin, OriginKind
from karox.remote_tools import _pid_alive
from karox.sessions import SessionStore


def _sleeper_profile() -> ManagedServerProfile:
    return ManagedServerProfile(
        name="py-sleeper",
        argv=(sys.executable, "-c", "import time; time.sleep(60)"),
    )


class DevServerIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        initialize_git_repository(self.repository)
        self.sessions = SessionStore(self.root / "sessions")
        self.sessions.create(
            self.repository,
            "identity fixtures",
            AccessProfile.ELEVATED,
            session_id="sess-ident",
        )
        self.runtime = HostedToolsRuntime(
            self.repository,
            self.sessions,
            "sess-ident",
            (DEV_SERVER_START, DEV_SERVER_STOP),
            access_profile=AccessProfile.ELEVATED,
            hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "test-ident"),
            server_profiles=(_sleeper_profile(),),
        )
        self._live_pids: list[int] = []

    def tearDown(self) -> None:
        from karox.remote_tools import _kill_pid_tree

        for pid in self._live_pids:
            if _pid_alive(pid):
                _kill_pid_tree(pid)
        self.temporary.cleanup()

    def _payload(self, result: object) -> dict:
        return result if isinstance(result, dict) else result.structuredContent  # type: ignore[union-attr]

    def _start(self, process_id: str) -> dict:
        result = self.runtime.execute(
            DEV_SERVER_START,
            {
                "argv": list(_sleeper_profile().argv),
                "process_id": process_id,
            },
            deadline_seconds=10,
        )
        payload = self._payload(result)
        self.assertTrue(payload.get("ok"), payload)
        self._live_pids.append(int(payload["pid"]))
        return payload

    def test_start_writes_identity_sidecar(self) -> None:
        payload = self._start("srv-sidecar")
        sidecar = self.runtime._identity_path("srv-sidecar")
        self.assertTrue(sidecar.exists())
        facts = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(int(facts["pid"]), int(payload["pid"]))
        self.assertTrue(
            facts.get("created_at") is not None
            or facts.get("executable") is not None
            or facts.get("cmdline_digest") is not None
        )

    def test_verified_stop_kills_and_reports_verification(self) -> None:
        self._start("srv-verified")
        result = self._payload(
            self.runtime.execute(
                DEV_SERVER_STOP,
                {"process_id": "srv-verified"},
                deadline_seconds=10,
            )
        )
        self.assertTrue(result.get("ok"), result)
        self.assertIn(
            result.get("identity"), ("verified", "verified_by_start_time")
        )
        self.assertFalse(result.get("running"))

    def test_forged_creation_time_refuses_and_spares_the_process(self) -> None:
        payload = self._start("srv-forged")
        sidecar = self.runtime._identity_path("srv-forged")
        facts = json.loads(sidecar.read_text(encoding="utf-8"))
        facts["created_at"] = 1.0
        sidecar.write_text(json.dumps(facts), encoding="utf-8")
        result = self._payload(
            self.runtime.execute(
                DEV_SERVER_STOP,
                {"process_id": "srv-forged"},
                deadline_seconds=10,
            )
        )
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error_code"), "identity_mismatch")
        self.assertTrue(_pid_alive(int(payload["pid"])))

    def test_missing_sidecar_still_stops_via_start_time_window(self) -> None:
        self._start("srv-legacy")
        self.runtime._identity_path("srv-legacy").unlink()
        result = self._payload(
            self.runtime.execute(
                DEV_SERVER_STOP,
                {"process_id": "srv-legacy"},
                deadline_seconds=10,
            )
        )
        self.assertTrue(result.get("ok"), result)
        self.assertIn(
            result.get("identity"), ("verified_by_start_time", "verified")
        )


if __name__ == "__main__":
    unittest.main()
