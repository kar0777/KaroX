import asyncio
import contextlib
import json
import shlex
import shutil
import subprocess
import sys

import pytest

from karox6.context.compaction import COMPACTION_INSTRUCTION, split_for_summary
from karox6.core.events import RunFinished
from karox6.core.messages import Message, Price, ToolCall, Usage
from karox6.core.session import Session, SessionConfig
from karox6.core.store import (
    UNKNOWN_RESULT,
    SessionStore,
    close_dangling_calls,
)
from karox6.providers.base import ProviderError, RetryPolicy
from karox6.providers.scripted import ScriptedProvider, Turn
from karox6.tools.base import ToolResult
from karox6.tools.danger import DangerMatch


def config(tmp_path, **kwargs):
    return SessionConfig(model="test", workspace=tmp_path, home=tmp_path / "home", **kwargs)


def run(session, prompt="task"):
    async def collect():
        return [event async for event in session.run(prompt)]

    return asyncio.run(collect())


def finish(events):
    assert isinstance(events[-1], RunFinished)
    return events[-1]


def results(session):
    return [m for m in session.messages if m.role == "tool"]


def test_final_text(tmp_path):
    provider = ScriptedProvider([Turn(text="done")])
    events = run(Session(config(tmp_path), provider))
    assert events[0].type == "run_started"
    assert events[-1].type == "run_finished"
    assert finish(events).status == "completed"
    assert finish(events).final_text == "done"
    assert len(provider.requests) == 1


def test_tool_round_trip(tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    provider = ScriptedProvider([Turn(calls=[("read", {"file_path": "a.txt"})]), Turn(text="done")])
    session = Session(config(tmp_path), provider)
    assert finish(run(session)).status == "completed"
    assistant, tool = provider.requests[1].messages[-2:]
    assert assistant.tool_calls[0].id == tool.tool_call_id
    assert "hello" in tool.content


class RecordingTool:
    description = "record execution"
    parameters = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }

    def __init__(self, name, read_only, trace):
        self.name, self.read_only, self.trace = name, read_only, trace

    async def run(self, args, ctx):
        value = args["value"]
        self.trace.append(("start", value))
        await asyncio.sleep(0.02 if value != "a" else 0.04)
        self.trace.append(("end", value))
        return ToolResult(value)


def test_parallel_and_sequential_order(tmp_path):
    trace = []
    tools = [RecordingTool("read", True, trace), RecordingTool("write", False, trace)]
    provider = ScriptedProvider(
        [
            Turn(
                calls=[
                    ("read", {"value": "a"}),
                    ("read", {"value": "b"}),
                    ("write", {"value": "c"}),
                    ("write", {"value": "d"}),
                ]
            ),
            Turn(text="done"),
        ]
    )
    session = Session(config(tmp_path), provider, tools=tools)
    assert finish(run(session)).status == "completed"
    assert trace[:2] == [("start", "a"), ("start", "b")]
    assert trace[4:] == [("start", "c"), ("end", "c"), ("start", "d"), ("end", "d")]
    assert [m.content for m in results(session)] == ["a", "b", "c", "d"]


@pytest.mark.parametrize("name", ["Read", "functions.read"])
def test_normalized_tool_name(tmp_path, name):
    (tmp_path / "a").write_text("hello")
    session = Session(
        config(tmp_path),
        ScriptedProvider([Turn(calls=[(name, {"file_path": "a"})]), Turn(text="done")]),
    )
    assert finish(run(session)).status == "completed"
    assert "hello" in results(session)[0].content


def test_unknown_tool(tmp_path):
    session = Session(
        config(tmp_path), ScriptedProvider([Turn(calls=[("missing", {})]), Turn(text="done")])
    )
    assert finish(run(session)).status == "completed"
    assert results(session)[0].is_error
    assert "Available tools" in results(session)[0].content


@pytest.mark.parametrize("args", ["{", {}])
def test_invalid_arguments(tmp_path, args):
    session = Session(
        config(tmp_path), ScriptedProvider([Turn(calls=[("read", args)]), Turn(text="done")])
    )
    assert finish(run(session)).status == "completed"
    assert results(session)[0].is_error
    assert any(
        s in results(session)[0].content.lower() for s in ("invalid arguments", "not valid json")
    )


