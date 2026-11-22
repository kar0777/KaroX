"""Token-limit handling in the loop: replies cut while thinking, and what a compaction
costs when the provider has a prompt cache."""

import asyncio

from karox6.context.compaction import COMPACTION_INSTRUCTION, PRUNE_MARKER
from karox6.core import events as ev
from karox6.core.messages import Message, ToolCall, Usage
from karox6.core.session import (
    THINKING_CUT_NOTICE,
    TRUNCATION_NOTICE,
    Session,
    SessionConfig,
)
from karox6.providers.base import ProviderError, RetryPolicy
from karox6.providers.scripted import ScriptedProvider, Turn


def config(tmp_path, **kwargs):
    return SessionConfig(model="test", workspace=tmp_path, home=tmp_path / "home", **kwargs)


def run(session, prompt="task"):
    async def collect():
        return [event async for event in session.run(prompt)]

    return asyncio.run(collect())


def thinking_cut():
    return Turn(text="", reasoning="r" * 60, finish_reason="length")


def test_a_reply_cut_while_thinking_is_told_what_happened(tmp_path):
    provider = ScriptedProvider([thinking_cut(), Turn(text="done")])
    session = Session(config(tmp_path), provider)
    events = run(session)
    assert events[-1].status == "completed"
    notice = provider.requests[1].messages[-1]
    assert notice.role == "user"
    assert notice.content == THINKING_CUT_NOTICE
    # the thinking it had done is still there to build on
    assert provider.requests[1].messages[-2].reasoning == "r" * 60
    # no effort is configured, so none is invented for the provider
    assert [r.reasoning_effort for r in provider.requests] == [None, None]


def test_a_reply_cut_mid_text_keeps_the_write_shorter_notice(tmp_path):
    cut = Turn(text="half a file", finish_reason="length")
    provider = ScriptedProvider([cut, Turn(text="done")])
    run(Session(config(tmp_path), provider))
    assert provider.requests[1].messages[-1].content == TRUNCATION_NOTICE


def test_effort_is_eased_after_all_thinking_cuts_and_restored_by_a_reply(tmp_path):
    provider = ScriptedProvider(
        [thinking_cut(), thinking_cut(), Turn(text="done"), Turn(text="again")]
    )
    session = Session(config(tmp_path, reasoning_effort="max"), provider)
    events = run(session)
    assert events[-1].status == "completed"
    assert [r.reasoning_effort for r in provider.requests] == ["max", "high", "medium"]
    eased = [e.text for e in events if isinstance(e, ev.Notice) and e.kind == "effort"]
    assert eased == [
        "reasoning effort high until a reply fits",
        "reasoning effort medium until a reply fits",
    ]
    # the effort notice is for the user only: the model got just the two cut notices
    kinds = [m.meta.get("kind") for m in session.messages if m.role == "user"]
    assert kinds == [None, "truncated", "truncated"]
    # a reply came through: the next turn asks for the configured effort again
    run(session, "more")
    assert provider.requests[3].reasoning_effort == "max"


def test_the_lowest_effort_is_not_lowered_further(tmp_path):
    provider = ScriptedProvider([thinking_cut(), thinking_cut(), Turn(text="done")])
    session = Session(config(tmp_path, reasoning_effort="medium"), provider)
    run(session)
    assert [r.reasoning_effort for r in provider.requests] == ["medium", "low", "low"]


def reading_script(cached: bool, steps: int = 4):
    """``steps`` tool steps that each read a 10k-character file, then two text replies."""
    turns = []
    for i in range(steps):
        size = 1000 + 3000 * i
        usage = Usage(
            input_tokens=size,
            cached_tokens=size - 200 if cached and i else 0,
            output_tokens=20,
            requests=1,
        )
        turns.append(
            Turn(calls=[("read", {"file_path": "big.txt"})], reasoning=f"thought {i}", usage=usage)
        )
    return [*turns, Turn(text="## checkpoint"), Turn(text="done")]


def big_file(tmp_path):
    (tmp_path / "big.txt").write_text("\n".join("x" * 99 for _ in range(100)), encoding="utf-8")


def modes(events):
    return [e.mode for e in events if isinstance(e, ev.Compacted)]


def wire(messages):
    return [(m.role, m.content, m.reasoning) for m in messages]


def test_with_a_live_prompt_cache_the_checkpoint_comes_before_any_pruning(tmp_path):
    big_file(tmp_path)
    provider = ScriptedProvider(reading_script(cached=True))
    session = Session(config(tmp_path, compact_at_tokens=12_000), provider)
    events = run(session)
    assert events[-1].status == "completed"
    assert modes(events) == ["summary"]  # no prune: it would have dropped the cache
    summary = provider.requests[4]
    assert summary.messages[-1].content == COMPACTION_INSTRUCTION
    # the request is the previous one plus the instruction: same text, same reasoning,
    # nothing cut, so the provider serves the conversation from its cache
    assert wire(summary.messages[:-1]) == wire(provider.requests[3].messages)
    assert summary.messages[1].reasoning == "thought 0"
    assert not any(PRUNE_MARKER in m.content for m in summary.messages)
    # afterwards the context is the task, the checkpoint and the newest step
    after = provider.requests[5].messages
    assert [m.role for m in after] == ["user", "user", "assistant", "tool"]
    assert after[0].content == "task"
    assert "## checkpoint" in after[1].content


