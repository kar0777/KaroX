from __future__ import annotations

import unittest

from karox.cli_shortcuts import normalize_cli_arguments


class CliShortcutTests(unittest.TestCase):
    def test_plural_nouns_default_to_list_without_breaking_flags(self) -> None:
        self.assertEqual(normalize_cli_arguments(["models"]), ["model", "list"])
        self.assertEqual(
            normalize_cli_arguments(["agents", "--json"]),
            ["intelligence", "list", "--json"],
        )
        self.assertEqual(
            normalize_cli_arguments(["providers", "list", "--json"]),
            ["provider", "list", "--json"],
        )

    def test_models_support_human_verbs(self) -> None:
        self.assertEqual(
            normalize_cli_arguments(["models", "use", "openai", "gpt-x"]),
            ["model", "select", "openai", "gpt-x"],
        )
        self.assertEqual(
            normalize_cli_arguments(["models", "auto", "--provider", "openai"]),
            ["model", "repair-selection", "--provider", "openai"],
        )
        self.assertEqual(
            normalize_cli_arguments(["models", "refresh", "openrouter", "--json"]),
            ["model", "discover", "--provider", "openrouter", "--json"],
        )

    def test_agents_apply_and_refresh_hide_internal_discovery_spelling(self) -> None:
        self.assertEqual(
            normalize_cli_arguments(["agents", "apply", "--json"]),
            ["intelligence", "discover-agents", "--apply", "--json"],
        )
        self.assertEqual(
            normalize_cli_arguments(["agents", "refresh"]),
            ["intelligence", "discover-agents"],
        )

    def test_mission_run_id_defaults_to_show(self) -> None:
        self.assertEqual(
            normalize_cli_arguments(["mission", "run-123", "--json"]),
            ["mission-control", "show", "run-123", "--json"],
        )
        self.assertEqual(
            normalize_cli_arguments(["mission", "command", "run-123", "pause"]),
            ["mission-control", "command", "run-123", "pause"],
        )

    def test_orchestrate_is_task_first_and_executes_by_default(self) -> None:
        self.assertEqual(
            normalize_cli_arguments(["orchestrate", "fix", "the", "auth", "flow"]),
            ["orchestrate", "run", "--objective", "fix the auth flow"],
        )
        self.assertEqual(
            normalize_cli_arguments(
                ["orchestrate", "run", "fix", "auth", "--preset", "balanced"]
            ),
            [
                "orchestrate",
                "run",
                "--objective",
                "fix auth",
                "--preset",
                "balanced",
            ],
        )

    def test_explicit_power_user_grammar_is_unchanged(self) -> None:
        argv = [
            "orchestrate",
            "plan",
            "--objective",
            "precise task",
            "--recipe",
            "security",
        ]
        self.assertEqual(normalize_cli_arguments(argv), argv)
        model = ["model", "inspect", "openai", "gpt-x", "--json"]
        self.assertEqual(normalize_cli_arguments(model), model)


if __name__ == "__main__":
    unittest.main()
