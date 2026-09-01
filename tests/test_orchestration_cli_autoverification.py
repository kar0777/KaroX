from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

from karox.cli_shortcuts import normalize_cli_arguments
from karox.orchestration_cli import (
    _verification_commands_for_args,
    register_orchestration_commands,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    register_orchestration_commands(commands)
    return parser


def test_task_first_run_accepts_automatic_verification() -> None:
    args = _parser().parse_args(
        normalize_cli_arguments(["orchestrate", "run", "fix", "the", "auth", "flow"])
    )
    assert args.command == "orchestrate"
    assert args.orchestrate_command == "run"
    assert args.objective == "fix the auth flow"
    assert args.verification_command == []


def test_task_first_start_accepts_automatic_verification() -> None:
    args = _parser().parse_args(
        normalize_cli_arguments(["orchestrate", "start", "implement", "authentication"])
    )
    assert args.orchestrate_command == "start"
    assert args.objective == "implement authentication"
    assert args.verification_command == []


def test_explicit_verification_remains_an_exact_advanced_override(tmp_path: Path) -> None:
    args = argparse.Namespace(
        repository=tmp_path,
        verification_command=['["python", "-m", "pytest", "-q"]'],
    )
    with patch("karox.orchestration_cli.discover_verification_commands") as discover:
        commands = _verification_commands_for_args(args)
    assert commands == (("python", "-m", "pytest", "-q"),)
    discover.assert_not_called()


def test_missing_verification_uses_bounded_repository_discovery(tmp_path: Path) -> None:
    expected = (("git", "diff", "--check"),)
    args = argparse.Namespace(repository=tmp_path, verification_command=[])
    with patch(
        "karox.orchestration_cli.discover_verification_commands",
        return_value=expected,
    ) as discover:
        commands = _verification_commands_for_args(args)
    assert commands == expected
    discover.assert_called_once_with(tmp_path)


def test_task_first_run_defaults_to_guarded_delegation_and_worktree_isolation() -> None:
    args = _parser().parse_args(
        normalize_cli_arguments(["orchestrate", "run", "implement", "authentication"])
    )
    assert args.delegate_workers is True
    assert args.isolate_implementers is True


def test_power_user_can_disable_delegation_and_worktree_isolation() -> None:
    args = _parser().parse_args(
        normalize_cli_arguments(
            [
                "orchestrate",
                "run",
                "implement",
                "authentication",
                "--no-delegate-workers",
                "--no-isolate-implementers",
            ]
        )
    )
    assert args.delegate_workers is False
    assert args.isolate_implementers is False
