"""karo-core no-progress detector: a repeated identical step ends the run.

Port 4 of the KaroX task loop. Evidence: goodconf v2a2 spent 512 of 641 calls
on `echo "DONE"` / `echo "Task complete."` with byte-identical output, hit the
wall clock and never submitted. Four identical action+observation pairs are a
finished agent, not work in progress.
"""

from __future__ import annotations

from karox.engine.agents.default import DefaultAgent


class FakeEnv:
    """Returns the same output per command; records how many steps ran."""

    def __init__(self, *, vary_output: bool = False):
        self.calls = 0
        self.vary_output = vary_output

    def execute(self, action: dict, cwd: str = "", *, timeout=None) -> dict:
        self.calls += 1
        output = f"out {self.calls}\n" if self.vary_output else "DONE\n"
        return {"output": output, "returncode": 0, "exception_info": ""}

    def get_template_vars(self, **kwargs):
        return {}

    def serialize(self):
        return {}


class ScriptedModel:
    """Replays a fixed command list (or keeps repeating the last command)."""

    def __init__(self, commands: list[str], content: str = "Task complete."):
        self.commands = commands
        self.content = content
        self.n = 0

    def query(self, messages: list[dict], **kwargs) -> dict:
        command = self.commands[self.n % len(self.commands)]
        self.n += 1
        return {
            "role": "assistant",
            "content": self.content,
            "extra": {"actions": [{"command": command}], "cost": 0.0},
        }

    def get_template_vars(self, **kwargs):
        return {}

    def format_message(self, **kwargs):
        return {"role": kwargs["role"], "content": kwargs["content"]}

    def format_observation_messages(self, message, outputs, template_vars=None):
        return [{"role": "user", "content": str(o.get("output", ""))} for o in outputs]

    def serialize(self):
        return {}


def make_agent(model: ScriptedModel, env: FakeEnv, **config) -> DefaultAgent:
    return DefaultAgent(
        model,  # type: ignore[arg-type]
        env,  # type: ignore[arg-type]
        system_template="sys",
        instance_template="task",
        step_limit=10,
        cost_limit=10**9,
        wall_time_limit_seconds=0,
        **config,
    )


def test_repeated_identical_step_submits():
    model = ScriptedModel(['cd /testbed && echo "DONE"'])
    env = FakeEnv()
    agent = make_agent(model, env)
    agent.run(task="fix it")

    assert env.calls == 4, "the fourth identical step ends the run"
    assert agent.messages[-1]["extra"]["exit_status"] == "Submitted"
    assert agent.messages[-1]["extra"]["exit_reason"] == "no-progress"
    assert agent.messages[-1]["extra"]["submission"] == "Task complete."


def test_different_commands_do_not_submit():
    model = ScriptedModel(["echo a", "echo b"])
    env = FakeEnv()
    agent = make_agent(model, env)
    agent.run(task="fix it")

    assert env.calls == 10
    assert agent.messages[-1]["extra"]["exit_status"] == "LimitsExceeded"


def test_same_command_with_new_output_is_progress():
    model = ScriptedModel(["pytest -q"])
    env = FakeEnv(vary_output=True)
    agent = make_agent(model, env)
    agent.run(task="fix it")

    assert env.calls == 10
    assert agent.messages[-1]["extra"]["exit_status"] == "LimitsExceeded"


def test_streak_zero_disables_the_check():
    model = ScriptedModel(['echo "DONE"'])
    env = FakeEnv()
    agent = make_agent(model, env, no_progress_streak=0)
    agent.run(task="fix it")

    assert env.calls == 10
    assert agent.messages[-1]["extra"]["exit_status"] == "LimitsExceeded"

def _notes(agent: DefaultAgent) -> list[dict]:
    return [m for m in agent.messages if m.get("kind") == "engine_note"]


def test_the_step_before_the_limit_warns_the_model_once():
    model = ScriptedModel(['echo "DONE"'])
    env = FakeEnv()
    agent = make_agent(model, env)
    agent.run(task="fix it")

    assert env.calls == 4, "the note does not change when the run ends"
    notes = _notes(agent)
    assert len(notes) == 1
    assert "same action and got the same result" in notes[0]["content"]
    note_index = agent.messages.index(notes[0])
    # Placed after the third observation, never between a call and its result.
    assert agent.messages[note_index - 1]["content"] == "DONE\n"
    assert agent.messages[note_index + 1]["role"] == "assistant"
    assert sum(1 for m in agent.messages[:note_index] if m.get("role") == "assistant") == 3


def test_a_model_that_changes_course_after_the_note_keeps_working():
    model = ScriptedModel(['echo "DONE"'] * 3 + ["pytest -q"] * 7)
    env = FakeEnv()
    agent = make_agent(model, env)
    agent.run(task="fix it")

    assert len(_notes(agent)) >= 1
    assert agent.messages[-1]["extra"]["exit_status"] == "Submitted"
    assert agent.messages[-1]["extra"]["exit_reason"] == "no-progress"
    assert env.calls == 7, "three echoes, then four identical pytest steps"


def test_two_alternating_actions_with_unchanged_results_are_noted_once():
    model = ScriptedModel(["echo a", "echo b"])
    env = FakeEnv()
    agent = make_agent(model, env)
    agent.run(task="fix it")

    assert env.calls == 10, "a cycle is only noted, never submitted"
    assert agent.messages[-1]["extra"]["exit_status"] == "LimitsExceeded"
    notes = _notes(agent)
    assert len(notes) == 1
    assert "alternated between the same two actions" in notes[0]["content"]


def test_varying_output_and_disabled_note_add_nothing():
    varying = make_agent(ScriptedModel(["pytest -q"]), FakeEnv(vary_output=True))
    varying.run(task="fix it")
    assert _notes(varying) == []

    silent_env = FakeEnv()
    silent = make_agent(ScriptedModel(['echo "DONE"']), silent_env, no_progress_note=False)
    silent.run(task="fix it")
    assert silent_env.calls == 4
    assert _notes(silent) == []