def test_loop_notices(tmp_path):
    turns = [Turn(calls=[("read", {"file_path": "none"})]) for _ in range(8)] + [Turn(text="done")]
    session = Session(config(tmp_path), ScriptedProvider(turns))
    events = run(session)
    notices = [e for e in events if e.type == "notice"]
    assert len(notices) == 3
    assert all(e.kind == "loop_guard" for e in notices)
    assert notices[0].text.startswith("You are repeating")
    assert "consecutive_calls: 5" in notices[1].text
    assert "consecutive_calls: 8" in notices[2].text
    assert len([m for m in session.messages if m.meta.get("kind") == "loop_guard"]) == 3


def test_loop_hard_stop(tmp_path):
    session = Session(
        config(tmp_path, loop_hard_stop=4),
        ScriptedProvider([Turn(calls=[("read", {"file_path": "a"})]) for _ in range(4)]),
    )
    assert finish(run(session)).status == "loop"


def test_loop_resets(tmp_path):
    turns = [Turn(calls=[("read", {"file_path": x})]) for x in ["a", "a", "b", "a", "a"]] + [
        Turn(text="done")
    ]
    assert not [
        e for e in run(Session(config(tmp_path), ScriptedProvider(turns))) if e.type == "notice"
    ]


@pytest.mark.parametrize(("count", "status"), [(1, "completed"), (4, "error")])
def test_truncation(tmp_path, count, status):
    provider = ScriptedProvider(
        [Turn(text="partial", finish_reason="length") for _ in range(count)] + [Turn(text="done")]
    )
    events = run(Session(config(tmp_path), provider))
    assert finish(events).status == status
    assert len([e for e in events if e.type == "notice" and e.kind == "truncated"]) == min(count, 3)


def test_step_limit(tmp_path):
    provider = ScriptedProvider([Turn(calls=[("read", {"file_path": "a"})]) for _ in range(2)])
    result = finish(run(Session(config(tmp_path, max_steps=2), provider)))
    assert result.status == "limit"
    assert "step limit" in result.reason


def test_time_limit(tmp_path):
    class SlowProvider(ScriptedProvider):
        async def complete(self, request, on_delta=None):
            await asyncio.sleep(1.2)
            return await super().complete(request, on_delta)

    assert (
        finish(
            run(Session(config(tmp_path, max_seconds=0.01), SlowProvider([Turn(text="done")])))
        ).status
        == "limit"
    )


@pytest.mark.parametrize(
    ("code", "status", "event"), [("rate_limit", "completed", "retry"), ("auth", "error", "error")]
)
def test_provider_errors(tmp_path, code, status, event):
    provider = ScriptedProvider([Turn(error=ProviderError(code, "failed")), Turn(text="done")])
    events = run(Session(config(tmp_path, retry=RetryPolicy(initial_delay_s=0.001)), provider))
    assert finish(events).status == status
    assert any(e.type == event for e in events)


def test_persistence_resume(tmp_path):
    cfg = config(tmp_path)
    original = Session(cfg, ScriptedProvider([Turn(text="first")]))
    run(original, "old prompt")
    records = [
        json.loads(line) for line in original.store.path.read_text(encoding="utf-8").splitlines()
    ]
    assert {"meta", "message", "usage", "event"} <= {r["kind"] for r in records}
    loaded = SessionStore.load(cfg.home, original.session_id)
    provider = ScriptedProvider([Turn(text="second")])
    resumed = Session(
        cfg, provider, session_id=loaded.session_id, messages=loaded.messages, usage=loaded.usage
    )
    events = run(resumed, "new prompt")
    assert events[0].resumed
    assert [m.content for m in provider.requests[0].messages] == [
        "old prompt",
        "first",
        "new prompt",
    ]


def test_dangling_calls_are_answered_and_finished_results_kept():
    messages = [
        Message("user", "task"),
        Message("assistant", tool_calls=[ToolCall("a", "edit", "{}"), ToolCall("b", "read", "{}")]),
        Message("tool", "Edited x.py", tool_call_id="a"),
    ]
    closed = close_dangling_calls(messages)
    assert closed[:3] == messages
    assert closed[3].tool_call_id == "b"
    # Loaded after a hard stop: the call may have run, so "cancelled" would be a guess.
    assert closed[3].content == UNKNOWN_RESULT
    assert close_dangling_calls(closed) == closed


