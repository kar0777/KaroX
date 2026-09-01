"""Modes are WIRED: /mode reaches argv, the parser, grants, prompt, artifacts.

Covers the enforcement seams of Build/Plan/Ideate: the TUI argv builder emits
--mode from an explicit choice or the persisted preference (Build emits
nothing, keeping a legacy argv bit for bit identical), the CLI resolver turns
--mode into enforceable rules, Plan and Ideate provably lose the write
capabilities, every mode\'s system-prompt delta differs, the report carries
the stance, and a Plan/Ideate answer becomes a durable artifact with the
expected sections.
"""

import argparse
from pathlib import Path

import pytest

from karox import cli as karox_cli
from karox import tui as karox_tui
from karox.agent import AgentReport
from karox.agent_modes import mode_policy, mode_prompt_delta
from karox.mode_artifacts import (
    CONCEPT_SECTIONS,
    PLAN_SECTIONS,
    artifact_skeleton,
    save_mode_artifact,
)
from karox.models import Capability


def _argv_namespace(**overrides):
    base = {"mode": None}
    base.update(overrides)
    return argparse.Namespace(**base)


def _make_report(**overrides):
    base = dict(
        session_id="s",
        status="verified",
        phase="done",
        verified=True,
        reason="answer",
        steps=1,
        changed_files=(),
        checks=(),
        git_state={},
        evidence=(),
        usage={},
    )
    base.update(overrides)
    return AgentReport(**base)


class TestModeRulesResolution:
    def test_no_flag_is_the_legacy_default(self):
        assert karox_cli._mode_rules(_argv_namespace()) is None

    def test_each_mode_resolves_to_its_policy(self):
        for mode in ("build", "plan", "ideate"):
            rules = karox_cli._mode_rules(_argv_namespace(mode=mode))
            assert rules is not None
            assert rules.mode == mode

    def test_prompt_deltas_differ_per_mode(self):
        deltas = {mode_prompt_delta(m) for m in ("build", "plan", "ideate")}
        assert len(deltas) == 3


class TestModeGrantEnforcement:
    @staticmethod
    def _grants():
        return {
            Capability.REPO_READ,
            Capability.REPO_WRITE,
            Capability.PROCESS_RUN,
            Capability.CHECKS_RUN,
            Capability.GIT_READ,
            Capability.MCP_CALL,
            Capability.GIT_COMMIT,
        }

    def test_plan_and_ideate_lose_write_and_commit(self):
        for mode in ("plan", "ideate"):
            granted = karox_cli._mode_grants(self._grants(), mode_policy(mode))
            assert Capability.REPO_WRITE not in granted
            assert Capability.GIT_COMMIT not in granted
            # Read-and-verify stays: the stance is read-only toward
            # production code, not toward evidence.
            assert Capability.REPO_READ in granted
            assert Capability.CHECKS_RUN in granted
            assert Capability.MCP_CALL in granted

    def test_build_and_legacy_keep_everything(self):
        full = self._grants()
        assert karox_cli._mode_grants(set(full), mode_policy("build")) == full
        assert karox_cli._mode_grants(set(full), None) == full


class TestAgentArgvMode:
    def test_explicit_plan_is_emitted(self, monkeypatch):
        monkeypatch.setattr(karox_tui, "_load_preferences", lambda: {})
        argv = karox_tui._agent_argv(
            "task", Path("."), (), "session", agent_mode="plan"
        )
        index = argv.index("--mode")
        assert argv[index + 1] == "plan"

    def test_stored_preference_is_normalized_and_emitted(self, monkeypatch):
        monkeypatch.setattr(
            karox_tui, "_load_preferences", lambda: {"agent_mode": "idea"}
        )
        argv = karox_tui._agent_argv("task", Path("."), (), "session")
        index = argv.index("--mode")
        assert argv[index + 1] == "ideate"

    def test_build_emits_nothing(self, monkeypatch):
        monkeypatch.setattr(
            karox_tui, "_load_preferences", lambda: {"agent_mode": "build"}
        )
        assert "--mode" not in karox_tui._agent_argv(
            "task", Path("."), (), "session"
        )
        assert "--mode" not in karox_tui._agent_argv(
            "task", Path("."), (), "session", agent_mode="build"
        )

    def test_garbage_preference_emits_nothing(self, monkeypatch):
        monkeypatch.setattr(
            karox_tui, "_load_preferences", lambda: {"agent_mode": "vibe"}
        )
        assert "--mode" not in karox_tui._agent_argv(
            "task", Path("."), (), "session"
        )


class TestModePreferencePersistence:
    def test_load_normalizes_and_falls_back(self, monkeypatch):
        monkeypatch.setattr(
            karox_tui, "_load_preferences", lambda: {"agent_mode": "P"}
        )
        assert karox_tui._load_agent_mode() == "plan"
        monkeypatch.setattr(
            karox_tui, "_load_preferences", lambda: {"agent_mode": "vibe"}
        )
        assert karox_tui._load_agent_mode() == "build"
        monkeypatch.setattr(karox_tui, "_load_preferences", lambda: {})
        assert karox_tui._load_agent_mode() == "build"

    def test_save_rejects_unknown_modes(self, monkeypatch):
        saved = {}
        monkeypatch.setattr(
            karox_tui, "_save_preferences", lambda **kw: saved.update(kw)
        )
        karox_tui._save_agent_mode("idea")
        assert saved == {"agent_mode": "ideate"}
        with pytest.raises(ValueError):
            karox_tui._save_agent_mode("vibe")


