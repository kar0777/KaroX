"""karo-core finish contract: a prose final answer is a submission, not a crash.

Port 3 of the KaroX task loop. The stage-A evidence: almost every race run ended
in RepeatedFormatError while the finished patch sat in the working tree, because
the model answers "the solution is complete" in prose and the protocol has no
such exit (plopp a1/a2, pelita smoke). These tests pin the three-way decision:
prose -> Submitted, truncated -> RepeatedFormatError, empty -> RepeatedFormatError.
"""

from __future__ import annotations

from karox.engine.agents.default import DefaultAgent
from karox.engine.environments.local import LocalEnvironment
from karox.engine.exceptions import FormatError


class ProseModel:
    """Always fails to produce a tool call and answers in prose."""

    def __init__(self, *, content: str = "The solution is complete.", finish_reason: str = "stop"):
        self.content = content
        self.finish_reason = finish_reason
        self.calls = 0

    def query(self, messages: list[dict], **kwargs) -> dict:
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
                                "finish_reason": self.finish_reason,
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


def make_agent(model: ProseModel, **config) -> DefaultAgent:
    return DefaultAgent(
        model,  # type: ignore[arg-type]
        LocalEnvironment(cwd="."),  # type: ignore[arg-type]
        system_template="sys",
        instance_template="task",
        step_limit=0,
        cost_limit=10**9,
        wall_time_limit_seconds=0,
        max_consecutive_format_errors=3,
        **config,
    )


def test_prose_final_answer_submits():
    model = ProseModel(content="  The solution is complete.  ")
    agent = make_agent(model)
    agent.run(task="fix it")

    assert model.calls == 3, "the model must get its full format-error budget first"
    assert agent.messages[-1]["extra"]["exit_status"] == "Submitted"
    assert agent.messages[-1]["extra"]["submission"] == "The solution is complete."
    assert agent.messages[-1]["content"] == "The solution is complete."


def test_truncated_answer_is_not_a_submission():
    model = ProseModel(content="The solution is comp", finish_reason="length")
    agent = make_agent(model)
    agent.run(task="fix it")

    assert agent.messages[-1]["extra"]["exit_status"] == "RepeatedFormatError"


def test_empty_answer_is_not_a_submission():
    model = ProseModel(content="   ")
    agent = make_agent(model)
    agent.run(task="fix it")

    assert agent.messages[-1]["extra"]["exit_status"] == "RepeatedFormatError"


def test_flag_disables_submit_on_final_answer():
    model = ProseModel()
    agent = make_agent(model, submit_on_final_answer=False)
    agent.run(task="fix it")

    assert agent.messages[-1]["extra"]["exit_status"] == "RepeatedFormatError"
    assert model.calls == 3


def test_action_fence_is_not_a_final_answer():
    """sentry-4179 v3: 'Let me look at the tests...' + a command fence was
    accepted as a finish and the run ended at step 6 with nothing done."""
    model = ProseModel(content='Let me look at the tests.\n\n```mswea_bash_command\ngrep -rn x tests/\n```')
    agent = make_agent(model)
    agent.run(task="fix it")

    assert agent.messages[-1]["extra"]["exit_status"] == "RepeatedFormatError"
    assert model.calls == 3