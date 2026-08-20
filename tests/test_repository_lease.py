from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from _support import SRC, initialize_git_repository  # noqa: F401

from karox.repository_lease import (
    RepositoryLease,
    RepositoryLeaseConflict,
    RepositoryLeaseError,
    RepositoryLeaseStore,
)


class RepositoryLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.repo = root / "repo"
        initialize_git_repository(self.repo)
        self.store = RepositoryLeaseStore(root / "leases")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _acquire(self, session: str, task: str, connection: str):
        return self.store.acquire(
            self.repo,
            session_id=session,
            task_id=task,
            connection_id=connection,
            current_operation="patch files",
            ttl_seconds=30,
        )

    def test_only_one_mutating_session_can_hold_repository(self) -> None:
        lease, recovered = self._acquire("session-a", "task-a", "chat-a")
        self.assertFalse(recovered)
        with self.assertRaises(RepositoryLeaseConflict) as caught:
            self._acquire("session-b", "task-b", "chat-b")
        details = caught.exception.details
        self.assertEqual(details["lease_owner"]["session_id"], "session-a")
        self.assertTrue(details["read_only_operations_allowed"])
        self.assertFalse(details["automatic_takeover_allowed"])
        self.assertIn("request_user_takeover", details["safe_options"])
        self.store.release(self.repo, lease)

    def test_same_owner_refreshes_without_creating_new_lease(self) -> None:
        first, _ = self._acquire("session-a", "task-a", "chat-a")
        second, recovered = self.store.acquire(
            self.repo,
            session_id="session-a",
            task_id="task-a",
            connection_id="chat-a",
            current_operation="run checks",
            ttl_seconds=30,
        )
        self.assertFalse(recovered)
        self.assertEqual(first.lease_id, second.lease_id)
        self.assertEqual(second.current_operation, "run checks")
        self.assertGreaterEqual(second.heartbeat_at, first.heartbeat_at)

    def test_expired_dead_owner_is_recovered_strictly(self) -> None:
        lease, _ = self._acquire("session-a", "task-a", "chat-a")
        path, _lock = self.store._paths(self.repo)
        expired = RepositoryLease(
            **{
                **lease.to_dict(),
                "heartbeat_at": time.time() - 120,
                "expires_at": time.time() - 60,
            }
        )
        path.write_text(json.dumps(expired.to_dict()), encoding="utf-8")
        with patch("karox.repository_lease._process_alive", return_value=False):
            replacement, recovered = self._acquire("session-b", "task-b", "chat-b")
        self.assertTrue(recovered)
        self.assertEqual(replacement.session_id, "session-b")

    def test_expired_but_live_owner_is_never_taken_over(self) -> None:
        lease, _ = self._acquire("session-a", "task-a", "chat-a")
        path, _lock = self.store._paths(self.repo)
        expired = RepositoryLease(
            **{
                **lease.to_dict(),
                "heartbeat_at": time.time() - 120,
                "expires_at": time.time() - 60,
            }
        )
        path.write_text(json.dumps(expired.to_dict()), encoding="utf-8")
        with (
            patch("karox.repository_lease._process_alive", return_value=True),
            patch(
                "karox.repository_lease.process_creation_marker",
                return_value=lease.owner_creation_marker,
            ),
        ):
            with self.assertRaises(RepositoryLeaseConflict):
                self._acquire("session-b", "task-b", "chat-b")

    def test_pid_reuse_marker_allows_expired_recovery(self) -> None:
        lease, _ = self._acquire("session-a", "task-a", "chat-a")
        path, _lock = self.store._paths(self.repo)
        expired_payload = {
            **lease.to_dict(),
            "owner_creation_marker": "old-process",
            "heartbeat_at": time.time() - 120,
            "expires_at": time.time() - 60,
        }
        path.write_text(json.dumps(expired_payload), encoding="utf-8")
        with (
            patch("karox.repository_lease._process_alive", return_value=True),
            patch("karox.repository_lease.process_creation_marker", return_value="new-process"),
        ):
            replacement, recovered = self._acquire("session-b", "task-b", "chat-b")
        self.assertTrue(recovered)
        self.assertEqual(replacement.session_id, "session-b")

    def test_release_refuses_another_lease_id(self) -> None:
        lease, _ = self._acquire("session-a", "task-a", "chat-a")
        impostor = RepositoryLease(**{**lease.to_dict(), "lease_id": "different"})
        with self.assertRaisesRegex(RepositoryLeaseError, "another session"):
            self.store.release(self.repo, impostor)
        self.assertIsNotNone(self.store.load(self.repo))

    def test_concurrent_acquire_has_one_winner(self) -> None:
        barrier = threading.Barrier(2)
        winners: list[str] = []
        conflicts: list[str] = []

        def worker(name: str) -> None:
            barrier.wait()
            try:
                lease, _ = self._acquire(name, f"task-{name}", f"chat-{name}")
                winners.append(lease.session_id)
            except RepositoryLeaseConflict:
                conflicts.append(name)

        threads = [threading.Thread(target=worker, args=(name,)) for name in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(conflicts), 1)


if __name__ == "__main__":
    unittest.main()
