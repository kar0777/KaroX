"""The engine-backed default kernel: karo-core drives the KaroX product path.

These tests pin the B1 contract: ``AgentKernel`` must execute through
``src/karox/engine`` (the vendored karo-core loop) by default, with the
provider layer, typed Core tools, policy/risk gates, harness verification,
durable checkpoints, cancellation and stream events all preserved.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, List, Optional

import pytest

from karox.agent import AgentEvent, AgentEventKind, AgentKernel, SYSTEM_PROMPT
from karox.agent import AgentLimits, ContextBudget
from karox.core import CoreRuntime
from karox.engine_kernel import EngineKernel
from karox.models import AccessProfile, Capability, Origin, OriginKind
from karox.policy import CapabilityPolicy
from karox.providers import ModelRequest, ModelResponse, ProviderTool, ToolCall
from karox.sessions import SessionStore


GOOD_CHECK = ("python", "-c", "print('ok')")
BAD_CHECK = ("python", "-c", "raise SystemExit(3)")
CONTENT_CHECK = (
    "python",
    "-c",
    "import sys; sys.exit(0 if open('hello.txt', encoding='utf-8').read() == 'hi' else 1)",
)


def _git(repository: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        env={
            "PATH": __import__("os").environ.get("PATH", ""),
            "HOME": str(repository.parent),
            "GIT_CONFIG_NOSYSTEM": "1",
        },
    )


@pytest.fixture()
def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


class ScriptedProvider:
    """Replays scripted responses; records every request it was asked to send."""

    def __init__(self, responses: List[ModelResponse]) -> None:
        self._responses = list(responses)
        self.requests: List[ModelRequest] = []
        self.hook: Optional[Any] = None

    @property
    def provider_name(self) -> str:
        return "scripted"

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self.hook is not None:
            self.hook(self)
        if not self._responses:
            raise AssertionError("scripted provider ran out of responses")
        return self._responses.pop(0)

    def close(self) -> None:
        return None


def _tool_call(call_id: str, name: str, arguments: dict) -> ToolCall:
    return ToolCall(
        call_id=call_id,
        name=name,
        raw_arguments=json.dumps(arguments, ensure_ascii=False, sort_keys=True),
    )


def _response(
    tool_calls: tuple[ToolCall, ...] = (),
    content: Optional[str] = None,
    finish_reason: str = "stop",
) -> ModelResponse:
    return ModelResponse(
        content=content,
        tool_calls=tool_calls,
        finish_reason=finish_reason,
        usage={"prompt_tokens": 10, "completion_tokens": 5},
    )


def _write_call(call_id: str, path: str, content: str) -> ToolCall:
    return _tool_call(call_id, "repo_write_file", {"path": path, "content": content})


def _finish_call(call_id: str, answer: str) -> ToolCall:
    return _tool_call(call_id, "finish", {"answer": answer})


class Harness:
    def __init__(
        self,
        repository: Path,
        tmp_path: Path,
        responses: List[ModelResponse],
        *,
        verification: tuple[tuple[str, ...], ...] = (GOOD_CHECK,),
        grants: Optional[set] = None,
        session_id: str = "s1",
        max_steps: int = 12,
        create_session: bool = True,
    ) -> None:
        self.repository = repository
        self.provider = ScriptedProvider(responses)
        self.events: List[AgentEvent] = []
        self.store = SessionStore(tmp_path / "sessions")
        self.origin = Origin(OriginKind.NATIVE_AGENT, "test-kernel")
        policy = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
        policy.set_grants(
            self.origin,
            grants
            if grants is not None
            else {
                Capability.REPO_READ,
                Capability.REPO_WRITE,
                Capability.CHECKS_RUN,
                Capability.PROCESS_RUN,
                Capability.GIT_READ,
            },
        )
        self.core = CoreRuntime(
            repository,
            policy,
            self.store,
            tmp_path / "audit.jsonl",
            verification_commands=verification,
        )
        if create_session:
            self.store.create(
                repository,
                "create hello.txt with hi",
                AccessProfile.WORKSPACE_WRITE,
                session_id=session_id,
            )
        self.kernel = AgentKernel(
            provider=self.provider,
            model="test-model",
            core=self.core,
            sessions=self.store,
            origin=self.origin,
            limits=AgentLimits(max_steps=max_steps, max_seconds=120.0),
            system_prompt=SYSTEM_PROMPT,
            context=ContextBudget(),
            on_event=self.events.append,
        )

    def run(self) -> Any:
        return self.kernel.run("s1")

    def record(self) -> Any:
        return self.store.load("s1")

    def kinds(self) -> List[str]:
        return [event.kind.value for event in self.events]


def test_agent_kernel_default_is_the_engine_kernel(
    tmp_path: Path, repository: Path
) -> None:
    """The public name selects the karo-core engine, not the legacy loop."""
    harness = Harness(
        repository,
        tmp_path,
        [_response((_finish_call("c1", "done"),))],
    )
    assert isinstance(harness.kernel.implementation, EngineKernel)
    legacy = AgentKernel(
        provider=harness.provider,
        model="test-model",
        core=harness.core,
        sessions=harness.store,
        origin=harness.origin,
        system_prompt=SYSTEM_PROMPT,
        legacy=True,
    )
    from karox.agent import LegacyAgentKernel

    assert isinstance(legacy.implementation, LegacyAgentKernel)


def test_engine_run_verifies_a_change_end_to_end(tmp_path: Path, repository: Path) -> None:
    harness = Harness(
        repository,
        tmp_path,
        [
            _response((_write_call("c1", "hello.txt", "hi"),), content="writing the file"),
            _response((_finish_call("c2", "created hello.txt with hi"),)),
        ],
    )
    report = harness.run()

    assert (repository / "hello.txt").read_text(encoding="utf-8") == "hi"
    assert report.verified is True, report.reason
    assert report.status == "verified"
    assert report.steps >= 2
    assert report.changed_files == ("hello.txt",)
    assert report.provider_message == "created hello.txt with hi"
    assert report.reason == "submitted"
    # The harness, not the model, collected the finishing evidence.
    record = harness.record()
    harness_calls = [
        entry
        for entry in record.provider_history
        if entry.get("role") == "tool"
        and entry.get("result", {}).get("requested_by") == "harness_final"
    ]
    executed = {entry.get("core_name") for entry in harness_calls}
    assert "checks.run" in executed
    assert "git.status" in executed
    assert "git.diff" in executed
    assert record.status == "verified"
    assert record.evidence, "a verified run must persist evidence"


def test_engine_run_emits_the_stream_event_contract(tmp_path: Path, repository: Path) -> None:
    harness = Harness(
        repository,
        tmp_path,
        [
            _response((_write_call("c1", "hello.txt", "hi"),)),
            _response((_finish_call("c2", "done"),)),
        ],
    )
    harness.run()

    kinds = harness.kinds()
    assert kinds[0] == "step_started"
    assert "tool_started" in kinds
    assert "tool_finished" in kinds
    assert "file_edited" in kinds
    assert "step_finished" in kinds
    assert kinds[-1] == "finished"
    for event in harness.events:
        assert isinstance(event.step, int)
    tool_events = [e for e in harness.events if e.kind is AgentEventKind.TOOL_FINISHED]
    assert all(e.ok is not None for e in tool_events)
    finished = [e for e in harness.events if e.kind is AgentEventKind.FINISHED][0]
    assert finished.status == "verified"
    # Every event must survive the JSON stream the CLI/TUI reads.
    for event in harness.events:
        json.dumps(
            {
                "type": event.kind.value,
                "step": event.step,
                "tool": event.tool,
                "ok": event.ok,
                "summary": event.summary,
                "detail": event.detail,
                "reason": event.reason,
            },
            ensure_ascii=False,
        )


def test_engine_failed_check_is_not_verified(tmp_path: Path, repository: Path) -> None:
    harness = Harness(
        repository,
        tmp_path,
        [
            _response((_write_call("c1", "hello.txt", "hi"),)),
            _response((_finish_call("c2", "done"),)),
            _response((_finish_call("c3", "still done"),)),
            _response((_finish_call("c4", "still done"),)),
        ],
        verification=(BAD_CHECK,),
    )

    report = harness.run()
    assert report.verified is False
    assert report.status == "stopped"
    assert (
        "check" in report.reason
        or "unresolved" in report.reason
        or "failed" in report.reason
    )
    # The failed check is visible in the session, not just the report.
    assert any(
        entry.get("core_name") == "checks.run"
        and isinstance(entry.get("result"), dict)
        and entry["result"].get("ok") is False
        for entry in harness.record().provider_history
    )
    # A failed verification must never restore over the user's bytes without
    # a proven state; there is none here, so the file stays as the model left it.
    assert (repository / "hello.txt").read_text(encoding="utf-8") == "hi"


def test_engine_answer_task_verifies_from_inspection_basis(
    tmp_path: Path, repository: Path
) -> None:
    read_call = _tool_call("c1", "repo_read_file", {"path": "seed.txt"})
    harness = Harness(
        repository,
        tmp_path,
        [
            _response((read_call,), content="reading the seed"),
            _response((_finish_call("c2", "the repository contains seed.txt"),)),
        ],
    )
    with harness.store.mutate("s1", "test", ttl_seconds=30.0) as record:
        record.task = "what does the repository contain?"

    report = harness.run()
    assert report.verified is True, report.reason
    assert report.reason == "answer"
    assert report.answer_basis, "an answer must name its inspections"
    assert report.answer_basis[0]["tool"] == "repo.read_file"


def test_engine_resume_continues_from_durable_checkpoint(
    tmp_path: Path, repository: Path
) -> None:
    first = Harness(
        repository,
        tmp_path,
        [_response((_write_call("c1", "hello.txt", "hi"),))],
        max_steps=1,
    )
    stopped = first.run()
    assert stopped.verified is False
    assert stopped.reason == "step_limit"
    persisted = first.record().provider_history
    assert any(entry.get("role") == "assistant" for entry in persisted)
    assert any(entry.get("role") == "tool" for entry in persisted)

    second = Harness(
        repository,
        tmp_path,
        [_response((_finish_call("c2", "created hello.txt with hi"),))],
        max_steps=6,
        create_session=False,
    )
    report = second.run()
    assert report.verified is True, report.reason
    # The resumed request carried the persisted transcript: the model saw the
    # earlier write and its observation.
    last = second.provider.requests[-1]
    roles = [message.role for message in last.messages]
    assert "assistant" in roles and "tool" in roles
    tool_names = [
        call.name for message in last.messages if message.role == "assistant" for call in message.tool_calls
    ]
    assert "repo_write_file" in tool_names


def test_engine_cancel_stops_cleanly(tmp_path: Path, repository: Path) -> None:
    harness = Harness(
        repository,
        tmp_path,
        [
            _response((_write_call("c1", "hello.txt", "hi"),)),
            _response((_finish_call("c2", "done"),)),
        ],
    )
    harness.provider.hook = lambda provider: harness.kernel.cancel()
    report = harness.run()
    assert report.status == "stopped"
    assert report.reason == "cancelled"
    assert (repository / "hello.txt").exists()


def test_engine_policy_refusal_is_an_observation_not_a_crash(
    tmp_path: Path, repository: Path
) -> None:
    harness = Harness(
        repository,
        tmp_path,
        [
            _response((_write_call("c1", "hello.txt", "hi"),)),
            _response((_finish_call("c2", "done"),)),
        ],
        grants={Capability.REPO_READ, Capability.GIT_READ},
    )
    report = harness.run()
    assert report.verified is False
    assert not (repository / "hello.txt").exists()
    refusals = [
        entry
        for entry in harness.record().provider_history
        if entry.get("role") == "tool" and entry.get("core_name") == "repo.write_file"
    ]
    assert refusals
    assert refusals[0].get("error", {}).get("type") == "PolicyDenied"


def test_engine_recovers_from_an_empty_response(tmp_path: Path, repository: Path) -> None:
    harness = Harness(
        repository,
        tmp_path,
        [
            _response(content=None, finish_reason="stop"),
            _response((_write_call("c1", "hello.txt", "hi"),)),
            _response((_finish_call("c2", "done"),)),
        ],
    )
    report = harness.run()
    assert report.verified is True, report.reason
    nudges = [
        entry
        for entry in harness.record().provider_history
        if entry.get("role") == "user" and "format" in str(entry.get("content", "")).lower()
    ]
    assert nudges, "an empty response must produce a corrective nudge"


def test_engine_format_error_usage_is_accounted(tmp_path: Path, repository: Path) -> None:
    """A billed response that fails parsing still counts its tokens.

    The legacy loop folds every provider response into session usage; the
    engine bridge must not drop the tokens of a format-error response, or the
    session's token evidence undercounts real spend.
    """
    harness = Harness(
        repository,
        tmp_path,
        [
            _response(content=None, finish_reason="stop"),
            _response((_write_call("c1", "hello.txt", "hi"),)),
            _response((_finish_call("c2", "done"),)),
        ],
    )
    report = harness.run()
    assert report.verified is True, report.reason
    usage = harness.record().usage
    assert usage["requests"] == 3
    assert usage["prompt_tokens"] == 30
    assert usage["completion_tokens"] == 15
    events = usage.get("events") or []
    assert len(events) == 3
    # The failed response is attributed to the step it was answered on.
    assert events[0]["step"] == 1
    assert events[0]["kind"] == "format_error"


def test_engine_recovers_from_a_truncated_response(tmp_path: Path, repository: Path) -> None:
    harness = Harness(
        repository,
        tmp_path,
        [
            _response(content=None, finish_reason="length"),
            _response((_write_call("c1", "hello.txt", "hi"),)),
            _response((_finish_call("c2", "done"),)),
        ],
    )
    report = harness.run()
    assert report.verified is True, report.reason
    nudges = [
        entry
        for entry in harness.record().provider_history
        if entry.get("role") == "user" and "output token limit" in str(entry.get("content", ""))
    ]
    assert nudges


def test_engine_prose_final_answer_after_format_errors(
    tmp_path: Path, repository: Path
) -> None:
    harness = Harness(
        repository,
        tmp_path,
        [
            _response(content="The work is complete.", finish_reason="stop"),
            _response(content="The work is complete.", finish_reason="stop"),
            _response(content="The work is complete.", finish_reason="stop"),
        ],
    )
    with harness.store.mutate("s1", "test", ttl_seconds=30.0) as record:
        record.task = "привет"
    report = harness.run()
    assert report.provider_message == "The work is complete."
    assert report.reason in {"submitted", "answer"}
    assert report.status == "verified" or report.status == "stopped"


def test_engine_context_compaction_fires_and_reports(tmp_path: Path, repository: Path) -> None:
    big = "x" * 40_000
    read_call = _tool_call("cr", "repo_read_file", {"path": "hello.txt"})
    harness = Harness(
        repository,
        tmp_path,
        [
            _response((_write_call("c1", "hello.txt", big),)),
            _response((read_call,)),
            _response((_finish_call("c2", "done"),)),
        ],
    )
    harness.kernel.implementation.context = ContextBudget(max_input_tokens=1)
    harness.kernel.implementation._engine_keep_recent_messages = 1
    report = harness.run()
    kinds = harness.kinds()
    assert "compacted" in kinds
    assert report.compaction is not None
    assert report.compaction["tokens_after"] < report.compaction["tokens_before"]


def test_engine_rerun_after_verified_needs_no_model(tmp_path: Path, repository: Path) -> None:
    first = Harness(
        repository,
        tmp_path,
        [
            _response((_write_call("c1", "hello.txt", "hi"),)),
            _response((_finish_call("c2", "done"),)),
        ],
    )
    assert first.run().verified is True
    second = Harness(
        repository,
        tmp_path,
        [_response((_finish_call("cx", "should not be needed"),))],
        create_session=False,
    )
    report = second.run()
    assert report.verified is True
    assert report.reason == "already_verified"
    assert second.provider.requests == []


def test_engine_budget_exceeded_ends_the_run(tmp_path: Path, repository: Path) -> None:
    over = ModelResponse(
        content=None,
        tool_calls=(),
        finish_reason="stop",
        usage={"prompt_tokens": 1, "completion_tokens": 1},
        budget_exceeded=True,
        budget_reason="cost_budget",
    )
    harness = Harness(repository, tmp_path, [over])
    report = harness.run()
    assert report.status == "stopped"
    assert report.reason.startswith("budget_exceeded")


def test_engine_verified_snapshot_and_regression_restore(
    tmp_path: Path, repository: Path
) -> None:
    first = Harness(
        repository,
        tmp_path,
        [
            _response((_write_call("c1", "hello.txt", "hi"),)),
            _response((_finish_call("c2", "done"),)),
        ],
        verification=(CONTENT_CHECK,),
    )
    assert first.run().verified is True
    snapshot = first.record().verified_snapshot
    assert "hello.txt" in snapshot.get("files", {})

    # A second turn replaces the file with worse bytes and the check fails:
    # the run must not ship the regression over the proven state. Bounded
    # repair rounds get their scripted responses too; none of them helps.
    first.store.continue_task("s1", "rewrite hello.txt with worse content")
    second = Harness(
        repository,
        tmp_path,
        [
            _response((_write_call("c3", "hello.txt", "wrong"),)),
            _response((_finish_call("c4", "rewrote it"),)),
            _response((_finish_call("c5", "still wrong"),)),
            _response((_finish_call("c6", "still wrong"),)),
        ],
        verification=(CONTENT_CHECK,),
        create_session=False,
    )
    report = second.run()
    assert report.verified is False
    assert report.restored is not None
    assert (repository / "hello.txt").read_text(encoding="utf-8") == "hi"


def test_engine_model_bridge_builds_provider_requests(repository: Path) -> None:
    """Unit contract of the bridge: typed tools, ceilings, message conversion."""
    from karox.engine_kernel import EngineProviderModel

    provider = ScriptedProvider([_response((_finish_call("c1", "done"),))])
    model = EngineProviderModel(
        provider=provider,
        model_id="test-model",
        tools=(
            __import__("karox.providers", fromlist=["ProviderTool"]).ProviderTool(
                "repo_write_file", "write", {"type": "object"}
            ),
        ),
        max_output_tokens=1234,
        reasoning_effort="low",
        event=lambda kind, **fields: None,
        deadline_seconds=30.0,
    )
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
        {
            "role": "assistant",
            "content": "note",
            "tool_calls": [
                {"call_id": "c0", "name": "repo_write_file", "raw_arguments": "{}"}
            ],
        },
        {"role": "tool", "content": "obs", "tool_call_id": "c0"},
    ]
    message = model.query(messages)
    request = provider.requests[0]
    assert request.model == "test-model"
    assert request.max_output_tokens == 1234
    assert request.reasoning_effort == "low"
    assert [tool.name for tool in request.tools] == ["repo_write_file"]
    roles = [m.role for m in request.messages]
    assert roles == ["system", "user", "assistant", "tool"]
    assert request.messages[2].tool_calls[0].name == "repo_write_file"
    assert request.messages[3].tool_call_id == "c0"
    assert message["role"] == "assistant"
    assert message["extra"]["actions"][0]["tool_call_id"] == "c1"


def test_engine_model_bridge_streams_progress_deltas(repository: Path) -> None:
    from karox.providers import ModelEvent, ModelEventKind

    from karox.engine_kernel import EngineProviderModel

    class StreamingProvider(ScriptedProvider):
        def stream(self, request: ModelRequest):
            self.requests.append(request)
            yield ModelEvent(ModelEventKind.TEXT_DELTA, text_delta="par")
            yield ModelEvent(ModelEventKind.REASONING_SUMMARY_DELTA, reasoning_summary_delta="sum")
            yield ModelEvent(
                ModelEventKind.COMPLETION,
                finish_reason="stop",
                response=_response((_finish_call("c1", "done"),)),
            )

    deltas: List[tuple] = []
    model = EngineProviderModel(
        provider=StreamingProvider([]),
        model_id="m",
        tools=(),
        event=lambda kind, **fields: deltas.append((kind, fields)),
        deadline_seconds=30.0,
    )
    model.query([{"role": "user", "content": "task"}])
    assert any(kind == "text_delta" for kind, _ in deltas)
    assert any(kind == "reasoning_summary_delta" for kind, _ in deltas)


def test_engine_model_bridge_salvages_a_fenced_command(repository: Path) -> None:
    """Port 5 through the product bridge: a fenced command in prose executes."""
    from karox.engine.exceptions import FormatError

    from karox.engine_kernel import EngineProviderModel

    provider = ScriptedProvider(
        [
            ModelResponse(
                content='Let me create it.\n```bash\nrepo_write_file "a.txt" "x"\n```',
                tool_calls=(),
                finish_reason="stop",
                usage={},
            )
        ]
    )
    model = EngineProviderModel(
        provider=provider,
        model_id="m",
        tools=(),
        deadline_seconds=30.0,
    )
    message = model.query([{"role": "user", "content": "task"}])
    assert message["extra"]["actions"] == [{"command": 'repo_write_file "a.txt" "x"'}]

    empty = ScriptedProvider(
        [ModelResponse(content=None, tool_calls=(), finish_reason="stop", usage={})]
    )
    model2 = EngineProviderModel(
        provider=empty, model_id="m", tools=(), deadline_seconds=30.0
    )
    with pytest.raises(FormatError) as caught:
        model2.query([{"role": "user", "content": "task"}])
    extra = caught.value.messages[0]["extra"]
    # Port 3 depends on the OpenAI-shaped response persisted on the error.
    assert extra["response"]["choices"][0]["finish_reason"] == "stop"


def _engine_conversation() -> List[dict]:
    return [
        {"role": "system", "content": "sys prompt"},
        {"role": "user", "content": "create hello.txt with hi"},
        {
            "role": "assistant",
            "content": "creating the file",
            "tool_calls": [
                {
                    "call_id": "c1",
                    "name": "repo_write_file",
                    "raw_arguments": json.dumps(
                        {"path": "hello.txt", "content": "hi"}
                    ),
                },
                {
                    "call_id": "c2",
                    "name": "finish",
                    "raw_arguments": json.dumps({"answer": "created hello.txt"}),
                },
            ],
        },
        {
            "role": "tool",
            "content": "<returncode>0</returncode>\n<output>\nwrote hello.txt\n</output>",
            "tool_call_id": "c1",
        },
    ]


def _bridge_request() -> ModelRequest:
    from karox.engine_kernel import EngineProviderModel, _finish_provider_tool

    provider = ScriptedProvider([_response((_finish_call("c2", "done"),))])
    tools = (
        ProviderTool(
            "repo_write_file",
            "Atomically write a UTF-8 text file inside the repository.",
            {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        ),
        _finish_provider_tool(),
    )
    model = EngineProviderModel(
        provider=provider,
        model_id="engine-model",
        tools=tools,
        max_output_tokens=4096,
        deadline_seconds=30.0,
    )
    model.query(_engine_conversation())
    return provider.requests[0]


def test_engine_bridge_request_satisfies_openai_chat_payload() -> None:
    from karox.providers import OpenAIChatCompletionsProvider

    payload = OpenAIChatCompletionsProvider._request_payload(_bridge_request())
    names = {tool["function"]["name"] for tool in payload["tools"]}
    assert {"repo_write_file", "finish"} <= names
    assistant = [m for m in payload["messages"] if m["role"] == "assistant"][0]
    assert {call["function"]["name"] for call in assistant["tool_calls"]} == {
        "repo_write_file",
        "finish",
    }
    tool_messages = [m for m in payload["messages"] if m["role"] == "tool"]
    assert tool_messages and tool_messages[0]["tool_call_id"] == "c1"


def test_engine_bridge_request_satisfies_openai_responses_payload() -> None:
    from karox.provider_adapters import OpenAIResponsesProvider

    provider = OpenAIResponsesProvider("https://provider.example/v1")
    payload = provider._request_payload(_bridge_request())
    names = {tool["name"] for tool in payload["tools"]}
    assert {"repo_write_file", "finish"} <= names
    assert payload["input"], "the engine transcript must become Responses input"


def test_engine_bridge_request_satisfies_anthropic_payload() -> None:
    from karox.provider_adapters import AnthropicMessagesProvider

    provider = AnthropicMessagesProvider("https://provider.example/v1")
    payload = provider._request_payload(_bridge_request())
    names = {tool["name"] for tool in payload["tools"]}
    assert {"repo_write_file", "finish"} <= names
    assert payload["system"] == "sys prompt"
    blocks = []
    for message in payload["messages"]:
        blocks.extend(message.get("content", []))
    tool_uses = [b for b in blocks if b.get("type") == "tool_use"]
    tool_results = [b for b in blocks if b.get("type") == "tool_result"]
    assert {b["name"] for b in tool_uses} == {"repo_write_file", "finish"}
    assert tool_results and tool_results[0]["tool_use_id"] == "c1"


def test_engine_bridge_request_satisfies_gemini_payload() -> None:
    from karox.provider_adapters import GeminiGenerateContentProvider

    provider = GeminiGenerateContentProvider("https://provider.example/v1")
    payload = provider._request_payload(_bridge_request())
    declarations = payload["tools"][0]["functionDeclarations"]
    names = {item["name"] for item in declarations}
    assert {"repo_write_file", "finish"} <= names
    assert payload["systemInstruction"], "the system prompt must survive conversion"
    parts = []
    for content in payload["contents"]:
        parts.extend(content.get("parts", []))
    calls = [p for p in parts if "functionCall" in p]
    responses = [p for p in parts if "functionResponse" in p]
    assert {p["functionCall"]["name"] for p in calls} == {"repo_write_file", "finish"}
    assert responses and responses[0]["functionResponse"]["name"] == "repo_write_file"


def test_engine_resume_synthesizes_results_for_interrupted_calls(
    tmp_path: Path, repository: Path
) -> None:
    """A run killed between query and execution must still resume cleanly.

    The durable record holds an assistant tool call with no observation; a
    transport that pairs tool_use with tool_result (Anthropic, Gemini) would
    refuse the next request outright. The kernel synthesizes the missing
    observation in memory while the record still says the call never ran.
    """
    harness = Harness(
        repository,
        tmp_path,
        [
            _response((_write_call("c8", "hello.txt", "hi"),)),
            _response((_finish_call("c9", "done"),)),
        ],
    )
    interrupted_call = {
        "call_id": "c1",
        "name": "repo_write_file",
        "raw_arguments": json.dumps({"path": "hello.txt", "content": "hi"}),
        "recoverable": True,
    }
    with harness.store.mutate("s1", "test", ttl_seconds=30.0) as record:
        record.provider_history.extend(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": "create hello.txt with hi",
                    "kind": "task",
                },
                {
                    "role": "assistant",
                    "origin": harness.origin.key,
                    "content": "writing",
                    "tool_calls": [interrupted_call],
                    "provider": "scripted",
                    "model": "test-model",
                    "finish_reason": "tool_calls",
                    "usage": {},
                },
            ]
        )
        record.status = "active"
        record.phase = "execution"

    report = harness.run()
    assert report.verified is True, report.reason
    # The resumed request carried a synthesized tool result for the dangling
    # call, and the interrupted write itself never executed.
    last = harness.provider.requests[-1]
    dangling = [
        message
        for message in last.messages
        if message.role == "tool" and message.tool_call_id == "c1"
    ]
    assert dangling
    assert "interrupted before this call executed" in (dangling[0].content or "")
