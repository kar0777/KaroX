"""The engine-backed default kernel: karo-core drives the KaroX product path.

These tests pin the B1 contract: ``AgentKernel`` must execute through
``src/karox/engine`` (the vendored karo-core loop) by default, with the
provider layer, typed Core tools, policy/risk gates, harness verification,
durable checkpoints, cancellation and stream events all preserved.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, List, Optional

import pytest

from karox.agent import AgentEvent, AgentEventKind, AgentKernel, SYSTEM_PROMPT
from karox.agent import AgentLimits, ContextBudget
from karox.core import CoreRuntime
from karox.core_tools import ExtendedCoreRuntime
from karox.engine import __version__ as engine_version
from karox.engine_kernel import EngineKernel, _model_request_deadline
from karox.models import AccessProfile, Capability, CoreCommand, Origin, OriginKind
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
        runtime_cls: type = CoreRuntime,
        task: str = "create hello.txt with hi",
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
        self.core = runtime_cls(
            repository,
            policy,
            self.store,
            tmp_path / "audit.jsonl",
            verification_commands=verification,
        )
        if create_session:
            self.store.create(
                repository,
                task,
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
    """The public name selects the karo-core engine; the v5 loop is gone."""
    harness = Harness(
        repository,
        tmp_path,
        [_response((_finish_call("c1", "done"),))],
    )
    assert isinstance(harness.kernel.implementation, EngineKernel)
    # The retired v5 loop must not come back through the constructor.
    with pytest.raises(ValueError, match="legacy v5 loop was removed"):
        AgentKernel(
            provider=harness.provider,
            model="test-model",
            core=harness.core,
            sessions=harness.store,
            origin=harness.origin,
            system_prompt=SYSTEM_PROMPT,
            legacy=True,
        )


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


def test_engine_does_not_rerun_a_check_the_model_already_passed(
    tmp_path: Path, repository: Path
) -> None:
    """A successful check by the model is proof; the harness must not repeat it.

    A task that says "run the tests" makes the model verify itself. The former
    fallback ran the first approved command whenever anything was missing,
    including only the final git.diff review, so such a task paid for the whole
    suite twice. Here only the Git reads are owed.
    """
    harness = Harness(
        repository,
        tmp_path,
        [
            _response((_write_call("c1", "hello.txt", "hi"),)),
            _response((_tool_call("c2", "checks_run", {"argv": list(GOOD_CHECK)}),)),
            _response((_finish_call("c3", "created hello.txt with hi"),)),
        ],
    )
    report = harness.run()

    assert report.verified is True, report.reason
    record = harness.record()
    checks = [entry for entry in record.provider_history if entry.get("core_name") == "checks.run"]
    assert len(checks) == 1, "the harness re-ran a check the model had already passed"
    harness_calls = [
        entry.get("core_name")
        for entry in record.provider_history
        if entry.get("role") == "tool"
        and (entry.get("result") or {}).get("requested_by") == "harness_final"
    ]
    assert harness_calls == ["git.status", "git.diff"]


def test_engine_runs_independent_calls_batched_in_one_response(
    tmp_path: Path, repository: Path
) -> None:
    """Two independent mutations in one response cost one model call, not two.

    The execution contract tells the model to batch independent actions, and
    the product prompt asks for the same. This is the mechanical guarantee
    behind that instruction: both calls run in order, both results are
    persisted against their own call ids, and the run still verifies.
    """
    harness = Harness(
        repository,
        tmp_path,
        [
            _response(
                (
                    _write_call("c1", "a.txt", "one"),
                    _write_call("c2", "b.txt", "two"),
                )
            ),
            _response((_finish_call("c3", "created both files"),)),
        ],
    )
    report = harness.run()

    assert (repository / "a.txt").read_text(encoding="utf-8") == "one"
    assert (repository / "b.txt").read_text(encoding="utf-8") == "two"
    assert report.verified is True, report.reason
    assert report.changed_files == ("a.txt", "b.txt")
    # Two model calls: the batch and the finish. A serialised loop would need
    # three and would re-pay the whole prefix for the extra one.
    assert report.steps == 2
    persisted = {
        str(entry.get("tool_call_id"))
        for entry in harness.record().provider_history
        if entry.get("role") == "tool" and entry.get("tool_call_id") in {"c1", "c2"}
    }
    assert persisted == {"c1", "c2"}


def test_engine_batch_keeps_valid_calls_when_one_is_malformed(
    tmp_path: Path, repository: Path
) -> None:
    """One unparseable call must not discard the valid calls beside it.

    Batching is only worth the contract text if a typo in one call does not
    void the whole response. The valid write runs, the bad call returns an
    actionable observation against its own call id, and the run continues.
    """
    malformed = ToolCall(call_id="c2", name="repo_write_file", raw_arguments="{not json")
    harness = Harness(
        repository,
        tmp_path,
        [
            _response((_write_call("c1", "a.txt", "one"), malformed)),
            _response((_finish_call("c3", "created a.txt; one call was rejected"),)),
        ],
    )
    report = harness.run()

    assert (repository / "a.txt").read_text(encoding="utf-8") == "one"
    assert not (repository / "b.txt").exists()
    assert report.verified is True, report.reason
    assert report.steps == 2
    refused = [
        entry
        for entry in harness.record().provider_history
        if entry.get("role") == "tool" and entry.get("tool_call_id") == "c2"
    ]
    assert refused, "the rejected call must be visible in the durable transcript"
    assert refused[0]["error"]["type"] == "malformed_arguments"
    assert "parsing tool call arguments" in refused[0]["error"]["message"].lower()


def test_engine_single_malformed_call_still_gets_the_format_error_nudge(
    tmp_path: Path, repository: Path
) -> None:
    """A lone bad call keeps the format-error recovery, not an observation.

    There is no batch to preserve, and the nudge names the contract the
    provider broke. The run recovers on the next model call.
    """
    malformed = ToolCall(call_id="c1", name="repo_write_file", raw_arguments="{not json")
    harness = Harness(
        repository,
        tmp_path,
        [
            _response((malformed,)),
            _response((_write_call("c2", "hello.txt", "hi"),)),
            _response((_finish_call("c3", "created hello.txt"),)),
        ],
    )
    report = harness.run()

    notes = [
        entry
        for entry in harness.record().provider_history
        if entry.get("kind") == "engine_note"
    ]
    assert any(
        "Error parsing tool call arguments" in str(entry.get("content"))
        for entry in notes
    )
    assert (repository / "hello.txt").read_text(encoding="utf-8") == "hi"
    assert report.verified is True, report.reason
    # Three billed responses: the malformed one, the write, the finish. The
    # malformed response has no assistant turn, so the step count stays at 2
    # while usage still charges all three requests.
    assert report.steps == 2
    usage = harness.record().usage
    assert usage["requests"] == 3
    assert (usage.get("events") or [])[0]["kind"] == "format_error"


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

    Every provider response must fold into session usage; the engine bridge
    must not drop the tokens of a format-error response, or the session's
    token evidence undercounts real spend.
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


def test_compaction_briefing_keeps_requirements_files_and_failure(
    tmp_path: Path, repository: Path
) -> None:
    """After compaction the model still sees what the task requires and what failed."""

    check = _calc_repository(repository)
    task = "1. Implement add()\n2. Keep the tests green"
    harness = Harness(
        repository,
        tmp_path,
        [
            _response((_write_call("c1", "calc.py", CALC_WRONG),)),
            _response((_tool_call("c2", "checks_run", {"argv": list(check)}),)),
            _response((_finish_call("c3", "implemented add"),)),
            _response((_write_call("c4", "calc.py", CALC_RIGHT),)),
            _response((_finish_call("c5", "fixed add"),)),
        ],
        verification=(check,),
        task=task,
    )
    harness.kernel.implementation.context = ContextBudget(max_input_tokens=1)
    harness.kernel.implementation._engine_keep_recent_messages = 1
    report = harness.run()

    assert report.verified is True, report.reason
    task_messages: List[str] = []
    for request in harness.provider.requests:
        for message in request.messages:
            content = message.content or ""
            if message.role == "user" and "Implement add()" in content:
                task_messages.append(content)
                break
    assert task_messages, "the task message must be present in every request"
    briefing = task_messages[-1]
    assert briefing.count("[KaroX durable state") == 1
    assert "- R1 Implement add()" in briefing
    assert "- R2 Keep the tests green" in briefing
    assert "Changed files (1): calc.py" in briefing
    assert "tests/test_calc.py:5 · test_add" in briefing
    assert "Verification: `" in briefing


def test_finish_batched_with_work_is_refused_and_work_still_runs(
    tmp_path: Path, repository: Path
) -> None:
    """A submission batched with work must not drop that work or claim it."""

    harness = Harness(
        repository,
        tmp_path,
        [
            _response((_write_call("c1", "a.txt", "hi"),)),
            _response(
                (
                    _finish_call("c2", "created a.txt and b.txt"),
                    _write_call("c3", "b.txt", "hi"),
                )
            ),
            _response((_finish_call("c4", "created a.txt and b.txt"),)),
        ],
    )
    report = harness.run()

    assert (repository / "a.txt").read_text(encoding="utf-8") == "hi"
    assert (repository / "b.txt").read_text(encoding="utf-8") == "hi"
    assert report.verified is True, report.reason
    assert set(report.changed_files) == {"a.txt", "b.txt"}
    observed = "\n".join(_tool_messages(harness))
    assert "finish must be the only tool call" in observed
    assert "b.txt" in observed
    # The observation carries the diff, not the audit metadata.
    assert "previous_sha256" not in observed
    assert "secret_literal_allowed" not in observed


def test_unproducible_obligation_stops_without_duplicate_checks(
    tmp_path: Path, repository: Path
) -> None:
    """A proof no approved command can produce must not loop repair turns."""

    tests = repository / "tests"
    tests.mkdir()
    (tests / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    check = (sys.executable, "-m", "pytest", "-q", "tests/test_ok.py")
    harness = Harness(
        repository,
        tmp_path,
        [
            _response((_write_call("c1", "main.py", "print('hi')\n"),)),
            _response((_finish_call("c2", "created main.py"),)),
        ],
        verification=(check,),
    )
    report = harness.run()

    assert report.verified is False
    assert report.reason == "unproducible_requirement"
    assert any(
        "no approved producer for runtime smoke evidence" in item
        for item in report.unmet
    )
    assert "engine_repair_prompt" not in [
        getattr(event, "reason", None) for event in harness.events
    ]
    checks = [
        entry
        for entry in harness.record().provider_history
        if entry.get("core_name") == "checks.run"
    ]
    assert len(checks) == 1


def test_resume_gets_a_fresh_step_budget(tmp_path: Path, repository: Path) -> None:
    """The configured step limit is this run's budget, not the session's total."""

    read = _tool_call("r1", "repo_read_file", {"path": "seed.txt"})
    first = Harness(
        repository,
        tmp_path,
        [_response((read,))],
        max_steps=1,
    )
    first_report = first.run()
    assert first_report.verified is False
    assert first_report.reason == "step_limit"

    second = Harness(
        repository,
        tmp_path,
        [
            _response((_write_call("c1", "hello.txt", "hi"),)),
            _response((_finish_call("c2", "done"),)),
        ],
        create_session=False,
        max_steps=2,
    )
    report = second.run()

    assert report.verified is True, report.reason


def test_resumed_transcript_keeps_tool_results_next_to_their_calls(
    tmp_path: Path, repository: Path
) -> None:
    """A resumed request must be valid for transports that pair calls/results."""

    read = _tool_call("r1", "repo_read_file", {"path": "seed.txt"})
    first = Harness(repository, tmp_path, [_response((read,))], max_steps=1)
    assert first.run().reason == "step_limit"

    with first.store.mutate("s1", "test", ttl_seconds=30.0) as record:
        record.provider_history.append(
            {
                "role": "assistant",
                "content": "ghost call",
                "tool_calls": [
                    {
                        "call_id": "ghost",
                        "name": "repo_read_file",
                        "raw_arguments": '{"path":"seed.txt"}',
                    }
                ],
            }
        )
        record.task = "follow up: read the seed file"

    second = Harness(
        repository,
        tmp_path,
        [_response((_finish_call("c2", "done"),))],
        create_session=False,
        max_steps=2,
    )
    second.run()

    messages = second.provider.requests[0].messages
    first_assistant = next(
        index for index, message in enumerate(messages) if message.role == "assistant"
    )
    task_index = next(
        index
        for index, message in enumerate(messages)
        if message.role == "user"
        and (message.content or "").endswith("follow up: read the seed file")
    )
    assert task_index < first_assistant

    for index, message in enumerate(messages):
        if message.role != "assistant" or not message.tool_calls:
            continue
        call_ids = {call.call_id for call in message.tool_calls}
        seen = set()
        cursor = index + 1
        while cursor < len(messages) and messages[cursor].role == "tool":
            seen.add(messages[cursor].tool_call_id)
            cursor += 1
        assert call_ids <= seen, (index, call_ids, seen)


def test_failure_map_reads_past_the_display_elision(
    tmp_path: Path, repository: Path
) -> None:
    """A failure in the elided middle of a >1 MB log is still named."""

    code = (
        "import sys\n"
        "sys.stdout.write('x' * 600000 + '\\n')\n"
        "sys.stdout.write('FAILED tests/test_mid.py::test_mid - AssertionError: boom\\n')\n"
        "sys.stdout.write('x' * 600000 + '\\n')\n"
        "sys.exit(1)\n"
    )
    check = (sys.executable, "-c", code)
    harness = Harness(repository, tmp_path, [], verification=(check,))
    lease = harness.store.acquire("s1", "test", ttl_seconds=30.0)
    command = CoreCommand(
        name="checks.run",
        arguments={"argv": list(check)},
        session_id="s1",
        origin=harness.origin,
        correlation_id="map-elision",
        idempotency_key="map-elision",
        deadline_seconds=30.0,
    )
    try:
        result = harness.core.execute(command, lease=lease)
    finally:
        harness.store.release(lease)

    assert result.ok is False
    failure_map = result.data["failure_map"]
    assert failure_map["total"] == 1
    assert failure_map["failures"][0]["path"] == "tests/test_mid.py"
    assert result.data["stdout_truncated"] is True


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


# ------------------------------------------------------- CLI-level integration

def _request_text(request: ModelRequest) -> str:
    return "\n".join(message.content or "" for message in request.messages)


def _request_tools(request: ModelRequest) -> set[str]:
    return {tool.name for tool in request.tools}


def _cli_repo(tmp_path: Path) -> Path:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "test@example.invalid")
    _git(repository, "config", "user.name", "Test")
    _git(repository, "add", "seed.txt")
    _git(repository, "commit", "-q", "-m", "seed")
    return repository


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
        "6",
        "--max-seconds",
        "120",
        "--verification-command",
        json.dumps(["python", "-c", "print('ok')"]),
        "--stream-json",
    ]
    arguments.extend(flags)
    return arguments


def _scripted_cli(monkeypatch: pytest.MonkeyPatch, responses: List[ModelResponse]) -> Any:
    from karox import cli

    provider = ScriptedProvider(responses)
    monkeypatch.setattr(cli, "OpenAIChatCompletionsProvider", lambda *args, **kwargs: provider)
    return provider


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


@pytest.fixture(scope="module")
def mcp_stdio_server(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """One real stdio MCP echo server, discovered once for these tests."""

    from _mcp_support import ECHO_SERVER, child_process_cleanup
    from karox.mcp_client import McpClient, McpRegistry, McpServerRecord

    root = tmp_path_factory.mktemp("engine-cli-mcp")
    repository = root / "repo"
    repository.mkdir()
    record = McpServerRecord(
        server_id="echo",
        namespace="echo",
        transport="stdio",
        command=sys.executable,
        args=(str(ECHO_SERVER),),
        environment={},
        read_only_tools=("echo",),
        timeout_seconds=20.0,
        max_transport_retries=0,
    )
    registry = McpRegistry(root / "mcp-servers.json")
    registry.put(record)
    tools = McpClient(registry).discover_record(record, repository)
    try:
        yield {"record": record, "registry": registry, "tools": tools}
    finally:
        with child_process_cleanup(scripts=[ECHO_SERVER]):
            pass


def test_cli_default_run_executes_the_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI's default run is the engine run: the trajectory proves it."""

    import contextlib
    import io

    from karox import cli

    monkeypatch.setenv("KAROX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KAROX_RUNTIME_DIR", str(tmp_path / "runtime"))
    repository = _cli_repo(tmp_path)
    provider = _scripted_cli(
        monkeypatch,
        [
            _response((_write_call("c1", "hello.txt", "hi"),), content="writing the file"),
            _response((_finish_call("c2", "created hello.txt with hi"),)),
        ],
    )
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = cli.main(_agent_run_args(repository))
    assert code == 0
    events, report = _parse_stream(output.getvalue())
    assert report["verified"] is True
    assert (repository / "hello.txt").read_text(encoding="utf-8") == "hi"
    assert "tool_started" in [event["kind"] for event in events]
    # The engine's own trajectory is the durable identity proof.
    trajectory = (
        tmp_path / "runtime" / "vnext" / "sessions" / "engine-1" / "engine" / "trajectory.json"
    )
    assert trajectory.is_file()
    data = json.loads(trajectory.read_text(encoding="utf-8"))
    agent_blob = json.dumps(data)
    assert "karox.engine_kernel.EngineAgent" in agent_blob
    # The report itself names the loop that ran, so a metrics document (the
    # race harness reads exactly this block) never has to guess.
    engine_block = report["engine"]
    assert engine_block["name"] == "karo-core"
    assert engine_block["version"] == engine_version
    assert engine_block["implementation"] == "karox.engine_kernel.EngineAgent"
    # The engine path advertises typed tools, never a raw shell.
    assert "repo_write_file" in _request_tools(provider.requests[0])


