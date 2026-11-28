"""Hooks: the user's and plugins' commands at points of the agent loop."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from karox6 import hooks as hooks_mod
from karox6.core import events as ev
from karox6.core.session import Session, SessionConfig
from karox6.providers.scripted import ScriptedProvider, Turn

PY = Path(sys.executable).as_posix()


def _script(folder: Path, name: str, body: str) -> str:
    """A Python hook script; returns the command that runs it."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text("import json, sys\nevent = json.load(sys.stdin)\n" + body, encoding="utf-8")
    return f'"{PY}" "{path.as_posix()}"'


def _hooks_file(home: Path, table: dict) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "hooks.json").write_text(json.dumps({"hooks": table}), encoding="utf-8")


def _one(command: str, matcher: str = "", timeout: float | None = None) -> list:
    hook: dict = {"type": "command", "command": command}
    if timeout is not None:
        hook["timeout"] = timeout
    return [{"matcher": matcher, "hooks": [hook]}]


def _session(tmp_path: Path, turns: list[Turn]) -> tuple[Session, ScriptedProvider]:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    config = SessionConfig(model="m", workspace=ws, home=tmp_path / "home", hooks=True)
    provider = ScriptedProvider(turns)
    return Session(config, provider), provider


def _run(session: Session, prompt: str) -> list[ev.Event]:
    async def go() -> list[ev.Event]:
        return [e async for e in session.run(prompt)]

    return asyncio.run(go())


def _tool_results(provider: ScriptedProvider) -> list[str]:
    last = provider.requests[-1]
    return [m.content for m in last.messages if m.role == "tool"]


def test_a_pre_tool_hook_blocks_a_call_with_exit_2(tmp_path):
    log = tmp_path / "seen.json"
    command = _script(
        tmp_path / "s",
        "deny.py",
        f"open({str(log.as_posix())!r}, 'w').write(json.dumps(event))\n"
        "if 'rm' in event['tool_input'].get('command', ''):\n"
        "    print('no deleting here', file=sys.stderr); sys.exit(2)\n",
    )
    _hooks_file(tmp_path / "home", {"PreToolUse": _one(command, matcher="Bash")})
    session, provider = _session(
        tmp_path,
        [Turn(calls=[("bash", {"command": "rm -rf build"})]), Turn(text="ok, I will not")],
    )
    events = _run(session, "clean up")
    results = _tool_results(provider)
    assert any("PreToolUse hook blocked this call: no deleting here" in r for r in results)
    seen = json.loads(log.read_text())
    assert seen["hook_event_name"] == "PreToolUse"
    assert seen["tool_name"] == "Bash"  # Claude Code's name
    assert seen["karox_tool_name"] == "bash"
    assert seen["tool_input"] == {"command": "rm -rf build"}
    assert events[-1].status == "completed"


def test_a_json_deny_blocks_and_other_tools_are_not_matched(tmp_path):
    command = _script(
        tmp_path / "s",
        "deny.py",
        "print(json.dumps({'hookSpecificOutput': {'permissionDecision': 'deny',"
        " 'permissionDecisionReason': 'read-only today'}}))\n",
    )
    _hooks_file(tmp_path / "home", {"PreToolUse": _one(command, matcher="Write|Edit")})
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "a.txt").write_text("hello\n")
    session, provider = _session(
        tmp_path,
        [
            Turn(calls=[("read", {"file_path": "a.txt"})]),
            Turn(calls=[("write", {"file_path": "b.txt", "content": "x"})]),
            Turn(text="done"),
        ],
    )
    _run(session, "go")
    results = _tool_results(provider)
    assert "hello" in results[0]  # read is not matched
    assert "read-only today" in results[1]
    assert not (tmp_path / "ws" / "b.txt").exists()


def test_post_tool_and_prompt_hooks_add_context(tmp_path):
    post = _script(
        tmp_path / "s",
        "post.py",
        "print(json.dumps({'hookSpecificOutput': {'additionalContext':"
        " 'lint: ' + str(event['tool_response']['is_error'])}}))\n",
    )
    prompt = _script(tmp_path / "s", "prompt.py", "print('today is a holiday')\n")
    start = _script(tmp_path / "s", "start.py", "print('started from ' + event['source'])\n")
    _hooks_file(
        tmp_path / "home",
        {
            "PostToolUse": _one(post),
            "UserPromptSubmit": _one(prompt),
            "SessionStart": _one(start),
        },
    )
    session, provider = _session(
        tmp_path, [Turn(calls=[("bash", {"command": "echo hi"})]), Turn(text="ok"), Turn(text="2")]
    )
    _run(session, "first")
    first = [m.content for m in provider.requests[0].messages if m.role == "user"][-1]
    assert first.startswith("first")
    assert "today is a holiday" in first
    assert "started from startup" in first
    assert "[hook] lint: False" in _tool_results(provider)[0]
    _run(session, "second")
    second = [m.content for m in provider.requests[-1].messages if m.role == "user"][-1]
    assert "today is a holiday" in second
    assert "started from" not in second  # SessionStart runs once