def test_dangling_calls_are_answered_in_every_turn_not_only_the_last():
    # Resumed once (repair kept in memory only), then more turns were appended.
    messages = [
        Message("user", "task"),
        Message("assistant", tool_calls=[ToolCall("a", "bash", "{}"), ToolCall("b", "bash", "{}")]),
        Message("tool", "ok", tool_call_id="a"),
        Message("user", "continue"),
        Message("assistant", tool_calls=[ToolCall("c", "read", "{}")]),
        Message("tool", "text", tool_call_id="c"),
    ]
    closed = close_dangling_calls(messages)
    assert [m.tool_call_id for m in closed] == [None, None, "a", "b", None, None, "c"]
    assert closed[3].content == UNKNOWN_RESULT
    assert close_dangling_calls(closed) == closed


@pytest.mark.parametrize(
    ("mode", "expected"), [("turn", [None, "new"]), ("none", [None, None]), ("all", ["old", "new"])]
)
def test_reasoning_replay(tmp_path, mode, expected):
    messages = [
        Message("user", "old task"),
        Message("assistant", "old", reasoning="old"),
        Message("user", "new task"),
        Message("assistant", "new", reasoning="new"),
        Message("user", "notice", meta={"kind": "loop_guard"}),
    ]
    session = Session(
        config(tmp_path, replay_reasoning=mode), ScriptedProvider([]), messages=messages
    )
    assert [m.reasoning for m in session._request().messages if m.role == "assistant"] == expected


def test_compaction_prune_and_summary(tmp_path):
    call = ToolCall("id", "read", "{}")
    messages = [
        Message("user", "original"),
        Message("assistant", tool_calls=[call]),
        Message("tool", "x" * 20000, tool_call_id="id"),
    ] + [Message("assistant", "tail" * 100) for _ in range(7)]
    provider = ScriptedProvider([Turn(text="checkpoint"), Turn(text="done")])
    session = Session(config(tmp_path, compact_at_tokens=100), provider, messages=messages)
    (tmp_path / "changed.txt").write_text("changed", encoding="utf-8")
    session.files.record_write(tmp_path / "changed.txt")
    events = run(session, "continue")
    assert [e.mode for e in events if e.type == "compacted"] == ["prune", "summary"]
    assert provider.requests[0].messages[-1].content == COMPACTION_INSTRUCTION
    assert any(m.meta.get("pruned") for m in provider.requests[0].messages)
    assert session.messages[0].content == "original"
    checkpoint = session.messages[1]
    assert checkpoint.meta["kind"] == "compaction"
    assert "<compacted-summary>" in checkpoint.content
    assert "Files changed in the workspace" in checkpoint.content
    assert "changed.txt" in checkpoint.content


def test_compaction_keeps_tool_pair():
    pair = [
        Message("assistant", tool_calls=[ToolCall("id", "read", "{}")]),
        Message("tool", "result", tool_call_id="id"),
    ]
    head, tail = split_for_summary([Message("user", "task"), *pair], retain_tokens=8)
    assert (pair[0] in head and pair[1] in head) or (pair[0] in tail and pair[1] in tail)


def test_denied_command(tmp_path):
    session = Session(
        config(tmp_path),
        ScriptedProvider([Turn(calls=[("bash", {"command": "rm -rf /"})]), Turn(text="done")]),
    )
    events = run(session)
    assert any(e.type == "approval_requested" for e in events)
    assert any(e.type == "approval_resolved" and not e.approved for e in events)
    assert "Not executed" in results(session)[0].content


def test_approved_harmless_command(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "karox6.tools.shell.check_command", lambda *a, **kw: DangerMatch("test", "test", "test")
    )

    async def approve(_request):
        return True

    session = Session(
        config(tmp_path),
        ScriptedProvider([Turn(calls=[("bash", {"command": "echo safe"})]), Turn(text="done")]),
        approver=approve,
    )
    events = run(session)
    assert any(e.type == "approval_resolved" and e.approved for e in events)
    assert "safe" in results(session)[0].content


def test_background_finish_notice(tmp_path):
    python = shlex.quote(sys.executable.replace("\\", "/"))
    sleep = python + " -c " + shlex.quote("import time; time.sleep(0.3)")
    wait = python + " -c " + shlex.quote("import time; time.sleep(0.8)")
    provider = ScriptedProvider(
        [
            Turn(calls=[("bash", {"command": sleep, "run_in_background": True})]),
            Turn(calls=[("bash", {"command": wait})]),
            Turn(text="done"),
        ]
    )
    session = Session(config(tmp_path), provider)
    assert finish(run(session)).status == "completed"
    assert "[background job 1 finished" in results(session)[-1].content