def test_cli_legacy_loop_flag_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--legacy-loop is no longer a CLI switch; the engine is the only kernel."""

    import contextlib
    import io

    from karox import cli

    monkeypatch.setenv("KAROX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KAROX_RUNTIME_DIR", str(tmp_path / "runtime"))
    repository = _cli_repo(tmp_path)

    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        with pytest.raises(SystemExit) as exc:
            cli.main(_agent_run_args(repository, "--legacy-loop"))
    assert exc.value.code != 0
    assert "unrecognized arguments" in output.getvalue()


def test_cli_engine_run_calls_the_selected_mcp_tool(
    mcp_stdio_server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session's selected MCP tool is advertised and callable on the engine path."""

    import contextlib
    import io

    from karox import cli
    from karox.mcp_client import McpRegistry, mcp_selection
    from karox.paths import config_dir, runtime_dir
    from karox.sessions import SessionStore

    monkeypatch.setenv("KAROX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KAROX_RUNTIME_DIR", str(tmp_path / "runtime"))
    (config_dir() / "vnext").mkdir(parents=True, exist_ok=True)
    McpRegistry(config_dir() / "vnext" / "mcp-servers.json").put(mcp_stdio_server["record"])
    repository = _cli_repo(tmp_path)
    task = "create hello.txt with hi"
    store = SessionStore(runtime_dir() / "vnext" / "sessions")
    store.create(repository, task, AccessProfile.WORKSPACE_WRITE, session_id="engine-1")
    selection = mcp_selection(
        mcp_stdio_server["record"], mcp_stdio_server["tools"], {"echo": "allow"}
    )
    with store.mutate("engine-1", "test", ttl_seconds=30.0) as record:
        record.mcp_servers = [selection]

    provider = _scripted_cli(
        monkeypatch,
        [
            _response(
                (_tool_call("c1", "mcp_echo_echo", {"message": "from-the-engine"}),),
                content="calling the bound MCP tool",
            ),
            _response((_write_call("c2", "hello.txt", "hi"),), content="writing the file"),
            _response((_finish_call("c3", "done"),)),
        ],
    )
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = cli.main(_agent_run_args(repository))
    assert code == 0
    # The bound MCP tool is advertised to the model under its provider alias.
    assert "mcp_echo_echo" in _request_tools(provider.requests[0])
    # The real stdio server answered; the observation rode back into history.
    assert "from-the-engine" in _request_text(provider.requests[1])
    _, report = _parse_stream(output.getvalue())
    assert report["verified"] is True
    assert (repository / "hello.txt").read_text(encoding="utf-8") == "hi"