def test_without_a_prompt_cache_old_tool_output_is_pruned_first(tmp_path):
    big_file(tmp_path)
    provider = ScriptedProvider(reading_script(cached=False))
    session = Session(config(tmp_path, compact_at_tokens=12_000), provider)
    events = run(session)
    assert events[-1].status == "completed"
    assert modes(events) == ["prune"]  # free, and enough: no model call for a summary
    assert len(provider.requests) == 5
    assert PRUNE_MARKER in provider.requests[4].messages[2].content


def test_compact_command_follows_the_same_order(tmp_path):
    big_file(tmp_path)
    for cached, expected in ((True, ["summary"]), (False, ["prune", "summary"])):
        turns = reading_script(cached)
        last = Usage(input_tokens=13_000, cached_tokens=12_800 if cached else 0, requests=1)
        turns[4:] = [Turn(text="done", usage=last), Turn(text="## checkpoint")]
        provider = ScriptedProvider(turns)
        cfg = SessionConfig(model="test", workspace=tmp_path, home=tmp_path / f"home-{cached}")
        session = Session(cfg, provider)
        run(session)
        compacted = asyncio.run(session.compact())
        assert modes(compacted) == expected
        assert any(m.meta.get("kind") == "compaction" for m in session.messages)


# -- a server error that repeats on the same request --------------------------------------

QUICK = RetryPolicy(initial_delay_s=0.001, max_delay_s=0.002)


class FailsOn(ScriptedProvider):
    """A server that answers 500 whenever the request contains ``marker`` (it fails on the
    request itself: waiting does not help), and follows the script otherwise."""

    def __init__(self, turns, marker):
        super().__init__(turns)
        self.marker = marker
        self.refused = 0

    async def complete(self, request, on_delta=None):
        if any(self.marker in m.content for m in request.messages):
            self.refused += 1
            raise ProviderError("server", "the provider returned an internal error", status=500)
        return await ScriptedProvider.complete(self, request, on_delta)


def history_with_a_big_tool_result(middle):
    call = ToolCall("id", "grep", "{}")
    return [
        Message("user", "original"),
        Message("assistant", tool_calls=[call]),
        Message("tool", "a" * 5000 + middle + "b" * 5000, tool_call_id="id"),
    ]


def test_a_server_error_on_the_same_request_gets_one_try_with_a_smaller_request(tmp_path):
    # devt2b: one request was answered with HTTP 500 thirty times in a row while the other
    # requests on the key went through, and the run ended there after two steps.
    provider = FailsOn([Turn(text="done")], marker="WHAT-THE-SERVER-FAILS-ON")
    messages = history_with_a_big_tool_result("WHAT-THE-SERVER-FAILS-ON")
    session = Session(config(tmp_path, retry=QUICK), provider, messages=messages)
    events = run(session, "continue")
    assert events[-1].status == "completed"
    assert events[-1].final_text == "done"
    assert provider.refused == 1 + QUICK.max_retries  # every ordinary retry came first
    assert [e.mode for e in events if e.type == "compacted"] == ["prune"]
    assert PRUNE_MARKER in session.messages[2].content  # the newest tool result was cut too
    assert [e.code for e in events if e.type == "error"] == []


def test_a_server_error_that_survives_the_smaller_request_ends_the_run(tmp_path):
    provider = FailsOn([Turn(text="never")], marker="original")  # nothing to cut helps
    messages = history_with_a_big_tool_result("x")
    session = Session(config(tmp_path, retry=QUICK), provider, messages=messages)
    events = run(session, "continue")
    assert events[-1].status == "error"
    assert "internal error" in events[-1].reason
    assert provider.refused == 2 * (1 + QUICK.max_retries)  # one more round, not a loop


def test_a_server_error_with_nothing_to_cut_ends_the_run_as_before(tmp_path):
    provider = FailsOn([Turn(text="never")], marker="task")
    session = Session(config(tmp_path, retry=QUICK), provider)
    events = run(session)
    assert events[-1].status == "error"
    assert provider.refused == 1 + QUICK.max_retries
    assert [e for e in events if e.type == "compacted"] == []


def test_an_outage_is_waited_out_not_answered_with_a_smaller_request(tmp_path):
    class Down(FailsOn):
        async def complete(self, request, on_delta=None):
            self.refused += 1
            raise ProviderError("server", "service unavailable", status=503)

    policy = RetryPolicy(initial_delay_s=0.001, max_delay_s=0.002, unavailable_budget_s=0.01)
    provider = Down([Turn(text="never")], marker="")
    messages = history_with_a_big_tool_result("x")
    session = Session(config(tmp_path, retry=policy), provider, messages=messages)
    events = run(session, "continue")
    assert events[-1].status == "error"
    assert [e for e in events if e.type == "compacted"] == []
    assert PRUNE_MARKER not in session.messages[2].content
