"""Project fact map: deterministic onboarding facts with incremental refresh."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

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

    def test_load_rejects_foreign_repository_maps(self) -> None:
        self.map.build()
        payload = json.loads(self.storage.read_text(encoding="utf-8"))
        payload["repository"] = "D:/somewhere/else"
        self.storage.write_text(json.dumps(payload), encoding="utf-8")
        self.assertIsNone(self.map.load())


if __name__ == "__main__":
    unittest.main()