def test_a_prompt_hook_can_refuse_the_prompt(tmp_path):
    command = _script(
        tmp_path / "s",
        "gate.py",
        "if 'password' in event['prompt']:\n"
        "    print(json.dumps({'decision': 'block', 'reason': 'secrets stay out'}))\n",
    )
    _hooks_file(tmp_path / "home", {"UserPromptSubmit": _one(command)})
    session, provider = _session(tmp_path, [Turn(text="ok")])
    events = _run(session, "my password is hunter2")
    assert provider.requests == []
    assert events[-1].status == "error"
    assert "secrets stay out" in events[-1].reason
    assert session.messages == [] or all("hunter2" not in m.content for m in session.messages)


def test_a_stop_hook_sends_the_model_back_at_most_three_times(tmp_path):
    command = _script(
        tmp_path / "s",
        "stop.py",
        "print('tests are not run yet', file=sys.stderr); sys.exit(2)\n",
    )
    _hooks_file(tmp_path / "home", {"Stop": _one(command)})
    session, provider = _session(tmp_path, [Turn(text=f"done {i}") for i in range(6)])
    events = _run(session, "fix it")
    assert len(provider.requests) == 4  # the first answer + three times sent back
    notices = [e for e in events if isinstance(e, ev.Notice) and e.kind == "hook"]
    assert len(notices) == 3
    assert "tests are not run yet" in notices[0].text
    assert events[-1].final_text == "done 3"


def test_a_failing_or_slow_hook_only_warns(tmp_path):
    broken = _script(tmp_path / "s", "broken.py", "sys.exit(3)\n")
    slow = _script(tmp_path / "s", "slow.py", "import time; time.sleep(30)\n")
    _hooks_file(
        tmp_path / "home",
        {"PreToolUse": _one(broken) + _one(slow, timeout=1)},
    )
    session, provider = _session(
        tmp_path, [Turn(calls=[("bash", {"command": "echo hi"})]), Turn(text="ok")]
    )
    events = _run(session, "go")
    texts = [e.text for e in events if isinstance(e, ev.Notice) and e.kind == "hook"]
    assert any("failed (exit 3)" in t for t in texts)
    assert any("longer than 1s" in t for t in texts)
    assert "hi" in _tool_results(provider)[0]  # the call still ran


def test_plugin_hooks_get_their_root_and_bad_files_warn(tmp_path):
    home = tmp_path / "my home"  # a space: the shell expands the quoted variable
    plugin = home / "plugins" / "guard"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text('{"name": "guard"}')
    (plugin / "hooks").mkdir()
    (plugin / "check.py").write_text(
        "import json, os\n"
        "print(json.dumps({'hookSpecificOutput': {'additionalContext':"
        " 'root ok: ' + str(os.path.isfile(os.path.join(os.environ['CLAUDE_PLUGIN_ROOT'],"
        " 'check.py')))}}))\n"
    )
    command = f'"{PY}" "${{CLAUDE_PLUGIN_ROOT}}/check.py"'
    (plugin / "hooks" / "hooks.json").write_text(
        json.dumps({"hooks": {"PostToolUse": _one(command)}})
    )
    _hooks_file(home, {"Notification": _one("echo hi")})
    warn: list[str] = []
    found = hooks_mod.load(home, warn)
    assert [h.command for h in found] == [command]  # never pasted into the command text
    assert found[0].source == "plugin:guard"
    assert any("Notification hooks are not supported" in w for w in warn)
    ws = tmp_path / "ws"
    ws.mkdir()
    provider = ScriptedProvider([Turn(calls=[("bash", {"command": "echo hi"})]), Turn(text="ok")])
    session = Session(SessionConfig(model="m", workspace=ws, home=home, hooks=True), provider)
    events = _run(session, "go")
    assert "[hook] root ok: True" in _tool_results(provider)[0]
    notices = [e.text for e in events if isinstance(e, ev.Notice) and e.kind == "hook"]
    assert any("Notification hooks are not supported" in t for t in notices)
    (home / "hooks.json").write_text("{not json")
    warn = []
    hooks_mod.load(home, warn)
    assert any("not JSON" in w for w in warn)


