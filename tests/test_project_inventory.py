import json

from karox6.core.prompt import project_inventory


def test_inventory_lists_declared_project_facts(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "python").write_text("")
    scripts = {"test": "vitest", "build": "tsc", "typecheck": "tsc --noEmit"}
    (tmp_path / "package.json").write_text(json.dumps({"scripts": scripts}))
    (tmp_path / "pnpm-lock.yaml").write_text("")
    (tmp_path / "go.mod").write_text("module example.com/thing\n\ngo 1.22\n")
    (tmp_path / "Makefile").write_text(
        ".PHONY: test\ntest:\n\tgo test ./...\nVAR := 1\nlint: test\n"
    )
    items = project_inventory(tmp_path)
    assert items == [
        "Python project files: pyproject.toml; pytest configured",
        "package.json (lockfile: pnpm); scripts: test, build, typecheck",
        "Go module: example.com/thing",
        "Makefile targets: test, lint",
    ]


def test_inventory_is_empty_for_a_bare_directory_and_survives_bad_json(tmp_path):
    assert project_inventory(tmp_path) == []
    (tmp_path / "package.json").write_text("{not json")
    assert project_inventory(tmp_path) == ["package.json"]


def test_inventory_caps_long_lists(tmp_path):
    scripts = {f"s{i}": "x" for i in range(20)}
    (tmp_path / "package.json").write_text(json.dumps({"scripts": scripts}))
    (line,) = project_inventory(tmp_path, max_items=5)
    assert line.endswith("s0, s1, s2, s3, s4 (+15 more)")


def test_inventory_bounds_long_names(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"x" * 100_000: "y"}}))
    (line,) = project_inventory(tmp_path)
    assert len(line) < 500


def test_python_interpreter_from_fake_venv_pyvenv_cfg(tmp_path, monkeypatch):
    from karox6.core import prompt
    from karox6.tools.shell import ShellInfo

    # Must not run any subprocess when reading pyvenv.cfg.
    def forbid_run(*_args, **_kwargs):
        raise AssertionError("subprocess.run must not be called for venv interpreter")

    monkeypatch.setattr(prompt.subprocess, "run", forbid_run)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)

    # .venv with Scripts/python.exe and version_info in pyvenv.cfg (uv/virtualenv style).
    monkeypatch.setattr(prompt.platform, "system", lambda: "Windows")
    venv = tmp_path / ".venv"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Scripts" / "python.exe").write_text("")
    (venv / "pyvenv.cfg").write_text("home = /opt/python\nversion_info = 3.13.14.final.0\n")

    line = prompt.python_interpreter(tmp_path)
    assert (
        line
        == "Python interpreter: `.venv/Scripts/python.exe` (project virtualenv, Python 3.13.14)"
    )
    assert len(f"- {line}") <= 160
    assert "\n" not in line

    # Included in environment_block as ONE bullet <= 160 chars (merged with Project virtualenv).
    monkeypatch.setattr(prompt, "git_summary", lambda _ws: "Git: not a git repository")
    monkeypatch.setattr(prompt, "python_hint", lambda: None)
    block = prompt.environment_block(tmp_path, ShellInfo("bash", "bash", "/bin/bash"))
    interp_lines = [ln for ln in block.splitlines() if ln.startswith("- Python interpreter:")]
    assert interp_lines == [
        "- Python interpreter: `.venv/Scripts/python.exe` (project virtualenv, Python 3.13.14)"
    ]
    assert "Project virtualenv:" not in block
    assert len(interp_lines[0]) <= 160

    # Also handles `version = ...` and `venv/bin/python` on POSIX.
    monkeypatch.setattr(prompt.platform, "system", lambda: "Linux")
    ws2 = tmp_path / "ws2"
    (ws2 / "venv" / "bin").mkdir(parents=True)
    (ws2 / "venv" / "bin" / "python").write_text("")
    (ws2 / "venv" / "pyvenv.cfg").write_text("version = 3.12.4\n")
    line2 = prompt.python_interpreter(ws2)
    assert line2 == "Python interpreter: `venv/bin/python` (project virtualenv, Python 3.12.4)"


