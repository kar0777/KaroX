from __future__ import annotations

import contextlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from mcp.types import CallToolResult, TextContent

from _support import SRC  # noqa: F401

from karox.tool_telemetry import (
    ToolTraceContext,
    ToolTraceSpan,
    ToolTraceStore,
    json_size,
    result_metadata,
    safety_tier_for_tool,
)


class ToolTelemetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = ToolTraceStore("session-a", root=self.root, limit=100)
        self.context = ToolTraceContext(
            session_id="session-a",
            task_id="task-abc",
            connection_id="profile-a",
            client_kind="chatgpt-web",
            permission_profile="workspace_write",
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _span(self, arguments: dict[str, object], *, key: str = "op-1") -> ToolTraceSpan:
        return ToolTraceSpan(
            store=self.store,
            context=self.context,
            tool_name="karox.repo.read_file",
            arguments=arguments,
            read_only=True,
            idempotency_key=key,
            repository_revision_before=3,
        )

    def test_trace_persists_metadata_without_arguments_or_results(self) -> None:
        secret = "ghp_" + "0123456789abcdefghijklmnopqrstuvwxyz"
        password = "correct horse battery staple"
        arguments = {
            "path": "src/private.py",
            "authorization": f"Bearer {secret}",
            "password": password,
        }
        span = self._span(arguments)
        result = {
            "ok": True,
            "data": {"content": f"do not persist {secret}", "cache_hit": True},
        }
        span.finish(result, repository_revision_after=3)

        rows = self.store.list()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["tool_name"], "karox.repo.read_file")
        self.assertEqual(row["input_size_bytes"], json_size(arguments))
        self.assertTrue(row["success"])
        self.assertTrue(row["cache_hit"])
        self.assertNotIn("arguments", row)
        self.assertNotIn("content", row)

        with contextlib.closing(sqlite3.connect(self.store.path)) as connection:
            raw = "\n".join(
                str(item[0]) for item in connection.execute("SELECT payload FROM tool_trace")
            )
        self.assertNotIn(secret, raw)
        self.assertNotIn(password, raw)
        self.assertNotIn("src/private.py", raw)

    def test_trace_result_metadata_detects_errors_and_artifacts(self) -> None:
        error = CallToolResult(
            content=[TextContent(type="text", text="denied")],
            structuredContent={"ok": False, "error_code": "denied"},
            isError=True,
        )
        error_meta = result_metadata(error)
        self.assertFalse(error_meta["success"])
        self.assertEqual(error_meta["error_code"], "denied")
        self.assertEqual(error_meta["result_mode"], "error")

        artifact_meta = result_metadata(
            {
                "ok": True,
                "artifact_id": "art-1",
                "size": 4096,
                "idempotent_replay": True,
            }
        )
        self.assertTrue(artifact_meta["success"])
        self.assertEqual(artifact_meta["artifact_output_bytes"], 4096)
        self.assertEqual(artifact_meta["result_mode"], "artifact")
        self.assertTrue(artifact_meta["idempotent_replay"])

    def test_store_is_bounded_and_aggregates_baseline_metrics(self) -> None:
        for index in range(125):
            span = self._span({"path": f"file-{index % 2}.py"}, key=f"same-{index % 2}")
            span.finish(
                {
                    "ok": index % 10 != 0,
                    "error_code": "invalid_request" if index % 10 == 0 else None,
                },
                repository_revision_after=3,
            )
        rows = self.store.list(limit=1000)
        self.assertEqual(len(rows), 100)
        metrics = self.store.aggregate()
        self.assertEqual(metrics["tool_calls"], 100)
        self.assertGreater(metrics["repeated_tool_calls"], 0)
        self.assertGreater(metrics["input_argument_bytes"], 0)
        self.assertGreater(metrics["validation_failures"], 0)
        self.assertFalse(metrics["task_success"])

    def test_safety_tiers_are_explicit(self) -> None:
        self.assertEqual(safety_tier_for_tool("karox.repo.read_file", read_only=True), 0)
        self.assertEqual(safety_tier_for_tool("karox.repo.write_file", read_only=False), 1)
        self.assertEqual(safety_tier_for_tool("karox.task.execute_plan", read_only=False), 2)
        self.assertEqual(safety_tier_for_tool("karox.browser.close", read_only=False), 3)

    def test_trace_context_hashes_task_text(self) -> None:
        class Runtime:
            def session_info(self) -> dict[str, object]:
                return {
                    "session_id": "session-a",
                    "task": "private user objective",
                    "access_profile": "workspace_write",
                }

        context = ToolTraceContext.from_runtime(
            Runtime(),
            {"saved_profile": "clickup-opus", "target_profile": "chatgpt-web"},
        )
        self.assertIsNotNone(context)
        assert context is not None
        self.assertTrue(context.task_id.startswith("task-"))
        self.assertNotIn("private", context.task_id)
        self.assertEqual(context.connection_id, "clickup-opus")
        self.assertEqual(context.client_kind, "chatgpt-web")

    def test_event_schema_contains_no_free_form_payload_fields(self) -> None:
        span = self._span({"blob": "secret"})
        span.finish(
            {"ok": True, "data": {"blob": "secret-result"}},
            repository_revision_after=4,
        )
        row = self.store.list()[0]
        forbidden = {
            "arguments",
            "tool_arguments",
            "file_contents",
            "user_message",
            "authorization",
            "headers",
            "clipboard",
            "browser_form_values",
        }
        self.assertTrue(forbidden.isdisjoint(row))
        json.dumps(row, sort_keys=True)


if __name__ == "__main__":
    unittest.main()