def test_files_changed_write_edit(tmp_path):
    provider = ScriptedProvider(
        [
            Turn(
                calls=[
                    ("write", {"file_path": "a.txt", "content": "old"}),
                    ("edit", {"file_path": "a.txt", "old_string": "old", "new_string": "new"}),
                ]
            ),
            Turn(text="done"),
        ]
    )
    result = finish(run(Session(config(tmp_path), provider)))
    assert "a.txt" in result.files_changed
    assert (tmp_path / "a.txt").read_text() == "new"


def test_files_changed_shell_git(tmp_path):
    if not shutil.which("git"):
        pytest.skip("git unavailable")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    python = shlex.quote(sys.executable.replace("\\", "/"))
    command = (
        python
        + " -c "
        + shlex.quote("from pathlib import Path; Path('created.txt').write_text('hi')")
    )
    session = Session(
        config(tmp_path),
        ScriptedProvider([Turn(calls=[("bash", {"command": command})]), Turn(text="done")]),
    )
    assert "created.txt" in finish(run(session)).files_changed


def test_cost(tmp_path):
    usage = Usage(input_tokens=1000, cached_tokens=200, output_tokens=500)
    result = finish(
        run(
            Session(
                config(tmp_path, price=Price(1.0, 2.0, 0.1)),
                ScriptedProvider([Turn(text="done", usage=usage)]),
            )
        )
    )
    assert result.cost_usd == pytest.approx((800 + 20 + 1000) / 1_000_000)


def test_read_grep_glob_batch(tmp_path):
    (tmp_path / "sample.txt").write_text("needle", encoding="utf-8")
    provider = ScriptedProvider(
        [
            Turn(
                calls=[
                    ("read", {"file_path": "sample.txt"}),
                    ("grep", {"pattern": "needle"}),
                    ("glob", {"pattern": "*.txt"}),
                ]
            ),
            Turn(text="done"),
        ]
    )
    session = Session(config(tmp_path), provider)
    assert finish(run(session)).status == "completed"
    assert [m.name for m in results(session)] == ["read", "grep", "glob"]
    assert all(not m.is_error for m in results(session))
    assert "needle" in results(session)[1].content
    assert "sample.txt" in results(session)[2].content


