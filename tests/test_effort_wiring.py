"""Effort is WIRED: the ladder reaches argv, the parser, and real budgets.

Covers the three seams of the /effort feature: the TUI argv builder emits
--effort-level from an explicit choice or the persisted preference, the CLI
resolver turns a level into concrete AgentLimits/ContextBudget numbers with
explicit flags always winning, and different levels provably produce
different runtime budgets (the mandate's "not a prompt string" guarantee).
"""

import argparse
from pathlib import Path

import pytest

from karox import cli as karox_cli
from karox import tui as karox_tui
from karox.effort import TaskSignals, budget_for
from karox.effort_signals import DerivedSignals


def _argv_namespace(**overrides):
    base = {
        "effort_level": None,
        "effort": None,
        "max_steps": None,
        "max_seconds": None,
        "context_utilization": None,
        "max_tool_result_chars": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


class TestEffortRuntimeResolution:
    def test_no_level_and_no_flags_keeps_legacy_defaults(self):
        steps, seconds, context, reasoning = karox_cli._effort_runtime(
            _argv_namespace()
        )
        assert steps == 24
        assert seconds == 900.0
        assert context == {}
        assert reasoning is None

    def test_level_fills_unset_budgets(self):
        steps, seconds, context, reasoning = karox_cli._effort_runtime(
            _argv_namespace(effort_level="ultra")
        )
        ultra = budget_for("ultra")
        assert steps == ultra.agent_limits.max_steps
        assert seconds == ultra.agent_limits.max_seconds
        assert context["utilization"] == ultra.context.utilization
        assert context["max_tool_result_chars"] == (
            ultra.context.max_tool_result_chars
        )
        assert context["keep_recent_groups"] == ultra.context.keep_recent_groups
        assert reasoning == ultra.reasoning_effort

    def test_explicit_flags_beat_the_ladder(self):
        steps, seconds, context, reasoning = karox_cli._effort_runtime(
            _argv_namespace(
                effort_level="ultra",
                max_steps=7,
                max_seconds=120.0,
                context_utilization=0.4,
                max_tool_result_chars=9000,
                effort="low",
            )
        )
        assert steps == 7
        assert seconds == 120.0
        assert context["utilization"] == 0.4
        assert context["max_tool_result_chars"] == 9000
        assert reasoning == "low"

    def test_different_levels_produce_different_budgets(self):
        low = karox_cli._effort_runtime(_argv_namespace(effort_level="low"))
        high = karox_cli._effort_runtime(_argv_namespace(effort_level="high"))
        assert low[0] < high[0]
        assert low[1] < high[1]
        assert low[2]["max_tool_result_chars"] < high[2]["max_tool_result_chars"]
        assert low[3] != high[3]

    def test_auto_resolves_from_derived_signals_and_explains(self, monkeypatch):
        derived = DerivedSignals(
            signals=TaskSignals(risk_area=True, dependency_breadth=12),
            evidence=("12 co-change partners in git history",),
        )
        monkeypatch.setattr(
            karox_cli, "derive_task_signals", lambda task, **kw: derived
        )
        args = _argv_namespace(effort_level="auto")
        args.task = "fix token refresh in the auth subsystem"
        args.repository = Path(".")
        steps, seconds, context, reasoning = karox_cli._effort_runtime(args)
        resolution = args.effort_auto
        assert resolution["requested"] == "auto"
        assert resolution["level"] in ("high", "extra-high", "ultra")
        chosen = budget_for(resolution["level"])
        assert steps == chosen.agent_limits.max_steps
        assert seconds == chosen.agent_limits.max_seconds
        assert reasoning == chosen.reasoning_effort
        assert resolution["reasons"]
        assert resolution["evidence"]

    def test_auto_never_overrides_explicit_flags(self, monkeypatch):
        derived = DerivedSignals(
            signals=TaskSignals(risk_area=True, dependency_breadth=12),
            evidence=(),
        )
        monkeypatch.setattr(
            karox_cli, "derive_task_signals", lambda task, **kw: derived
        )
        args = _argv_namespace(effort_level="auto", max_steps=5)
        args.task = "anything"
        steps, _, _, _ = karox_cli._effort_runtime(args)
        assert steps == 5


class TestAgentArgvEffort:
    def test_explicit_level_is_emitted(self, monkeypatch):
        monkeypatch.setattr(karox_tui, "_load_preferences", lambda: {})
        argv = karox_tui._agent_argv(
            "task", Path("."), (), "session", effort_level="high"
        )
        index = argv.index("--effort-level")
        assert argv[index + 1] == "high"

    def test_stored_preference_is_normalized_and_emitted(self, monkeypatch):
        monkeypatch.setattr(
            karox_tui, "_load_preferences", lambda: {"effort_level": "xhigh"}
        )
        argv = karox_tui._agent_argv("task", Path("."), (), "session")
        index = argv.index("--effort-level")
        assert argv[index + 1] == "extra-high"

    def test_auto_is_forwarded_for_signal_resolution(self, monkeypatch):
        monkeypatch.setattr(
            karox_tui, "_load_preferences", lambda: {"effort_level": "auto"}
        )
        argv = karox_tui._agent_argv("task", Path("."), (), "session")
        index = argv.index("--effort-level")
        assert argv[index + 1] == "auto"

    def test_garbage_preference_emits_nothing(self, monkeypatch):
        monkeypatch.setattr(
            karox_tui, "_load_preferences", lambda: {"effort_level": "turbo"}
        )
        argv = karox_tui._agent_argv("task", Path("."), (), "session")
        assert "--effort-level" not in argv


class TestEffortPreferencePersistence:
    def test_load_normalizes_and_falls_back(self, monkeypatch):
        monkeypatch.setattr(
            karox_tui, "_load_preferences", lambda: {"effort_level": "MAX"}
        )
        assert karox_tui._load_effort_level() == "ultra"
        monkeypatch.setattr(
            karox_tui, "_load_preferences", lambda: {"effort_level": "broken"}
        )
        assert karox_tui._load_effort_level() == "auto"
        monkeypatch.setattr(karox_tui, "_load_preferences", lambda: {})
        assert karox_tui._load_effort_level() == "auto"

    def test_save_rejects_unknown_levels(self, monkeypatch):
        saved = {}
        monkeypatch.setattr(
            karox_tui, "_save_preferences", lambda **kw: saved.update(kw)
        )
        karox_tui._save_effort_level("extra_high")
        assert saved == {"effort_level": "extra-high"}
        with pytest.raises(ValueError):
            karox_tui._save_effort_level("warp9")


class TestEffortCommandSurface:
    def test_effort_is_a_visible_first_class_command(self):
        assert "/effort" in karox_tui.VISIBLE_COMMANDS
        assert "/effort" in karox_tui.SLASH_COMMANDS
        assert "/effort" in karox_tui._COMMANDS_RU
        for language in ("en", "ru"):
            assert "/effort" in karox_tui._commands(language)

    def test_run_parser_accepts_the_ladder_and_rejects_junk(self):
        parser = karox_cli._parser()
        args = parser.parse_args(
            [
                "agent", "run",
                "--repository", ".",
                "--task", "t",
                "--verification-command", '["python","-m","pytest","-q"]',
                "--effort-level", "extra-high",
            ]
        )
        assert args.effort_level == "extra-high"
        assert args.max_steps is None
        assert args.max_seconds is None
        with pytest.raises(SystemExit):
            parser.parse_args(
                [
                    "agent", "run",
                    "--repository", ".",
                    "--task", "t",
                    "--verification-command", '["python","-m","pytest","-q"]',
                    "--effort-level", "turbo",
                ]
            )
