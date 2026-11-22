import asyncio
import json
from pathlib import Path

from karox6.context.elide import (
    ELIDE_ARGUMENT_CHARS,
    KEEP_RECENT_MESSAGES,
    REWRITE_BATCH_CHARS,
    REWRITE_PRESSURE_TOKENS,
    WireElider,
    _pending_error,
    read_stub,
)
from karox6.core.messages import Message, ToolCall, Usage
from karox6.core.session import Session, SessionConfig
from karox6.providers.scripted import ScriptedProvider, Turn

WS = Path("/work")
BIG = 30_000


def read_text(path: str, lines: int, *, start: int = 1, width: int = 60) -> str:
    body = "\n".join(f"{n}: {'x' * width}" for n in range(start, start + lines))
    return (
        f"<path>{path}</path>\n<type>file</type>\n<content>\n{body}\n\n"
        f"(End of file - {start + lines - 1} lines.)\n</content>"
    )


class Convo:
    """A conversation built the way the session builds it: one call and one result per step."""

    def __init__(self) -> None:
        self.messages: list[Message] = [Message("user", "do the task")]
        self.n = 0

    def step(self, name: str, args: dict, result: str, *, error: bool = False, **extra) -> str:
        self.n += 1
        call_id = f"call_{self.n}"
        self.messages.append(
            Message(
                "assistant",
                "",
                tool_calls=[ToolCall(call_id, name, json.dumps(args))],
                **extra,
            )
        )
        self.messages.append(
            Message("tool", result, tool_call_id=call_id, name=name, is_error=error)
        )
        return call_id

    def read(self, path: str, lines: int = 400, **kw) -> str:
        args = {"file_path": path, **{k: v for k, v in kw.items() if k in ("offset", "limit")}}
        start = int(kw.get("offset", 1))
        return self.step("read", args, read_text(path, lines, start=start))

    def write(self, path: str, size: int = BIG) -> str:
        return self.step("write", {"file_path": path, "content": "y" * size}, f"Created {path}.")

    def edit(self, path: str, *, error: bool = False) -> str:
        args = {"file_path": path, "old_string": "a", "new_string": "b"}
        return self.step(
            "edit", args, "Error: not found" if error else f"Edited {path}.", error=error
        )

    def filler(self, count: int) -> None:
        for _ in range(count):
            self.step("bash", {"command": "true"}, "ok\n[exit code: 0]")


def dump(messages) -> list[dict]:
    return [m.to_dict() for m in messages]


def elider() -> WireElider:
    return WireElider(WS)


HIGH = REWRITE_PRESSURE_TOKENS


def test_nothing_changes_below_the_token_pressure():
    c = Convo()
    c.read("a.py")
    c.edit("a.py")
    c.filler(5)
    assert len(c.messages[2].content) > REWRITE_BATCH_CHARS / 2
    view = elider().build(c.messages, HIGH - 1)
    assert dump(view) == dump(c.messages)


def test_nothing_changes_when_too_little_is_reclaimable():
    c = Convo()
    c.read("a.py", lines=50)
    c.edit("a.py")
    c.filler(5)
    assert len(c.messages[2].content) < REWRITE_BATCH_CHARS
    view = elider().build(c.messages, HIGH * 3)
    assert dump(view) == dump(c.messages)


def test_read_superseded_by_later_edit_is_stubbed():
    c = Convo()
    c.read("src/a.py")
    c.read("src/other.py")
    c.edit("src/a.py")
    c.filler(4)
    stored = dump(c.messages)
    view = elider().build(c.messages, HIGH)
    assert view[2].content == read_stub("src/a.py")
    assert view[2].tool_call_id == c.messages[2].tool_call_id
    assert not view[2].is_error
    assert view[4].content == c.messages[4].content
    assert dump(c.messages) == stored


def test_read_superseded_by_later_write_or_apply_patch():
    c = Convo()
    c.read("a.py")
    c.read("b.py")
    c.write("a.py", size=100)
    c.step(
        "apply_patch",
        {"input": "*** Begin Patch\n*** Update File: b.py\n@@\n-x\n+y\n*** End Patch"},
        "Updated b.py",
    )
    c.filler(4)
    view = elider().build(c.messages, HIGH)
    assert view[2].content == read_stub("a.py")
    assert view[4].content == read_stub("b.py")