def test_python_interpreter_path_fallback_and_absent_when_missing(tmp_path, monkeypatch):
    from karox6.core import prompt
    from karox6.tools.shell import ShellInfo

    monkeypatch.setattr(prompt.platform, "system", lambda: "Linux")
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setattr(
        prompt.shutil,
        "which",
        lambda name: "/usr/bin/python3" if name == "python3" else None,
    )
    assert prompt.python_interpreter(tmp_path) == "Python interpreter: `/usr/bin/python3`"

    # Overlong PATH entry is dropped instead of truncated so we never emit an unrunnable path.
    long_path = "/" + "a" * 200 + "/python3"
    monkeypatch.setattr(
        prompt.shutil, "which", lambda name: long_path if name == "python3" else None
    )
    assert prompt.python_interpreter(tmp_path) is None

    # Nothing found: returns None and is omitted from environment_block.
    monkeypatch.setattr(prompt.shutil, "which", lambda _name: None)
    assert prompt.python_interpreter(tmp_path) is None
    monkeypatch.setattr(prompt, "git_summary", lambda _ws: "Git: not a git repository")
    monkeypatch.setattr(prompt, "python_hint", lambda: None)
    block = prompt.environment_block(tmp_path, ShellInfo("bash", "bash", "/bin/bash"))
    assert "Python interpreter:" not in block


def test_python_interpreter_windows_precedence_and_caching(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from karox6.core import prompt

    prompt._answers_version.cache_clear()
    monkeypatch.setattr(prompt.platform, "system", lambda: "Windows")
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)

    # 1. Current-OS layout only: Linux .venv/bin/python is ignored on Windows.
    ws_linux_venv = tmp_path / "ws_linux_venv"
    (ws_linux_venv / ".venv" / "bin").mkdir(parents=True)
    (ws_linux_venv / ".venv" / "bin" / "python").write_text("")
    monkeypatch.setattr(prompt.shutil, "which", lambda _name: None)
    assert prompt.python_interpreter(ws_linux_venv) is None

    # MSYS2 bin/python.exe IS recognized on Windows.
    (ws_linux_venv / ".venv" / "bin" / "python.exe").write_text("")
    assert (
        prompt.python_interpreter(ws_linux_venv)
        == "Python interpreter: `.venv/bin/python.exe` (project virtualenv)"
    )

    # Scripts/python.exe beats bin/python.exe and bin/python on Windows.
    (ws_linux_venv / ".venv" / "Scripts").mkdir(parents=True)
    (ws_linux_venv / ".venv" / "Scripts" / "python.exe").write_text("")
    assert (
        prompt.python_interpreter(ws_linux_venv)
        == "Python interpreter: `.venv/Scripts/python.exe` (project virtualenv)"
    )

    # On Linux, Windows Scripts/python.exe is ignored.
    ws_win_only = tmp_path / "ws_win_only"
    (ws_win_only / ".venv" / "Scripts").mkdir(parents=True)
    (ws_win_only / ".venv" / "Scripts" / "python.exe").write_text("")
    monkeypatch.setattr(prompt.platform, "system", lambda: "Linux")
    assert prompt.python_interpreter(ws_win_only) is None

    # 2. On Windows, VIRTUAL_ENV wins over PATH python3/python and uses POSIX slashes + backticks.
    monkeypatch.setattr(prompt.platform, "system", lambda: "Windows")
    ws_bare = tmp_path / "ws_bare"
    ws_bare.mkdir()
    active = tmp_path / "v"
    (active / "Scripts").mkdir(parents=True)
    (active / "Scripts" / "python.exe").write_text("")
    (active / "pyvenv.cfg").write_text("version = 3.12.7\n")
    monkeypatch.setenv("VIRTUAL_ENV", str(active))
    monkeypatch.setattr(
        prompt.shutil,
        "which",
        lambda name: r"C:\Python312\python3.exe" if name == "python3" else None,
    )
    active_line = prompt.python_interpreter(ws_bare)
    assert active_line is not None
    assert "\\" not in active_line
    assert active_line.startswith("Python interpreter: `")
    assert "/Scripts/python.exe`" in active_line
    assert len(f"- {active_line}") <= 160
    if len(f"Python interpreter: `{active.as_posix()}/Scripts/python.exe` (Python 3.12.7)") <= 158:
        assert active_line.endswith("(Python 3.12.7)")

    # 3. Without VIRTUAL_ENV, Windows tries python before python3 with as_posix + backticks.
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setattr(
        prompt.shutil,
        "which",
        {
            "python": r"C:\Program Files\Python312\python.exe",
            "python3": r"C:\Users\me\AppData\Local\Microsoft\WindowsApps\python3.exe",
        }.get,
    )
    assert (
        prompt.python_interpreter(ws_bare)
        == "Python interpreter: `C:/Program Files/Python312/python.exe`"
    )

    # 4. WindowsApps stub is skipped and _answers_version is cached across both probes.
    stub_py = r"C:\Users\me\AppData\Local\Microsoft\WindowsApps\python.exe"
    stub_py3 = r"C:\Users\me\AppData\Local\Microsoft\WindowsApps\python3.exe"
    probe_calls: list[str] = []

    def fake_run(cmd, **_kwargs):
        probe_calls.append(cmd[0])
        return SimpleNamespace(returncode=49, stdout=b"Python\n", stderr=b"")

    monkeypatch.setattr(prompt.subprocess, "run", fake_run)
    monkeypatch.setattr(
        prompt.shutil,
        "which",
        {"python": stub_py, "python3": stub_py3}.get,
    )
    prompt._answers_version.cache_clear()
    assert prompt.python_hint() is not None
    assert prompt.python_interpreter(ws_bare) is None
    assert probe_calls.count(stub_py3) == 1
    assert probe_calls.count(stub_py) == 1

    # When python is a WindowsApps stub and python3 is real, python3 is selected.
    monkeypatch.setattr(
        prompt.shutil,
        "which",
        {"python": stub_py, "python3": r"C:\Python313\python3.exe"}.get,
    )
    assert prompt.python_interpreter(ws_bare) == "Python interpreter: `C:/Python313/python3.exe`"
    assert probe_calls.count(stub_py) == 1  # served from lru_cache
    prompt._answers_version.cache_clear()