def test_finish_writes_snapshot_and_ignores_own_home(tmp_path):
    provider = ScriptedProvider(
        [Turn(calls=[("write", {"file_path": "pkg/a.txt", "content": "new"})]), Turn(text="ok")]
    )
    session = Session(config(tmp_path), provider)
    result = finish(run(session))
    assert result.files_changed == ["pkg/a.txt"]
    manifest = json.loads(
        (session.store.dir / "submissions" / "01" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "completed"
    assert list(manifest["files"]) == ["pkg/a.txt"]
    copied = session.store.dir / "submissions" / "01" / "files" / "pkg" / "a.txt"
    assert copied.read_text(encoding="utf-8") == "new"


def test_snapshot_can_be_disabled(tmp_path):
    session = Session(
        config(tmp_path, snapshot_on_finish=False), ScriptedProvider([Turn(text="ok")])
    )
    finish(run(session))
    assert not (session.store.dir / "submissions").exists()


def test_wrap_up_notices_on_long_runs(tmp_path, monkeypatch):
    from karox6.core import session as session_module

    monkeypatch.setattr(session_module, "FINAL_CALL_STEPS", 2)
    # different files, so the loop guard stays quiet
    turns = [Turn(calls=[("read", {"file_path": f"f{i}"})]) for i in range(6)] + [Turn(text="done")]
    session = Session(config(tmp_path, wrap_up_steps=2), ScriptedProvider(turns))
    events = run(session)
    notices = [e for e in events if e.type == "notice" and e.kind == "wrap_up"]
    assert [n.text.split(".")[0] for n in notices] == [
        "This run has taken 2 steps, and every further step re-sends the whole conversation",
        "This run has taken 4 steps",
    ]
    assert "without calling more tools" in notices[1].text
    assert finish(events).status == "completed"
    quiet = Session(config(tmp_path, wrap_up_steps=None), ScriptedProvider(list(turns)))
    assert not [e for e in run(quiet) if e.type == "notice"]


def test_deadline_notice_comes_once_near_the_time_limit(tmp_path, monkeypatch):
    import time as real_time

    from karox6.core import session as session_module

    clock = [1000.0]

    class FakeTime:  # the session's clock only; the event loop keeps the real one
        def __getattr__(self, name):
            return getattr(real_time, name)

        def monotonic(self):
            return clock[0]

    monkeypatch.setattr(session_module, "time", FakeTime())

    class Ticking(ScriptedProvider):
        async def complete(self, request, on_delta=None):
            clock[0] += 100.0  # every model call takes 100 s
            return await super().complete(request, on_delta)

    turns = [Turn(calls=[("read", {"file_path": f"f{i}"})]) for i in range(5)] + [Turn(text="done")]
    cfg = config(tmp_path, max_seconds=1000.0, deadline_notice_s=750)
    events = run(Session(cfg, Ticking(turns)))
    notices = [e for e in events if e.type == "notice" and e.kind == "deadline"]
    assert len(notices) == 1
    assert notices[0].text.startswith(
        "Time check: about 12 minutes remain of this run's 17-minute time limit"
    )
    assert finish(events).status == "completed"
    quiet = Session(config(tmp_path, max_seconds=1000.0), Ticking(list(turns)))
    assert not [e for e in run(quiet) if e.type == "notice"]


def test_step_limits_by_command(tmp_path):
    from karox6 import cli

    def built(*argv):
        args = cli._parser().parse_args([*argv, "--model", "m"])
        return cli._session_config(args, tmp_path, tmp_path / "home", stream_deltas=False)

    assert built("run", "t").max_steps is None
    assert built("mcp").max_steps is None
    assert built("chat").max_steps is None
    assert built("run", "t", "--max-steps", "0").max_steps is None
    assert built("run", "t", "--max-steps", "7").max_steps == 7
    assert built("chat").wrap_up_steps is None
    assert built("run", "t").wrap_up_steps is None
    assert built("run", "t", "--wrap-up-steps", "120").wrap_up_steps == 120
    assert built("run", "t", "--wrap-up-steps", "0").wrap_up_steps is None
    assert built("run", "t", "--max-steps", "-1").max_steps is None
    assert built("run", "t", "--wrap-up-steps", "-5").wrap_up_steps is None
    assert built("run", "t").deadline_notice_s is None
    assert built("run", "t", "--deadline-notice", "600").deadline_notice_s == 600


def test_context_limit_caps_the_model_window(tmp_path):
    cfg = config(tmp_path, context_window=1_048_576)
    assert cfg.effective_window() == 400_000
    assert cfg.compaction_threshold() == 320_000
    assert config(tmp_path, context_window=1_048_576, context_limit=200_000).effective_window() == (
        200_000
    )
    assert config(tmp_path, context_window=64_000).effective_window() == 64_000


def test_context_overflow_compacts_and_retries_once(tmp_path):
    call = ToolCall("id", "read", "{}")
    messages = [
        Message("user", "original"),
        Message("assistant", tool_calls=[call]),
        Message("tool", "x" * 20000, tool_call_id="id"),
    ] + [Message("assistant", "tail" * 100) for _ in range(7)]
    overflow = ProviderError("context_window", "maximum context length exceeded", status=400)
    provider = ScriptedProvider([Turn(error=overflow), Turn(text="checkpoint"), Turn(text="done")])
    session = Session(config(tmp_path), provider, messages=messages)
    events = run(session, "continue")
    finished = next(e for e in events if isinstance(e, RunFinished))
    assert finished.status == "completed"
    assert finished.final_text == "done"
    assert "prune" in [e.mode for e in events if e.type == "compacted"]
    assert provider.requests[1].messages[-1].content == COMPACTION_INSTRUCTION
    assert any(m.meta.get("kind") == "compaction" for m in session.messages)


def test_context_overflow_twice_ends_the_run(tmp_path):
    overflow = ProviderError("context_window", "too long", status=400)
    messages = [Message("user", "original"), Message("tool", "y" * 20000, tool_call_id="z")]
    provider = ScriptedProvider(
        [Turn(error=overflow), Turn(text="checkpoint"), Turn(error=overflow)]
    )
    session = Session(config(tmp_path), provider, messages=messages)
    events = run(session, "continue")
    finished = next(e for e in events if isinstance(e, RunFinished))
    assert finished.status == "error"
    assert "context_window" in finished.reason


def test_compaction_respects_the_run_time_limit(tmp_path):
    class SlowSummary(ScriptedProvider):
        async def complete(self, request, on_delta=None):
            if request.messages[-1].content == COMPACTION_INSTRUCTION:
                await asyncio.sleep(30)
            return await ScriptedProvider.complete(self, request, on_delta)

    messages = [Message("user", "original")] + [Message("assistant", "x" * 400) for _ in range(9)]
    provider = SlowSummary([Turn(text="checkpoint"), Turn(text="done")])
    session = Session(
        config(tmp_path, compact_at_tokens=100, max_seconds=1.5), provider, messages=messages
    )
    events = run(session, "continue")
    finished = next(e for e in events if isinstance(e, RunFinished))
    assert finished.elapsed_s < 10
    assert not any(m.meta.get("kind") == "compaction" for m in session.messages)


def test_truncation_limit_counts_consecutive_replies(tmp_path):
    cut = Turn(text="partial", finish_reason="length")
    ok = Turn(calls=[("glob", {"pattern": "*.none"})])
    provider = ScriptedProvider([cut, cut, ok, cut, cut, ok, Turn(text="done")])
    events = run(Session(config(tmp_path), provider))
    finished = next(e for e in events if isinstance(e, RunFinished))
    assert finished.status == "completed"
    assert finished.final_text == "done"


def test_compaction_retries_without_tools_after_a_tool_call(tmp_path):
    messages = [Message("user", "original")] + [Message("assistant", "x" * 400) for _ in range(9)]
    provider = ScriptedProvider(
        [Turn(calls=[("glob", {"pattern": "*"})]), Turn(text="checkpoint"), Turn(text="done")]
    )
    session = Session(config(tmp_path, compact_at_tokens=100), provider, messages=messages)
    run(session, "continue")
    assert provider.requests[0].tools
    assert provider.requests[1].tools == []
    assert any(m.meta.get("kind") == "compaction" for m in session.messages)


def test_context_estimate_counts_a_reply_once(tmp_path):
    reply = Turn(text="a" * 35, calls=[("glob", {"pattern": "*.none"})])
    provider = ScriptedProvider([reply, Turn(text="done")])
    session = Session(config(tmp_path), provider)
    seen = []
    original = session._maybe_compact

    async def spy(emit):
        seen.append((session._last_input_tokens, session._chars_since_measure))
        await original(emit)

    session._maybe_compact = spy
    run(session)
    args = len(json.dumps({"pattern": "*.none"}))
    result = next(m for m in session.messages if m.role == "tool")
    assert seen[1][1] == 35 + args + len(result.content)


def test_unknown_model_window_uses_the_user_limit(tmp_path):
    assert config(tmp_path).effective_window() == 400_000
    assert config(tmp_path, context_limit=0).effective_window() == 400_000
    assert config(tmp_path, context_window=64_000, context_limit=0).effective_window() == 64_000


def test_context_overflow_without_summary_drops_the_head(tmp_path):
    overflow = ProviderError("context_window", "too long", status=400)
    messages = [Message("user", "original")] + [
        Message("assistant", f"step {i} " + "x" * 4000) for i in range(40)
    ]
    # main call overflows, the summary call overflows too, the retried step succeeds
    provider = ScriptedProvider([Turn(error=overflow), Turn(error=overflow), Turn(text="done")])
    session = Session(config(tmp_path), provider, messages=messages)
    before = session.context_tokens()
    events = run(session, "continue")
    finished = next(e for e in events if isinstance(e, RunFinished))
    assert finished.status == "completed"
    assert "drop" in [e.mode for e in events if e.type == "compacted"]
    assert session.messages[0].content == "original"
    assert "No summary" in session.messages[1].content
    assert session.config.context_window is None  # the configured value is untouched
    assert session._learned_window is not None
    assert session._learned_window < before


def test_background_finish_notice_carries_the_unread_output(tmp_path):
    python = shlex.quote(sys.executable.replace("\\", "/"))
    short = python + " -c " + shlex.quote("import time; time.sleep(0.3); print('FAILED x')")
    wait = python + " -c " + shlex.quote("import time; time.sleep(0.8)")
    provider = ScriptedProvider(
        [
            Turn(calls=[("bash", {"command": short, "run_in_background": True})]),
            Turn(calls=[("bash", {"command": wait})]),
            Turn(calls=[("job_output", {"job_id": "1"})]),
            Turn(text="done"),
        ]
    )
    session = Session(config(tmp_path), provider)
    assert finish(run(session)).status == "completed"
    notice = results(session)[1].content
    assert "exited with code 0" in notice
    assert "Unread output:\nFAILED x" in notice
    assert "FAILED x" not in results(session)[2].content  # already shown, not repeated


def test_background_finish_notice_tail_keeps_the_full_log(tmp_path):
    python = shlex.quote(sys.executable.replace("\\", "/"))
    code = "import time; time.sleep(0.3); [print('line', i) for i in range(2000)]"
    loud = python + " -c " + shlex.quote(code)
    wait = python + " -c " + shlex.quote("import time; time.sleep(1.0)")
    provider = ScriptedProvider(
        [
            Turn(calls=[("bash", {"command": loud, "run_in_background": True})]),
            Turn(calls=[("bash", {"command": wait})]),
            Turn(calls=[("job_output", {"job_id": "1"})]),
            Turn(text="done"),
        ]
    )
    session = Session(config(tmp_path), provider)
    assert finish(run(session)).status == "completed"
    notice = results(session)[1].content
    assert "line 1999" in notice
    assert "job_output returns all of them" in notice
    assert "line 0\n" in results(session)[2].content


def test_cancel_mid_batch_keeps_finished_results(tmp_path):
    python = shlex.quote(sys.executable.replace("\\", "/"))
    slow = python + " -c " + shlex.quote("import time; time.sleep(30)")
    provider = ScriptedProvider(
        [
            Turn(
                calls=[
                    ("write", {"file_path": "done.txt", "content": "x"}),
                    ("bash", {"command": slow}),
                ]
            )
        ]
    )
    session = Session(config(tmp_path), provider)

    async def scenario():
        async def consume():
            async for _ in session.run("go"):
                pass

        task = asyncio.create_task(consume())
        for _ in range(100):
            await asyncio.sleep(0.1)
            if (tmp_path / "done.txt").exists():
                break
        await asyncio.sleep(0.3)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    tool_messages = [m for m in session.messages if m.role == "tool"]
    assert tool_messages[0].content.startswith("Created done.txt")
    loaded = SessionStore.load(session.store.home, session.session_id)
    loaded_tools = [m for m in loaded.messages if m.role == "tool"]
    assert loaded_tools[0].content.startswith("Created done.txt")
    # The slow command was killed mid-run: on resume its outcome is unknown.
    assert loaded_tools[1].content == UNKNOWN_RESULT


def test_planned_compaction_that_overflows_is_recovered(tmp_path):
    overflow = ProviderError("context_window", "too long", status=400)
    messages = [Message("user", "original")] + [
        Message("assistant", f"step {i} " + "x" * 400) for i in range(9)
    ]
    # summary overflows, the bounded retry of the summary overflows too, the step works
    provider = ScriptedProvider([Turn(error=overflow), Turn(error=overflow), Turn(text="done")])
    session = Session(config(tmp_path, compact_at_tokens=100), provider, messages=messages)
    events = run(session, "continue")
    assert next(e for e in events if isinstance(e, RunFinished)).status == "completed"
    assert "drop" in [e.mode for e in events if e.type == "compacted"]


def test_overflow_keeps_every_user_instruction(tmp_path):
    overflow = ProviderError("context_window", "too long", status=400)
    messages = [Message("user", "original task")]
    for i in range(30):
        messages.append(Message("assistant", f"step {i} " + "x" * 4000))
        if i == 3:
            messages.append(Message("user", "do not touch setup.py"))
    provider = ScriptedProvider([Turn(error=overflow), Turn(text="checkpoint"), Turn(text="ok")])
    session = Session(config(tmp_path), provider, messages=messages)
    run(session, "continue")
    users = [m.content for m in session.messages if m.role == "user" and not m.meta.get("kind")]
    assert users[:2] == ["original task", "do not touch setup.py"]


def test_small_window_still_compacts_below_its_size(tmp_path):
    cfg = config(tmp_path, context_window=6_000)
    assert cfg.compaction_threshold() == 3_000
    assert cfg.compaction_threshold(learned=4_000) == 2_000


def test_compaction_ignores_a_preamble_before_a_tool_call(tmp_path):
    messages = [Message("user", "original")] + [Message("assistant", "x" * 400) for _ in range(9)]
    provider = ScriptedProvider(
        [
            Turn(text="Let me check the file first.", calls=[("glob", {"pattern": "*"})]),
            Turn(text="checkpoint"),
            Turn(text="done"),
        ]
    )
    session = Session(config(tmp_path, compact_at_tokens=100), provider, messages=messages)
    run(session, "continue")
    assert provider.requests[1].tools == []
    summary = next(m for m in session.messages if m.meta.get("kind") == "compaction")
    assert "checkpoint" in summary.content
    assert "Let me check" not in summary.content


def test_compaction_rejects_a_summary_cut_at_the_limit(tmp_path):
    messages = [Message("user", "original")] + [Message("assistant", "x" * 400) for _ in range(9)]
    provider = ScriptedProvider(
        [
            Turn(text="partial summ", finish_reason="length"),
            Turn(text="checkpoint"),
            Turn(text="done"),
        ]
    )
    session = Session(config(tmp_path, compact_at_tokens=100), provider, messages=messages)
    run(session, "continue")
    summary = next(m for m in session.messages if m.meta.get("kind") == "compaction")
    assert "partial summ" not in summary.content


def test_content_filter_stop_is_not_a_completed_run(tmp_path):
    provider = ScriptedProvider([Turn(text="I will now", finish_reason="content_filter")])
    events = run(Session(config(tmp_path), provider))
    assert finish(events).status == "error"


def test_resume_keeps_records_with_unicode_line_separators(tmp_path):
    store = SessionStore(tmp_path, "20260101-000000-abcdef")
    store.message(Message("user", "a\u2028b\u2029c\x85d"))
    loaded = SessionStore.load(tmp_path, "20260101-000000-abcdef")
    assert [m.content for m in loaded.messages] == ["a\u2028b\u2029c\x85d"]


def test_split_keeps_the_newest_step_even_when_it_is_over_budget():
    messages = [
        Message("user", "task"),
        Message("assistant", "a"),
        Message("assistant", "", tool_calls=[ToolCall("c1", "read", "{}")]),
        Message("tool", "x" * 50_000, tool_call_id="c1"),
    ]
    head, tail = split_for_summary(messages, retain_tokens=10)
    assert [m.role for m in tail] == ["assistant", "tool"]
    assert head == messages[:2]


def test_events_carry_time_and_usage_carries_call_timing(tmp_path):
    from karox6.core import events as ev
    from karox6.core.messages import Message as Msg
    from karox6.providers.base import Delta, ModelResponse

    class SlowStream:
        name = "slow"
        calls = 0

        async def complete(self, request, on_delta=None):
            SlowStream.calls += 1
            if SlowStream.calls == 1:
                raise ProviderError("rate_limit", "busy", status=429, retry_after=0.05)
            await asyncio.sleep(0.05)
            if on_delta:
                on_delta(Delta("text", "do"))
            await asyncio.sleep(0.05)
            return ModelResponse(
                message=Msg("assistant", "done"),
                usage=Usage(input_tokens=10, output_tokens=2, requests=1),
                finish_reason="stop",
                model="slow",
            )

        async def aclose(self):
            return None

    session = Session(config(tmp_path), SlowStream())
    events = run(session)
    assert all("ts" in e.to_dict() for e in events)
    usage = next(e for e in events if isinstance(e, ev.UsageUpdate))
    assert usage.finish_reason == "stop"
    assert usage.retries == 1
    assert 0.04 <= usage.ttft_s < usage.latency_s <= usage.total_s
    assert usage.total_s >= usage.latency_s + 0.04  # the retry wait counts only in total


def test_summary_keeps_every_user_requirement_verbatim(tmp_path):
    later = Message("user", "Do not change the public API; support Python 3.12.")
    messages = (
        [Message("user", "original")]
        + [Message("assistant", "x" * 400) for _ in range(4)]
        + [later]
        + [Message("assistant", "y" * 400) for _ in range(5)]
    )
    provider = ScriptedProvider([Turn(text="checkpoint that forgets it"), Turn(text="done")])
    session = Session(config(tmp_path, compact_at_tokens=100), provider, messages=messages)
    run(session, "continue")
    assert any(m.meta.get("kind") == "compaction" for m in session.messages)
    contents = [m.content for m in session.messages if m.role == "user"]
    assert "original" in contents
    assert later.content in contents


def test_a_failed_summary_shrinks_without_the_model(tmp_path):
    messages = [Message("user", "original")] + [Message("assistant", "x" * 400) for _ in range(9)]
    provider = ScriptedProvider(
        [
            Turn(text="", reasoning="r" * 50, finish_reason="length"),
            Turn(text="", reasoning="r" * 50, finish_reason="length"),
            Turn(text="done"),
        ]
    )
    session = Session(config(tmp_path, compact_at_tokens=100), provider, messages=messages)
    events = run(session, "continue")
    assert finish(events).status == "completed"
    assert len(provider.requests) == 3  # two summary tries, then the task: no third try
    assert any(m.meta.get("kind") == "compaction" for m in session.messages)
    assert session.messages[0].content == "original"
