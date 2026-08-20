from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from _support import initialize_git_repository

from karox.autonomy_runtime import CHECKS_RUN_AFFECTED, AutonomyRuntime
from karox.hosted_bridge import CoreToolBridge
from karox.models import AccessProfile, Origin, OriginKind
from karox.sessions import SessionStore


class AffectedChecksNestedLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.repository = root / "repo"
        initialize_git_repository(self.repository)
        (self.repository / "src").mkdir(exist_ok=True)
        (self.repository / "src" / "example.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.sessions = SessionStore(root / "sessions")
        self.session_id = "affected-nested-lease"
        self.sessions.create(
            self.repository,
            "Verify nested affected checks",
            AccessProfile.WORKSPACE_WRITE,
            branch="main",
            session_id=self.session_id,
        )
        self.check_argv = (
            sys.executable,
            "-c",
            "print('affected-check-ran')",
        )
        self.core = CoreToolBridge(
            self.repository,
            self.sessions,
            self.session_id,
            ("karox.checks.run",),
            hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "nested-core"),
            verification_commands=(self.check_argv,),
        )
        self.runtime = AutonomyRuntime(
            self.repository,
            self.sessions,
            self.session_id,
            (CHECKS_RUN_AFFECTED,),
            access_profile=AccessProfile.WORKSPACE_WRITE,
            hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "nested-autonomy"),
            connection_profile="chatgpt-web-test",
            verification_commands=(self.check_argv,),
            operation_runtime=self.core,
            client_kind="chatgpt-web",
        )

    def tearDown(self) -> None:
        self.runtime.close()
        self.temporary.cleanup()

    def test_run_affected_reuses_outer_session_lease_without_self_deadlock(self) -> None:
        arguments = {
            "mode": "current",
            "changed_files": ["src/example.py"],
            "timeout_seconds": 30,
        }
        result = self.runtime.execute(
            CHECKS_RUN_AFFECTED,
            arguments,
            idempotency_key="affected-nested-1",
        )

        self.assertTrue(result["ok"], result)
        self.assertFalse(result["idempotent_replay"])
        self.assertEqual(len(result["selected_checks"]), 1)
        self.assertEqual(result["selected_checks"][0]["tool"], "karox.checks.run")
        self.assertTrue(result["results"][0]["compact"]["ok"])
        self.assertFalse(self.sessions.lease_path(self.session_id).exists())

        replay = self.runtime.execute(
            CHECKS_RUN_AFFECTED,
            arguments,
            idempotency_key="affected-nested-1",
        )
        self.assertTrue(replay["ok"])
        self.assertTrue(replay["idempotent_replay"])
        self.assertFalse(self.sessions.lease_path(self.session_id).exists())


if __name__ == "__main__":
    unittest.main()
