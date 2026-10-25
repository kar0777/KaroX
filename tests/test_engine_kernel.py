"""The engine-default contract: ``karox agent run`` executes src/karox/engine.

These tests pin the decisive integration property: the default native agent
path runs the vendored karo-core loop (engine identity in the report, engine
steps and events on the NDJSON wire), every engine shell action passes the
capability policy, the string-level command invariants, repository
confinement and the consequence engine before execution, and verification
stays honest -- the harness runs the user-approved checks itself, so a run
is verified only by a real check exit code, never by the model's own claim.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
from typing import Any, List

import pytest

from karox import cli
from karox.action_policy import ActionDecisionEngine
from karox.agent import AgentEvent, AgentEventKind, AgentReport
from karox.engine_kernel import (
    EngineUsage,
    GuardedEngineEnvironment,
    ProviderEngineModel,
    _confinement_escape,
    _writes_anything,
)
from karox.models import AccessProfile, Capability, Origin, OriginKind
from karox.policy import CapabilityPolicy
from karox.providers import ModelRequest, ModelResponse, ToolCall


# ----------------------------------------------------------------- fakes


class ScriptedProvider:
    """A Provider that replays prepared responses; no network is touched."""

    provider_name = "scripted"

    def __init__(self, responses: List[ModelResponse]) -> None:
        self._responses = list(responses)
        self.requests: List[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("no scripted response left")
        return self._responses.pop(0)

    def close(self) -> None:  # pragma: no cover - protocol convenience
        pass


def bash_response(command: str, content: str = "THOUGHT: working on it.") -> ModelResponse:
    return ModelResponse(
        content=content,
        tool_calls=(
            ToolCall(
                call_id="call-1",
                name="bash",
                raw_arguments=json.dumps({"command": command}),
            ),
        ),
        finish_reason="stop",
        usage={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
        cost=0.01,
        currency="USD",
    )


def _engine_environment(tmp_path: Path, **overrides: Any) -> GuardedEngineEnvironment:
    policy = CapabilityPolicy(AccessProfile.ELEVATED)
    origin = Origin(OriginKind.NATIVE_AGENT, "test-engine")
    policy.set_grants(
        origin,
        {
            Capability.PROCESS_RUN,
            Capability.DEV_COMMAND,
            Capability.REPO_READ,
            Capability.REPO_WRITE,
        },
    )
    options: dict[str, Any] = dict(
        policy=policy,
        origin=origin,
        session_id="engine-test",
        repository=tmp_path,
        task="test the gate",
        decisions=ActionDecisionEngine(),
        bypass_mode=False,
        mode=None,
        on_event=None,
        command_timeout=15,
        output_char_limit=500,
    )
    options.update(overrides)
    return GuardedEngineEnvironment(**options)


def _agent_run_args(repository: Path, *flags: str) -> List[str]:
    arguments = [
        "agent",
        "run",
        "--repository",
        str(repository),
        "--task",
        "create hello.txt with hi",
        "--model",
        "scripted-model",
        "--base-url",
        "http://127.0.0.1:9/v1",
        "--session-id",
        "engine-1",
        "--max-steps",
        "5",
        "--max-seconds",
        "120",
    ]
    arguments.extend(flags)
    return arguments


def _hello_check() -> str:
    return json.dumps(
        [
            "python",
            "-c",
            "import pathlib; assert pathlib.Path('hello.txt').read_text()"
            ".strip() == 'hi'",
        ]
    )


def _parse_stream(output: str) -> tuple[List[dict], dict]:
    documents = [
        json.loads(line)
        for line in output.splitlines()
        if line.strip() and line.startswith("{")
    ]
    events = [item for item in documents if item.get("type") == "event"]
    results = [item for item in documents if item.get("type") == "result"]
    assert results, "the stream must end with one result document"
    return events, results[-1]["report"]


def _scripted_cli(
    monkeypatch: pytest.MonkeyPatch, responses: List[ModelResponse]
) -> ScriptedProvider:
    provider = ScriptedProvider(responses)
    monkeypatch.setattr(
        cli, "OpenAIChatCompletionsProvider", lambda *args, **kwargs: provider
    )
    return provider


# ------------------------------------------------------- model adapter


def test_provider_model_adapter_returns_engine_messages_and_usage() -> None:
    usage = EngineUsage()
    model = ProviderEngineModel(
        ScriptedProvider([bash_response("echo hi")]),
        "scripted-model",
        format_error_template="{{ error }}",
        observation_template="<returncode>{{output.returncode}}</returncode>",
        usage=usage,
    )
    message = model.query(
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task"},
        ]
    )
    assert message["role"] == "assistant"
    assert message["extra"]["actions"] == [
        {"command": "echo hi", "tool_call_id": "call-1"}
    ]
    assert message["extra"]["cost"] == pytest.approx(0.01)
    assert usage.requests == 1
    assert usage.total_tokens == 14
    assert usage.costs == {"USD": pytest.approx(0.01)}
    observations = model.format_observation_messages(
        message,
        [{"output": "hi", "returncode": 0, "exception_info": ""}],
    )
    assert observations[0]["content"].startswith("<returncode>0</returncode>")


def test_provider_model_adapter_roundtrips_history_through_the_provider() -> None:
    provider = ScriptedProvider(
        [
            bash_response("step one"),
            bash_response("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"),
        ]
    )
    model = ProviderEngineModel(
        provider,
        "scripted-model",
        format_error_template="{{ error }}",
        observation_template="out",
        usage=EngineUsage(),
    )
    message_one = model.query([{"role": "user", "content": "task"}])
    observations = model.format_observation_messages(
        message_one, [{"output": "done", "returncode": 0, "exception_info": ""}]
    )
    history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
        message_one,
        *observations,
    ]
    message_two = model.query(history)
    assert message_two["extra"]["actions"]
    # The resent history carried the tool-call pair intact, so the request
    # contained an assistant tool call and its tool result.
    roles = [item.role for item in provider.requests[-1].messages]
    assert "assistant" in roles and "tool" in roles


def test_provider_model_adapter_salvages_one_fenced_command() -> None:
    response = ModelResponse(
        content="Let me look around.\n\n```bash\nls -la\n```",
        tool_calls=(),
        finish_reason="stop",
        usage={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
    )
    model = ProviderEngineModel(
        ScriptedProvider([response]),
        "scripted-model",
        format_error_template="{{ error }}",
        observation_template="out",
        usage=EngineUsage(),
    )
    message = model.query([{"role": "user", "content": "task"}])
    assert message["extra"]["actions"] == [{"command": "ls -la"}]


def test_provider_model_adapter_persists_response_on_format_error() -> None:
    response = ModelResponse(
        content="I think we are done.",
        tool_calls=(),
        finish_reason="stop",
        usage={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
        cost=0.02,
    )
    model = ProviderEngineModel(
        ScriptedProvider([response]),
        "scripted-model",
        format_error_template="{{ error }}",
        observation_template="out",
        usage=EngineUsage(),
    )
    with pytest.raises(Exception) as raised:
        model.query([{"role": "user", "content": "task"}])
    messages = getattr(raised.value, "messages", [])
    assert messages, "the format error must carry its retry message"
    extra = messages[0].get("extra") or {}
    assert extra.get("cost") == pytest.approx(0.02)
    assert extra["response"]["choices"][0]["finish_reason"] == "stop"


# ------------------------------------------------------------------ gates


def test_the_gate_refuses_git_push_hidden_behind_a_separator(tmp_path: Path) -> None:
    environment = _engine_environment(tmp_path)
    output = environment.execute({"command": "echo ready && git push origin main"})
    assert output["returncode"] == 126
    assert "refused" in output["output"]
    assert environment.refusals and environment.refusals[0]["risk"] == "invariant"


def test_the_gate_refuses_publishing_and_remote_transports(tmp_path: Path) -> None:
    environment = _engine_environment(tmp_path)
    for command in ("npm publish", "twine upload dist/*", "scp file host:/tmp"):
        output = environment.execute({"command": command})
        assert output["returncode"] == 126, command
        assert "refused" in output["output"], command


def test_the_gate_fails_closed_on_destructive_deletion(tmp_path: Path) -> None:
    environment = _engine_environment(tmp_path)
    (tmp_path / "data.txt").write_text("important", encoding="utf-8")
    output = environment.execute({"command": "rm -rf data.txt"})
    assert output["returncode"] == 126
    assert "approval" in output["output"]
    assert (tmp_path / "data.txt").read_text(encoding="utf-8") == "important"


def test_the_gate_refuses_redirects_outside_the_repository(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    assert _confinement_escape("echo x > ../outside.txt", repository) is not None
    assert _confinement_escape("echo x >> /tmp/escape.txt", repository) is not None
    assert _confinement_escape("cd .. && run thing", repository) is not None
    assert _confinement_escape("cd sub && run thing", repository) is None
    assert _confinement_escape("echo x > inside.txt", repository) is None
    environment = _engine_environment(repository)
    output = environment.execute({"command": "echo leak > ../outside.txt"})
    assert output["returncode"] == 126
    assert not (tmp_path / "outside.txt").exists()


def test_the_gate_runs_ordinary_commands_in_the_repository(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    environment = _engine_environment(repository)
    output = environment.execute({"command": "echo engine-alive"})
    assert output["returncode"] == 0, output
    assert "engine-alive" in output["output"]


def test_the_gate_honors_the_capability_policy(tmp_path: Path) -> None:
    policy = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
    origin = Origin(OriginKind.NATIVE_AGENT, "denied-engine")
    policy.set_grants(origin, {Capability.REPO_READ})
    environment = GuardedEngineEnvironment(
        policy=policy,
        origin=origin,
        session_id="engine-test",
        repository=tmp_path,
        task="test",
        decisions=ActionDecisionEngine(),
        bypass_mode=False,
        mode=None,
        on_event=None,
        command_timeout=15,
        output_char_limit=500,
    )
    output = environment.execute({"command": "echo blocked"})
    assert output["returncode"] == 126
    assert "cannot use process.run" in output["output"]


def test_plan_mode_refuses_writes_but_allows_reads(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    environment = _engine_environment(repository, mode="plan")
    write = environment.execute({"command": "echo note > scratch.txt"})
    assert write["returncode"] == 126
    assert not (repository / "scratch.txt").exists()
    read = environment.execute({"command": "echo read-only-summary"})
    assert read["returncode"] == 0, read


def test_write_detection_covers_shell_and_arglevel_forms() -> None:
    assert _writes_anything("echo hi > file")
    assert _writes_anything("git commit -m x")
    assert _writes_anything("sed -i 's/a/b/' file")
    assert not _writes_anything("git status")
    assert not _writes_anything("sed -n '1p' file")
    assert not _writes_anything("cat notes.md")


# ----------------------------------------------- the decisive CLI contract


def test_default_agent_run_executes_the_engine_and_verifies_honestly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KAROX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KAROX_RUNTIME_DIR", str(tmp_path / "runtime"))
    repository = tmp_path / "repo"
    repository.mkdir()
    _scripted_cli(
        monkeypatch,
        [
            bash_response("echo hi > hello.txt"),
            bash_response(
                "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT",
                content="THOUGHT: the file is in place, submitting.",
            ),
        ],
    )
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = cli.main(
            _agent_run_args(
                repository, "--verification-command", _hello_check(), "--stream-json"
            )
        )
    assert code == 0
    events, report = _parse_stream(output.getvalue())
    # The decisive property: the report was produced by the vendored engine.
    assert report["engine"]["name"] == "karo-core"
    assert report["engine"]["module"] == "karox.engine"
    assert report["engine"]["agent"] == "karox.engine.agents.default.DefaultAgent"
    assert report["engine"]["steps"] >= 2
    assert Path(report["engine"]["trajectory"]).is_file()
    # The engine really ran the task in the repository.
    assert (repository / "hello.txt").read_text(encoding="utf-8").strip() == "hi"
    # Verification is the harness's, decided by a real check exit code.
    assert report["verified"] is True
    assert report["checks"] and report["checks"][0]["ok"] is True
    assert report["usage"]["requests"] == 2
    assert report["usage"]["total_tokens"] == 28
    # The NDJSON protocol the TUI consumes is unchanged.
    kinds = [event["kind"] for event in events]
    assert "step_started" in kinds
    assert "tool_started" in kinds
    assert "tool_finished" in kinds
    assert "finished" in kinds


def test_engine_run_without_an_approved_check_is_not_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KAROX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KAROX_RUNTIME_DIR", str(tmp_path / "runtime"))
    repository = tmp_path / "repo"
    repository.mkdir()
    _scripted_cli(
        monkeypatch,
        [
            bash_response("echo hi > hello.txt"),
            bash_response("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"),
        ],
    )
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = cli.main(_agent_run_args(repository, "--stream-json"))
    assert code == 1
    _, report = _parse_stream(output.getvalue())
    assert report["engine"]["module"] == "karox.engine"
    assert report["verified"] is False
    assert report["reason"] == "no_approved_check"


def test_engine_run_refused_commands_reach_the_model_and_the_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KAROX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KAROX_RUNTIME_DIR", str(tmp_path / "runtime"))
    repository = tmp_path / "repo"
    repository.mkdir()
    _scripted_cli(
        monkeypatch,
        [
            bash_response("git push origin main", content="THOUGHT: publishing."),
            bash_response("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"),
        ],
    )
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = cli.main(
            _agent_run_args(
                repository,
                "--verification-command",
                json.dumps(["python", "-c", "print('ok')"]),
                "--stream-json",
            )
        )
    assert code == 0
    events, report = _parse_stream(output.getvalue())
    assert report["engine"]["module"] == "karox.engine"
    tool_events = [event for event in events if event["kind"] == "tool_finished"]
    assert tool_events and tool_events[0]["ok"] is False
    # The refusal is durable evidence, visible in the checksummed session.
    from karox.paths import session_dir
    from karox.sessions import SessionStore

    record = SessionStore(session_dir()).load("engine-1")
    refusals = [
        entry
        for entry in record.provider_history
        if entry.get("kind") == "engine_refusal"
    ]
    assert refusals and "git push" in refusals[0]["command"]


def test_legacy_loop_flag_selects_the_v5_kernel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KAROX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KAROX_RUNTIME_DIR", str(tmp_path / "runtime"))
    repository = tmp_path / "repo"
    repository.mkdir()
    captured: dict[str, Any] = {}

    class StubKernel:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        def run(self, session_id: str) -> AgentReport:
            return AgentReport(
                session_id=session_id,
                status="stopped",
                phase="execution",
                verified=False,
                reason="stub",
                steps=0,
                changed_files=(),
                checks=(),
                git_state={},
                evidence=(),
                usage={},
            )

    monkeypatch.setattr(cli, "AgentKernel", StubKernel)
    _scripted_cli(monkeypatch, [])
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = cli.main(_agent_run_args(repository, "--legacy-loop", "--json"))
    assert code == 1
    assert "system_prompt" in captured  # the v5 kernel signature
    report = json.loads(output.getvalue())
    assert "engine" not in report  # the legacy loop is not the engine


def test_engine_commands_need_only_the_ordinary_process_capability() -> None:
    # The engine loop borrows no elevated capability: a workspace_write
    # session's ordinary process.run authority is the whole grant it uses,
    # and the consequence engine + invariants bound the rest.
    grants = cli._agent_grants(AccessProfile.WORKSPACE_WRITE, "build")
    assert Capability.PROCESS_RUN in grants
    assert Capability.DEV_COMMAND not in grants
    engine_environment_caps = {Capability.PROCESS_RUN}
    assert engine_environment_caps <= set(grants)


def test_engine_skill_block_phrases_skills_for_the_shell() -> None:
    class Item:
        name = "deploy-check"
        description = "verify deploys"
        directory = Path(".karox/skills/deploy-check")

    class Catalog:
        def discover(self) -> tuple:
            return (Item(),)

    block = cli._engine_skill_block(Catalog())
    assert "deploy-check" in block
    assert "shell command" in block
    assert "skill_load" not in block
    assert cli._engine_skill_block(None) == ""


def test_engine_events_use_the_ndjson_event_shape(tmp_path: Path) -> None:
    events: List[AgentEvent] = []

    def observe(event: AgentEvent) -> None:
        events.append(event)

    environment = _engine_environment(tmp_path, on_event=observe)
    environment.execute({"command": "echo shape-check"})
    kinds = [event.kind for event in events]
    assert kinds[0] is AgentEventKind.TOOL_STARTED
    assert kinds[-1] is AgentEventKind.TOOL_FINISHED
    assert events[-1].tool == "engine.bash"
    assert events[-1].ok is True


def test_engine_limits_are_enforced_by_the_engine_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KAROX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KAROX_RUNTIME_DIR", str(tmp_path / "runtime"))
    repository = tmp_path / "repo"
    repository.mkdir()
    _scripted_cli(
        monkeypatch,
        [bash_response("echo one"), bash_response("echo two")],
    )
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = cli.main(
            _agent_run_args(
                repository,
                "--max-steps",
                "1",
                "--verification-command",
                json.dumps(["python", "-c", "print('ok')"]),
                "--stream-json",
            )
        )
    assert code == 0
    _, report = _parse_stream(output.getvalue())
    # The engine's own step limit ended the run after one model call.
    assert report["engine"]["steps"] == 1
    assert report["reason"] == "step_limit"
