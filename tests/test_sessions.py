from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from _support import SRC  # noqa: F401 - inserts src on sys.path
from karox.models import AccessProfile
from karox.sessions import (
    IdempotencyConflict,
    SessionBusy,
    SessionError,
    SessionStore,
    StaleSessionRevision,
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


if __name__ == "__main__":
    unittest.main()