def test_cli_engine_run_auto_activates_and_advertises_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Triggered recipes ride the prompt and skill.run is a typed tool."""

    import contextlib
    import io

    from karox import cli
    from karox.paths import runtime_dir

    monkeypatch.setenv("KAROX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KAROX_RUNTIME_DIR", str(tmp_path / "runtime"))
    repository = _cli_repo(tmp_path)
    skill = repository / ".karox" / "skills" / "greet-feature" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\n"
        "name: greet-feature\n"
        "version: 1.0.0\n"
        "description: Greet the reviewer politely.\n"
        "when_to_use: the task asks for a greeting.\n"
        "triggers: [greet]\n"
        "---\n"
        "# Greet recipe\n"
        "GREET RECIPE BODY: open with a polite hello.\n",
        encoding="utf-8",
    )
    provider = _scripted_cli(
        monkeypatch,
        [
            _response((_write_call("c1", "hello.txt", "hi"),), content="writing the file"),
            _response((_finish_call("c2", "done"),)),
        ],
    )
    arguments = _agent_run_args(repository)
    arguments[arguments.index("--task") + 1] = "greet the reviewer and create hello.txt with hi"
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = cli.main(arguments)
    assert code == 0
    # The activated recipe rides the system prompt the engine renders.
    assert "GREET RECIPE BODY" in _request_text(provider.requests[0])
    # The session-bound runtime advertises the leased skill.run tool.
    assert "skill_run" in _request_tools(provider.requests[0])
    audit = runtime_dir() / "vnext" / "skill-audit" / "engine-1.jsonl"
    assert audit.is_file()
    assert "greet-feature" in audit.read_text(encoding="utf-8")


def test_reasoning_summary_and_compaction_reach_the_observer(
    tmp_path: Path, repository: Path
) -> None:
    """Every event the kernel emits must construct ``AgentEvent``.

    The reasoning-summary channel and the compaction line each used a field the
    event dataclass did not have; a single TypeError used to mute the observer
    for the rest of the run, so the UI stopped updating with no error anywhere.
    This pins both the fields and the survival of the observer.
    """
    harness = Harness(
        repository,
        tmp_path,
        [_response((_finish_call("c1", "done"),))],
    )
    kernel = harness.kernel.implementation
    kernel._model_event("reasoning_summary_delta", reasoning_summary_delta="plan")
    kernel._on_compacted(1200, 400, 1)

    kinds = harness.kinds()
    assert "reasoning_summary_delta" in kinds
    assert kinds[-1] == "compacted"
    summaries = [
        event
        for event in harness.events
        if event.kind is AgentEventKind.REASONING_SUMMARY_DELTA
    ]
    assert summaries[-1].reasoning_summary_delta == "plan"
    compacted = [
        event for event in harness.events if event.kind is AgentEventKind.COMPACTED
    ][-1]
    assert "compact" in (compacted.summary or "")

    # One unrenderable event is dropped; the observer stays attached.
    kernel._emit(AgentEventKind.WARNING, unknown_field="x")  # type: ignore[arg-type]
    kernel._emit(AgentEventKind.WARNING, summary="still alive")
    assert harness.events[-1].summary == "still alive"


def test_agent_event_carries_the_reasoning_summary_field() -> None:
    """The TUI protocol reads ``reasoning_summary_delta`` from the JSON event."""
    from dataclasses import asdict

    event = AgentEvent(
        AgentEventKind.REASONING_SUMMARY_DELTA, 1, reasoning_summary_delta="thinking"
    )
    assert asdict(event)["reasoning_summary_delta"] == "thinking"


def test_system_prompt_only_asks_for_arguments_the_tools_have() -> None:
    """The prompt must not demand a schema field, or prose answers, the engine refuses.

    ``more_semantic_changes`` was a requirement of the retired loop: no tool
    schema carries it, so a model that obeyed the prompt produced rejected
    calls, and the same paragraph told the model to answer without a tool call
    while the engine contract calls that a format error.
    """
    lowered = SYSTEM_PROMPT.lower()
    assert "more_semantic_changes" not in SYSTEM_PROMPT
    assert "no tool call" not in lowered


# --------------------------------------------------------------------------- #
# Failure intelligence                                                        #
# --------------------------------------------------------------------------- #

CALC_TEST = """\
import calc


