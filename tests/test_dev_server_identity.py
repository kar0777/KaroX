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
    DEV_SERVER_LOGS,
    DEV_SERVER_RESTART,
    DEV_SERVER_START,
    DEV_SERVER_STATUS,
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
            (
                DEV_SERVER_START,
                DEV_SERVER_STATUS,
                DEV_SERVER_LOGS,
                DEV_SERVER_STOP,
                DEV_SERVER_RESTART,
            ),
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

    def _start(self, process_id: str, *, workstream_id: str = "default") -> dict:
        result = self.runtime.execute(
            DEV_SERVER_START,
            {
                "argv": list(_sleeper_profile().argv),
                "process_id": process_id,
                "workstream_id": workstream_id,
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

    def test_missing_identity_sidecar_refuses_and_spares_process(self) -> None:
        payload = self._start("srv-legacy")
        self.runtime._identity_path("srv-legacy").unlink()
        result = self._payload(
            self.runtime.execute(
                DEV_SERVER_STOP,
                {"process_id": "srv-legacy"},
                deadline_seconds=10,
            )
        )
        self.assertFalse(result.get("ok"), result)
        self.assertEqual(result.get("error_code"), "identity_unverifiable")
        self.assertTrue(_pid_alive(int(payload["pid"])))


    def test_start_writes_workstream_scope_without_returning_token(self) -> None:
        payload = self._start("srv-scope", workstream_id="alpha")
        sidecar = self.runtime._scope_path("srv-scope")
        self.assertTrue(sidecar.exists())
        scope = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(scope["workstream_id"], "alpha")
        self.assertEqual(int(scope["pid"]), int(payload["pid"]))
        self.assertTrue(scope.get("ownership_token"))
        self.assertNotIn("ownership_token", payload)
        self.assertNotIn(str(scope["ownership_token"]), json.dumps(payload))

    def test_wrong_workstream_cannot_inspect_or_mutate_service(self) -> None:
        payload = self._start("srv-scoped", workstream_id="alpha")
        for tool_name in (
            DEV_SERVER_STATUS,
            DEV_SERVER_LOGS,
            DEV_SERVER_STOP,
            DEV_SERVER_RESTART,
        ):
            result = self._payload(
                self.runtime.execute(
                    tool_name,
                    {"process_id": "srv-scoped", "workstream_id": "beta"},
                    deadline_seconds=10,
                )
            )
            self.assertFalse(result.get("ok"), (tool_name, result))
            self.assertEqual(
                result.get("error_code"), "ownership_scope_mismatch", (tool_name, result)
            )
            self.assertTrue(_pid_alive(int(payload["pid"])), (tool_name, result))

    def test_forged_scope_refuses_and_spares_process(self) -> None:
        payload = self._start("srv-scope-forged", workstream_id="alpha")
        sidecar = self.runtime._scope_path("srv-scope-forged")
        scope = json.loads(sidecar.read_text(encoding="utf-8"))
        scope["repo_fingerprint"] = "forged"
        sidecar.write_text(json.dumps(scope), encoding="utf-8")
        result = self._payload(
            self.runtime.execute(
                DEV_SERVER_STOP,
                {"process_id": "srv-scope-forged", "workstream_id": "alpha"},
                deadline_seconds=10,
            )
        )
        self.assertFalse(result.get("ok"), result)
        self.assertEqual(result.get("error_code"), "ownership_scope_mismatch")
        self.assertTrue(_pid_alive(int(payload["pid"])))

    def test_restart_confirms_exit_and_preserves_workstream_scope(self) -> None:
        first = self._start("srv-restart", workstream_id="alpha")
        result = self._payload(
            self.runtime.execute(
                DEV_SERVER_RESTART,
                {"process_id": "srv-restart", "workstream_id": "alpha"},
                deadline_seconds=10,
            )
        )
        self.assertTrue(result.get("ok"), result)
        self.assertTrue(result.get("restarted"), result)
        self.assertTrue(result.get("exit_confirmed"), result)
        self.assertEqual(result.get("old_pid"), first["pid"])
        self.assertTrue(result.get("running"), result)
        new_pid = int(result["pid"])
        self._live_pids.append(new_pid)
        scope = json.loads(
            self.runtime._scope_path("srv-restart").read_text(encoding="utf-8")
        )
        self.assertEqual(scope["workstream_id"], "alpha")
        self.assertEqual(int(scope["pid"]), new_pid)
        identity = json.loads(
            self.runtime._identity_path("srv-restart").read_text(encoding="utf-8")
        )
        self.assertEqual(int(identity["pid"]), new_pid)
        if new_pid != int(first["pid"]):
            self.assertFalse(_pid_alive(int(first["pid"])))

    def test_cleanup_uses_persisted_named_workstream(self) -> None:
        payload = self._start("srv-cleanup-scope", workstream_id="alpha")
        result = self.runtime.cleanup_session()
        self.assertIn("srv-cleanup-scope", result["stopped_servers"])
        self.assertNotIn("srv-cleanup-scope", result["refused_servers"])
        self.assertFalse(_pid_alive(int(payload["pid"])))


if __name__ == "__main__":
    unittest.main()