def test_read_superseded_by_later_read_only_when_it_covers_it():
    c = Convo()
    c.read("a.py", lines=400)
    c.read("a.py", lines=500)  # the same lines and more: covers the first
    c.read("b.py", lines=400)
    c.read("b.py", lines=100, offset=50)  # a slice: does not cover the whole read
    c.read("c.py", lines=500)
    c.read("c.py", lines=400)  # fewer lines than before: does not cover it
    c.filler(4)
    view = elider().build(c.messages, HIGH)
    contents = {m.tool_call_id: m.content for m in view if m.role == "tool"}
    stored = {m.tool_call_id: m.content for m in c.messages if m.role == "tool"}
    assert contents["call_1"] == read_stub("a.py")
    for kept in ("call_2", "call_3", "call_4", "call_5", "call_6"):
        assert contents[kept] == stored[kept]


def test_failed_edit_does_not_supersede_a_read():
    c = Convo()
    c.read("a.py")
    c.edit("a.py", error=True)
    c.filler(4)
    assert dump(elider().build(c.messages, HIGH)) == dump(c.messages)


def test_error_results_are_never_elided():
    c = Convo()
    c.step("read", {"file_path": "a.py"}, read_text("a.py", 400), error=True)
    c.read("a.py")
    c.edit("a.py")
    c.filler(4)
    view = elider().build(c.messages, HIGH)
    assert view[2].content == c.messages[2].content


def test_multi_file_reads_and_directories_are_left_alone():
    c = Convo()
    c.step("read", {"file_paths": ["a.py", "b.py"]}, read_text("a.py", 500))
    c.step("read", {"file_path": "d"}, "<path>d</path>\n<type>directory</type>\n" + "e\n" * 500)
    c.edit("a.py")
    c.filler(4)
    assert dump(elider().build(c.messages, HIGH)) == dump(c.messages)


def test_old_large_write_arguments_are_stubbed_but_the_last_two_stay():
    c = Convo()
    ids = [c.write(f"f{k}.txt") for k in range(4)]
    c.filler(4)
    view = elider().build(c.messages, HIGH)
    calls = {call.id: call for m in view if m.role == "assistant" for call in m.tool_calls}
    for old in ids[:2]:
        args = json.loads(calls[old].arguments)
        assert args["file_path"] == f"f{ids.index(old)}.txt"
        assert str(BIG) in args["content"]
        assert len(calls[old].arguments) < 300
    for recent in ids[2:]:
        assert len(json.loads(calls[recent].arguments)["content"]) == BIG
    assert calls[ids[0]].name == "write"
    assert calls[ids[0]].id == ids[0]


def test_edit_arguments_and_small_calls():
    c = Convo()
    big_edit = {"file_path": "a.py", "old_string": "o" * 2500, "new_string": "n" * 2500}
    first = c.step("edit", big_edit, "Edited a.py.")
    c.write("b.txt")
    c.write("c.txt")
    c.write("d.txt")
    small = c.edit("a.py")
    c.filler(4)
    view = elider().build(c.messages, HIGH)
    calls = {call.id: call for m in view if m.role == "assistant" for call in m.tool_calls}
    args = json.loads(calls[first].arguments)
    assert args["file_path"] == "a.py"
    assert "2500" in args["old_string"]
    assert "2500" in args["new_string"]
    assert json.loads(calls[small].arguments)["old_string"] == "a"


def test_short_arguments_below_the_limit_are_kept():
    c = Convo()
    for k in range(5):
        c.step(
            "write",
            {"file_path": f"f{k}", "content": "z" * ELIDE_ARGUMENT_CHARS},
            "Created.",
        )
    c.filler(4)
    assert dump(elider().build(c.messages, HIGH)) == dump(c.messages)


def test_nothing_in_the_last_messages_is_elided():
    c = Convo()
    c.read("old.py")
    c.edit("old.py")
    c.filler(2)
    c.read("a.py")  # superseded by the edit below, but both are among the newest messages
    c.edit("a.py")
    c.write("w.txt")
    assert len(c.messages) > KEEP_RECENT_MESSAGES
    view = elider().build(c.messages, HIGH)
    assert view[2].content == read_stub("old.py")
    assert dump(view[-KEEP_RECENT_MESSAGES:]) == dump(c.messages[-KEEP_RECENT_MESSAGES:])
    assert c.messages[-6].tool_calls[0].arguments == view[-6].tool_calls[0].arguments


