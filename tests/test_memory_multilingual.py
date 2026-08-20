"""Unicode/Cyrillic memory retrieval: Russian is first-class KaroX UX.

Regression for the release-live-pass3 finding: a purely Cyrillic question
("Как меня зовут?") used to return an empty context because ranking relied on
raw token overlap with English-keyed entries. Retrieval must now work for
Cyrillic, Latin, and mixed-language queries deterministically, without an
embedding subsystem, and within the retrieval budget.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from _support import SRC  # noqa: F401 - inserts src on sys.path

from karox.memory import KaroXMemory, MemoryKind, MemoryScope


class CyrillicRetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.memory = KaroXMemory(Path(self.temporary.name))
        self.memory.remember(
            scope=MemoryScope.USER,
            kind=MemoryKind.PREFERENCE,
            content="preferred_name = Егор. The user prefers to be addressed as Егор.",
            key="preferred_name",
            provenance="chatgpt-web",
            sensitivity="personal",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _recall(self, query: str):
        return self.memory.recall(
            query=query, scopes=[(MemoryScope.USER, "default")]
        )

    def test_cyrillic_question_finds_preferred_name(self) -> None:
        for query in (
            "Как меня зовут?",
            "как меня зовут",
            "МОЁ ИМЯ?",
        ):
            with self.subTest(query=query):
                found = self._recall(query)
                self.assertEqual(len(found), 1, query)
                self.assertIn("Егор", found[0].content)

    def test_latin_question_still_finds_preferred_name(self) -> None:
        found = self._recall("what is my name?")
        self.assertEqual(len(found), 1)
        self.assertIn("Егор", found[0].content)

    def test_mixed_language_query_finds_preferred_name(self) -> None:
        found = self._recall("please remind: как меня зовут, my name")
        self.assertEqual(len(found), 1)
        self.assertIn("Егор", found[0].content)

    def test_unrelated_cyrillic_query_matches_nothing(self) -> None:
        self.assertEqual(self._recall("какая погода завтра"), ())

    def test_cyrillic_task_builds_nonempty_context(self) -> None:
        block = self.memory.context(
            scopes=[(MemoryScope.USER, "default")],
            task="Ответить пользователю на вопрос: Как меня зовут?",
        )
        self.assertIn("Егор", block)

    def test_yo_normalization_is_symmetric(self) -> None:
        self.memory.remember(
            scope=MemoryScope.USER,
            kind=MemoryKind.NOTE,
            content="ёлка у офиса наряжается в декабре",
            key="office.tree",
        )
        found = self._recall("елка")
        self.assertEqual(len(found), 1)
        self.assertIn("офиса", found[0].content)

    def test_exact_key_mention_boosts_deterministically(self) -> None:
        self.memory.remember(
            scope=MemoryScope.USER,
            kind=MemoryKind.NOTE,
            content="name badge printers live in room 4",
            key="office.badges",
        )
        first = self._recall("preferred_name")
        self.assertGreaterEqual(len(first), 1)
        self.assertEqual(first[0].key, "preferred_name")
        again = self._recall("preferred_name")
        self.assertEqual([e.entry_id for e in first], [e.entry_id for e in again])

    def test_budget_still_bounds_retrieval(self) -> None:
        for index in range(10):
            self.memory.remember(
                scope=MemoryScope.USER,
                kind=MemoryKind.NOTE,
                content=("имя проекта " + str(index) + " ") * 30,
                key=f"project.name.{index}",
            )
        found = self.memory.recall(
            query="имя",
            scopes=[(MemoryScope.USER, "default")],
            limit=20,
            budget_chars=600,
        )
        self.assertLessEqual(sum(len(e.content) for e in found), 600)


if __name__ == "__main__":
    unittest.main()
