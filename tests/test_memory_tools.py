"""karox.memory.* hosted tools: the cross-client memory protocol surface."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from _support import SRC, initialize_git_repository  # noqa: F401

from karox.autonomy_runtime import (
    MEMORY_CONTEXT,
    MEMORY_FORGET,
    MEMORY_LIST,
    MEMORY_RECALL,
    MEMORY_REMEMBER,
    AutonomyRuntime,
)
from karox.hosted_bridge import AUTONOMY_TOOL_NAMES
from karox.models import AccessProfile, Origin, OriginKind
from karox.sessions import SessionStore

_MEMORY_TOOLS = (
    MEMORY_REMEMBER,
    MEMORY_RECALL,
    MEMORY_CONTEXT,
    MEMORY_LIST,
    MEMORY_FORGET,
)


class MemoryToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.repo = root / "repo"
        initialize_git_repository(self.repo)
        self.sessions = SessionStore(root / "sessions")
        self.runtimes: list[AutonomyRuntime] = []

    def tearDown(self) -> None:
        for runtime in self.runtimes:
            runtime.close()
        self.temp.cleanup()

    def _runtime(self, session_id: str, client_kind: str) -> AutonomyRuntime:
        self.sessions.create(
            self.repo,
            "cross-client memory acceptance",
            AccessProfile.WORKSPACE_WRITE,
            session_id=session_id,
        )
        runtime = AutonomyRuntime(
            self.repo,
            self.sessions,
            session_id,
            _MEMORY_TOOLS,
            access_profile=AccessProfile.WORKSPACE_WRITE,
            hosted_origin=Origin(OriginKind.HOSTED_CLIENT, f"test-{client_kind}"),
            connection_profile="memory-acceptance",
            client_kind=client_kind,
        )
        self.runtimes.append(runtime)
        return runtime

    def test_memory_tools_are_registered_in_the_catalogue(self) -> None:
        for name in _MEMORY_TOOLS:
            self.assertIn(name, AUTONOMY_TOOL_NAMES)

    def test_remember_then_recall_round_trip(self) -> None:
        runtime = self._runtime("session-a", "chatgpt-web")
        stored = runtime.execute(
            MEMORY_REMEMBER,
            {
                "scope": "user",
                "kind": "fact",
                "content": "The user's name is Egor",
                "key": "user.name",
            },
        )
        self.assertTrue(stored["ok"])
        self.assertEqual(stored["entry"]["provenance"], "chatgpt-web")
        found = runtime.execute(
            MEMORY_RECALL, {"query": "what is the user's name?"}
        )
        self.assertTrue(found["ok"])
        self.assertEqual(found["count"], 1)
        self.assertIn("Egor", found["entries"][0]["content"])

    def test_cross_client_memory_without_shared_transcript(self) -> None:
        # Client A (ChatGPT) stores a user fact in its own session.
        chatgpt = self._runtime("session-chatgpt", "chatgpt-web")
        chatgpt.execute(
            MEMORY_REMEMBER,
            {
                "scope": "user",
                "kind": "fact",
                "content": "The user's name is Egor",
                "key": "user.name",
            },
        )
        # Client B (Adapt) has a different session and no transcript access,
        # yet recalls the same user-scoped memory through the same runtime.
        adapt = self._runtime("session-adapt", "adapt")
        found = adapt.execute(MEMORY_RECALL, {"query": "user name?"})
        self.assertTrue(found["ok"])
        self.assertEqual(found["count"], 1)
        self.assertIn("Egor", found["entries"][0]["content"])
        # Session-scoped memory stays isolated between those clients.
        chatgpt.execute(
            MEMORY_REMEMBER,
            {"scope": "session", "kind": "note", "content": "scratch detail"},
        )
        session_view = adapt.execute(MEMORY_LIST, {"scope": "session"})
        self.assertEqual(session_view["count"], 0)

    def test_context_tool_returns_prompt_ready_block(self) -> None:
        runtime = self._runtime("session-a", "adapt")
        runtime.execute(
            MEMORY_REMEMBER,
            {
                "scope": "user",
                "kind": "preference",
                "content": "The user prefers concise answers",
            },
        )
        block = runtime.execute(
            MEMORY_CONTEXT, {"task": "concise answer style preference"}
        )
        self.assertTrue(block["ok"])
        self.assertIn("concise", block["context"])

    def test_credential_content_is_refused_with_error_envelope(self) -> None:
        runtime = self._runtime("session-a", "adapt")
        result = runtime.execute(
            MEMORY_REMEMBER,
            {
                "scope": "user",
                "kind": "note",
                "content": "api_key = sk-abcdefghijklmnopqrstu123456",
            },
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "memory_rejected")
        empty = runtime.execute(MEMORY_LIST, {"scope": "user"})
        self.assertEqual(empty["count"], 0)

    def test_forget_removes_by_key(self) -> None:
        runtime = self._runtime("session-a", "adapt")
        runtime.execute(
            MEMORY_REMEMBER,
            {
                "scope": "user",
                "kind": "fact",
                "content": "The user's name is Egor",
                "key": "user.name",
            },
        )
        removed = runtime.execute(
            MEMORY_FORGET, {"scope": "user", "key": "user.name"}
        )
        self.assertTrue(removed["ok"])
        self.assertEqual(removed["removed"], 1)
        empty = runtime.execute(MEMORY_LIST, {"scope": "user"})
        self.assertEqual(empty["count"], 0)

    def test_unknown_scope_is_rejected(self) -> None:
        runtime = self._runtime("session-a", "adapt")
        result = runtime.execute(
            MEMORY_REMEMBER,
            {"scope": "global", "kind": "fact", "content": "x"},
        )
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
