"""The base prompt's rules on the scope of a change and on how it is checked.

In the saved comparison runs (same model, DSH's prompt has no such rules) KaroX's reasoning
quoted "smallest general change" in 17 of 17 trials and "do not broaden a contract" in 9 of
17, and in the tasks it lost it used them to leave out the neighbouring cases the hidden
tests checked. These tests pin the wording that replaced those two sentences and the order
to add tests.
"""

import asyncio
import re

from karox6.core.prompt import BASE_PROMPT, build_system_prompt
from karox6.core.session import Session, SessionConfig
from karox6.providers.scripted import ScriptedProvider, Turn
from karox6.tools.shell import detect_shell

NAMED = re.compile(
    r"\b(?:pytest|unittest|tox|jest|vitest|mocha|cargo|gradle|maven|npm|pnpm|yarn|pip|django|"
    r"flask|fastapi|numpy|pandas|react|github|python|javascript|typescript|rust|java)\b",
    re.IGNORECASE,
)


def _section(title: str) -> str:
    body = BASE_PROMPT.split(f"# {title}\n", 1)[1]
    return body.split("\n\n", 1)[0]


def _first_request_system(tmp_path, **kwargs) -> str:
    provider = ScriptedProvider([Turn(text="done")])
    config = SessionConfig(model="test", workspace=tmp_path, home=tmp_path / "home", **kwargs)

    async def collect():
        return [event async for event in Session(config, provider).run("task")]

    asyncio.run(collect())
    return provider.requests[0].system


def test_prompt_stays_within_its_word_limit():
    assert len(BASE_PROMPT.split()) <= 625


def test_examples_are_samples_of_the_inputs_a_task_is_about():
    explore = _section("Explore and understand")
    assert "What the task states is exact" in explore
    assert "accepted and rejected types" in explore
    assert "Its examples are samples" in explore
    assert "belong to the task unless it rules them out" in explore


def test_the_change_fixes_the_cause_and_leaves_the_rest_alone():
    change = _section("Make the change")
    assert "Fix the cause, not only the reported example" in change
    assert "Leave behaviour the task does not concern as it is" in change
    assert "no unrelated refactors" in change


def test_the_sentences_quoted_to_narrow_a_fix_are_gone():
    lowered = BASE_PROMPT.lower()
    assert "smallest" not in lowered
    assert "broaden" not in lowered
    assert "exact contract" not in lowered


def test_checks_go_beyond_the_example_of_the_task():
    verify = _section("Verify")
    assert "Passing the task's example proves only that example" in verify
    assert "inputs the task did not spell out" in verify
    assert "another form of the same input" in verify
    assert "an input that must be rejected" in verify
    assert "another path to the same code" in verify
    assert "fix what fails for the same cause" in verify
    assert "not from your implementation" in verify


def test_no_standing_order_to_add_tests():
    """The bench task says to leave its tests alone; "add a few focused tests" made KaroX
    edit a test file in 13 of 17 trials all the same (DSH, without the order: 6)."""
    verify = _section("Verify")
    assert "Whether to add tests is the task's and the project's call" in verify
    assert "distinctive names so they cannot collide" in verify
    assert "Delete temporary scripts before finishing" in verify
    assert not re.search(r"\badd (?:a few |some |focused )+tests\b", BASE_PROMPT, re.IGNORECASE)


def test_prompt_names_no_project_library_or_test_runner():
    """DECISIONS.md D-021: no rule may mention a specific task, repository or library."""
    assert not NAMED.search(BASE_PROMPT)
    assert not re.search(r"#\d+|https?://", BASE_PROMPT)


def test_the_model_receives_the_rules(tmp_path):
    system = _first_request_system(tmp_path)
    assert system.startswith(BASE_PROMPT + "\n\nEnvironment:")
    assert "Its examples are samples" in system
    assert "inputs the task did not spell out" in system


def test_an_appended_variant_follows_the_rules_it_extends(tmp_path):
    system = _first_request_system(tmp_path, system_append="Variant rule.")
    assert system.startswith(BASE_PROMPT + "\n\nVariant rule.\n\nEnvironment:")
    assert build_system_prompt(tmp_path, detect_shell(), base="Other base.").startswith(
        "Other base.\n\nEnvironment:"
    )
