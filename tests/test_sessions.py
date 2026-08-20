from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from _support import SRC  # noqa: F401 - inserts src on sys.path
from karox.models import AccessProfile
from karox.sessions import (
    IdempotencyConflict,
    SessionBusy,
    SessionError,
    SessionStore,
    StaleSessionRevision,
    current_mutation_lease,
    mutation_lease_context,
)


class SessionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        self.repository.mkdir()
        self.store = SessionStore(self.root / "sessions")
        self.record = self.store.create(
            self.repository,
            "repair the sample",
            AccessProfile.WORKSPACE_WRITE,
            session_id="sample",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_checksum_corruption_is_rejected(self) -> None:
        path = self.store.state_path("sample")
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["task"] = "tampered"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(SessionError, "checksum mismatch"):
            self.store.load("sample")

    def test_lease_fences_expired_owner(self) -> None:
        first = self.store.acquire("sample", "first", ttl_seconds=5)
        with self.assertRaises(SessionBusy):
            self.store.acquire("sample", "second", ttl_seconds=5)
        lease_path = self.store.lease_path("sample")
        payload = json.loads(lease_path.read_text(encoding="utf-8"))
        payload["expires_at"] = 0
        lease_path.write_text(json.dumps(payload), encoding="utf-8")
        second = self.store.acquire("sample", "second", ttl_seconds=5)
        self.assertNotEqual(first.fencing_token, second.fencing_token)
        with self.assertRaises(SessionBusy):
            self.store.validate_lease(first)
        self.store.release(second)

    def test_stale_session_revision_cannot_overwrite_newer_state(self) -> None:
        lease = self.store.acquire("sample", "owner")
        first = self.store.load("sample")
        stale = self.store.load("sample")
        first.summary = "newer"
        self.store.save(first, 0, lease)
        stale.summary = "stale"
        with self.assertRaises(StaleSessionRevision):
            self.store.save(stale, 0, lease)
        self.store.release(lease)

    def test_mutation_lease_context_is_explicit_and_does_not_weaken_acquire(self) -> None:
        lease = self.store.acquire("sample", "outer")
        try:
            self.assertIsNone(current_mutation_lease("sample"))
            with mutation_lease_context(lease):
                self.assertIs(current_mutation_lease("sample"), lease)
                self.assertIsNone(current_mutation_lease("different-session"))
                with self.assertRaises(SessionBusy):
                    self.store.acquire("sample", "parallel", ttl_seconds=5)
            self.assertIsNone(current_mutation_lease("sample"))
        finally:
            self.store.release(lease)

    def test_idempotency_replay_and_conflicting_input(self) -> None:
        lease = self.store.acquire("sample", "owner")
        record = self.store.load("sample")
        self.assertIsNone(
            self.store.begin_idempotent(record, lease, "same-key", "digest-a")
        )
        self.store.complete_idempotent(
            record, lease, "same-key", {"ok": True, "value": 1}
        )
        replay = self.store.begin_idempotent(
            self.store.load("sample"), lease, "same-key", "digest-a"
        )
        self.assertEqual(replay, {"ok": True, "value": 1})
        with self.assertRaises(IdempotencyConflict):
            self.store.begin_idempotent(
                self.store.load("sample"), lease, "same-key", "digest-b"
            )
        self.store.release(lease)

    def test_task_credentials_are_redacted_before_persistence(self) -> None:
        secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
        record = self.store.create(
            self.repository,
            f"debug provider {secret}",
            session_id="redacted-task",
        )
        self.assertNotIn(secret, record.task)
        self.assertNotIn(
            secret,
            self.store.state_path("redacted-task").read_text(encoding="utf-8"),
        )

    def test_atomic_session_save_retries_transient_windows_permission_error(self) -> None:
        lease = self.store.acquire("sample", "owner")
        record = self.store.load("sample")
        record.summary = "saved after transient lock"
        real_replace = os.replace
        attempts = 0

        def flaky_replace(source: object, destination: object) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise PermissionError(13, "transient Windows file lock")
            real_replace(source, destination)

        try:
            with patch("karox.sessions.os.replace", side_effect=flaky_replace), patch(
                "karox.sessions.time.sleep", return_value=None
            ):
                saved = self.store.save(record, 0, lease)
            self.assertEqual(saved.summary, "saved after transient lock")
            self.assertGreaterEqual(attempts, 2)
            self.assertEqual(self.store.load("sample").summary, "saved after transient lock")
        finally:
            self.store.release(lease)

    def test_revoke_atomically_fences_an_active_lease(self) -> None:
        lease = self.store.acquire("sample", "holder")
        revoked = self.store.revoke("sample")
        self.assertTrue(revoked.revoked)
        self.assertEqual(revoked.status, "revoked")
        with self.assertRaises(SessionBusy):
            self.store.validate_lease(lease)
        with self.assertRaisesRegex(SessionError, "revoked"):
            self.store.acquire("sample", "new-holder")

    def test_cross_process_takeover_save_and_heartbeat_are_serialized(self) -> None:
        original = self.store.acquire("sample", "expired-owner", ttl_seconds=5)
        lease_path = self.store.lease_path("sample")
        payload = json.loads(lease_path.read_text(encoding="utf-8"))
        payload["expires_at"] = 0
        lease_path.write_text(json.dumps(payload), encoding="utf-8")

        ready = self.root / "worker.ready"
        release = self.root / "worker.release"
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join(
            [str(Path(__file__).resolve().parent.parent / "src"), environment.get("PYTHONPATH", "")]
        )
        worker = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve().parent / "_session_process_worker.py"),
                str(self.store.root),
                str(ready),
                str(release),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
        )
        try:
            deadline = time.time() + 10
            while not ready.exists() and worker.poll() is None and time.time() < deadline:
                time.sleep(0.02)
            if not ready.exists():
                stdout, stderr = worker.communicate(timeout=1)
                self.fail(f"worker did not become ready: {(stdout, stderr)}")
            with self.assertRaises(SessionBusy):
                self.store.validate_lease(original)
            with self.assertRaises(SessionBusy):
                self.store.acquire("sample", "contender", ttl_seconds=5)
            self.assertEqual(self.store.load("sample").summary, "saved by worker")
            release.touch()
            stdout, stderr = worker.communicate(timeout=10)
            self.assertEqual(worker.returncode, 0, (stdout, stderr))
            successor = self.store.acquire("sample", "successor", ttl_seconds=5)
            self.store.release(successor)
        finally:
            release.touch(exist_ok=True)
            if worker.poll() is None:
                worker.terminate()
                worker.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
