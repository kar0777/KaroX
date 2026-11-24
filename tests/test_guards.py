"""Tests for the finish gate and no-progress wrap-up guard."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from karox6 import cli
from karox6.core import events as ev
from karox6.core import loopguard
from karox6.core.loopguard import (
    FINISH_GATE_CAP_NOTICE,
    FINISH_GATE_MAX_STEPS,
    NO_PROGRESS_NOTICE,
    WorkspaceMonitor,
    finish_gate_message,
    is_test_file,
)
from karox6.core.messages import Usage
from karox6.core.session import Session, SessionConfig
from karox6.core.store import SessionStore, resume_kwargs
from karox6.providers.scripted import ScriptedProvider, Turn
from karox6.settings import load_settings
from karox6.tools.files import FileTracker
from karox6.tools.fs import ReadTool

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def git(ws: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=ws, check=True, capture_output=True, stdin=subprocess.DEVNULL
    )


def make_repo(tmp_path: Path, files: dict[str, str] | None = None, *, name: str = "ws") -> Path:
    ws = tmp_path / name
    ws.mkdir()
    git(ws, "init", "-q")
    git(ws, "config", "user.email", "t@example.com")
    git(ws, "config", "user.name", "t")
    initial = files or {"app.py": "def f():\n    return 1\n", "tests/test_app.py": "assert True\n"}
    for rel, content in initial.items():
        path = ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git(ws, "add", ".")
    git(ws, "commit", "-qm", "init")
    return ws


def run(session: Session, prompt: str = "task") -> list[ev.Event]:
    async def collect() -> list[ev.Event]:
        return [e async for e in session.run(prompt)]

    return asyncio.run(collect())


def finished(events: list[ev.Event]) -> ev.RunFinished:
    assert isinstance(events[-1], ev.RunFinished)
    return events[-1]


# -- finish gate --------------------------------------------------------------------------


def test_is_test_file_patterns() -> None:
    for path in (
        "tests/test_core.py",
        "tests/helpers.py",
        "pkg/test/utils.js",
        "test_top.py",
        "pkg/mod_test.py",
        "pkg/foo_test.go",
        "src/AppTest.java",
        "src/AppTests.java",
        "conftest.py",
        "pkg/conftest.py",
        "src/button.test.tsx",
        "src/button.spec.js",
        r"tests\win_helper.py",
    ):
        assert is_test_file(path), path
    for path in (
        "src/app.py",
        "src/testing/helpers.py",
        "src/contest.py",
        "src/Contest.java",
        "src/Latest.java",
        "src/inspect.py",
        "src/spec.py",
        "src/test.py",
        "src/test.go",
    ):
        assert not is_test_file(path), path


def test_finish_gate_template_is_short_and_generic() -> None:
    without_tests = finish_gate_message(["app.py"], [])
    with_tests = finish_gate_message(["app.py"], ["tests/test_app.py"])
    assert len(without_tests.split()) <= 120
    assert len(with_tests.split()) <= 120
    assert (
        "update them only if the task explicitly covers them; "
        "otherwise mention them in your final answer."
    ) in without_tests
    assert "Existing test files you modified" not in without_tests
    assert (
        "Existing test files you modified: tests/test_app.py. "
        "Keep changes the requested behaviour needs; revert only edits that weaken or delete "
        "assertions just to make tests pass."
    ) in with_tests


@needs_git
def test_finish_gate_fires_once_with_lists_and_store_record(tmp_path: Path) -> None:
    ws = make_repo(tmp_path)
    provider = ScriptedProvider(
        [
            Turn(
                calls=[
                    ("write", {"file_path": "helper.py", "content": "x = 1\n"}),
                    ("write", {"file_path": "tests/test_new.py", "content": "assert 1\n"}),
                    (
                        "edit",
                        {"file_path": "app.py", "old_string": "return 1", "new_string": "return 2"},
                    ),
                ]
            ),
            Turn(text="initial answer"),
            Turn(text="verified final answer"),
        ]
    )
    cfg = SessionConfig(model="m", workspace=ws, home=tmp_path / "home", finish_gate=True)
    session = Session(cfg, provider)
    events = run(session)
    done = finished(events)
    assert done.status == "completed"
    assert done.final_text == "verified final answer"
    assert len(provider.requests) == 3

    gate_notices = [e for e in events if isinstance(e, ev.Notice) and e.kind == "finish_gate"]
    assert len(gate_notices) == 1
    msg = gate_notices[0].text
    assert "Before you finish, check your change against the task once more." in msg
    assert "Changed files: app.py, helper.py, tests/test_new.py." in msg
    # tests/test_new.py is a newly created test file, so the existing-test clause is omitted.
    assert "Existing test files you modified" not in msg
    assert provider.requests[2].messages[-1].role == "user"
    assert provider.requests[2].messages[-1].content == msg

    records = [
        json.loads(line)
        for line in session.store.path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    gate_records = [r for r in records if r.get("kind") == "finish_gate"]
    assert len(gate_records) == 1
    assert gate_records[0]["fired"] is True
    assert gate_records[0]["step"] == 2
    assert gate_records[0]["files"] == ["app.py", "helper.py", "tests/test_new.py"]


@needs_git
def test_finish_gate_lists_modified_existing_tests_and_caps_changed_files(tmp_path: Path) -> None:
    files = {f"mod_{i:02d}.py": f"v = {i}\n" for i in range(22)}
    files["tests/test_app.py"] = "assert True\n"
    files["web/app.test.ts"] = "expect(1).toBe(1);\n"
    ws = make_repo(tmp_path, files=files)
    edits = [
        (
            "edit",
            {
                "file_path": f"mod_{i:02d}.py",
                "old_string": f"v = {i}",
                "new_string": f"v = {i + 1}",
            },
        )
        for i in range(22)
    ]
    edits.extend(
        [
            (
                "edit",
                {
                    "file_path": "tests/test_app.py",
                    "old_string": "assert True",
                    "new_string": "assert False",
                },
            ),
            (
                "edit",
                {
                    "file_path": "web/app.test.ts",
                    "old_string": "expect(1).toBe(1);",
                    "new_string": "expect(1).toBe(2);",
                },
            ),
        ]
    )

    provider = ScriptedProvider([Turn(calls=edits), Turn(text="first"), Turn(text="second")])
    session = Session(
        SessionConfig(model="m", workspace=ws, home=tmp_path / "home", finish_gate=True), provider
    )
    events = run(session)
    gate_notices = [e for e in events if isinstance(e, ev.Notice) and e.kind == "finish_gate"]
    assert len(gate_notices) == 1
    text = gate_notices[0].text
    assert "(+4 more)" in text
    assert (
        "Existing test files you modified: tests/test_app.py, web/app.test.ts. "
        "Keep changes the requested behaviour needs; revert only edits that weaken or delete "
        "assertions just to make tests pass."
    ) in text


@needs_git
def test_finish_gate_dirty_tree_excludes_preexisting_user_test_edits(tmp_path: Path) -> None:
    ws = make_repo(tmp_path)
    # User already has an uncommitted test edit before the run starts.
    (ws / "tests/test_app.py").write_text("assert 1 == 1\n", encoding="utf-8")

    # Agent only edits app.py -> user's test edit must NOT appear in changed or modified_tests.
    provider = ScriptedProvider(
        [
            Turn(
                calls=[
                    (
                        "edit",
                        {"file_path": "app.py", "old_string": "return 1", "new_string": "return 2"},
                    )
                ]
            ),
            Turn(text="initial"),
            Turn(text="final"),
        ]
    )
    events = run(
        Session(
            SessionConfig(model="m", workspace=ws, home=tmp_path / "h1", finish_gate=True),
            provider,
        )
    )
    gate_notices = [e for e in events if isinstance(e, ev.Notice) and e.kind == "finish_gate"]
    assert len(gate_notices) == 1
    msg = gate_notices[0].text
    assert "Changed files: app.py." in msg
    assert "tests/test_app.py" not in msg
    assert "Existing test files you modified" not in msg

    # If the agent DOES also edit the already-dirty test file (same byte length!), it IS listed.
    provider2 = ScriptedProvider(
        [
            Turn(
                calls=[
                    (
                        "edit",
                        {
                            "file_path": "tests/test_app.py",
                            "old_string": "assert 1 == 1",
                            "new_string": "assert 2 == 2",
                        },
                    )
                ]
            ),
            Turn(text="initial2"),
            Turn(text="final2"),
        ]
    )
    events2 = run(
        Session(
            SessionConfig(model="m", workspace=ws, home=tmp_path / "h2", finish_gate=True),
            provider2,
        )
    )
    gate_notices2 = [e for e in events2 if isinstance(e, ev.Notice) and e.kind == "finish_gate"]
    assert len(gate_notices2) == 1
    assert "Existing test files you modified: tests/test_app.py." in gate_notices2[0].text


@needs_git
def test_finish_gate_does_not_fire_without_changes_or_outside_git(tmp_path: Path) -> None:
    # Clean git repo (with an ignored file and KaroX home inside workspace)
    ws = make_repo(tmp_path, files={"app.py": "x = 1\n", ".gitignore": "ignored.log\n"})
    (ws / "ignored.log").write_text("noise\n", encoding="utf-8")
    provider = ScriptedProvider([Turn(text="nothing to change")])
    events = run(
        Session(
            SessionConfig(model="m", workspace=ws, home=ws / "home", finish_gate=True), provider
        )
    )
    assert finished(events).final_text == "nothing to change"
    assert len(provider.requests) == 1
    assert not [e for e in events if isinstance(e, ev.Notice) and e.kind == "finish_gate"]

    # Dirty git repo with pre-existing changes, but THIS run makes no edits -> does not fire.
    (ws / "app.py").write_text("x = 2\n", encoding="utf-8")
    provider_pre = ScriptedProvider([Turn(text="read-only answer")])
    events_pre = run(
        Session(
            SessionConfig(model="m", workspace=ws, home=tmp_path / "h_pre", finish_gate=True),
            provider_pre,
        )
    )
    assert finished(events_pre).final_text == "read-only answer"
    assert len(provider_pre.requests) == 1
    assert not [e for e in events_pre if isinstance(e, ev.Notice) and e.kind == "finish_gate"]

    # Non-git directory with edits
    plain = tmp_path / "plain"
    plain.mkdir()
    provider2 = ScriptedProvider(
        [Turn(calls=[("write", {"file_path": "a.py", "content": "x = 1\n"})]), Turn(text="done")]
    )
    events2 = run(
        Session(
            SessionConfig(model="m", workspace=plain, home=tmp_path / "h2", finish_gate=True),
            provider2,
        )
    )
    assert finished(events2).final_text == "done"
    assert len(provider2.requests) == 2
    assert not [e for e in events2 if isinstance(e, ev.Notice) and e.kind == "finish_gate"]


@needs_git
def test_finish_gate_skips_length_and_content_filter(tmp_path: Path) -> None:
    ws = make_repo(tmp_path)
    provider = ScriptedProvider(
        [
            Turn(
                calls=[
                    (
                        "edit",
                        {"file_path": "app.py", "old_string": "return 1", "new_string": "return 2"},
                    )
                ]
            ),
            Turn(text="cut off", finish_reason="length"),
            Turn(text="blocked", finish_reason="content_filter"),
        ]
    )
    events = run(
        Session(
            SessionConfig(model="m", workspace=ws, home=tmp_path / "home", finish_gate=True),
            provider,
        )
    )
    done = finished(events)
    assert done.status == "error"
    assert "content_filter" in done.reason
    assert len(provider.requests) == 3
    assert not [e for e in events if isinstance(e, ev.Notice) and e.kind == "finish_gate"]


@needs_git
def test_finish_gate_skips_subagents_and_read_only_sessions(tmp_path: Path) -> None:
    ws = make_repo(tmp_path)
    # Subagent spawned inside a git repo must not trigger the finish gate even after an edit.
    provider = ScriptedProvider(
        [
            Turn(
                calls=[
                    (
                        "edit",
                        {"file_path": "app.py", "old_string": "return 1", "new_string": "return 2"},
                    ),
                    ("agent", {"description": "check", "prompt": "inspect app.py"}),
                ]
            ),
            Turn(text="subagent report"),  # child finishes in 1 call (no gate)
            Turn(text="parent initial"),  # parent triggers finish gate
            Turn(text="parent final"),
        ]
    )
    parent = Session(
        SessionConfig(
            model="m", workspace=ws, home=tmp_path / "home", subagents=True, finish_gate=True
        ),
        provider,
    )
    events = run(parent)
    assert finished(events).final_text == "parent final"
    assert len(provider.requests) == 4
    agent_msg = next(m for m in parent.messages if m.role == "tool" and m.name == "agent")
    assert agent_msg.content == "subagent report"

    # Read-only session with only read-only tools also skips finish gate.
    ro_provider = ScriptedProvider([Turn(text="read only done")])
    ro_session = Session(
        SessionConfig(model="m", workspace=ws, home=tmp_path / "home-ro", finish_gate=True),
        ro_provider,
        tools=[ReadTool()],
    )
    ro_events = run(ro_session)
    assert finished(ro_events).final_text == "read only done"
    assert len(ro_provider.requests) == 1


@needs_git
def test_finish_gate_repl_fires_once_per_run_and_drops_store_reparse(tmp_path: Path) -> None:
    ws = make_repo(tmp_path)
    home = tmp_path / "home"
    cfg = SessionConfig(model="m", workspace=ws, home=home, finish_gate=True)
    assert not hasattr(SessionStore, "has_finish_gate")

    provider = ScriptedProvider(
        [
            # Task 1: edits app.py -> gate fires once
            Turn(
                calls=[
                    (
                        "edit",
                        {"file_path": "app.py", "old_string": "return 1", "new_string": "return 2"},
                    )
                ]
            ),
            Turn(text="task 1 initial"),
            Turn(text="task 1 verified"),
            # Task 2 (same REPL session): no edits -> gate does NOT fire
            Turn(text="task 2 read-only"),
            # Task 3 (same REPL session): new edit -> gate fires once for task 3
            Turn(
                calls=[
                    (
                        "edit",
                        {"file_path": "app.py", "old_string": "return 2", "new_string": "return 3"},
                    )
                ]
            ),
            Turn(text="task 3 initial"),
            Turn(text="task 3 verified"),
        ]
    )
    session = Session(cfg, provider)
    e1 = run(session, "task 1")
    assert finished(e1).final_text == "task 1 verified"
    assert len([e for e in e1 if isinstance(e, ev.Notice) and e.kind == "finish_gate"]) == 1

    e2 = run(session, "task 2")
    assert finished(e2).final_text == "task 2 read-only"
    assert not [e for e in e2 if isinstance(e, ev.Notice) and e.kind == "finish_gate"]

    e3 = run(session, "task 3")
    assert finished(e3).final_text == "task 3 verified"
    assert len([e for e in e3 if isinstance(e, ev.Notice) and e.kind == "finish_gate"]) == 1

    # Saved log records finish_gate_fired on single-pass load; resume_kwargs works cleanly.
    loaded = SessionStore.load(home, session.session_id)
    assert loaded.finish_gate_fired is True
    kwargs = resume_kwargs(home, session.session_id, workspace=ws)
    assert "finish_gate_fired" not in kwargs


@needs_git
def test_finish_gate_8_step_cap_tools_less_turn_and_gate_text_fallback(tmp_path: Path) -> None:
    ws = make_repo(tmp_path)
    # Case 1: post-gate tool turns carry preamble text, final tools-less turn gives final answer.
    turns = [
        Turn(
            calls=[
                (
                    "edit",
                    {"file_path": "app.py", "old_string": "return 1", "new_string": "return 2"},
                )
            ]
        ),
        Turn(text="initial answer"),  # step 2: triggers finish_gate (cap = 10)
        *[
            Turn(text=f"Now let me run check {i}", calls=[("glob", {"pattern": f"p{i}*.none"})])
            for i in range(FINISH_GATE_MAX_STEPS - 1)
        ],
        Turn(text="final cap summary"),  # step 10: tools-less turn
    ]
    provider = ScriptedProvider(turns)
    session = Session(
        SessionConfig(model="m", workspace=ws, home=tmp_path / "h1", finish_gate=True), provider
    )
    events = run(session)
    done = finished(events)
    assert done.status == "completed"
    assert done.reason == "finish_gate cap"
    assert done.steps == 2 + FINISH_GATE_MAX_STEPS
    assert len(provider.requests) == 2 + FINISH_GATE_MAX_STEPS
    assert done.final_text == "final cap summary"
    # Final turn was tools-less and preceded by the cap notice.
    assert provider.requests[-1].tools == []
    assert provider.requests[-1].messages[-1].content == FINISH_GATE_CAP_NOTICE
    assert len([e for e in events if isinstance(e, ev.Notice) and e.kind == "finish_gate"]) == 1

    # Case 2: turns carry preamble text AND final turn still returns a tool call (ignored) ->
    # must fall back to pre-gate answer (gate_text), NEVER tool-call preamble text.
    turns2 = [
        Turn(
            calls=[
                (
                    "edit",
                    {"file_path": "app.py", "old_string": "return 2", "new_string": "return 3"},
                )
            ]
        ),
        Turn(text="pre-gate answer"),
        *[
            Turn(text="Now let me run the tests", calls=[("glob", {"pattern": f"q{i}*.none"})])
            for i in range(FINISH_GATE_MAX_STEPS + 2)
        ],
    ]
    provider2 = ScriptedProvider(turns2)
    session2 = Session(
        SessionConfig(model="m", workspace=ws, home=tmp_path / "h2", finish_gate=True), provider2
    )
    events2 = run(session2)
    done2 = finished(events2)
    assert done2.status == "completed"
    assert done2.reason == "finish_gate cap"
    assert done2.steps == 2 + FINISH_GATE_MAX_STEPS
    assert provider2.requests[-1].tools == []
    assert done2.final_text == "pre-gate answer"


@needs_git
def test_finish_gate_skips_near_step_limit_and_deadline_and_preserves_gate_text(
    tmp_path: Path,
) -> None:
    ws = make_repo(tmp_path)
    # At max_steps - 1 (steps=2, max_steps=3 -> only 1 step left < 2): skip the gate.
    p_step = ScriptedProvider(
        [
            Turn(
                calls=[
                    (
                        "edit",
                        {"file_path": "app.py", "old_string": "return 1", "new_string": "return 2"},
                    )
                ]
            ),
            Turn(text="answer at max_steps-1"),
        ]
    )
    events_step = run(
        Session(
            SessionConfig(
                model="m", workspace=ws, home=tmp_path / "h1", max_steps=3, finish_gate=True
            ),
            p_step,
        )
    )
    done_step = finished(events_step)
    assert done_step.status == "completed"
    assert done_step.final_text == "answer at max_steps-1"
    assert len(p_step.requests) == 2
    assert not [e for e in events_step if isinstance(e, ev.Notice) and e.kind == "finish_gate"]

    # Near deadline (< 90 s remaining): skip the gate.
    p_time = ScriptedProvider(
        [
            Turn(
                calls=[
                    (
                        "edit",
                        {"file_path": "app.py", "old_string": "return 2", "new_string": "return 3"},
                    )
                ]
            ),
            Turn(text="answer near deadline"),
        ]
    )
    events_time = run(
        Session(
            SessionConfig(
                model="m", workspace=ws, home=tmp_path / "h2", max_seconds=60.0, finish_gate=True
            ),
            p_time,
        )
    )
    done_time = finished(events_time)
    assert done_time.status == "completed"
    assert done_time.final_text == "answer near deadline"
    assert len(p_time.requests) == 2
    assert not [e for e in events_time if isinstance(e, ev.Notice) and e.kind == "finish_gate"]

    # When gate DOES fire (max_steps=4, steps=2 -> 2 steps left) and post-gate hits max_steps:
    # final_text must preserve pre-gate gate_text instead of returning "".
    p_limit = ScriptedProvider(
        [
            Turn(
                calls=[
                    (
                        "edit",
                        {"file_path": "app.py", "old_string": "return 3", "new_string": "return 4"},
                    )
                ]
            ),
            Turn(text="saved pre-gate answer"),  # step 2: gate fires
            Turn(text="checking", calls=[("glob", {"pattern": "a*"})]),  # step 3
            Turn(text="still checking", calls=[("glob", {"pattern": "b*"})]),  # step 4 -> limit
        ]
    )
    events_limit = run(
        Session(
            SessionConfig(
                model="m", workspace=ws, home=tmp_path / "h3", max_steps=4, finish_gate=True
            ),
            p_limit,
        )
    )
    done_limit = finished(events_limit)
    assert done_limit.status == "limit"
    assert "step limit 4" in done_limit.reason
    assert done_limit.final_text == "saved pre-gate answer"

    # When gate fires and the next call hits the time deadline -> preserves gate_text.
    p_dl = ScriptedProvider(
        [
            Turn(
                calls=[
                    (
                        "edit",
                        {"file_path": "app.py", "old_string": "return 4", "new_string": "return 5"},
                    )
                ]
            ),
            Turn(text="saved pre-gate deadline answer"),
            Turn(text="never reached"),
        ]
    )
    sess_dl = Session(
        SessionConfig(
            model="m", workspace=ws, home=tmp_path / "h4", max_seconds=300.0, finish_gate=True
        ),
        p_dl,
    )
    real_call = sess_dl._call_model

    async def timeout_on_step3(
        step: int, started: float, emit: Any, *, allow_tools: bool = True
    ) -> Any:
        if step == 3:
            return None
        return await real_call(step, started, emit, allow_tools=allow_tools)

    sess_dl._call_model = timeout_on_step3  # type: ignore[method-assign]
    events_dl = run(sess_dl)
    done_dl = finished(events_dl)
    assert done_dl.status == "limit"
    assert "time limit" in done_dl.reason
    assert done_dl.final_text == "saved pre-gate deadline answer"


@needs_git
def test_finish_gate_does_not_rearm_after_midrun_compaction(tmp_path: Path) -> None:
    ws = make_repo(tmp_path, files={"app.py": "def f():\n    return 1\n"})
    # Keep compact_at_tokens above the system prompt (~1500 tokens) so compaction runs ONLY
    # after step 3 (after finish_gate already fired at step 2) and summarizes away the notice.
    provider = ScriptedProvider(
        [
            # Step 1: edit app.py
            Turn(
                calls=[
                    (
                        "edit",
                        {"file_path": "app.py", "old_string": "return 1", "new_string": "return 2"},
                    )
                ],
                usage=Usage(input_tokens=500, output_tokens=50, requests=1),
            ),
            # Step 2: initial answer -> fires finish_gate (cap = 10)
            Turn(
                text="initial answer",
                usage=Usage(input_tokens=600, output_tokens=50, requests=1),
            ),
            # Step 3: post-gate tool call whose usage exceeds compact_at_tokens=4000,
            # plus a long tail message (> retain=800 tokens) so finish_gate notice lands in head.
            Turn(
                text="x" * 3500,
                calls=[("glob", {"pattern": "*.py"})],
                usage=Usage(input_tokens=4500, output_tokens=100, requests=1),
            ),
            # Compaction summary call (invoked by _maybe_compact before step 4)
            Turn(text="Checkpoint summary of work so far."),
            # Step 4: after compaction dropped the finish_gate notice, model finishes ->
            # gate must NOT fire a second time!
            Turn(text="post-compaction final answer"),
        ]
    )
    cfg = SessionConfig(
        model="m",
        workspace=ws,
        home=tmp_path / "home",
        compact_at_tokens=4000,
        finish_gate=True,
        progress_guard=False,
    )
    session = Session(cfg, provider)
    events = run(session)
    done = finished(events)
    assert done.status == "completed"
    assert done.final_text == "post-compaction final answer"
    assert any(isinstance(e, ev.Compacted) for e in events)
    assert all(m.meta.get("kind") != "finish_gate" for m in session.messages)
    assert len([e for e in events if isinstance(e, ev.Notice) and e.kind == "finish_gate"]) == 1


@needs_git
def test_finish_gate_flag_off_matches_old_behaviour(tmp_path: Path) -> None:
    ws = make_repo(tmp_path)
    provider = ScriptedProvider(
        [
            Turn(
                calls=[
                    (
                        "edit",
                        {"file_path": "app.py", "old_string": "return 1", "new_string": "return 2"},
                    )
                ]
            ),
            Turn(text="done"),
        ]
    )
    cfg = SessionConfig(model="m", workspace=ws, home=tmp_path / "home")
    assert cfg.finish_gate is False
    events = run(Session(cfg, provider))
    assert finished(events).final_text == "done"
    assert len(provider.requests) == 2
    assert not [e for e in events if isinstance(e, ev.Notice) and e.kind == "finish_gate"]


# -- git edge cases: no HEAD, failure, timeout, submodules, home/backslash ignore ---------


@needs_git
def test_repo_with_no_head_supports_finish_gate_and_progress_guard(tmp_path: Path) -> None:
    ws = tmp_path / "unborn"
    ws.mkdir()
    git(ws, "init", "-q")
    provider = ScriptedProvider(
        [
            Turn(calls=[("write", {"file_path": "fresh.py", "content": "x = 1\n"})]),
            Turn(text="initial"),
            Turn(text="verified"),
        ]
    )
    events = run(
        Session(
            SessionConfig(model="m", workspace=ws, home=tmp_path / "home", finish_gate=True),
            provider,
        )
    )
    done = finished(events)
    assert done.status == "completed"
    assert done.final_text == "verified"
    gate_notices = [e for e in events if isinstance(e, ev.Notice) and e.kind == "finish_gate"]
    assert len(gate_notices) == 1
    assert "Changed files: fresh.py." in gate_notices[0].text
    assert "Existing test files you modified" not in gate_notices[0].text


@needs_git
def test_git_failure_keeps_previous_hash_and_timeout_disables_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = make_repo(tmp_path)

    # 1. Transient failure (returns None / rc=128) keeps previous hash and stays in git mode.
    real_run_git = loopguard._run_git
    calls = 0

    def flaky_git(
        workspace: Path, *args: str, timeout: float = 20.0
    ) -> subprocess.CompletedProcess[bytes] | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_run_git(workspace, *args, timeout=timeout)
        if calls == 2:
            return None
        return subprocess.CompletedProcess(["git", *args], 128, b"", b"transient error")

    monkeypatch.setattr(loopguard, "_run_git", flaky_git)
    monitor = WorkspaceMonitor(ws)
    start_hash = monitor.initialize()
    assert monitor.in_git is True
    assert monitor.disabled is False
    # Transient failures must NOT switch to nogit or alter the hash.
    assert monitor.diff_hash() == start_hash
    assert monitor.diff_hash() == start_hash
    assert monitor.in_git is True
    assert monitor.disabled is False

    # 2. Timeout disables the guard for the rest of the run (no more git calls after timeout).
    timeout_calls = 0

    def timeout_on_second(
        workspace: Path, *args: str, timeout: float = 20.0
    ) -> subprocess.CompletedProcess[bytes] | None:
        nonlocal timeout_calls
        timeout_calls += 1
        if timeout_calls == 1:
            return real_run_git(workspace, *args, timeout=timeout)
        raise subprocess.TimeoutExpired(["git", *args], timeout)

    monkeypatch.setattr(loopguard, "_run_git", timeout_on_second)
    turns = [Turn(calls=[("glob", {"pattern": f"t{i}*"})]) for i in range(14)] + [Turn(text="done")]
    provider = ScriptedProvider(turns)
    cfg = SessionConfig(model="m", workspace=ws, home=tmp_path / "home_to", finish_gate=False)
    events = run(Session(cfg, provider))
    assert finished(events).status == "completed"
    # Call 1 at initialize(), call 2 on step 1 raises TimeoutExpired -> disabled, 0 calls after!
    assert timeout_calls == 2
    assert not [e for e in events if isinstance(e, ev.Notice) and e.kind == "progress_guard"]


@needs_git
def test_submodule_edits_update_hash_and_list_inner_files_in_finish_gate(tmp_path: Path) -> None:
    sub_src = make_repo(
        tmp_path, files={"lib.py": "val = 1\n", "tests/test_lib.py": "assert True\n"}, name="subsrc"
    )
    ws = make_repo(tmp_path, files={"app.py": "x = 1\n"}, name="main")
    git(ws, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub_src), "sub")
    git(ws, "commit", "-qm", "add submodule")

    # Repeated edits inside the submodule must change the workspace hash every time (no false stall)
    # and the finish gate must list sub/lib.py (not just "sub").
    monitor = WorkspaceMonitor(ws)
    h0 = monitor.initialize()
    (ws / "sub" / "lib.py").write_text("val = 2\n", encoding="utf-8")
    h1 = monitor.diff_hash()
    assert h1 != h0
    (ws / "sub" / "lib.py").write_text("val = 3\n", encoding="utf-8")
    h2 = monitor.diff_hash()
    assert h2 != h1
    changes = monitor.changes_since_start()
    assert changes is not None
    assert changes.changed == ["sub/lib.py"]
    assert changes.modified_tests == []

    # Now edit an existing test inside the submodule as well -> listed in modified_tests.
    (ws / "sub" / "tests" / "test_lib.py").write_text("assert 1 == 1\n", encoding="utf-8")
    changes2 = monitor.changes_since_start()
    assert changes2 is not None
    assert changes2.changed == ["sub/lib.py", "sub/tests/test_lib.py"]
    assert changes2.modified_tests == ["sub/tests/test_lib.py"]


@needs_git
def test_home_inside_workspace_and_backslash_ignore_entries(tmp_path: Path) -> None:
    ws = make_repo(tmp_path)
    home = ws / ".karox" / "home"
    # With KaroX home inside the workspace, session.jsonl writes every step must NOT count as
    # workspace progress (progress guard still fires at step 12) and must NOT trigger finish_gate.
    turns = [Turn(calls=[("glob", {"pattern": f"k{i}*"})]) for i in range(13)] + [Turn(text="done")]
    provider = ScriptedProvider(turns)
    cfg = SessionConfig(model="m", workspace=ws, home=home, finish_gate=True)
    events = run(Session(cfg, provider))
    assert finished(events).final_text == "done"
    pg_notices = [e for e in events if isinstance(e, ev.Notice) and e.kind == "progress_guard"]
    assert len(pg_notices) == 1
    assert not [e for e in events if isinstance(e, ev.Notice) and e.kind == "finish_gate"]

    # Explicit backslash ignore entries are normalized on both sides.
    monitor = WorkspaceMonitor(ws, ignore=(r".karox\home",))
    h0 = monitor.initialize()
    (home / "extra.log").write_text("more data\n", encoding="utf-8")
    assert monitor.diff_hash() == h0
    assert monitor.changes_since_start() is None

    tracker = FileTracker(ws, ignore=(r".karox\home",))
    tracker.record_write(home / "extra.log")
    assert tracker.changed_files() == []


# -- progress guard -----------------------------------------------------------------------


@needs_git
def test_progress_guard_fires_once_after_step_12_and_not_before(tmp_path: Path) -> None:
    ws = make_repo(tmp_path)
    # 11 tool steps + finish: must NOT fire before step 12.
    short_turns = [Turn(calls=[("glob", {"pattern": f"x{i}*"})]) for i in range(11)] + [
        Turn(text="done")
    ]
    short_provider = ScriptedProvider(short_turns)
    short_cfg = SessionConfig(model="m", workspace=ws, home=tmp_path / "h1", finish_gate=False)
    short_events = run(Session(short_cfg, short_provider))
    assert not [e for e in short_events if isinstance(e, ev.Notice) and e.kind == "progress_guard"]

    # 15 tool steps + finish: fires once right after step 12 (visible on step 13 request).
    long_turns = [Turn(calls=[("glob", {"pattern": f"y{i}*"})]) for i in range(15)] + [
        Turn(text="done")
    ]
    long_provider = ScriptedProvider(long_turns)
    long_cfg = SessionConfig(model="m", workspace=ws, home=tmp_path / "h2", finish_gate=False)
    long_events = run(Session(long_cfg, long_provider))
    notices = [e for e in long_events if isinstance(e, ev.Notice) and e.kind == "progress_guard"]
    assert len(notices) == 1
    assert notices[0].text == NO_PROGRESS_NOTICE
    assert all(
        NO_PROGRESS_NOTICE not in m.content
        for req in long_provider.requests[:12]
        for m in req.messages
    )
    assert long_provider.requests[12].messages[-1].content == NO_PROGRESS_NOTICE
    assert all(req.max_output_tokens is None for req in long_provider.requests)


@needs_git
def test_progress_guard_counts_new_shell_commands_as_progress(tmp_path: Path) -> None:
    ws = make_repo(tmp_path)
    # 15 distinct read-only shell commands: exploration through the shell is progress.
    new_turns = [Turn(calls=[("bash", {"command": f"echo explore {i}"})]) for i in range(15)] + [
        Turn(text="done")
    ]
    cfg = SessionConfig(model="m", workspace=ws, home=tmp_path / "h1", finish_gate=False)
    events = run(Session(cfg, ScriptedProvider(new_turns)))
    assert not [e for e in events if isinstance(e, ev.Notice) and e.kind == "progress_guard"]

    # The same command repeated (whitespace differences ignored) is not progress.
    same = [
        Turn(calls=[("bash", {"command": "echo  same" if i % 2 else "echo same"})])
        for i in range(15)
    ] + [Turn(text="done")]
    cfg2 = SessionConfig(model="m", workspace=ws, home=tmp_path / "h2", finish_gate=False)
    events2 = run(Session(cfg2, ScriptedProvider(same)))
    assert len([e for e in events2 if isinstance(e, ev.Notice) and e.kind == "progress_guard"]) == 1


@needs_git
def test_progress_guard_resets_on_diff_change(tmp_path: Path) -> None:
    ws = make_repo(tmp_path)
    turns = [
        *[Turn(calls=[("glob", {"pattern": f"a{i}*"})]) for i in range(9)],
        # Step 10 edits app.py -> diff changes -> resets streak to 0.
        Turn(
            calls=[
                (
                    "edit",
                    {"file_path": "app.py", "old_string": "return 1", "new_string": "return 2"},
                )
            ]
        ),
        # Steps 11..19 (9 steps without diff change): still < 10 -> must NOT fire yet.
        *[Turn(calls=[("glob", {"pattern": f"b{i}*"})]) for i in range(9)],
        # Step 20 (10th consecutive no-progress step): fires once!
        Turn(calls=[("glob", {"pattern": "c0*"})]),
        Turn(text="done"),
    ]
    provider = ScriptedProvider(turns)
    cfg = SessionConfig(model="m", workspace=ws, home=tmp_path / "home", finish_gate=False)
    events = run(Session(cfg, provider))
    notices = [e for e in events if isinstance(e, ev.Notice) and e.kind == "progress_guard"]
    assert len(notices) == 1
    assert all(
        NO_PROGRESS_NOTICE not in m.content for req in provider.requests[:20] for m in req.messages
    )
    assert provider.requests[20].messages[-1].content == NO_PROGRESS_NOTICE


@needs_git
def test_progress_guard_resets_stall_on_first_time_read_only(tmp_path: Path) -> None:
    files = {f"f{i}.txt": f"line {i}\n" for i in range(5)}
    ws = make_repo(tmp_path, files=files)
    turns = [
        *[Turn(calls=[("glob", {"pattern": f"g{i}*"})]) for i in range(9)],
        # Step 10 reads f0.txt for the FIRST time -> resets no-progress streak.
        Turn(calls=[("read", {"file_path": "f0.txt"})]),
        # Steps 11..12 re-read f0.txt and run glob -> streak is only 2 (< 10), no notice!
        Turn(calls=[("read", {"file_path": "f0.txt"})]),
        Turn(calls=[("glob", {"pattern": "g99*"})]),
        Turn(text="done"),
    ]
    provider = ScriptedProvider(turns)
    cfg = SessionConfig(model="m", workspace=ws, home=tmp_path / "home", finish_gate=False)
    events = run(Session(cfg, provider))
    assert not [e for e in events if isinstance(e, ev.Notice) and e.kind == "progress_guard"]


@needs_git
def test_progress_guard_token_trigger_requires_step_12_and_resets_on_diff(
    tmp_path: Path,
) -> None:
    files = {f"f{i:02d}.txt": f"line {i}\n" for i in range(18)}
    files["app.py"] = "def f():\n    return 1\n"
    ws = make_repo(tmp_path, files=files)
    big = Usage(input_tokens=1000, output_tokens=10_000, requests=1)

    # 1. Exploration-only run BEFORE step 12 (11 steps, 110k output tokens, 11 no-diff steps):
    # must NOT fire before step 12!
    early_turns = [
        *[Turn(calls=[("read", {"file_path": f"f{i:02d}.txt"})], usage=big) for i in range(11)],
        Turn(text="done"),
    ]
    p_early = ScriptedProvider(early_turns)
    e_early = run(
        Session(
            SessionConfig(model="m", workspace=ws, home=tmp_path / "h1", finish_gate=False),
            p_early,
        )
    )
    assert not [e for e in e_early if isinstance(e, ev.Notice) and e.kind == "progress_guard"]

    # 2. Exploration-only run reaching step 12 (>60k tokens since start, >=8 no-diff steps):
    # fires right after step 12 (visible on request 12, i.e., step 13).
    step12_turns = [
        *[Turn(calls=[("read", {"file_path": f"f{i:02d}.txt"})], usage=big) for i in range(13)],
        Turn(text="done"),
    ]
    p_step12 = ScriptedProvider(step12_turns)
    e_step12 = run(
        Session(
            SessionConfig(model="m", workspace=ws, home=tmp_path / "h2", finish_gate=False),
            p_step12,
        )
    )
    notices = [e for e in e_step12 if isinstance(e, ev.Notice) and e.kind == "progress_guard"]
    assert len(notices) == 1
    assert all(
        NO_PROGRESS_NOTICE not in m.content for req in p_step12.requests[:12] for m in req.messages
    )
    assert p_step12.requests[12].messages[-1].content == NO_PROGRESS_NOTICE

    # 3. Output tokens since last diff change: spending 70k before a diff change at step 6,
    # then only 40k across 8 no-diff steps (steps 7..14) must NOT trigger!
    small = Usage(input_tokens=1000, output_tokens=5_000, requests=1)
    reset_turns: list[Any] = [
        *[Turn(calls=[("read", {"file_path": f"f{i:02d}.txt"})], usage=big) for i in range(5)],
        Turn(
            calls=[
                (
                    "edit",
                    {"file_path": "app.py", "old_string": "return 1", "new_string": "return 2"},
                )
            ],
            usage=big,
        ),  # step 6: 60k total, diff changed -> token count since diff resets to 0
        *[
            Turn(calls=[("read", {"file_path": f"f{i:02d}.txt"})], usage=small)
            for i in range(5, 13)
        ],  # steps 7..14: 8 no-diff steps, 40k tokens since diff (<= 60k) -> no fire
        Turn(text="done"),
    ]
    p_reset = ScriptedProvider(reset_turns)
    e_reset = run(
        Session(
            SessionConfig(model="m", workspace=ws, home=tmp_path / "h3", finish_gate=False),
            p_reset,
        )
    )
    assert not [e for e in e_reset if isinstance(e, ev.Notice) and e.kind == "progress_guard"]


@needs_git
def test_progress_guard_flag_off_matches_old_behaviour(tmp_path: Path) -> None:
    ws = make_repo(tmp_path)
    turns = [Turn(calls=[("glob", {"pattern": f"z{i}*"})]) for i in range(15)] + [Turn(text="done")]
    provider = ScriptedProvider(turns)
    cfg = SessionConfig(
        model="m", workspace=ws, home=tmp_path / "home", finish_gate=False, progress_guard=False
    )
    events = run(Session(cfg, provider))
    assert finished(events).status == "completed"
    assert not [e for e in events if isinstance(e, ev.Notice)]


# -- CLI and config flags -----------------------------------------------------------------


def test_flags_defaults_and_cli_config_switchable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KAROX_HOME", str(tmp_path))
    # Defaults: finish_gate OFF, progress_guard ON
    default_cfg = SessionConfig(model="m")
    assert default_cfg.finish_gate is False
    assert default_cfg.progress_guard is True

    args = cli._parser().parse_args(["run", "--model", "m", "task"])
    cfg = cli._session_config(args, tmp_path, tmp_path / "home", stream_deltas=False)
    assert cfg.finish_gate is False
    assert cfg.progress_guard is True

    # CLI flags toggle both
    args_toggled = cli._parser().parse_args(
        ["run", "--model", "m", "--finish-gate", "--no-progress-guard", "task"]
    )
    cfg_toggled = cli._session_config(
        args_toggled, tmp_path, tmp_path / "home", stream_deltas=False
    )
    assert cfg_toggled.finish_gate is True
    assert cfg_toggled.progress_guard is False

    # config.toml switches both, and CLI flags can override them back
    (tmp_path / "config.toml").write_text(
        "finish_gate = true\nprogress_guard = false\n", encoding="utf-8"
    )
    settings = load_settings()
    assert settings == {"finish_gate": True, "progress_guard": False}
    args_cfg = cli._parser(settings).parse_args(["run", "--model", "m", "task"])
    assert args_cfg.finish_gate is True
    assert args_cfg.progress_guard is False
    args_override = cli._parser(settings).parse_args(
        ["run", "--model", "m", "--no-finish-gate", "--progress-guard", "task"]
    )
    assert args_override.finish_gate is False
    assert args_override.progress_guard is True
