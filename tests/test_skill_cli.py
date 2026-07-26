from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _support import SRC  # noqa: F401 - inserts src on sys.path
from karox.cli import main
from karox.sessions import SessionStore


class SkillCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        self.repository.mkdir()
        self.environment = patch.dict(
            os.environ,
            {
                "KAROX_VNEXT_CONFIG_DIR": str(self.root / "config"),
                "KAROX_VNEXT_RUNTIME_DIR": str(self.root / "runtime"),
            },
        )
        self.environment.start()
        self.sessions = SessionStore(
            self.root / "runtime" / "vnext" / "sessions"
        )

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def invoke(self, *arguments: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(arguments)
        return code, stdout.getvalue(), stderr.getvalue()

    def write_skill(
        self,
        name: str = "bounded",
        *,
        version: str = "1.0.0",
        description: str = "Bounded test Skill",
    ) -> Path:
        directory = self.repository / ".karox" / "skills" / name
        directory.mkdir(parents=True, exist_ok=True)
        manifest = (
            "---\n"
            f"name: {name}\n"
            f"description: {description}\n"
            f"version: {version}\n"
            "permissions: [repo.write]\n"
            "files: [reference.txt]\n"
            "---\n"
            "INSTRUCTION_TOKEN\n"
        )
        (directory / "SKILL.md").write_bytes(manifest.encode("utf-8"))
        (directory / "reference.txt").write_bytes(b"REFERENCE_TOKEN\n")
        return directory

    def create_session(self, session_id: str = "skill-session") -> None:
        code, _, stderr = self.invoke(
            "session",
            "create",
            "--repository",
            str(self.repository),
            "--task",
            "test Skill selection",
            "--id",
            session_id,
            "--json",
        )
        self.assertEqual(code, 0, stderr)

    def test_list_and_show_are_metadata_only_but_load_returns_content(self) -> None:
        self.write_skill()

        code, stdout, stderr = self.invoke(
            "skill",
            "list",
            "--repository",
            str(self.repository),
            "--json",
        )
        self.assertEqual(code, 0, stderr)
        listed = json.loads(stdout)
        self.assertEqual([item["name"] for item in listed["skills"]], ["bounded"])
        self.assertNotIn("INSTRUCTION_TOKEN", stdout)
        self.assertNotIn("REFERENCE_TOKEN", stdout)

        code, stdout, stderr = self.invoke(
            "skill",
            "show",
            "bounded",
            "--repository",
            str(self.repository),
            "--json",
        )
        self.assertEqual(code, 0, stderr)
        shown = json.loads(stdout)
        self.assertEqual(shown["skill"]["identity"], "bounded@1.0.0")
        self.assertNotIn("INSTRUCTION_TOKEN", stdout)
        self.assertNotIn("REFERENCE_TOKEN", stdout)

        code, stdout, stderr = self.invoke(
            "skill",
            "load",
            "bounded",
            "--repository",
            str(self.repository),
            "--json",
        )
        self.assertEqual(code, 0, stderr)
        loaded = json.loads(stdout)["skill"]
        self.assertEqual(loaded["instructions"], "INSTRUCTION_TOKEN\n")
        self.assertEqual(
            loaded["references"], {"reference.txt": "REFERENCE_TOKEN\n"}
        )

    def test_select_and_deselect_persist_without_noop_revision_churn(self) -> None:
        self.write_skill()
        self.create_session()

        code, stdout, stderr = self.invoke(
            "skill",
            "select",
            "bounded",
            "--session-id",
            "skill-session",
            "--repository",
            str(self.repository),
            "--permission",
            "repo.write=allow",
            "--json",
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(
            json.loads(stdout)["selection"]["permissions"]["repo.write"],
            "allow",
        )
        selected_revision = self.sessions.load("skill-session").revision

        code, stdout, stderr = self.invoke(
            "skill",
            "deselect",
            "bounded",
            "--session-id",
            "skill-session",
            "--repository",
            str(self.repository),
            "--json",
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["status"], "deselected")
        deselected = self.sessions.load("skill-session")
        self.assertGreater(deselected.revision, selected_revision)
        self.assertEqual(deselected.skills, [])

        code, stdout, stderr = self.invoke(
            "skill",
            "deselect",
            "bounded",
            "--session-id",
            "skill-session",
            "--repository",
            str(self.repository),
            "--json",
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["status"], "not_selected")
        self.assertEqual(
            self.sessions.load("skill-session").revision, deselected.revision
        )

    def test_changed_metadata_resets_previous_allow_to_ask(self) -> None:
        self.write_skill()
        self.create_session()
        code, _, stderr = self.invoke(
            "skill",
            "select",
            "bounded",
            "--session-id",
            "skill-session",
            "--repository",
            str(self.repository),
            "--permission",
            "repo.write=allow",
            "--json",
        )
        self.assertEqual(code, 0, stderr)

        self.write_skill(version="1.0.1", description="Changed metadata")
        code, stdout, stderr = self.invoke(
            "skill",
            "select",
            "bounded",
            "--session-id",
            "skill-session",
            "--repository",
            str(self.repository),
            "--json",
        )

        self.assertEqual(code, 0, stderr)
        selection = json.loads(stdout)["selection"]
        self.assertEqual(selection["identity"], "bounded@1.0.1")
        self.assertEqual(selection["permissions"]["repo.write"], "ask")
        persisted = self.sessions.load("skill-session").skills[0]
        self.assertEqual(persisted["permissions"]["repo.write"], "ask")

    def test_invalid_agent_skill_creates_no_session(self) -> None:
        directory = self.repository / ".karox" / "skills" / "invalid"
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            "---\n"
            "name: invalid\n"
            "description: Invalid Skill\n"
            "version: 1.0.0\n"
            "required_tools: [unknown_tool]\n"
            "---\n"
            "Never loaded.\n",
            encoding="utf-8",
        )

        code, _, stderr = self.invoke(
            "agent",
            "run",
            "--repository",
            str(self.repository),
            "--task",
            "must fail before state creation",
            "--model",
            "test-model",
            "--base-url",
            "https://provider.example/v1",
            "--session-id",
            "invalid-skill",
            "--verification-command",
            '["python", "-c", "print(\"ok\")"]',
            "--skill",
            "invalid",
            "--json",
        )

        self.assertEqual(code, 2)
        self.assertIn("invalid", stderr)
        self.assertFalse(self.sessions.state_path("invalid-skill").exists())


if __name__ == "__main__":
    unittest.main()
