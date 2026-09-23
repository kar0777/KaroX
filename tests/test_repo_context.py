from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _support import SRC, initialize_git_repository  # noqa: F401
from karox.artifacts import ArtifactStore
from karox.repo_context import RepositoryContextEngine, _safe_relative


class RepositoryContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        # resolve() canonicalizes runner-provided 8.3 TEMP aliases so path
        # comparisons inside _safe_relative stay consistent on Windows CI.
        root = Path(self.temp.name).resolve()
        self.repo = root / "repo"
        initialize_git_repository(self.repo)
        for name in ("src", "tests", "docs"):
            (self.repo / name).mkdir()
        (self.repo / "src" / "service.py").write_text(
            "class BridgeService:\n"
            "    def start_saved_bridge(self):\n"
            "        return launch_profile()\n\n"
            "def launch_profile():\n"
            "    return 'started'\n",
            encoding="utf-8",
        )
        (self.repo / "src" / "cli.py").write_text(
            "from .service import BridgeService\n\n"
            "def connect_command():\n"
            "    return BridgeService().start_saved_bridge()\n",
            encoding="utf-8",
        )
        (self.repo / "tests" / "test_service.py").write_text(
            "from src.service import BridgeService\n\n"
            "def test_start_saved_bridge():\n"
            "    assert BridgeService().start_saved_bridge() == 'started'\n",
            encoding="utf-8",
        )
        (self.repo / "docs" / "connect.md").write_text(
            "# Connect\nSaved bridge profile flow.\n", encoding="utf-8"
        )
        self.old_runtime = os.environ.get("KAROX_RUNTIME_DIR")
        os.environ["KAROX_RUNTIME_DIR"] = str(root / "runtime")
        self.artifacts = ArtifactStore("inspect-session")
        self.engine = RepositoryContextEngine(
            self.repo, self.artifacts, policy_profile="workspace_write"
        )

    def tearDown(self) -> None:
        if self.old_runtime is None:
            os.environ.pop("KAROX_RUNTIME_DIR", None)
        else:
            os.environ["KAROX_RUNTIME_DIR"] = self.old_runtime
        self.temp.cleanup()

    def test_inspect_ranks_flow_and_writes_artifact(self) -> None:
        result = self.engine.inspect(
            "BridgeService start_saved_bridge launch_profile flow tests", "focused"
        )
        self.assertTrue(result["ok"])
        paths = [item["path"] for item in result["important_findings"]]
        self.assertIn("src/service.py", paths)
        self.assertIn("tests/test_service.py", paths)
        data, record = self.artifacts.read(result["artifact_id"])
        full = json.loads(data)
        names = [item["name"] for item in full["symbols"]["definitions"]]
        self.assertIn("BridgeService", names)
        self.assertIn("start_saved_bridge", names)
        self.assertEqual(record.sha256, result["content_hash"])

    def test_primary_source_outweighs_snapshot_directory_keywords(self) -> None:
        source = "src/service.py"
        snapshot = "tools/bridge-profile-snapshot/src/service.py"
        tokens = ("bridge", "profile", "service")
        matches = [{"path": path, "line": 1, "text": "class BridgeProfileService:"}
                   for path in (source, snapshot)]
        ranked = self.engine._rank_files((source, snapshot), matches, {}, tokens)
        self.assertEqual(ranked[0]["path"], source)

    def test_excerpts_emit_each_source_line_once_and_read_each_file_once(self) -> None:
        from karox.repo_context import InspectBudget, _read_lines
        source = self.repo / "src" / "dense.py"
        source.write_text("\n".join(f"line_{i} = {i}" for i in range(1, 61)), encoding="utf-8")
        matches = [{"path": "src/dense.py", "line": i, "text": "match"}
                   for i in range(5, 41)]
        budget = InspectBudget(40, 200, 20, 3, 1_000_000)
        with mock.patch("karox.repo_context._read_lines", wraps=_read_lines) as read:
            excerpts = self.engine._excerpts(matches, [{"path": "src/dense.py"}], budget)
        numbers = [line["line"] for excerpt in excerpts for line in excerpt["lines"]]
        self.assertEqual(len(numbers), len(set(numbers)))
        self.assertTrue(set(range(5, 41)).issubset(numbers))
        self.assertLessEqual(len(numbers), 20 * 7)
        self.assertEqual(read.call_count, 1)

    def test_an_excerpt_never_hides_the_match_that_created_it(self) -> None:
        # The line budget can run out mid-window. A window that no longer covers
        # its own match spends context on a reason the reader cannot see, and
        # nothing in the result says the line was cut, so it must be dropped
        # rather than emitted partially.
        from karox.repo_context import InspectBudget

        dense = self.repo / "src" / "dense.py"
        dense.write_text(
            "\n".join(f"line_{i} = {i}" for i in range(1, 301)), encoding="utf-8"
        )
        late = self.repo / "src" / "late.py"
        late.write_text(
            "\n".join(f"line_{i} = {i}" for i in range(1, 301)), encoding="utf-8"
        )
        matches = [{"path": "src/dense.py", "line": i, "text": "match"}
                   for i in range(5, 136)]
        matches += [{"path": "src/late.py", "line": 200, "text": "match"},
                    {"path": "src/late.py", "line": 250, "text": "match"}]
        budget = InspectBudget(40, 200, 20, 3, 1_000_000)

        excerpts = self.engine._excerpts(
            matches, [{"path": "src/dense.py"}, {"path": "src/late.py"}], budget
        )

        self.assertTrue(excerpts)
        for excerpt in excerpts:
            covered = [
                match["line"]
                for match in matches
                if match["path"] == excerpt["path"]
                and excerpt["start"] <= match["line"] <= excerpt["end"]
            ]
            self.assertTrue(
                covered,
                "excerpt exposes no match of its own: "
                f"{excerpt['path']} {excerpt['start']}-{excerpt['end']}",
            )
        exposed = {
            (excerpt["path"], line)
            for excerpt in excerpts
            for line in range(excerpt["start"], excerpt["end"] + 1)
        }
        self.assertLess(
            len([match for match in matches
                 if (match["path"], match["line"]) in exposed]),
            len(matches),
            "the fixture no longer exhausts the line budget, so it proves nothing",
        )

    def test_non_git_directory_inspects_without_invoking_git(self) -> None:
        plain = Path(self.temp.name) / "plain-directory"
        (plain / "src").mkdir(parents=True)
        source = plain / "src" / "plain_handler.py"
        source.write_text(
            "def plain_handler():\n    return 'directory-ok'\n",
            encoding="utf-8",
        )
        artifacts = ArtifactStore("inspect-non-git-session")
        engine = RepositoryContextEngine(
            plain,
            artifacts,
            policy_profile="workspace_write",
        )

        self.assertFalse(engine.is_git_repository)
        with mock.patch.object(
            engine,
            "_git",
            side_effect=AssertionError("Git must not run for a non-Git root"),
        ):
            first = engine.inspect("plain_handler directory", "focused")
            cached = engine.inspect("plain_handler directory", "focused")

        self.assertTrue(first["ok"])
        self.assertTrue(cached["cache_hit"])
        self.assertIn(
            "src/plain_handler.py",
            [item["path"] for item in first["important_findings"]],
        )
        identity = engine._revision_identity()
        self.assertEqual(identity["repository_kind"], "directory")
        self.assertIsNone(identity["revision"])

        source.write_text(
            "def plain_handler():\n    return 'directory-updated'\n",
            encoding="utf-8",
        )
        with mock.patch.object(
            engine,
            "_git",
            side_effect=AssertionError("Git must not run for a non-Git root"),
        ):
            refreshed = engine.inspect("plain_handler directory", "focused")
        self.assertFalse(refreshed["cache_hit"])

    def test_dependency_hints_add_one_hop_local_imports_and_callers(self) -> None:
        (self.repo / "src" / "helper.py").write_text(
            "def format_state(value):\n    return value\n", encoding="utf-8"
        )
        service = self.repo / "src" / "service.py"
        service.write_text(
            "from .helper import format_state\n" + service.read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        result = self.engine.inspect(
            "BridgeService start_saved_bridge",
            "focused",
            include_dependency_hints=True,
        )
        hints = result["dependency_hints"]

        self.assertIn("src/helper.py", hints["implementation"])
        self.assertIn("tests/test_service.py", hints["tests"])
        self.assertGreaterEqual(hints["edge_count"], 2)

    def test_default_inspect_never_builds_the_broad_import_index(self) -> None:
        with mock.patch.object(
            self.engine,
            "_lightweight_imports",
            side_effect=AssertionError("broad import index entered default inspect"),
        ) as broad_index:
            result = self.engine.inspect("BridgeService start_saved_bridge", "focused")

        self.assertTrue(result["ok"])
        broad_index.assert_not_called()

    def test_typescript_symbol_is_indexed(self) -> None:
        (self.repo / "src" / "panel.ts").write_text(
            "export function buildPanel() {}\n", encoding="utf-8"
        )
        result = self.engine.inspect("buildPanel", "focused")
        data, _ = self.artifacts.read(result["artifact_id"])
        definitions = json.loads(data)["symbols"]["definitions"]
        self.assertTrue(any(item["name"] == "buildPanel" for item in definitions))

    def test_noisy_markdown_is_not_a_change_point(self) -> None:
        (self.repo / "NOISY.md").write_text(
            "BridgeService project map\n" * 500, encoding="utf-8"
        )
        (self.repo / "src" / "rare.py").write_text(
            "def rare_handler():\n    return 'BridgeService'\n", encoding="utf-8"
        )
        result = self.engine.inspect("BridgeService rare_handler project map", "focused")
        paths = [item["path"] for item in result["likely_change_points"]]
        self.assertNotIn("NOISY.md", paths)
        self.assertIn("src/rare.py", paths)
        data, _ = self.artifacts.read(result["artifact_id"])
        matches = json.loads(data)["matches"]
        noisy_matches = [item for item in matches if item["path"] == "NOISY.md"]
        self.assertLessEqual(len(noisy_matches), 10)

    def test_cache_hit_then_dirty_hash_invalidation(self) -> None:
        first = self.engine.inspect("BridgeService start_saved_bridge", "focused")
        cached = self.engine.inspect("BridgeService start_saved_bridge", "focused")
        self.assertTrue(cached["cache_hit"])
        self.assertEqual(first["artifact_id"], cached["artifact_id"])
        path = self.repo / "src" / "service.py"
        path.write_text(path.read_text(encoding="utf-8") + "\ndef stop_bridge():\n    return True\n", encoding="utf-8")
        changed = self.engine.inspect("BridgeService start_saved_bridge", "focused")
        self.assertFalse(changed["cache_hit"])
        self.assertNotEqual(first["artifact_id"], changed["artifact_id"])

    def test_selective_definition_read(self) -> None:
        result = self.engine.inspect("BridgeService", "focused")
        selected = self.artifacts.read_selection(
            result["artifact_id"], {"kind": "json_path", "path": "symbols.definitions"}
        )
        self.assertTrue(any(item["name"] == "BridgeService" for item in selected["content"]))

    def test_tracked_porcelain_path_keeps_first_character(self) -> None:
        subprocess.run(
            ["git", "-C", str(self.repo), "add", "."],
            check=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "-c",
                "user.name=KaroX Test",
                "-c",
                "user.email=karox@example.invalid",
                "commit",
                "-m",
                "fixture",
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        path = self.repo / "src" / "service.py"
        path.write_text(path.read_text(encoding="utf-8") + "\nTRACKED = True\n", encoding="utf-8")
        identity = self.engine._revision_identity()
        dirty_paths = [item["path"] for item in identity["dirty"]]
        self.assertIn("src/service.py", dirty_paths)
        self.assertNotIn("rc/service.py", dirty_paths)

    def test_fast_identity_compacts_wholly_untracked_directory(self) -> None:
        tree = self.repo / "scratch-tree"
        tree.mkdir()
        for index in range(20):
            (tree / f"file-{index}.txt").write_text(f"value-{index}\n", encoding="utf-8")
        identity = self.engine._fast_revision_identity()
        entries = [item for item in identity["dirty"] if item["path"].startswith("scratch-tree")]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["path"], "scratch-tree")

    def test_fast_identity_detects_change_inside_existing_untracked_directory(self) -> None:
        tree = self.repo / "scratch-tree"
        tree.mkdir()
        nested = tree / "nested.txt"
        nested.write_text("before\n", encoding="utf-8")
        before = self.engine._fast_revision_identity()
        nested.write_text("after-with-different-size\n", encoding="utf-8")
        after = self.engine._fast_revision_identity()
        self.assertNotEqual(before, after)
        before_entry = next(item for item in before["dirty"] if item["path"] == "scratch-tree")
        after_entry = next(item for item in after["dirty"] if item["path"] == "scratch-tree")
        self.assertNotEqual(before_entry["sha256"], after_entry["sha256"])

    def test_compact_content_identity_compacts_untracked_tree_but_remains_content_aware(self) -> None:
        tree = self.repo / "scratch-strict-tree"
        tree.mkdir()
        nested = tree / "nested.txt"
        nested.write_text("alpha\n", encoding="utf-8")
        before_stat = nested.stat()
        before = self.engine._compact_content_revision_identity()
        before_entries = [item for item in before["dirty"] if item["path"] == "scratch-strict-tree"]
        self.assertEqual(len(before_entries), 1)

        nested.write_text("omega\n", encoding="utf-8")
        os.utime(
            nested,
            ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns),
        )
        after = self.engine._compact_content_revision_identity()
        after_entries = [item for item in after["dirty"] if item["path"] == "scratch-strict-tree"]
        self.assertEqual(len(after_entries), 1)
        self.assertNotEqual(before_entries[0]["sha256"], after_entries[0]["sha256"])

    def test_identity_reads_normal_head_without_rev_parse_subprocess(self) -> None:
        subprocess.run(
            ["git", "-C", str(self.repo), "add", "."],
            check=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "-c",
                "user.name=KaroX Test",
                "-c",
                "user.email=karox@example.invalid",
                "commit",
                "-m",
                "head fixture",
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        with mock.patch.object(self.engine, "_git", wraps=self.engine._git) as git_call:
            identity = self.engine._fast_revision_identity()
        self.assertIn("revision", identity)
        self.assertFalse(
            any(call.args and call.args[0] == "rev-parse" for call in git_call.call_args_list)
        )

    def test_fast_identity_ignores_generated_runtime_trees(self) -> None:
        generated = self.repo / ".netlify" / "functions-serve"
        generated.mkdir(parents=True, exist_ok=True)
        (generated / "generated.ts").write_text("runtime churn\n", encoding="utf-8")
        vite = self.repo / "node_modules" / ".vite-temp"
        vite.mkdir(parents=True, exist_ok=True)
        (vite / "config.mjs").write_text("runtime churn\n", encoding="utf-8")
        identity = self.engine._fast_revision_identity()
        dirty_paths = [item["path"] for item in identity["dirty"]]
        self.assertFalse(any(path.startswith(".netlify") for path in dirty_paths))
        self.assertFalse(any(path.startswith("node_modules") for path in dirty_paths))

    def test_inspect_uses_git_grep_when_ripgrep_is_unavailable(self) -> None:
        with (
            mock.patch("karox.repo_context.shutil.which", return_value=None),
            mock.patch.object(
                self.engine,
                "_python_search",
                side_effect=AssertionError("python full scan should not run"),
            ),
        ):
            result = self.engine.inspect(
                "BridgeService fallback-native-search-unique", "focused"
            )
        self.assertTrue(result["ok"])
        paths = [item["path"] for item in result["important_findings"]]
        self.assertIn("src/service.py", paths)
        self.assertTrue(result["summary"]["matches"] >= 1)

    def test_rejects_parent_escape_and_invalid_depth(self) -> None:
        self.assertIsNone(_safe_relative(self.repo, "../outside.txt"))
        self.assertIsNone(_safe_relative(self.repo, "./../outside.txt"))
        self.assertEqual(_safe_relative(self.repo, "./src/service.py"), "src/service.py")
        with self.assertRaisesRegex(ValueError, "focused, standard, or deep"):
            self.engine.inspect("BridgeService", "unbounded")


if __name__ == "__main__":
    unittest.main()
