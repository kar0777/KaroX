from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from _support import SRC, initialize_git_repository  # noqa: F401
from karox.hosted_bridge import CompositeHostedBridge, CoreToolBridge
from karox.models import AccessProfile
from karox.proxy_server import build_proxy_asgi_app
from karox.sessions import SessionStore
from test_hosted_bridge import _jsonrpc_result, _tools_call, _wire_requests


class ChatGPTInlineResultBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.repository = root / "repo"
        initialize_git_repository(self.repository)
        (self.repository / "large.txt").write_bytes(b"x" * 40_000)
        self.previous_runtime = os.environ.get("KAROX_RUNTIME_DIR")
        os.environ["KAROX_RUNTIME_DIR"] = str(root / "runtime")
        self.sessions = SessionStore(root / "sessions")
        self.sessions.create(
            self.repository,
            "ChatGPT inline result budget",
            AccessProfile.WORKSPACE_WRITE,
            session_id="chatgpt-inline-budget",
        )
        self.runtime = CompositeHostedBridge(
            [
                CoreToolBridge(
                    self.repository,
                    self.sessions,
                    "chatgpt-inline-budget",
                    ["karox.repo.read_file"],
                )
            ]
        )
        self.token = "inline-budget-token"

    def tearDown(self) -> None:
        if self.previous_runtime is None:
            os.environ.pop("KAROX_RUNTIME_DIR", None)
        else:
            os.environ["KAROX_RUNTIME_DIR"] = self.previous_runtime
        self.temp.cleanup()

    def _call(self, threshold: int):
        app = build_proxy_asgi_app(
            self.runtime,
            self.token,
            diagnostics={"schema_version": 1},
            inline_result_bytes=threshold,
        )
        (response,) = _wire_requests(
            app,
            [
                _tools_call(
                    self.token,
                    "karox.repo.read_file",
                    {"path": "large.txt"},
                )
            ],
        )
        self.assertEqual(response.status, 200, response.text)
        return response, _jsonrpc_result(response)

    def test_chatgpt_32k_budget_artifact_backs_result_that_64k_keeps_inline(self) -> None:
        inline_response, inline = self._call(64 * 1024)
        compact_response, compact = self._call(32 * 1024)

        self.assertFalse(inline["isError"])
        self.assertFalse(compact["isError"])
        self.assertIn("data", inline["structuredContent"])
        self.assertEqual(len(inline["structuredContent"]["data"]["content"]), 40_000)

        envelope = compact["structuredContent"]
        self.assertEqual(envelope["result_mode"], "artifact")
        self.assertTrue(envelope["truncated"])
        self.assertGreater(envelope["total_size"], 32 * 1024)
        self.assertIsInstance(envelope["artifact_id"], str)
        self.assertLess(len(compact_response.text), len(inline_response.text) // 10)


if __name__ == "__main__":
    unittest.main()
