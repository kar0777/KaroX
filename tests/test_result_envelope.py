from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from mcp.types import CallToolResult

from _support import SRC  # noqa: F401

from karox.artifacts import ArtifactStore
from karox.result_envelope import NEVER_SPILLED_TOOLS, artifact_backed_result


class ResultEnvelopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.previous = os.environ.get("KAROX_VNEXT_RUNTIME_DIR")
        os.environ["KAROX_VNEXT_RUNTIME_DIR"] = str(Path(self.temp.name) / "runtime")
        os.environ.pop("KAROX_RUNTIME_DIR", None)
        self.store = ArtifactStore("result-session")

    def tearDown(self) -> None:
        if self.previous is None:
            os.environ.pop("KAROX_VNEXT_RUNTIME_DIR", None)
        else:
            os.environ["KAROX_VNEXT_RUNTIME_DIR"] = self.previous
        self.temp.cleanup()

    def test_small_result_is_returned_unchanged(self) -> None:
        original = {"ok": True, "data": {"value": 1}}
        result = artifact_backed_result(
            original,
            tool_name="karox.repo.read_file",
            store=self.store,
            threshold_bytes=4096,
        )
        self.assertIs(result, original)
        self.assertEqual(self.store.list(), ())

    def test_large_result_becomes_compact_artifact_envelope(self) -> None:
        original = {
            "ok": True,
            "command": "repo.search",
            "data": {
                "match_count": 500,
                "matches": [{"path": f"src/file-{index}.py", "text": "x" * 80} for index in range(500)],
            },
        }
        result = artifact_backed_result(
            original,
            tool_name="karox.repo.search",
            store=self.store,
            threshold_bytes=4096,
        )
        self.assertIsInstance(result, CallToolResult)
        assert isinstance(result, CallToolResult)
        self.assertFalse(result.isError)
        envelope = result.structuredContent
        assert envelope is not None
        self.assertTrue(envelope["truncated"])
        self.assertEqual(envelope["result_mode"], "artifact")
        self.assertGreater(envelope["total_size"], 4096)
        self.assertEqual(envelope["persistence_policy"], "session")
        self.assertIn("data.matches", envelope["available_sections"])
        artifact_id = envelope["artifact_id"]
        data, record = self.store.read(artifact_id)
        payload = json.loads(data)
        self.assertEqual(payload["data"]["match_count"], 500)
        self.assertEqual(record.sha256, envelope["content_hash"])

    def test_large_result_is_redacted_before_artifact_persistence(self) -> None:
        token = "ghp_" + "0123456789abcdefghijklmnopqrstuvwxyz"
        original = {
            "ok": True,
            "data": {
                "authorization": f"Bearer {token}",
                "content": (f"secret={token}\n" * 500),
            },
        }
        result = artifact_backed_result(
            original,
            tool_name="karox.repo.read_file",
            store=self.store,
            threshold_bytes=4096,
        )
        assert isinstance(result, CallToolResult)
        artifact_id = result.structuredContent["artifact_id"]
        data, _record = self.store.read(artifact_id)
        self.assertNotIn(token.encode(), data)
        self.assertIn(b"REDACTED", data)

    def test_selective_json_path_and_section_reads(self) -> None:
        payload = {
            "ok": True,
            "data": {
                "matches": [{"line": index, "text": f"match-{index}"} for index in range(1000)],
                "first_failure": {"line": 77, "message": "assertion failed"},
            },
        }
        record = self.store.put(
            json.dumps(payload).encode(),
            name="search.json",
            mime="application/json",
        )
        selection = self.store.read_selection(
            record.artifact_id,
            {"kind": "json_path", "path": "data.first_failure"},
        )
        self.assertEqual(selection["content"]["line"], 77)
        self.assertFalse(selection["truncated"])
        section = self.store.read_selection(
            record.artifact_id,
            {"kind": "section", "name": "data.matches.5"},
        )
        self.assertEqual(section["content"]["text"], "match-5")

    def test_selective_line_tail_regex_and_first_failure_reads(self) -> None:
        text = "\n".join(
            ["setup", "running", "ERROR first failure", "detail", "ERROR second failure"]
        )
        record = self.store.put(text.encode(), name="checks.txt", mime="text/plain")
        lines = self.store.read_selection(
            record.artifact_id,
            {"kind": "line_range", "start": 2, "count": 2},
        )
        self.assertEqual(lines["content"], "running\nERROR first failure")
        tail = self.store.read_selection(
            record.artifact_id,
            {"kind": "tail", "count": 2},
        )
        self.assertEqual(tail["content"], "detail\nERROR second failure")
        regex = self.store.read_selection(
            record.artifact_id,
            {"kind": "regex", "pattern": "ERROR", "max_matches": 10},
        )
        self.assertEqual(regex["diagnostics"]["match_count"], 2)
        first = self.store.read_selection(
            record.artifact_id,
            {"kind": "first_failure"},
        )
        self.assertEqual(first["content"][0]["line"], 3)

    def test_selection_is_bounded(self) -> None:
        record = self.store.put(
            ("line of output\n" * 5000).encode(),
            name="large.log",
            mime="text/plain",
        )
        selected = self.store.read_selection(
            record.artifact_id,
            {"kind": "tail", "count": 5000},
            max_output_bytes=1024,
        )
        self.assertTrue(selected["truncated"])
        self.assertLessEqual(selected["selected_size"], 1024)


