"""Product adapter for the vendored karo-core engine.

The engine ports themselves are covered by test_engine_port_*.py. These tests
cover the adapter: provider mapping, the policy-aware command runner, the
trajectory, and the statuses the CLI reports.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from karox.core_engine import CoreEngine, CoreEngineConfig


class FakeModel:
    """Replays scripted messages; records the history it was asked to send."""

    def __init__(self, messages: list[dict]):
        self._messages = messages
        self.seen: list[list[dict]] = []

    def query(self, messages: list[dict], **kwargs) -> dict:
        self.seen.append([dict(m) for m in messages])
        index = min(len(self.seen) - 1, len(self._messages) - 1)
        return self._messages[index]

    def get_template_vars(self, **kwargs):
        return {}

    def format_message(self, **kwargs):
        return {"role": kwargs["role"], "content": kwargs["content"]}

    def format_observation_messages(self, message, outputs, template_vars=None):
        return [{"role": "user", "content": str(o.get("output", ""))} for o in outputs]

    def serialize(self):
        return {}


def action(command: str) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "extra": {"actions": [{"command": command}], "cost": 0.0},
    }


def prose(text: str) -> dict:
    return {"role": "assistant", "content": text, "extra": {"actions": [], "cost": 0.0}}


class ProseFormatErrorModel:
    """Returns a no-tool-call response the way the real model layer does.

    The litellm layer raises FormatError for a response without a tool call, so
    a fake model that returns the message directly would bypass port 3 entirely.
    """

    def __init__(self, content: str):
        self.content = content
        self.calls = 0

    def query(self, messages: list[dict], **kwargs) -> dict:
        from karox.engine.exceptions import FormatError

        self.calls += 1
        raise FormatError(
            {
                "role": "user",
                "content": "Format error: no tool calls.",
                "extra": {
                    "interrupt_type": "FormatError",
                    "response": {
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {"role": "assistant", "content": self.content},
                            }
                        ]
                    },
                },
            }
        )

    def get_template_vars(self, **kwargs):
        return {}

    def format_message(self, **kwargs):
        return {"role": kwargs["role"], "content": kwargs["content"]}

    def format_observation_messages(self, message, outputs, template_vars=None):
        return [{"role": "user", "content": str(o.get("output", ""))} for o in outputs]

    def serialize(self):
        return {}


def config(tmp_path: Path, *, model_factory, command_runner=None, **kwargs) -> CoreEngineConfig:
    values = dict(
        repository=tmp_path,
        task="do the thing",
        model="deepseek-ai/DeepSeek-V4.1-Flash",
        max_steps=10,
        max_seconds=60,
        trajectory_path=tmp_path / "trajectory.json",
        model_factory=model_factory,
        command_runner=command_runner,
    )
    values.update(kwargs)
    return CoreEngineConfig(**values)


def test_openai_route_gets_the_provider_prefix():
    routed = CoreEngineConfig(
        repository=Path("."), task="t", model="deepseek-ai/DeepSeek-V4.1-Flash",
        base_url="https://api.inference.wandb.ai/v1",
    )
    direct = CoreEngineConfig(repository=Path("."), task="t", model="deepseek/deepseek-chat")
    assert routed.litellm_model_name == "openai/deepseek-ai/DeepSeek-V4.1-Flash"
    assert direct.litellm_model_name == "deepseek/deepseek-chat"


def test_command_runner_receives_the_command_and_cwd(tmp_path):
    calls: list[tuple[str, Path, int]] = []

    def runner(command: str, cwd: Path, timeout: int) -> dict:
        calls.append((command, cwd, timeout))
        return {"output": "ran\n", "returncode": 0}

    # The submit marker must be the first output line to end the run.
    model = FakeModel(
        [
            action('cd /repo && touch made.txt'),
            action('echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && echo done'),
        ]
    )
    report = CoreEngine(config(tmp_path, model_factory=lambda c: model, command_runner=runner)).run()

    assert report.status == "submitted", report.status
    assert calls[0][0] == 'cd /repo && touch made.txt'
    assert calls[0][1] == tmp_path
    assert calls[0][2] == 300


def test_large_runner_output_is_clipped(tmp_path):
    def runner(command: str, cwd: Path, timeout: int) -> dict:
        if "COMPLETE_TASK" in command:
            return {"output": "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n", "returncode": 0}
        return {"output": "x" * 120_000, "returncode": 0}

    model = FakeModel(
        [action("big"), action("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")]
    )
    engine = CoreEngine(
        config(tmp_path, model_factory=lambda c: model, command_runner=runner, output_char_limit=10_000)
    )
    engine.run()
    history = "\n".join(message["content"] for message in model.seen[-1])
    assert "karo-core: dropped" in history
    assert len(history) < 120_000


def test_prose_answer_submits_through_the_adapter(tmp_path):
    """Port 3 must stay active in the product path."""
    model = ProseFormatErrorModel("The work is complete.")
    report = CoreEngine(config(tmp_path, model_factory=lambda c: model)).run()

    assert report.status == "submitted"
    assert report.submission == "The work is complete."
    assert report.steps == 3
    assert model.calls == 3


def test_identical_steps_submit_through_the_adapter(tmp_path):
    """Port 4 must stay active: four identical action+output pairs end the run."""

    def runner(command: str, cwd: Path, timeout: int) -> dict:
        return {"output": "DONE\n", "returncode": 0}

    model = FakeModel([action('echo "DONE"')])
    report = CoreEngine(
        config(tmp_path, model_factory=lambda c: model, command_runner=runner)
    ).run()

    assert report.status == "submitted"
    assert report.steps == 4


def test_step_limit_is_reported(tmp_path):
    def runner(command: str, cwd: Path, timeout: int) -> dict:
        return {"output": f"step {command}\n", "returncode": 0}

    model = FakeModel([action("echo a"), action("echo b")])
    report = CoreEngine(
        config(tmp_path, model_factory=lambda c: model, command_runner=runner, max_steps=2)
    ).run()

    assert report.status == "limits_exceeded"
    assert report.steps == 2


def test_trajectory_is_written(tmp_path):
    def runner(command: str, cwd: Path, timeout: int) -> dict:
        return {"output": "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n", "returncode": 0}

    model = FakeModel([action("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")])
    report = CoreEngine(
        config(tmp_path, model_factory=lambda c: model, command_runner=runner)
    ).run()

    path = Path(report.trajectory_path)
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["info"]["model_stats"]["api_calls"] == 1


def test_repository_must_exist(tmp_path):
    missing = tmp_path / "missing"
    with pytest.raises(ValueError):
        CoreEngine(
            CoreEngineConfig(repository=missing, task="t", model="m", model_factory=lambda c: FakeModel([]))
        )