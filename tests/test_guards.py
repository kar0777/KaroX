"""Tests for the finish gate and no-progress wrap-up guard."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from karox6 import cli
from karox6.core import events as ev
from karox6.core.loopguard import (
    FINISH_GATE_MAX_STEPS,
    FINISH_GATE_TEMPLATE,
    NO_PROGRESS_NOTICE,
    is_test_file,
)
from karox6.core.messages import Usage
from karox6.core.session import Session, SessionConfig
from karox6.core.store import SessionStore, resume_kwargs
from karox6.providers.scripted import ScriptedProvider, Turn
from karox6.settings import load_settings
from karox6.tools.fs import ReadTool

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def git(ws: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=ws, check=True, capture_output=True, stdin=subprocess.DEVNULL
    )


def make_repo(tmp_path: Path, files: dict[str, str] | None = None) -> Path:
    ws = tmp_path / "ws"
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
        "src/button.test.tsx",
        "src/button.spec.js",
        r"tests\win_helper.py",
    ):
        assert is_test_file(path), path
    for path in (
        "src/app.py",
        "src/testing/helpers.py",
        "src/contest.py",
        "src/inspect.py",
        "src/spec.py",
        "src/test.py",
    ):
        assert not is_test_file(path), path


def test_finish_gate_template_is_short_and_generic() -> None:
    assert len(FINISH_GATE_TEMPLATE.split()) <= 120


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
    cfg = SessionConfig(model="m", workspace=ws, home=tmp_path / "home")
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
    # tests/test_new.py is a newly created test file, not an existing modified test file.
    assert "Existing test files you modified: none —" in msg
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
    session = Session(SessionConfig(model="m", workspace=ws, home=tmp_path / "home"), provider)
    events = run(session)
    gate_notices = [e for e in events if isinstance(e, ev.Notice) and e.kind == "finish_gate"]
    assert len(gate_notices) == 1
    text = gate_notices[0].text
    assert "(+4 more)" in text
    assert "Existing test files you modified: tests/test_app.py, web/app.test.ts —" in text


@needs_git
def test_finish_gate_does_not_fire_without_changes_or_outside_git(tmp_path: Path) -> None:
    # Clean git repo (with an ignored file and KaroX home inside workspace)
    ws = make_repo(tmp_path, files={"app.py": "x = 1\n", ".gitignore": "ignored.log\n"})
    (ws / "ignored.log").write_text("noise\n", encoding="utf-8")
    provider = ScriptedProvider([Turn(text="nothing to change")])
    events = run(Session(SessionConfig(model="m", workspace=ws, home=ws / "home"), provider))
    assert finished(events).final_text == "nothing to change"
    assert len(provider.requests) == 1
    assert not [e for e in events if isinstance(e, ev.Notice) and e.kind == "finish_gate"]

    # Dirty git repo with pre-existing changes, but THIS run makes no edits -> does not fire.
    (ws / "app.py").write_text("x = 2\n", encoding="utf-8")
    provider_pre = ScriptedProvider([Turn(text="read-only answer")])
    events_pre = run(
        Session(SessionConfig(model="m", workspace=ws, home=tmp_path / "h_pre"), provider_pre)
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
        Session(SessionConfig(model="m", workspace=plain, home=tmp_path / "h2"), provider2)
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
    events = run(Session(SessionConfig(model="m", workspace=ws, home=tmp_path / "home"), provider))
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
        SessionConfig(model="m", workspace=ws, home=tmp_path / "home", subagents=True),
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
        SessionConfig(model="m", workspace=ws, home=tmp_path / "home-ro"),
        ro_provider,
        tools=[ReadTool()],
    )
    ro_events = run(ro_session)
    assert finished(ro_events).final_text == "read only done"
    assert len(ro_provider.requests) == 1


@needs_git
def test_finish_gate_does_not_fire_again_after_resume(tmp_path: Path) -> None:
    ws = make_repo(tmp_path)
    home = tmp_path / "home"
    cfg = SessionConfig(model="m", workspace=ws, home=home)

    first = Session(
        cfg,
        ScriptedProvider(
            [
                Turn(
                    calls=[
                        (
                            "edit",
                            {
                                "file_path": "app.py",
                                "old_string": "return 1",
                                "new_string": "return 2",
                            },
                        )
                    ]
                ),
                Turn(text="first"),
                Turn(text="verified"),
            ]
        ),
    )
    run(first)

    # Resume via resume_kwargs and make another edit: gate must STILL NOT fire again.
    kwargs = resume_kwargs(home, first.session_id, workspace=ws)
    assert kwargs["finish_gate_fired"] is True
    p2 = ScriptedProvider(
        [
            Turn(
                calls=[
                    (
                        "edit",
                        {"file_path": "app.py", "old_string": "return 2", "new_string": "return 3"},
                    )
                ]
            ),
            Turn(text="resumed done"),
        ]
    )
    resumed = Session(cfg, p2, **kwargs)
    events2 = run(resumed, "follow-up")
    assert finished(events2).final_text == "resumed done"
    assert len(p2.requests) == 2
    assert not [e for e in events2 if isinstance(e, ev.Notice) and e.kind == "finish_gate"]

    # Even if compaction dropped the finish_gate notice, the store record still blocks it.
    loaded = SessionStore.load(home, first.session_id)
    stripped = [m for m in loaded.messages if m.meta.get("kind") != "finish_gate"]
    p3 = ScriptedProvider(
        [
            Turn(
                calls=[
                    (
                        "edit",
                        {"file_path": "app.py", "old_string": "return 3", "new_string": "return 4"},
                    )
                ]
            ),
            Turn(text="still no gate"),
        ]
    )
    resumed_stripped = Session(cfg, p3, session_id=first.session_id, messages=stripped)
    events3 = run(resumed_stripped, "again")
    assert finished(events3).final_text == "still no gate"
    assert len(p3.requests) == 2


@needs_git
def test_finish_gate_respects_8_step_cap_and_finishes_normally(tmp_path: Path) -> None:
    ws = make_repo(tmp_path)
    turns = [
        Turn(
            calls=[
                (
                    "edit",
                    {"file_path": "app.py", "old_string": "return 1", "new_string": "return 2"},
                )
            ]
        ),
        Turn(text="initial answer"),  # step 2: triggers finish_gate
        *[Turn(calls=[("glob", {"pattern": f"p{i}*.none"})]) for i in range(12)],
    ]
    provider = ScriptedProvider(turns)
    session = Session(SessionConfig(model="m", workspace=ws, home=tmp_path / "home"), provider)
    events = run(session)
    done = finished(events)
    assert done.status == "completed"
    assert done.steps == 2 + FINISH_GATE_MAX_STEPS
    assert len(provider.requests) == 2 + FINISH_GATE_MAX_STEPS
    assert done.final_text == "initial answer"


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
    cfg = SessionConfig(model="m", workspace=ws, home=tmp_path / "home", finish_gate=False)
    events = run(Session(cfg, provider))
    assert finished(events).final_text == "done"
    assert len(provider.requests) == 2
    assert not [e for e in events if isinstance(e, ev.Notice) and e.kind == "finish_gate"]


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
    # Not present before step 13's request; present at step 13's request.
    assert all(
        NO_PROGRESS_NOTICE not in m.content
        for req in long_provider.requests[:12]
        for m in req.messages
    )
    assert long_provider.requests[12].messages[-1].content == NO_PROGRESS_NOTICE
    # Never caps max_output_tokens.
    assert all(req.max_output_tokens is None for req in long_provider.requests)


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
    # Requests 0..19 (steps 1..20) must not contain the notice; request 20 (step 21) does.
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
def test_progress_guard_fires_on_output_tokens_over_60000_with_8_no_diff_steps(
    tmp_path: Path,
) -> None:
    files = {f"f{i}.txt": f"line {i}\n" for i in range(10)}
    ws = make_repo(tmp_path, files=files)
    big = Usage(input_tokens=1000, output_tokens=10_000, requests=1)
    # Even reading a new file each step, >60k output tokens with 8 steps of no diff change fires!
    turns = [
        *[Turn(calls=[("read", {"file_path": f"f{i}.txt"})], usage=big) for i in range(9)],
        Turn(text="done"),
    ]
    provider = ScriptedProvider(turns)
    cfg = SessionConfig(model="m", workspace=ws, home=tmp_path / "home", finish_gate=False)
    events = run(Session(cfg, provider))
    notices = [e for e in events if isinstance(e, ev.Notice) and e.kind == "progress_guard"]
    assert len(notices) == 1
    # Must not fire on step 7 (70k tokens, 7 no-diff steps); fires after step 8 (seen on request 8).
    assert all(
        NO_PROGRESS_NOTICE not in m.content for req in provider.requests[:8] for m in req.messages
    )
    assert provider.requests[8].messages[-1].content == NO_PROGRESS_NOTICE


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


def test_flags_default_on_and_cli_config_switchable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KAROX_HOME", str(tmp_path))
    # Defaults ON
    args = cli._parser().parse_args(["run", "--model", "m", "task"])
    cfg = cli._session_config(args, tmp_path, tmp_path / "home", stream_deltas=False)
    assert cfg.finish_gate is True
    assert cfg.progress_guard is True

    # CLI flags switch them OFF
    args_off = cli._parser().parse_args(
        ["run", "--model", "m", "--no-finish-gate", "--no-progress-guard", "task"]
    )
    cfg_off = cli._session_config(args_off, tmp_path, tmp_path / "home", stream_deltas=False)
    assert cfg_off.finish_gate is False
    assert cfg_off.progress_guard is False

    # config.toml switches them OFF, and CLI flags can turn them back ON
    (tmp_path / "config.toml").write_text(
        "finish_gate = false\nprogress_guard = false\n", encoding="utf-8"
    )
    settings = load_settings()
    assert settings == {"finish_gate": False, "progress_guard": False}
    args_cfg = cli._parser(settings).parse_args(["run", "--model", "m", "task"])
    assert args_cfg.finish_gate is False
    assert args_cfg.progress_guard is False
    args_override = cli._parser(settings).parse_args(
        ["run", "--model", "m", "--finish-gate", "--progress-guard", "task"]
    )
    assert args_override.finish_gate is True
    assert args_override.progress_guard is True