def test_recent_window_is_by_message_not_by_step():
    c = Convo()
    c.read("a.py")
    c.edit("a.py")
    c.filler(1)
    # a.py read is message 2; with 6 messages after it nothing may touch it yet
    assert len(c.messages) - 2 - 1 < KEEP_RECENT_MESSAGES
    assert dump(elider().build(c.messages, HIGH)) == dump(c.messages)
    c.filler(1)
    assert elider().build(c.messages, HIGH)[2].content == read_stub("a.py")


def test_only_old_messages_change_and_user_and_text_messages_stay():
    c = Convo()
    c.messages.append(Message("user", "second request", meta={"kind": "step"}))
    c.read("a.py")
    c.edit("a.py")
    c.messages.append(Message("assistant", "thinking out loud"))
    c.filler(4)
    view = elider().build(c.messages, HIGH)
    for before, after in zip(c.messages, view, strict=True):
        if before.role in ("user", "assistant") and not before.tool_calls:
            assert before == after


def test_prefix_is_identical_between_batches():
    c = Convo()
    c.read("a.py")
    c.edit("a.py")
    for k in range(4):
        c.write(f"f{k}.txt")
    c.filler(3)
    e = elider()
    first = dump(e.build(c.messages, HIGH))
    assert first != dump(c.messages)
    # the conversation grows, the prompt is small again, then big but with little to reclaim
    c.filler(4)
    second = dump(e.build(c.messages, 1_000))
    third = dump(e.build(c.messages, HIGH * 2))
    assert second[: len(first) - KEEP_RECENT_MESSAGES] == first[: len(first) - KEEP_RECENT_MESSAGES]
    assert second == third
    # once applied an elision is not undone even when the pressure is gone
    assert second[2]["content"] == read_stub("a.py")
    assert dump(e.build(c.messages, 0)) == second
    assert dump(e.view(c.messages)) == second


def test_second_batch_extends_the_first_without_changing_it():
    c = Convo()
    c.read("a.py")
    c.edit("a.py")
    c.filler(6)
    e = elider()
    first = dump(e.build(c.messages, HIGH))
    for k in range(4):
        c.read(f"n{k}.py")
        c.edit(f"n{k}.py")
    c.filler(6)
    second = dump(e.build(c.messages, HIGH))
    cut = len(first) - KEEP_RECENT_MESSAGES
    assert second[:cut] == first[:cut]
    assert second[len(first) + 1]["content"] == read_stub("n0.py")


def test_stale_decision_is_dropped_when_the_superseding_call_is_gone():
    c = Convo()
    c.read("a.py")
    c.edit("a.py")
    c.filler(6)
    e = elider()
    e.build(c.messages, HIGH)
    assert e.view(c.messages)[2].content == read_stub("a.py")
    undone = [m for m in c.messages if m.tool_call_id != "call_2" and not _calls(m, "call_2")]
    assert e.view(undone)[2].content == c.messages[2].content


def _calls(message, call_id):
    return [call for call in message.tool_calls if call.id == call_id]


def test_ambiguous_call_ids_are_skipped():
    c = Convo()
    c.read("a.py")
    c.edit("a.py")
    c.filler(6)
    c.messages[3].tool_calls[0].id = "call_1"
    c.messages[4].tool_call_id = "call_1"
    assert dump(elider().build(c.messages, HIGH)) == dump(c.messages)


def test_reasoning_and_extra_survive_on_elided_assistant_messages():
    c = Convo()
    c.step(
        "write",
        {"file_path": "w.txt", "content": "y" * BIG},
        "Created w.txt.",
        reasoning="private thoughts",
        extra={"reasoning_field": "reasoning", "signature": "sig"},
    )
    c.write("x.txt")
    c.write("z.txt")
    c.filler(4)
    view = elider().build(c.messages, HIGH)
    assert len(view[1].tool_calls[0].arguments) < 300
    assert view[1].reasoning == "private thoughts"
    assert view[1].extra == {"reasoning_field": "reasoning", "signature": "sig"}
    assert c.messages[1].tool_calls[0].arguments.count("y") == BIG


def test_pending_error_helper():
    c = Convo()
    c.edit("a.py", error=True)
    assert _pending_error(c.messages, 2)
    c.messages.append(Message("assistant", "I will fix it"))
    assert not _pending_error(c.messages, 2)
    c.edit("a.py")
    assert not _pending_error(c.messages, 4)


# -- through a real Session -----------------------------------------------------------------


class RecordingProvider(ScriptedProvider):
    seen: list[list[dict]]

    def __init__(self, turns):
        super().__init__(turns)
        self.seen = []

    async def complete(self, request, on_delta=None):
        self.seen.append(dump(request.messages))
        return await super().complete(request, on_delta)


