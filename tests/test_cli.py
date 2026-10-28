"""Contracts for the release command surface."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path

import pytest

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
    assert "config_dir:" in text
    assert "runtime_dir:" in text
    assert "session_dir:" in text
    assert os.path.normcase(str(tmp_path)) in os.path.normcase(text)


def test_paths_json_uses_stable_keys(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KAROX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KAROX_RUNTIME_DIR", str(tmp_path / "runtime"))
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        assert cli.main(["paths", "--json"]) == 0
    payload = json.loads(output.getvalue())
    assert set(payload) == {"config_dir", "runtime_dir", "session_dir"}


def test_short_daily_commands_and_bare_groups_list(monkeypatch) -> None:
    assert cli._normalize_argv(["models"]) == ["model", "list"]
    assert cli._normalize_argv(["providers", "--json"]) == ["providers", "--json"]
    assert cli._normalize_argv(["model"]) == ["model", "list"]
    assert cli._normalize_argv(["session", "--json"]) == ["session", "list", "--json"]
    assert cli._normalize_argv(["model", "--help"]) == ["model", "--help"]


def test_local_provider_setup_needs_no_key_and_doctor_is_healthy(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("KAROX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KAROX_RUNTIME_DIR", str(tmp_path / "runtime"))
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        assert (
            cli.main(
                [
                    "provider",
                    "setup",
                    "local-openai",
                    "--model",
                    "llama3",
                    "--no-test",
                    "--json",
                ]
            )
            == 0
        )
    setup = json.loads(output.getvalue())
    assert setup["provider"]["privacy_class"] == "local"
    assert setup["provider"]["credential_ref"] is None

    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        assert cli.main(["doctor", "--json"]) == 0
    doctor = json.loads(output.getvalue())
    assert doctor["status"] == "ok"
    assert doctor["checks"]["registry"]["selected"] == "local-openai/llama3"


def test_remote_provider_setup_without_credential_is_refused(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("KAROX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KAROX_RUNTIME_DIR", str(tmp_path / "runtime"))
    with contextlib.redirect_stderr(io.StringIO()):
        code = cli.main(
            ["provider", "setup", "openai", "--model", "gpt-5", "--no-test"]
        )
    assert code == 2


def test_agent_refuses_a_drive_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KAROX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KAROX_RUNTIME_DIR", str(tmp_path / "runtime"))
    drive_root = Path.cwd().anchor
    with contextlib.redirect_stderr(io.StringIO()):
        code = cli.main(
            [
                "agent",
                "run",
                "--repository",
                drive_root,
                "--task",
                "change something",
            ]
        )
    assert code == 2


@pytest.mark.skipif(os.name != "nt", reason="Windows system folders")
def test_agent_refuses_a_windows_system_folder(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KAROX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KAROX_RUNTIME_DIR", str(tmp_path / "runtime"))
    system = Path(os.environ.get("WINDIR", r"C:\Windows"))
    if not system.is_dir():
        pytest.skip("no Windows directory")
    with contextlib.redirect_stderr(io.StringIO()):
        code = cli.main(
            [
                "agent",
                "run",
                "--repository",
                str(system),
                "--task",
                "change something",
            ]
        )
    assert code == 2


def test_project_instructions_refuse_a_symlink_outside_the_repository(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "secret.txt"
    outside.write_text("do not read me\n", encoding="utf-8")
    repository = tmp_path / "repo"
    repository.mkdir()
    link = repository / "AGENTS.md"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")
    suffix, metadata = cli._project_instructions(repository)
    assert "do not read me" not in suffix
    assert {"name": "AGENTS.md", "reason": "symlink"} in metadata["skipped"]

def _model(provider_id: str, model_id: str, vision: str):
    from karox.registry import ModelRecord

    return ModelRecord(provider_id=provider_id, model_id=model_id, vision=vision)


class _VisionRegistry:
    def __init__(self, models, selected) -> None:
        self._models = {(item.provider_id, item.model_id): item for item in models}
        self._selected = selected

    def model(self, provider_id: str, model_or_alias: str):
        key = (provider_id, model_or_alias)
        if key in self._models:
            return self._models[key]
        raise KeyError(key)

    def selected_model(self):
        return self._selected


def _vision_args(**overrides):
    from types import SimpleNamespace

    values = dict(model=None, base_url=None, api_key_env=None, route=())
    values.update(overrides)
    return SimpleNamespace(**values)


def test_route_model_with_vision_enables_the_checker(monkeypatch) -> None:
    registry = _VisionRegistry(
        [_model("wandb", "vision-model", "true")],
        _model("stepfun-plan", "step-5-preview", "false"),
    )
    monkeypatch.setattr(cli, "_registry", lambda: registry)

    args = _vision_args(route=("wandb/vision-model",))

    assert cli._run_vision_capability(args) is True


def test_route_model_without_vision_wins_over_a_vision_capable_selection(
    monkeypatch,
) -> None:
    registry = _VisionRegistry(
        [_model("wandb", "text-model", "false")],
        _model("stepfun-plan", "step-5-preview", "true"),
    )
    monkeypatch.setattr(cli, "_registry", lambda: registry)

    args = _vision_args(route=("wandb/text-model",))

    assert cli._run_vision_capability(args) is False


def test_unknown_route_capability_inherits_the_selected_model(monkeypatch) -> None:
    registry = _VisionRegistry(
        [_model("wandb", "unclassified", "unknown")],
        _model("stepfun-plan", "step-5-preview", "true"),
    )
    monkeypatch.setattr(cli, "_registry", lambda: registry)

    args = _vision_args(route=("wandb/unclassified",))

    assert cli._run_vision_capability(args) is True


def test_direct_provider_mode_keeps_the_checker_off(monkeypatch) -> None:
    registry = _VisionRegistry(
        [],
        _model("stepfun-plan", "step-5-preview", "true"),
    )
    monkeypatch.setattr(cli, "_registry", lambda: registry)

    args = _vision_args(model="anything", base_url="https://example.invalid/v1")

    assert cli._run_vision_capability(args) is False