class RetrievalToolsAreNeverSpilledTests(unittest.TestCase):
    """The tools that read an artifact must never answer with another pointer.

    ``karox.artifact.get`` is how a client escapes a spilled result. Spilling its
    own answer hands back a new artifact id instead of the requested bytes, and
    following that pointer spills again -- an unbounded regress that also writes
    a fresh copy of the payload on every hop. Its output is already bounded by
    the caller's ``max_output_bytes``, so it is returned as-is.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.previous = os.environ.get("KAROX_VNEXT_RUNTIME_DIR")
        os.environ["KAROX_VNEXT_RUNTIME_DIR"] = str(Path(self.temp.name) / "runtime")
        os.environ.pop("KAROX_RUNTIME_DIR", None)
        self.store = ArtifactStore("retrieval-session")

    def tearDown(self) -> None:
        if self.previous is None:
            os.environ.pop("KAROX_VNEXT_RUNTIME_DIR", None)
        else:
            os.environ["KAROX_VNEXT_RUNTIME_DIR"] = self.previous
        self.temp.cleanup()

    def _big_selection(self) -> dict[str, object]:
        return {
            "ok": True,
            "command": "artifact.get",
            "selection": {
                "artifact_id": "art-0123456789abcdef0123",
                "selector": "json_path",
                "content": "x" * 200_000,
                "truncated": False,
                "selected_size": 200_000,
            },
        }

    def test_artifact_get_result_is_returned_inline(self) -> None:
        original = self._big_selection()
        result = artifact_backed_result(
            original,
            tool_name="karox.artifact.get",
            store=self.store,
            threshold_bytes=4096,
        )
        self.assertIs(result, original)
        self.assertEqual(self.store.list(), ())

    def test_artifact_read_image_result_is_returned_inline(self) -> None:
        original = {"ok": True, "command": "artifact.read_image", "data": {"blob": "y" * 200_000}}
        result = artifact_backed_result(
            original,
            tool_name="karox.artifact.read_image",
            store=self.store,
            threshold_bytes=4096,
        )
        self.assertIs(result, original)

    def test_other_tools_still_spill(self) -> None:
        result = artifact_backed_result(
            self._big_selection(),
            tool_name="karox.repo.read_file",
            store=self.store,
            threshold_bytes=4096,
        )
        self.assertIsInstance(result, CallToolResult)

    def test_the_exemption_names_the_hosted_artifact_tools(self) -> None:
        self.assertEqual(
            NEVER_SPILLED_TOOLS,
            frozenset({"karox.artifact.get", "karox.artifact.read_image"}),
        )


if __name__ == "__main__":
    unittest.main()