def test_problems_are_shown_even_when_no_hook_loads(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "hooks.json").write_text("{not json")
    session, _ = _session(tmp_path, [Turn(text="ok")])
    assert session.hooks is None
    events = _run(session, "hi")
    assert any(isinstance(e, ev.Notice) and "not JSON" in e.text for e in events)


def test_a_pre_tool_hook_can_rewrite_the_call(tmp_path):
    command = _script(
        tmp_path / "s",
        "rewrite.py",
        "cmd = event['tool_input']['command'].replace('rm -rf', 'echo would remove')\n"
        "print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse',"
        " 'updatedInput': {'command': cmd}, 'additionalContext': 'rewritten'}}))\n",
    )
    _hooks_file(tmp_path / "home", {"PreToolUse": _one(command, matcher="Bash")})
    session, provider = _session(
        tmp_path, [Turn(calls=[("bash", {"command": "rm -rf build"})]), Turn(text="ok")]
    )
    events = _run(session, "go")
    result = _tool_results(provider)[0]
    assert "would remove build" in result
    assert "[hook] rewritten" in result
    assert any(isinstance(e, ev.Notice) and "changed the bash call" in e.text for e in events)


def test_continue_false_ends_the_run(tmp_path):
    command = _script(
        tmp_path / "s",
        "halt.py",
        "print(json.dumps({'continue': False, 'stopReason': 'budget spent'}))\n",
    )
    _hooks_file(tmp_path / "home", {"PostToolUse": _one(command), "Stop": _one(command)})
    session, provider = _session(
        tmp_path,
        [Turn(calls=[("bash", {"command": "echo hi"})]), Turn(text="more"), Turn(text="x")],
    )
    events = _run(session, "go")
    assert len(provider.requests) == 1
    assert events[-1].status == "error"
    assert "budget spent" in events[-1].reason


def test_session_start_matches_its_source_and_survives_a_blocked_prompt(tmp_path):
    resume = _script(tmp_path / "s", "resume.py", "print('resumed context')\n")
    start = _script(tmp_path / "s", "start.py", "print('startup context')\n")
    gate = _script(
        tmp_path / "s",
        "gate.py",
        "if 'no' in event['prompt']:\n    sys.exit(2)\n",
    )
    _hooks_file(
        tmp_path / "home",
        {
            "SessionStart": [
                {"matcher": "resume", "hooks": [{"type": "command", "command": resume}]},
                {"matcher": "startup", "hooks": [{"type": "command", "command": start}]},
            ],
            "UserPromptSubmit": _one(gate),
        },
    )
    session, provider = _session(tmp_path, [Turn(text="ok")])
    _run(session, "no")  # refused: the SessionStart context waits for the next prompt
    _run(session, "yes")
    sent = [m.content for m in provider.requests[0].messages if m.role == "user"][-1]
    assert "startup context" in sent
    assert "resumed context" not in sent


def test_matchers_know_claude_code_names():
    assert hooks_mod._matches("Bash", "bash")
    assert hooks_mod._matches("Write|Edit", "apply_patch")
    assert hooks_mod._matches("mcp__calc__.*", "mcp__calc__add")
    assert not hooks_mod._matches("Read", "bash")
    assert hooks_mod._matches("*", "anything")
    assert hooks_mod._matches("(", "(")  # a broken regex compares as text


def test_hooks_are_off_unless_asked(tmp_path, monkeypatch):
    _hooks_file(tmp_path / "home", {"Stop": _one("exit 2")})
    ws = tmp_path / "ws"
    ws.mkdir()
    off = Session(
        SessionConfig(model="m", workspace=ws, home=tmp_path / "home"),
        ScriptedProvider([Turn(text="ok")]),
    )
    assert off.hooks is None
    monkeypatch.setenv("KAROX_HOOKS", "0")
    assert not hooks_mod.enabled(True)
    monkeypatch.delenv("KAROX_HOOKS")
    assert hooks_mod.enabled(None)
    assert not hooks_mod.enabled(False)


def test_a_timeout_kills_what_the_hook_started(tmp_path):
    import time

    child = _script(
        tmp_path / "s",
        "parent.py",
        "import subprocess\n"
        "subprocess.run([sys.executable, '-c', 'import time; time.sleep(60)'])\n",
    )
    _hooks_file(tmp_path / "home", {"PreToolUse": _one(child, timeout=1)})
    session, provider = _session(
        tmp_path, [Turn(calls=[("bash", {"command": "echo hi"})]), Turn(text="ok")]
    )
    began = time.monotonic()
    events = _run(session, "go")
    assert time.monotonic() - began < 30  # the grandchild holding the pipes is gone too
    assert any(isinstance(e, ev.Notice) and "longer than 1s" in e.text for e in events)
