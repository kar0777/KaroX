"""Project fact map: deterministic onboarding facts with incremental refresh."""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _support import SRC, initialize_git_repository  # noqa: F401

from karox.project_map import ProjectFactMap


class ProjectFactMapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.repo = root / "repo"
        initialize_git_repository(self.repo)
        (self.repo / "pyproject.toml").write_text(
            '[build-system]\nrequires = ["setuptools"]\n\n'
            '[project]\nname = "sample-project"\nversion = "1.0"\n\n'
            "[project.scripts]\nsample = \"sample.cli:main\"\n\n"
            "[tool.pytest.ini_options]\ntestpaths = [\"tests\"]\n",
            encoding="utf-8",
        )
        (self.repo / "AGENTS.md").write_text(
            "# Agents\nRun pytest before committing.\n", encoding="utf-8"
        )
        (self.repo / "src").mkdir(exist_ok=True)
        (self.repo / "src" / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.repo / "tests").mkdir(exist_ok=True)
        (self.repo / "tests" / "test_sample.py").write_text(
            "def test_value():\n    assert True\n", encoding="utf-8"
        )
        self.storage = root / "maps" / "sample.json"
        self.map = ProjectFactMap(self.repo, self.storage)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_build_extracts_evidence_backed_facts(self) -> None:
        facts = self.map.build()
        project = facts["project"]
        self.assertEqual(project["project_type"]["value"], "python")
        self.assertEqual(project["name"]["value"], "sample-project")
        self.assertEqual(project["test_command"]["value"], "python -m pytest")
        self.assertIn("python -m build --wheel", project["build_commands"]["value"])
        self.assertEqual(project["entrypoints"]["value"], ["sample"])
        self.assertIn("python", facts["languages"])
        self.assertGreaterEqual(facts["test_files"], 1)
        # Every config-derived fact carries its evidence source and hash.
        self.assertEqual(project["name"]["source_path"], "pyproject.toml")
        self.assertIn("pyproject.toml", facts["sources"])

    def test_instruction_files_are_ingested_not_copied(self) -> None:
        facts = self.map.build()
        agents = facts["instructions"]["AGENTS.md"]
        self.assertTrue(agents["present"])
        self.assertIn("pytest", agents["head"])
        self.assertFalse(facts["instructions"]["CLAUDE.md"]["present"])

    def test_refresh_is_a_noop_for_unrelated_changes(self) -> None:
        built = self.map.build()
        refreshed = self.map.refresh(["docs/notes.txt"])
        self.assertEqual(built["generated_at"], refreshed["generated_at"])

    def test_refresh_updates_only_changed_config_facts(self) -> None:
        self.map.build()
        (self.repo / "pyproject.toml").write_text(
            '[build-system]\nrequires = ["setuptools"]\n\n'
            '[project]\nname = "renamed-project"\nversion = "2.0"\n',
            encoding="utf-8",
        )
        refreshed = self.map.refresh(["pyproject.toml"])
        self.assertEqual(refreshed["project"]["name"]["value"], "renamed-project")

    def test_code_changes_trigger_full_rebuild(self) -> None:
        self.map.build()
        (self.repo / "src" / "extra.py").write_text("X = 2\n", encoding="utf-8")
        refreshed = self.map.refresh(["src/extra.py"])
        self.assertGreaterEqual(refreshed["languages"]["python"], 2)

    def test_summary_is_compact_and_prompt_ready(self) -> None:
        self.map.build()
        digest = self.map.summary()
        self.assertIn("sample-project", digest)
        self.assertIn("languages", digest)
        self.assertLessEqual(len(digest), 900)

    def test_build_reads_each_evidence_file_once(self) -> None:
        (self.repo / "package.json").write_text(
            '{"name": "node-project", "scripts": {"test": "node --test"}}', encoding="utf-8"
        )
        paths = {self.repo / name for name in ("pyproject.toml", "package.json", "AGENTS.md")}
        reads = {path: 0 for path in paths}
        original = Path.open

        def counted(path, mode="r", *args, **kwargs):
            if path in reads and "r" in mode:
                reads[path] += 1
            return original(path, mode, *args, **kwargs)

        with mock.patch.object(Path, "open", counted):
            facts = self.map.build()
        self.assertEqual(reads, dict.fromkeys(paths, 1))
        for path in paths:
            self.assertEqual(facts["sources"][path.name], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(facts["project"]["name"]["value"], "sample-project")
        self.assertEqual(facts["project"]["build_commands"]["value"], ["npm run test"])

    def test_fact_and_digest_come_from_the_same_source_read(self) -> None:
        path = self.repo / "pyproject.toml"
        versions = {
            "first": b'[project]\nname = "first"\n',
            "second": b'[project]\nname = "second"\n',
        }
        original = Path.open
        calls = 0

        def changing_open(candidate, mode="r", *args, **kwargs):
            nonlocal calls
            if candidate != path:
                return original(candidate, mode, *args, **kwargs)
            calls += 1
            content = versions["first" if calls == 1 else "second"]
            return io.BytesIO(content) if "b" in mode else io.StringIO(content.decode())

        with mock.patch.object(Path, "open", changing_open):
            facts = self.map.build()
        name = facts["project"]["name"]
        self.assertEqual(name["source_sha256"], hashlib.sha256(versions[name["value"]]).hexdigest())

    def test_instruction_read_preserves_text_decoding_and_raw_digest(self) -> None:
        raw = b"# Agents\r\nline two\rline three\xff\n"
        (self.repo / "AGENTS.md").write_bytes(raw)
        facts = self.map.build()
        self.assertEqual(facts["instructions"]["AGENTS.md"]["head"], "# Agents\nline two\nline three\ufffd\n")
        self.assertEqual(facts["sources"]["AGENTS.md"], hashlib.sha256(raw).hexdigest())

    def test_refresh_discovers_new_extensionless_instructions(self) -> None:
        self.map.build()
        path = self.repo / ".cursorrules"
        path.write_text("Run all checks.\n", encoding="utf-8")
        facts = self.map.refresh([".cursorrules"])
        self.assertTrue(facts["instructions"][".cursorrules"]["present"])
        self.assertEqual(facts["sources"][".cursorrules"], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_refresh_removes_deleted_instruction_evidence(self) -> None:
        path = self.repo / ".windsurfrules"
        path.write_text("Run checks.\n", encoding="utf-8")
        self.map.build()
        path.unlink()
        facts = self.map.refresh([".windsurfrules"])
        self.assertFalse(facts["instructions"][".windsurfrules"]["present"])
        self.assertNotIn(".windsurfrules", facts["sources"])

    def test_refresh_tracks_package_manager_lockfile_addition_and_removal(self) -> None:
        self.map.build()
        path = self.repo / "uv.lock"
        path.write_text("version = 1\n", encoding="utf-8")
        facts = self.map.refresh(["uv.lock"])
        self.assertEqual(facts["project"]["package_manager"]["value"], "uv")
        path.unlink()
        facts = self.map.refresh(["uv.lock"])
        self.assertEqual(facts["project"]["package_manager"]["value"], "pip")

    def test_refresh_recomputes_node_manager_instead_of_retaining_old_fact(self) -> None:
        (self.repo / "pyproject.toml").unlink()
        (self.repo / "package.json").write_text('{"name": "node-project"}', encoding="utf-8")
        self.map.build()
        path = self.repo / "yarn.lock"
        path.write_text("# yarn lock\n", encoding="utf-8")
        facts = self.map.refresh(["yarn.lock"])
        self.assertEqual(facts["project"]["package_manager"]["value"], "yarn")
        path.unlink()
        facts = self.map.refresh(["yarn.lock"])
        self.assertEqual(facts["project"]["package_manager"]["value"], "npm")

    def test_load_rejects_non_object_json(self) -> None:
        for payload in ([], None, 42, "not a map", True):
            with self.subTest(payload=payload):
                self.storage.write_text(json.dumps(payload), encoding="utf-8")
                self.assertIsNone(self.map.load())

    def test_load_rejects_foreign_repository_maps(self) -> None:
        self.map.build()
        payload = json.loads(self.storage.read_text(encoding="utf-8"))
        payload["repository"] = "D:/somewhere/else"
        self.storage.write_text(json.dumps(payload), encoding="utf-8")
        self.assertIsNone(self.map.load())


if __name__ == "__main__":
    unittest.main()
