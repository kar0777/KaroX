"""karo-core context economy: output clipping + history compaction.

These tests are logic-only (dummy model/env, no network, no litellm calls).
They pin the two KaroX ports:
1. LocalEnvironment clips gigantic command output before it enters history.
2. manage_context keeps the history under budget without touching the system
   message, the task message or the recent tail, and without model calls.
"""

from __future__ import annotations


from karox.engine.agents.default import DefaultAgent
from karox.engine.environments.local import LocalEnvironment, clip_output


class DummyModel:
    """Records the history it was asked to query; returns one bash action."""

    def __init__(self):
        self.seen: list[list[dict]] = []
        self.n = 0

    def query(self, messages: list[dict], **kwargs) -> dict:
        self.seen.append([dict(m) for m in messages])
        self.n += 1
        return {
            "role": "assistant",
            "content": f"step {self.n}",
            "extra": {"actions": [{"command": f"echo {self.n}"}], "cost": 0.0},
        }

    def get_template_vars(self, **kwargs):
        return {}

    def format_message(self, **kwargs):
        return {"role": kwargs["role"], "content": kwargs["content"]}

    def format_observation_messages(self, message, outputs, template_vars=None):
        return [{"role": "user", "content": str(o.get("output", ""))} for o in outputs]

    def serialize(self):
        return {}


def make_agent(messages: list[dict], **config) -> DefaultAgent:
    agent = DefaultAgent(
        DummyModel(),  # type: ignore[arg-type]
        LocalEnvironment(cwd="."),  # type: ignore[arg-type]
        system_template="sys",
        instance_template="task",
        step_limit=0,
        cost_limit=10**9,
        wall_time_limit_seconds=0,
        max_consecutive_format_errors=0,
        **config,
    )
    agent.messages = messages
    return agent


def test_clip_output_keeps_head_and_tail():
    text = "A" * 3000 + "B" * 100
    out = clip_output(text, 1000)
    assert len(out) < 1500
    assert out.startswith("A")
    assert out.endswith("B" * 100)
    assert "karo-core" in out
    assert clip_output("short", 1000) == "short"
    assert clip_output("anything", 0) == "anything"


def test_environment_clips_huge_output():
    env = LocalEnvironment(cwd=".", output_char_limit=2000)
    out = env.execute({"command": "python -c \"print('x' * 100000)\""})
    assert len(out["output"]) < 3000
    assert "karo-core" in out["output"]


def test_manage_context_never_touches_head_or_tail():
    system = {"role": "system", "content": "S" * 4000}
    task = {"role": "user", "content": "T" * 4000}
    middle = [
        {"role": "assistant", "content": "M" * 8000},
        {"role": "user", "content": "O" * 8000},
        {"role": "assistant", "content": "M" * 8000},
        {"role": "user", "content": "O" * 8000},
    ]
    tail = [{"role": "user", "content": "R" * 8000}] * 4
    agent = make_agent(
        [system, task, *middle, *tail],
        context_token_budget=10_500,  # protected head+tail alone cost ~10k tokens
        keep_recent_messages=4,
        old_observation_chars=100,
    )
    agent.manage_context()
    assert agent.n_context_events == 1
    assert system["content"] == "S" * 4000  # untouched
    assert task["content"] == "T" * 4000  # untouched
    for m in tail:
        assert m["content"] == "R" * 8000  # untouched recent tail
    assert agent._estimate_tokens() <= 10_500
    for m in middle:
        assert len(m["content"]) < 500 or agent._DROP_MARKER in m["content"]


def test_manage_context_drops_when_clipping_is_not_enough():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
        *[{"role": "user", "content": "x" * 2000} for _ in range(50)],
        *[{"role": "user", "content": "keep"} for _ in range(4)],
    ]
    agent = make_agent(messages, context_token_budget=700, keep_recent_messages=4, old_observation_chars=100)
    agent.manage_context()
    assert agent._estimate_tokens() <= 700
    dropped = sum(1 for m in messages[2:-4] if m["content"] == agent._DROP_MARKER)
    assert dropped > 0
    assert all(m["content"] == "keep" for m in messages[-4:])


def test_manage_context_disabled_and_small_histories():
    agent = make_agent(
        [{"role": "system", "content": "s"}, {"role": "user", "content": "t"}],
        context_token_budget=0,
    )
    agent.manage_context()  # disabled: no-op, no crash
    assert agent.n_context_events == 0
    big = [{"role": "system", "content": "s"}, {"role": "user", "content": "t"}]
    agent2 = make_agent(big, context_token_budget=10, keep_recent_messages=100)
    agent2.manage_context()  # guard >= history: nothing touchable, must not loop
    assert agent2.n_context_events == 0


