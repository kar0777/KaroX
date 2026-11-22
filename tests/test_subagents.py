import asyncio

from karox6.core import session as session_mod
from karox6.core.messages import Message, ToolCall, Usage
from karox6.core.session import Session, SessionConfig
from karox6.core.store import active_sessions
from karox6.providers.base import ModelResponse
from karox6.providers.scripted import ScriptedProvider, Turn
from karox6.tools.agent import SUBAGENT_APPEND


def config(tmp_path, **kwargs):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return SessionConfig(model="t", workspace=ws, home=tmp_path / "home", **kwargs)


def run(session, prompt="go"):
    async def go():
        return [e async for e in session.run(prompt)]

    return asyncio.run(go())


def test_agent_tool_only_when_enabled(tmp_path):
    off = Session(config(tmp_path), ScriptedProvider([]))
    on = Session(config(tmp_path, subagents=True), ScriptedProvider([]))
    assert "agent" not in [t.name for t in off.tools]
    assert "agent" in [t.name for t in on.tools]


def test_subagent_reports_back(tmp_path):
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "a.py").write_text("x = 1\n")
    provider = ScriptedProvider(
        [
            Turn(calls=[("agent", {"description": "find x", "prompt": "Where is x set?"})]),
            Turn(calls=[("read", {"file_path": "a.py"})]),  # the child reads
            Turn(text="x is set in a.py:1"),  # the child's report
            Turn(text="done"),  # the parent finishes
        ]
    )
    parent = Session(config(tmp_path, subagents=True), provider)
    events = run(parent)
    assert events[-1].status == "completed"
    result = next(m for m in parent.messages if m.role == "tool")
    assert result.content == "x is set in a.py:1"
    assert [m.role for m in parent.messages] == ["user", "assistant", "tool", "assistant"]
    child_request = provider.requests[1]
    assert SUBAGENT_APPEND in child_request.system
    assert [m.content for m in child_request.messages] == ["Where is x set?"]
    child_tools = {s.name for s in child_request.tools}
    assert "agent" not in child_tools
    assert "baseline" not in child_tools
    assert "baseline" in {t.name for t in parent.tools}
    assert not child_tools & {"edit", "write", "apply_patch"}
    assert parent.usage.requests == 4  # child calls are counted in the parent
    names = [e.name for e in events if e.type == "tool_started"]
    assert names == ["agent", "agent-1/read"]
    sessions = active_sessions(tmp_path / "home")
    assert sessions == [parent.session_id]  # the child's own log is not listed
    assert (tmp_path / "home" / "sessions" / f"{parent.session_id}.agent-1").is_dir()


class RoutingProvider:
    """Parent turns from a script; each child answers once both children are waiting."""

    name = "routing"

    def __init__(self, parent_turns):
        self.parent = ScriptedProvider(parent_turns)
        self.waiting = 0
        self.both = asyncio.Event()

    async def complete(self, request, on_delta=None):
        if SUBAGENT_APPEND not in request.system:
            return await self.parent.complete(request, on_delta)
        self.waiting += 1
        if self.waiting == 2:
            self.both.set()
        await asyncio.wait_for(self.both.wait(), 5)  # fails if children run one by one
        text = "report on " + request.messages[0].content
        message = Message("assistant", text)
        usage = Usage(input_tokens=10, output_tokens=5, requests=1)
        return ModelResponse(message=message, usage=usage, finish_reason="stop", model="r")

    async def aclose(self):
        return None


def test_subagents_run_in_parallel(tmp_path):
    calls = [("agent", {"prompt": "alpha"}), ("agent", {"prompt": "beta"})]
    provider = RoutingProvider([Turn(calls=calls), Turn(text="done")])
    parent = Session(config(tmp_path, subagents=True), provider)
    events = run(parent)
    assert events[-1].status == "completed"
    results = [m.content for m in parent.messages if m.role == "tool"]
    assert results == ["report on alpha", "report on beta"]


def test_subagent_step_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(session_mod, "SUBAGENT_MAX_STEPS", 2)
    (tmp_path / "ws").mkdir()
    provider = ScriptedProvider(
        [
            Turn(calls=[("agent", {"prompt": "dig"})]),
            Turn(text="looking", calls=[("glob", {"pattern": "*.py"})]),
            Turn(text="still looking", calls=[("glob", {"pattern": "*.txt"})]),
            Turn(text="done"),
        ]
    )
    parent = Session(config(tmp_path, subagents=True), provider)
    run(parent)
    result = next(m for m in parent.messages if m.role == "tool")
    assert result.content.startswith("[subagent stopped: step limit 2]")
    assert "still looking" in result.content
    assert not result.is_error


def test_dangling_agent_call_closed(tmp_path):
    # A parent cancelled while a subagent ran must leave a valid history.
    parent = Session(config(tmp_path, subagents=True), ScriptedProvider([Turn(text="ok")]))
    parent.messages = [
        Message("user", "go"),
        Message("assistant", "", tool_calls=[ToolCall("c1", "agent", '{"prompt": "x"}')]),
    ]
    run(parent, "next")
    assert [m.role for m in parent.messages] == ["user", "assistant", "tool", "user", "assistant"]