def test_add():
    assert calc.add(2, 2) == 4
"""
CALC_WRONG = "def add(a, b):\n    return a - b\n"
CALC_WRONG_AGAIN = "def add(a, b):\n    return a - b - 0\n"
CALC_RIGHT = "def add(a, b):\n    return a + b\n"


def _calc_repository(repository: Path) -> tuple[str, ...]:
    """A committed pytest file whose failure names its own file and line."""

    tests = repository / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("", encoding="utf-8")
    (tests / "test_calc.py").write_text(CALC_TEST, encoding="utf-8")
    _git(repository, "add", "tests")
    _git(repository, "commit", "-q", "-m", "calc test")
    return (sys.executable, "-m", "pytest", "-q", "tests/test_calc.py")


def _tool_messages(harness: "Harness") -> List[str]:
    """Every string the provider was actually asked to read as a tool result."""

    seen: List[str] = []
    for request in harness.provider.requests:
        for message in request.messages:
            if message.role == "tool" and message.content:
                seen.append(message.content)
    return seen


def _user_messages(harness: "Harness") -> List[str]:
    seen: List[str] = []
    for request in harness.provider.requests:
        for message in request.messages:
            if message.role == "user" and message.content:
                seen.append(message.content)
    return seen


def test_failed_check_hands_the_model_a_parsed_failure_map(
    tmp_path: Path, repository: Path
) -> None:
    """The model must see file:line of the failing test, not the tail of a log."""

    check = _calc_repository(repository)
    harness = Harness(
        repository,
        tmp_path,
        [
            _response((_write_call("c1", "calc.py", CALC_WRONG),)),
            _response((_tool_call("c2", "checks_run", {"argv": list(check)}),)),
            _response((_finish_call("c3", "implemented add"),)),
            _response((_write_call("c4", "calc.py", CALC_RIGHT),)),
            _response((_finish_call("c5", "fixed add"),)),
        ],
        verification=(check,),
        runtime_cls=ExtendedCoreRuntime,
    )
    report = harness.run()

    assert report.verified is True, report.reason
    assert report.unmet == ()
    assert report.failure_map is None
    record = harness.record()
    failed = [
        entry
        for entry in record.provider_history
        if entry.get("core_name") == "checks.run"
        and entry.get("result", {}).get("ok") is False
    ]
    assert failed, "the fixture must have failed once before the repair"
    data = failed[0]["result"]["data"]
    failure_map = data["failure_map"]
    assert failure_map["total"] == 1
    failure = failure_map["failures"][0]
    assert failure["test_id"] == "tests/test_calc.py::test_add"
    assert failure["path"] == "tests/test_calc.py"
    assert failure["line"] == 5
    assert "assert" in failure["message"]

    # The same turn the model read carried the map, not only the record.
    observed = "\n".join(_tool_messages(harness))
    assert "KaroX failure map" in observed
    assert "tests/test_calc.py:5" in observed
    # The repair turn named the failing test and a copy-ready rerun.
    repair = [
        message
        for message in _user_messages(harness)
        if "Verification failed after your finish" in message
    ]
    assert repair, "a failed check must produce a repair turn"
    assert "tests/test_calc.py:5" in repair[-1]
    assert 'Targeted rerun: tests.run suite=focused targets=["tests/test_calc.py::test_add"]' in repair[-1]

    # The repair edit came back with its own verdict: the exact failing test
    # was rerun advisory, so the model did not spend a turn asking.
    repair_edit = next(
        entry for entry in record.provider_history if entry.get("tool_call_id") == "c4"
    )
    affected = repair_edit["result"]["data"]["affected_tests"]
    assert affected["advisory"] is True
    assert affected["ok"] is True
    assert affected["targets"] == ["tests/test_calc.py::test_add"]
    assert "verification_eligible" not in affected
    assert "affected tests: passed" in repair_edit["content"]
    # No extra approved check and no fabricated tool entry: this is feedback,
    # not a second verification pass.
    assert (
        len([e for e in record.provider_history if e.get("core_name") == "checks.run"]) == 2
    )
    assert not any(
        entry.get("core_name") == "tests.run" for entry in record.provider_history
    )


def test_repeated_identical_failure_is_called_out_in_the_repair_turn(
    tmp_path: Path, repository: Path
) -> None:
    """An edit that changed no failure must not be retried as if it were new."""

    check = _calc_repository(repository)
    harness = Harness(
        repository,
        tmp_path,
        [
            _response((_write_call("c1", "calc.py", CALC_WRONG),)),
            _response((_tool_call("c2", "checks_run", {"argv": list(check)}),)),
            _response((_finish_call("c3", "implemented add"),)),
            _response((_write_call("c4", "calc.py", CALC_WRONG_AGAIN),)),
            _response((_finish_call("c5", "tried again"),)),
            _response((_finish_call("c6", "done"),)),
        ],
        verification=(check,),
    )
    report = harness.run()

    assert report.verified is False
    # The report itself names why and where: no session digging required.
    assert report.unmet
    assert report.failure_map is not None
    assert report.failure_map["total"] == 1
    assert report.failure_map["failures"][0]["path"] == "tests/test_calc.py"
    assert report.failure_map["failures"][0]["line"] == 5
    repairs = [
        message
        for message in _user_messages(harness)
        if "Verification failed after your finish" in message
    ]
    assert len(repairs) >= 2
    assert "did not alter it" in repairs[-1]
    assert "same failure set is still present after 1 earlier run(s)" in repairs[-1]


def test_focused_test_failure_is_mapped_for_the_model(
    tmp_path: Path, repository: Path
) -> None:
    """A red focused run must name file:line too, not arrive as a JSON blob."""

    check = _calc_repository(repository)
    harness = Harness(
        repository,
        tmp_path,
        [
            _response((_write_call("c1", "calc.py", CALC_WRONG),)),
            _response((_tool_call("c2", "tests_run", {"suite": "focused"}),)),
            _response((_write_call("c3", "calc.py", CALC_RIGHT),)),
            _response((_finish_call("c4", "implemented add"),)),
        ],
        verification=(check,),
        runtime_cls=ExtendedCoreRuntime,
    )
    report = harness.run()

    assert report.verified is True, report.reason
    record = harness.record()
    runs = [
        entry
        for entry in record.provider_history
        if entry.get("core_name") == "tests.run"
    ]
    assert len(runs) == 1
    data = runs[0]["result"]["data"]
    assert data["targets_source"] == "session_changes"
    assert data["failure_map"]["total"] == 1
    observed = "\n".join(_tool_messages(harness))
    assert "KaroX failure map" in observed
    assert "tests/test_calc.py:5" in observed
    assert "[tests.run exit 1" in observed


def test_domain_tool_schemas_are_not_advertised_for_a_plain_repair(
    tmp_path: Path, repository: Path
) -> None:
    """A pytest repair must not pay for web, disk or batch schemas.

    The tools stay callable and keep their capability gate; only the request's
    advertised catalog shrinks. This is the measured fixed-cost lever: on the
    ledger task none of the hidden tools was ever called, yet their JSON rode
    in every one of the eight requests.
    """

    check = _calc_repository(repository)
    (repository / "calc.py").write_text(CALC_RIGHT, encoding="utf-8")
    harness = Harness(
        repository,
        tmp_path,
        [_response((_finish_call("c1", "already correct"),))],
        verification=(check,),
        runtime_cls=ExtendedCoreRuntime,
        task="fix the failing add function in calc.py",
    )
    harness.run()

    names = {tool.name for tool in harness.provider.requests[0].tools}
    assert "repo_edit_file" in names
    assert "repo_command" not in names
    assert "web_fetch" not in names
    assert "disk_scan" not in names


def test_a_task_that_names_the_domain_gets_its_tool(
    tmp_path: Path, repository: Path
) -> None:
    """The same repository advertises the batch tool when the task asks for it."""

    check = _calc_repository(repository)
    (repository / "calc.py").write_text(CALC_RIGHT, encoding="utf-8")
    harness = Harness(
        repository,
        tmp_path / "second",
        [_response((_finish_call("c1", "already correct"),))],
        verification=(check,),
        runtime_cls=ExtendedCoreRuntime,
        task="rename the module files in one atomic batch",
    )
    harness.run()

    names = {tool.name for tool in harness.provider.requests[0].tools}
    assert "repo_command" in names


def _big_file(repository: Path) -> Path:
    path = repository / "big.py"
    path.write_text("value = 1\n" + "# filler\n" * 800 + "tail = 'end'\n", encoding="utf-8")
    return path


def test_a_read_is_marked_stale_after_an_edit_of_the_same_file(
    tmp_path: Path, repository: Path
) -> None:
    """The full original read is not re-sent after the file changed."""

    big = _big_file(repository)
    harness = Harness(
        repository,
        tmp_path,
        [
            _response((_tool_call("c1", "repo_read_file", {"path": "big.py"}),)),
            _response(
                (
                    _tool_call(
                        "c2",
                        "repo_edit_file",
                        {
                            "path": "big.py",
                            "old_string": "value = 1",
                            "new_string": "value = 2",
                        },
                    ),
                )
            ),
            _response((_finish_call("c3", "fixed"),)),
        ],
        runtime_cls=ExtendedCoreRuntime,
    )
    report = harness.run()

    assert report.verified is True, report.reason
    assert big.read_text(encoding="utf-8").startswith("value = 2\n")
    final_tools = "\n".join(
        str(message.content or "")
        for message in harness.provider.requests[-1].messages
        if message.role == "tool"
    )
    assert "predates a later edit" in final_tools
    assert "tail = 'end'" in final_tools


def test_a_read_after_the_edit_is_not_marked_stale(
    tmp_path: Path, repository: Path
) -> None:
    _big_file(repository)
    harness = Harness(
        repository,
        tmp_path / "after",
        [
            _response(
                (
                    _tool_call(
                        "c1",
                        "repo_edit_file",
                        {
                            "path": "big.py",
                            "old_string": "value = 1",
                            "new_string": "value = 2",
                        },
                    ),
                )
            ),
            _response((_tool_call("c2", "repo_read_file", {"path": "big.py"}),)),
            _response((_finish_call("c3", "fixed"),)),
        ],
        runtime_cls=ExtendedCoreRuntime,
    )
    report = harness.run()

    assert report.verified is True, report.reason
    final_tools = "\n".join(
        str(message.content or "")
        for message in harness.provider.requests[-1].messages
        if message.role == "tool"
    )
    assert "predates a later edit" not in final_tools
    assert "value = 2" in final_tools


def test_mutation_observation_keeps_only_the_decision_facts() -> None:
    """The model reads the diff, not the audit metadata.

    Mutation results used to reach the model as a JSON blob with the whole
    data dict: hashes, previous hashes and evidence ids were re-sent on every
    later request. The full result still lives in the session record.
    """

    from karox.engine_tool_env import _mutation_body

    body = _mutation_body(
        "repo.edit_file",
        {
            "path": "calc.py",
            "changed": True,
            "replacements": 1,
            "bytes": 42,
            "diff": "--- a/calc.py\n+++ b/calc.py\n@@ -1 +1 @@\n-1\n+2\n",
            "quick_checks_line": "quick checks: py_compile ok",
            "affected_tests_line": "affected tests: passed (advisory, not evidence)",
            "previous_sha256": "a" * 64,
            "sha256": "b" * 64,
            "secret_literal_allowed": False,
        },
        True,
    )

    assert body.startswith("[repo.edit_file calc.py]")
    assert "changed · 1 replacement(s) · 42 bytes" in body
    assert "-1" in body and "+2" in body
    assert "quick checks: py_compile ok" in body
    assert "affected tests: passed" in body
    assert "sha256" not in body
    assert "secret_literal_allowed" not in body
    assert len(body) < 600


def test_one_advisory_rerun_per_batched_response(
    tmp_path: Path, repository: Path
) -> None:
    """Three edits in one response must not run the suite three times.

    The stale earlier results are not just wasted work: they arrive as red
    tool results beside the current one, so the model reads a failure set that
    no longer describes the workspace.
    """

    check = _calc_repository(repository)
    harness = Harness(
        repository,
        tmp_path,
        [
            _response((_write_call("c1", "calc.py", CALC_WRONG),)),
            _response((_tool_call("c2", "checks_run", {"argv": list(check)}),)),
            _response(
                (
                    _write_call("c3", "helpers_a.py", "A = 1\n"),
                    _write_call("c4", "helpers_b.py", "B = 2\n"),
                    _write_call("c5", "calc.py", CALC_RIGHT),
                )
            ),
            _response((_finish_call("c6", "implemented add"),)),
        ],
        verification=(check,),
        runtime_cls=ExtendedCoreRuntime,
    )
    report = harness.run()

    assert report.verified is True, report.reason
    record = harness.record()
    affected = [
        entry
        for entry in record.provider_history
        if "affected_tests" in ((entry.get("result") or {}).get("data") or {})
    ]
    assert [entry.get("tool_call_id") for entry in affected] == ["c5"]
    assert affected[0]["result"]["data"]["affected_tests"]["ok"] is True
    advisory = [item for item in record.checks if item.get("suite") == "focused"]
    assert len(advisory) == 1
    assert advisory[0]["ok"] is True


def test_focused_tests_derive_targets_from_the_session_changes(
    tmp_path: Path, repository: Path
) -> None:
    """A focused run with no targets must use the changed files, not refuse."""

    check = _calc_repository(repository)
    harness = Harness(
        repository,
        tmp_path,
        [
            _response((_write_call("c1", "calc.py", CALC_RIGHT),)),
            _response((_tool_call("c2", "tests_run", {"suite": "focused"}),)),
            _response((_finish_call("c3", "implemented add"),)),
        ],
        verification=(check,),
        runtime_cls=ExtendedCoreRuntime,
    )
    report = harness.run()

    assert report.verified is True, report.reason
    record = harness.record()
    write = next(
        entry for entry in record.provider_history if entry.get("tool_call_id") == "c1"
    )
    assert "affected_tests" not in write["result"]["data"]
    # The durable snapshot is a compaction aid, not prompt decoration: a short
    # run that never compacted must not pay for it.
    assert not any(
        "KaroX durable state" in (message.content or "")
        for request in harness.provider.requests
        for message in request.messages
    )
    runs = [
        entry
        for entry in record.provider_history
        if entry.get("core_name") == "tests.run"
    ]
    assert len(runs) == 1
    data = runs[0]["result"]["data"]
    assert data["suite"] == "focused"
    assert data["targets"] == ["tests/test_calc.py"]
    assert data["targets_source"] == "session_changes"


def test_one_model_reply_may_take_longer_than_two_minutes() -> None:
    """A whole-file answer is a normal reply, not a hung request.

    The first aquarium attempt streamed 34k completion tokens and was killed at
    120 s mid-answer. The cap now allows five minutes while still shrinking to
    whatever time the run has left.
    """

    assert _model_request_deadline(3600.0) == 300.0
    assert _model_request_deadline(42.0) == 42.0
    assert _model_request_deadline(0.0) == 5.0