def test_query_runs_compaction_before_model_call():
    agent = make_agent(
        [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "t"},
            {"role": "user", "content": "O" * 100_000},
            {"role": "user", "content": "O" * 100_000},
            {"role": "user", "content": "small"},
            {"role": "user", "content": "r"},
        ],
        context_token_budget=5_000,
        keep_recent_messages=2,
        old_observation_chars=100,
    )
    agent.query()
    sent = agent.model.seen[0]  # type: ignore[attr-defined]
    assert sum(len(str(m["content"])) for m in sent) // 4 <= 5_000
    assert agent.n_context_events == 1


def test_serialize_reports_context_events():
    agent = make_agent([{"role": "system", "content": "s"}, {"role": "user", "content": "t"}])
    data = agent.serialize()
    assert data["info"]["model_stats"]["context_events"] == 0


def test_tail_is_clipped_when_the_middle_cannot_free_enough():
    """The protected tail is bounded, not merely protected.

    A short history (here: no mutable middle at all) used to be resent forever
    however large it grew. Roles, order and count must survive; only bodies
    shrink.
    """

    system = {"role": "system", "content": "s"}
    task = {"role": "user", "content": "t"}
    tail = [{"role": "user", "content": "x" * 20_000} for _ in range(6)]
    agent = make_agent(
        [system, task, *tail],
        context_token_budget=2_000,
        keep_recent_messages=24,
        old_observation_chars=100,
    )
    agent.manage_context()
    assert agent._estimate_tokens() <= 2_000
    assert agent.context_overflow_tokens == 0
    assert len(agent.messages) == 8
    assert system["content"] == "s" and task["content"] == "t"
    assert all(message["role"] == "user" for message in tail)
    assert any(agent._TAIL_CLIP_MARKER in message["content"] for message in tail)
    assert agent.n_context_events == 1


def test_context_estimate_counts_tool_arguments_and_reasoning():
    huge = "x" * 40_000
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "t"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"call_id": "1", "name": "repo_write_file", "raw_arguments": huge}
            ],
            "reasoning_blocks": [{"kind": "text", "text": huge}],
        },
        {"role": "tool", "content": "ok"},
    ]
    agent = make_agent(messages, context_token_budget=1_000, keep_recent_messages=2)
    assert agent._estimate_tokens() > 20_000
    agent.manage_context()
    call = messages[2]["tool_calls"][0]
    assert call["raw_arguments"] == agent._ARGS_MARKER
    assert call["call_id"] == "1"
    # Reasoning blocks may carry provider signatures; they are counted but
    # never rewritten.
    assert messages[2]["reasoning_blocks"][0]["text"] == huge


def test_irreducible_floor_reports_the_overflow_instead_of_claiming_success():
    system = {"role": "system", "content": "S" * 8_000}
    task = {"role": "user", "content": "T" * 8_000}
    agent = make_agent(
        [system, task, {"role": "user", "content": "tail"}],
        context_token_budget=1_000,
        keep_recent_messages=4,
    )
    agent.manage_context()
    assert agent._estimate_tokens() > 1_000
    assert agent.context_overflow_tokens > 0
    assert agent.n_context_events == 0
    # The protected head is never sacrificed to silence the warning.
    assert system["content"] == "S" * 8_000
    assert task["content"] == "T" * 8_000


def test_tail_clipping_keeps_tool_call_pairing():
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "t"},
    ]
    for index in range(6):
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "call_id": f"call-{index}",
                        "name": "repo_write_file",
                        "raw_arguments": "x" * 30_000,
                    }
                ],
            }
        )
        messages.append(
            {"role": "tool", "content": "ok", "tool_call_id": f"call-{index}"}
        )
    agent = make_agent(
        messages,
        context_token_budget=1_500,
        keep_recent_messages=24,
    )
    agent.manage_context()
    for index in range(6):
        assistant = messages[2 + index * 2]
        result = messages[3 + index * 2]
        assert assistant["tool_calls"][0]["call_id"] == f"call-{index}"
        assert result["tool_call_id"] == f"call-{index}"
        assert result["role"] == "tool"
    assert agent._estimate_tokens() <= 1_500