def test_python_interpreter_stable_across_calls_and_session_steps(tmp_path, monkeypatch):
    import asyncio
    import platform

    from karox6.core import prompt
    from karox6.core.session import Session, SessionConfig
    from karox6.providers.scripted import ScriptedProvider, Turn
    from karox6.tools.shell import ShellInfo

    venv = tmp_path / ".venv"
    venv_exe = "Scripts/python.exe" if platform.system() == "Windows" else "bin/python"
    (venv / venv_exe).parent.mkdir(parents=True)
    (venv / venv_exe).write_text("")
    (venv / "pyvenv.cfg").write_text("version = 3.11.9\n")

    shell = ShellInfo("bash", "bash", "/bin/bash")
    monkeypatch.setattr(prompt, "git_summary", lambda _ws: "Git: not a git repository")
    monkeypatch.setattr(prompt, "python_hint", lambda: None)
    assert prompt.python_interpreter(tmp_path) == prompt.python_interpreter(tmp_path)
    assert prompt.environment_block(tmp_path, shell) == prompt.environment_block(tmp_path, shell)

    # Computed once at Session start: prompt prefix does not change between steps.
    ws = tmp_path / "session_ws"
    ws.mkdir()
    (ws / "note.txt").write_text("hi\n")
    provider = ScriptedProvider(
        [
            Turn(calls=[("read", {"file_path": "note.txt"})]),
            Turn(text="done"),
        ]
    )
    config = SessionConfig(model="test", workspace=ws, home=tmp_path / "home")
    session = Session(config, provider)
    before_interp = prompt.python_interpreter(ws)

    # Mutate workspace by adding a venv after Session init; system prompt must stay fixed.
    (ws / ".venv" / venv_exe).parent.mkdir(parents=True)
    (ws / ".venv" / venv_exe).write_text("")
    (ws / ".venv" / "pyvenv.cfg").write_text("version = 3.12.0\n")
    assert prompt.python_interpreter(ws) != before_interp

    async def run_session():
        return [e async for e in session.run("check")]

    asyncio.run(run_session())
    assert len(provider.requests) == 2
    assert provider.requests[0].system == provider.requests[1].system
    assert provider.requests[0].system == session.system_prompt
