"""Universal memory layer: scopes, privacy, budgeted recall, revalidation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from _support import SRC  # noqa: F401 - inserts src on sys.path

from karox.memory import (
    KaroXMemory,
    MemoryError,
    MemoryKind,
    MemoryPolicyError,
    MemoryScope,
)


class MemoryLayerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.memory = KaroXMemory(Path(self.temporary.name))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_user_fact_round_trips_across_instances(self) -> None:
        self.memory.remember(
            scope=MemoryScope.USER,
            kind=MemoryKind.FACT,
            content="The user's name is Egor",
            key="user.name",
            provenance="chatgpt",
        )
        fresh = KaroXMemory(Path(self.temporary.name))
        found = fresh.recall(
            query="what is the user's name?",
            scopes=[(MemoryScope.USER, "default")],
        )
        self.assertEqual(len(found), 1)
        self.assertIn("Egor", found[0].content)
        self.assertEqual(found[0].provenance, "chatgpt")

    def test_scopes_are_isolated_on_disk(self) -> None:
        self.memory.remember(
            scope=MemoryScope.PROJECT,
            kind=MemoryKind.DECISION,
            content="Tests must never touch the real keyring",
            scope_id="karox-v5",
        )
        self.assertEqual(
            self.memory.list(scope=MemoryScope.USER), ()
        )
        self.assertEqual(
            len(self.memory.list(scope=MemoryScope.PROJECT, scope_id="karox-v5")), 1
        )

    def test_keyed_remember_upserts_instead_of_accumulating(self) -> None:
        for city in ("Warsaw", "Krakow"):
            self.memory.remember(
                scope=MemoryScope.USER,
                kind=MemoryKind.PREFERENCE,
                content=f"The user lives in {city}",
                key="user.city",
            )
        entries = self.memory.list(scope=MemoryScope.USER)
        self.assertEqual(len(entries), 1)
        self.assertIn("Krakow", entries[0].content)

    def test_credential_shaped_content_is_refused(self) -> None:
        for bad in (
            "my api_key = sk-abcdefghijklmnopqrstu123456",
            "password: hunter2-super-secret",
            "-----BEGIN RSA PRIVATE KEY-----",
            "bearer token = eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIx.abcdefghij_klmnop",
        ):
            with self.assertRaises(MemoryPolicyError):
                self.memory.remember(
                    scope=MemoryScope.SESSION,
                    kind=MemoryKind.NOTE,
                    content=bad,
                )
        self.assertEqual(self.memory.list(scope=MemoryScope.SESSION), ())

    def test_recall_is_ranked_budgeted_and_never_a_dump(self) -> None:
        self.memory.remember(
            scope=MemoryScope.USER,
            kind=MemoryKind.FACT,
            content="The user's name is Egor",
            key="user.name",
        )
        for index in range(20):
            self.memory.remember(
                scope=MemoryScope.USER,
                kind=MemoryKind.NOTE,
                content=f"Unrelated note number {index} about the weather",
            )
        found = self.memory.recall(
            query="как зовут пользователя? what is the user name",
            scopes=[(MemoryScope.USER, "default")],
            limit=3,
        )
        self.assertGreaterEqual(len(found), 1)
        self.assertIn("Egor", found[0].content)
        self.assertLessEqual(len(found), 3)

    def test_empty_query_recall_stays_within_budget(self) -> None:
        for index in range(10):
            self.memory.remember(
                scope=MemoryScope.WORKSTREAM,
                kind=MemoryKind.NOTE,
                content=("step detail " * 30) + str(index),
                scope_id="release",
            )
        found = self.memory.recall(
            query="",
            scopes=[(MemoryScope.WORKSTREAM, "release")],
            limit=10,
            budget_chars=800,
        )
        total = sum(len(entry.content) for entry in found)
        self.assertLessEqual(total, 800 + 400)

    def test_forget_removes_permanently(self) -> None:
        entry = self.memory.remember(
            scope=MemoryScope.USER,
            kind=MemoryKind.FACT,
            content="The user's name is Egor",
        )
        removed = self.memory.forget(
            scope=MemoryScope.USER, entry_id=entry.entry_id
        )
        self.assertEqual(removed, 1)
        self.assertEqual(self.memory.list(scope=MemoryScope.USER), ())
        fresh = KaroXMemory(Path(self.temporary.name))
        self.assertEqual(fresh.list(scope=MemoryScope.USER), ())

    def test_ttl_expiry_filters_entries(self) -> None:
        self.memory.remember(
            scope=MemoryScope.SESSION,
            kind=MemoryKind.NOTE,
            content="short-lived scratch fact",
            ttl_seconds=-1.0,
        )
        self.assertEqual(self.memory.list(scope=MemoryScope.SESSION), ())

    def test_source_backed_entries_go_stale_and_revalidate(self) -> None:
        entry = self.memory.remember(
            scope=MemoryScope.PROJECT,
            kind=MemoryKind.FACT,
            content="build command is python -m build --wheel",
            scope_id="karox-v5",
            source_path="pyproject.toml",
            source_sha256="abc123",
        )
        flagged = self.memory.mark_stale(source_path="pyproject.toml")
        self.assertEqual(flagged, 1)
        stored = self.memory.list(scope=MemoryScope.PROJECT, scope_id="karox-v5")
        self.assertEqual(stored[0].validation, "stale")
        refreshed = self.memory.revalidate(
            entry_id=entry.entry_id,
            scope=MemoryScope.PROJECT,
            scope_id="karox-v5",
            current_sha256="abc123",
        )
        assert refreshed is not None
        self.assertEqual(refreshed.validation, "valid")

    def test_context_produces_prompt_ready_block_or_nothing(self) -> None:
        self.assertEqual(
            self.memory.context(scopes=[(MemoryScope.USER, "default")]), ""
        )
        self.memory.remember(
            scope=MemoryScope.USER,
            kind=MemoryKind.PREFERENCE,
            content="The user prefers concise answers",
        )
        block = self.memory.context(
            scopes=[(MemoryScope.USER, "default")],
            task="answer style preference concise",
        )
        self.assertIn("KaroX memory", block)
        self.assertIn("concise", block)

    def test_personal_entries_can_be_excluded(self) -> None:
        self.memory.remember(
            scope=MemoryScope.USER,
            kind=MemoryKind.FACT,
            content="The user's name is Egor",
            sensitivity="personal",
        )
        found = self.memory.recall(
            query="user name Egor",
            scopes=[(MemoryScope.USER, "default")],
            include_personal=False,
        )
        self.assertEqual(found, ())

    def test_invalid_inputs_are_rejected(self) -> None:
        with self.assertRaises(MemoryError):
            self.memory.remember(
                scope=MemoryScope.USER, kind=MemoryKind.FACT, content="   "
            )
        with self.assertRaises(MemoryError):
            self.memory.remember(
                scope=MemoryScope.USER,
                kind=MemoryKind.FACT,
                content="x" * 4001,
            )
        with self.assertRaises(MemoryError):
            self.memory.forget(scope=MemoryScope.USER)


if __name__ == "__main__":
    unittest.main()