class TestModeCommandSurface:
    def test_mode_is_a_first_class_visible_command(self):
        assert "/mode" in karox_tui.VISIBLE_COMMANDS
        assert "/mode" in karox_tui.SLASH_COMMANDS
        assert "/mode" in karox_tui._COMMANDS_RU
        for language in ("en", "ru"):
            assert "/mode" in karox_tui._commands(language)

    def test_run_parser_accepts_modes_and_rejects_junk(self):
        parser = karox_cli._parser()
        args = parser.parse_args(
            [
                "agent", "run",
                "--repository", ".",
                "--task", "t",
                "--verification-command", '["python","-m","pytest","-q"]',
                "--mode", "plan",
            ]
        )
        assert args.mode == "plan"
        with pytest.raises(SystemExit):
            parser.parse_args(
                [
                    "agent", "run",
                    "--repository", ".",
                    "--task", "t",
                    "--verification-command", '["python","-m","pytest","-q"]',
                    "--mode", "vibe",
                ]
            )


class TestHeaderShowsMode:
    @staticmethod
    def _line(**overrides):
        facts = {
            "repository": "proj",
            "model": "prov/model",
            "activity": "",
            "width": 120,
        }
        facts.update(overrides)
        return karox_tui._header_line(**facts)

    def test_plan_and_ideate_are_visible(self):
        assert "Plan" in self._line(mode="plan")
        assert "Ideate" in self._line(mode="ideate")

    def test_build_and_legacy_render_no_mode_chip(self):
        assert "Build" not in self._line(mode="build")
        assert "Build" not in self._line()


class TestModeArtifacts:
    def test_skeletons_carry_every_required_section(self):
        plan = artifact_skeleton("plan")
        for section in PLAN_SECTIONS:
            assert f"## {section}" in plan
        concept = artifact_skeleton("concept")
        for section in CONCEPT_SECTIONS:
            assert f"## {section}" in concept

    def test_save_writes_a_durable_document_with_honest_metadata(
        self, tmp_path
    ):
        content = "\n".join(f"## {name}\ntext" for name in PLAN_SECTIONS)
        saved = save_mode_artifact(
            session_id="sess-1",
            kind="plan",
            task="wire modes",
            content=content,
            root=tmp_path,
        )
        path = Path(saved["path"])
        assert path.exists()
        document = path.read_text(encoding="utf-8")
        assert "wire modes" in document
        assert saved["missing_sections"] == []
        assert saved["kind"] == "plan"

    def test_missing_sections_are_reported_not_papered_over(self, tmp_path):
        saved = save_mode_artifact(
            session_id="sess-2",
            kind="concept",
            task="t",
            content="## Title\nonly a title",
            root=tmp_path,
        )
        assert "Problem" in saved["missing_sections"]

    def test_empty_content_and_unknown_kind_are_refused(self, tmp_path):
        with pytest.raises(ValueError):
            save_mode_artifact(
                session_id="s", kind="plan", task="t", content="  ",
                root=tmp_path,
            )
        with pytest.raises(ValueError):
            save_mode_artifact(
                session_id="s", kind="memo", task="t", content="x",
                root=tmp_path,
            )


class TestReportCarriesMode:
    def test_mode_defaults_to_none_and_round_trips(self):
        assert _make_report().to_dict()["mode"] is None
        assert _make_report(mode="plan").to_dict()["mode"] == "plan"


class TestAttachModeArtifact:
    def test_plan_answer_becomes_a_durable_artifact(
        self, tmp_path, monkeypatch
    ):
        from karox import mode_artifacts as mode_artifacts_module

        monkeypatch.setattr(
            karox_cli,
            "save_mode_artifact",
            lambda **kw: mode_artifacts_module.save_mode_artifact(
                root=tmp_path, **kw
            ),
        )
        report = _make_report(
            provider_message="## Goal\nship modes", mode="plan"
        )
        karox_cli._attach_mode_artifact(
            report, mode_policy("plan"), session_id="sess", task="t"
        )
        recorded = report.project_context["mode_artifact"]
        assert recorded["saved"] is True
        assert Path(recorded["path"]).exists()

    def test_empty_answer_leaves_no_artifact(self):
        report = _make_report(provider_message=None, mode="plan")
        karox_cli._attach_mode_artifact(
            report, mode_policy("plan"), session_id="sess", task="t"
        )
        assert "mode_artifact" not in report.project_context

    def test_build_never_writes_an_artifact(self):
        report = _make_report(provider_message="done", mode="build")
        karox_cli._attach_mode_artifact(
            report, mode_policy("build"), session_id="sess", task="t"
        )
        assert "mode_artifact" not in report.project_context
