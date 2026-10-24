"""Contracts for the release command surface."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path

from karox import cli
from karox.agent import AgentEvent, AgentEventKind
from karox.models import AccessProfile, Capability


def test_parser_accepts_the_documented_agent_flags() -> None:
    args = cli._parser().parse_args(
        [
            "agent",
            "run",
            "--repository",
            ".",
            "--task",
            "fix it",
            "--session-id",
            "s1",
            "--continue-task",
            "--verification-command",
            '["python", "-m", "pytest", "-q"]',
            "--effort-level",
            "high",
            "--mode",
            "plan",
            "--stream-json",
        ]
    )
    assert args.command == "agent"
    assert args.continue_task is True
    assert args.effort_level == "high"
    assert args.mode == "plan"
    assert args.stream_json is True


def test_verification_argv_accepts_json_and_shell_forms() -> None:
    assert cli._verification_argv('["python", "-m", "pytest", "-q"]') == (
        "python",
        "-m",
        "pytest",
        "-q",
    )
    assert cli._verification_argv("git diff --check") == ("git", "diff", "--check")


def test_plan_mode_loses_write_and_commit_capabilities() -> None:
    build = cli._agent_grants(AccessProfile.ELEVATED, "build")
    plan = cli._agent_grants(AccessProfile.ELEVATED, "plan")
    assert Capability.REPO_WRITE in build
    assert Capability.GIT_COMMIT in build
    assert Capability.REPO_WRITE not in plan
    assert Capability.GIT_COMMIT not in plan
    assert Capability.REPO_READ in plan


def test_project_instructions_are_bounded_and_attributed(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("Run the focused tests first.\n", encoding="utf-8")
    suffix, metadata = cli._project_instructions(tmp_path)
    assert "Run the focused tests first." in suffix
    assert metadata["sources"] == ["AGENTS.md"]
    assert metadata["instruction_bytes"] > 0


def test_stream_json_writes_one_document_per_event(monkeypatch) -> None:
    event = AgentEvent(kind=AgentEventKind.TEXT_DELTA, step=1, text_delta="hi")
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        cli._stream_json()(event)
    payload = json.loads(buffer.getvalue())
    assert payload["type"] == "event"
    assert payload["kind"] == "text_delta"
    assert payload["text_delta"] == "hi"


def test_session_create_and_list_roundtrip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KAROX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KAROX_RUNTIME_DIR", str(tmp_path / "runtime"))
    repository = tmp_path / "repo"
    repository.mkdir()
    assert (
        cli.main(
            [
                "session",
                "create",
                "--repository",
                str(repository),
                "--task",
                "inspect the repository",
                "--id",
                "s1",
                "--json",
            ]
        )
        == 0
    )
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        assert cli.main(["session", "list", "--json"]) == 0
    records = json.loads(output.getvalue())
    assert records[0]["session_id"] == "s1"
    assert records[0]["repository"] == str(repository.resolve())


def test_paths_command_reports_the_three_roots(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KAROX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KAROX_RUNTIME_DIR", str(tmp_path / "runtime"))
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        assert cli.main(["paths"]) == 0
    text = output.getvalue()
    assert "config:" in text
    assert "runtime:" in text
    assert "sessions:" in text
    assert os.path.normcase(str(tmp_path)) in os.path.normcase(text)