def session_script(workspace):
    (workspace / "a.txt").write_text(
        "HEADER-UNIQUE\n" + "".join(f"line {k} {'x' * 40}\n" for k in range(600))
    )
    usage = Usage(input_tokens=HIGH + 20_000, output_tokens=50, requests=1)
    calls = [
        ("read", {"file_path": "a.txt"}),
        ("edit", {"file_path": "a.txt", "old_string": "HEADER-UNIQUE", "new_string": "HEADER"}),
        ("write", {"file_path": "b.txt", "content": "b" * BIG}),
        ("write", {"file_path": "c.txt", "content": "c" * BIG}),
        ("write", {"file_path": "d.txt", "content": "d" * BIG}),
        ("bash", {"command": "echo one"}),
        ("bash", {"command": "echo two"}),
        ("bash", {"command": "echo three"}),
    ]
    turns = [
        Turn(calls=[call], usage=usage, reasoning=f"thinking {k}") for k, call in enumerate(calls)
    ]
    turns.append(Turn(text="done", usage=usage))
    return turns


def test_session_sends_stubs_but_stores_everything(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    provider = RecordingProvider(session_script(ws))
    config = SessionConfig(
        model="test", workspace=ws, home=tmp_path / "home", replay_reasoning="all"
    )
    session = Session(config, provider)

    async def go():
        return [e async for e in session.run("task")]

    events = asyncio.run(go())
    assert events[-1].status == "completed"
    assert len(provider.seen) == 9

    full = {m.tool_call_id: m.content for m in session.messages if m.role == "tool"}
    assert "HEADER-UNIQUE" in full["call_1"]
    assert len(full["call_1"]) > 20_000
    stored_writes = [
        c.arguments for m in session.messages for c in m.tool_calls if c.name == "write"
    ]
    assert all(len(a) > BIG for a in stored_writes)

    last = provider.seen[-1]
    wire = {m["tool_call_id"]: m["content"] for m in last if m["role"] == "tool"}
    assert wire["call_1"] == read_stub("a.txt")
    assert wire["call_2"].startswith("Edited")
    sent_writes = [
        json.loads(c["arguments"])
        for m in last
        for c in m.get("tool_calls", [])
        if c["name"] == "write"
    ]
    assert len(sent_writes) == 3
    assert "elided" in sent_writes[0]["content"]
    assert sent_writes[0]["file_path"] == "b.txt"
    assert len(sent_writes[1]["content"]) == BIG
    assert len(sent_writes[2]["content"]) == BIG
    # reasoning replay is unaffected
    reasoning = [
        m.get("reasoning") for m in last if m["role"] == "assistant" and m.get("reasoning")
    ]
    assert reasoning == [f"thinking {k}" for k in range(8)]
    # the size of what was sent shrank by about the two big items
    sent_chars = sum(len(json.dumps(m)) for m in last)
    stored_chars = sum(len(json.dumps(m.to_dict())) for m in session.messages[:-1])
    assert stored_chars - sent_chars > 50_000

    # the session log on disk keeps the full text
    logged = (config.home / "sessions").rglob("*.jsonl")
    text = "".join(p.read_text(encoding="utf-8") for p in logged)
    assert text.count("b" * 1000) >= 1
    assert "HEADER-UNIQUE" in text


def synthetic_session_chars(steps: int = 60) -> tuple[int, int]:
    """Chars sent over a synthetic session: (without elision, with it)."""
    c = Convo()
    e = elider()
    plain = with_elision = 0
    for k in range(steps):
        name = f"mod{k % 6}.py"
        match k % 6:
            case 0 | 3:
                c.read(name, lines=300)
            case 1:
                c.edit(name)
            case 2:
                c.write(f"gen{k}.py", size=6_000)
            case _:
                c.step("bash", {"command": "pytest"}, "ok " * 400)
        sent = e.build(c.messages, REWRITE_PRESSURE_TOKENS + 1 if k > 15 else 0)
        plain += sum(
            len(m.content) + sum(len(t.arguments) for t in m.tool_calls) for m in c.messages
        )
        with_elision += sum(
            len(m.content) + sum(len(t.arguments) for t in m.tool_calls) for m in sent
        )
    return plain, with_elision


def test_synthetic_sixty_step_session_sends_less():
    plain, reduced = synthetic_session_chars(60)
    assert reduced < plain * 0.9
