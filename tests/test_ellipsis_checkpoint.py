from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

import _path_setup
from karox.ellipsis_checkpoint import WorkspaceCheckpointStore
from karox.models import AccessProfile
from karox.sessions import SessionStore


def git(repository: Path, *argv: str) -> str:
    completed = subprocess.run(
        ["git", *argv],
        cwd=repository,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)
    return completed.stdout


class EllipsisCheckpointTests(unittest.TestCase):
    def test_rollback_after_stop_restores_index_worktree_and_untracked_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="karox-checkpoint-") as temporary:
            root = Path(temporary)
            repository = root / "repo"
            repository.mkdir()
            git(repository, "init")
            git(repository, "config", "user.email", "test@example.invalid")
            git(repository, "config", "user.name", "KaroX Test")
            tracked = repository / "tracked.txt"
            tracked.write_text("committed\n", encoding="utf-8")
            git(repository, "add", "tracked.txt")
            git(repository, "commit", "-m", "initial")

            tracked.write_text("staged baseline\n", encoding="utf-8")
            git(repository, "add", "tracked.txt")
            tracked.write_text("unstaged baseline\n", encoding="utf-8")
            existing_untracked = repository / "notes.txt"
            existing_untracked.write_text("private baseline\n", encoding="utf-8")

            sessions = SessionStore(root / "sessions")
            session_id = "checkpoint-session"
            sessions.create(
                repository,
                "test rollback",
                AccessProfile.WORKSPACE_WRITE,
                branch=git(repository, "branch", "--show-current").strip(),
                session_id=session_id,
            )
            store = WorkspaceCheckpointStore(root / "checkpoints")
            checkpoint = store.create(
                repository,
                session_id=session_id,
                sessions=sessions,
            )

            tracked.write_text("agent changed tracked\n", encoding="utf-8")
            existing_untracked.write_text("agent changed private file\n", encoding="utf-8")
            created = repository / "created-by-agent.txt"
            created.write_text("new\n", encoding="utf-8")
            with sessions.mutate(session_id, "record-changes", ttl_seconds=30) as session:
                session.changed_files.extend(
                    ["tracked.txt", "notes.txt", "created-by-agent.txt"]
                )

            # This mirrors `karox agent stop`: remote authority is revoked first,
            # but the user's later explicit rollback must still be available.
            sessions.revoke(session_id)
            result = store.rollback(
                repository,
                session_id=session_id,
                checkpoint_id=checkpoint.checkpoint_id,
                sessions=sessions,
            )
            self.assertEqual(tracked.read_text(encoding="utf-8"), "unstaged baseline\n")
            self.assertEqual(git(repository, "show", ":tracked.txt"), "staged baseline\n")
            self.assertEqual(
                existing_untracked.read_text(encoding="utf-8"),
                "private baseline\n",
            )
            self.assertFalse(created.exists())
            self.assertEqual(set(result["restored"]), {"tracked.txt", "notes.txt"})
            self.assertEqual(result["deleted"], ["created-by-agent.txt"])
            self.assertTrue(result["session_was_revoked"])


if __name__ == "__main__":
    unittest.main()
