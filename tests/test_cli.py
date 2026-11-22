from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import types

import pytest

from karox6 import __version__, cli
from karox6.cli import main
from karox6.sysinfo import rss_mb


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"karox {__version__}"


def test_module_entry_point_runs() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "karox6", "--version"], capture_output=True, text=True, check=True
    )
    assert __version__ in out.stdout


def test_debug_rss_reports_positive_number(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["debug", "rss"]) == 0
    assert float(capsys.readouterr().out.strip()) > 0


def test_rss_mb_is_sane() -> None:
    assert 1 < rss_mb() < 4096


def test_append_system_prompt(tmp_path):
    from karox6.core.prompt import BASE_PROMPT, build_system_prompt
    from karox6.tools.shell import detect_shell

    text = build_system_prompt(tmp_path, detect_shell(), append="  Extra rule.  ")
    assert text.startswith(BASE_PROMPT + "\n\nExtra rule.\n\nEnvironment:")
    assert build_system_prompt(tmp_path, detect_shell(), append="  ").startswith(
        BASE_PROMPT + "\n\nEnvironment:"
    )


def test_python_hint_for_windows_store_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    from karox6.core import prompt

    stub = r"C:\Users\u\AppData\Local\Microsoft\WindowsApps\python3.exe"
    real = r"C:\Python312\python.exe"
    store = r"C:\Users\u\AppData\Local\Microsoft\WindowsApps\python.exe"
    monkeypatch.setattr(prompt.platform, "system", lambda: "Windows")
    store_installed = {store}  # WindowsApps entries that answer `--version`
    monkeypatch.setattr(prompt, "_answers_version", lambda path: path in store_installed)

    def which(paths: dict[str, str]):  # type: ignore[no-untyped-def]
        return paths.get

    monkeypatch.setattr(prompt.shutil, "which", which({"python3": stub, "python": real}))
    hint = prompt.python_hint()
    assert hint is not None
    assert hint.startswith("Python: run it as `python`")
    monkeypatch.setattr(prompt.shutil, "which", which({"python3": stub}))
    assert (prompt.python_hint() or "").startswith("Python: not installed")
    monkeypatch.setattr(prompt.shutil, "which", which({"python3": real, "python": real}))
    assert prompt.python_hint() is None
    monkeypatch.setattr(prompt.shutil, "which", which({"python": real}))
    assert prompt.python_hint() is None  # no python3 at all: "command not found" is clear
    # A real Store install also lives in WindowsApps: the probe tells it from the stub.
    monkeypatch.setattr(prompt.shutil, "which", which({"python3": stub, "python": store}))
    assert (prompt.python_hint() or "").startswith("Python: run it as `python`")
    store_installed.add(stub)
    assert prompt.python_hint() is None  # python3 itself works
    monkeypatch.setattr(prompt.platform, "system", lambda: "Linux")
    monkeypatch.setattr(prompt.shutil, "which", which({"python3": stub}))
    assert prompt.python_hint() is None


def test_piped_output_is_utf8_and_never_raises() -> None:
    """A redirected stream in a legacy code page (Windows: cp1251) used to raise on an
    emoji from the model and end the run with exit code 2."""
    code = (
        "import sys; from karox6.cli import safe_streams; safe_streams(); "
        "print('\\U0001f44b привет'); print('\\U0001f44b ошибка', file=sys.stderr)"
    )
    env = {**os.environ, "PYTHONIOENCODING": "cp1251"}
    env.pop("PYTHONUTF8", None)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, env=env, check=True)
    assert out.stdout.decode("utf-8").strip() == "\U0001f44b привет"
    assert out.stderr.decode("utf-8").strip() == "\U0001f44b ошибка"


def test_without_safe_streams_the_legacy_code_page_raises() -> None:
    """Guards the test above: the failure it prevents is real."""
    code = "print('\\U0001f44b')"
    env = {**os.environ, "PYTHONIOENCODING": "cp1251"}
    env.pop("PYTHONUTF8", None)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, env=env, check=False)
    assert out.returncode != 0
    assert b"UnicodeEncodeError" in out.stderr


@pytest.mark.parametrize("signals", [1, 2])
def test_sigterm_or_ctrl_break_runs_the_cleanup_and_a_second_one_exits_at_once(
    signals: int,
) -> None:
    """SIGTERM on POSIX; on Windows CTRL_BREAK, which `karox mcp --supervise` sends to its
    server (Python's default for it ends the process without running `finally`)."""
    import signal
    import time

    code = (
        "import asyncio\n"
        "from karox6.cli import run_async\n"
        "async def main():\n"
        "    try:\n"
        "        print('ready', flush=True)\n"
        "        await asyncio.sleep(60)\n"
        "    finally:\n"
        "        print('cleaning', flush=True)\n"
        "        await asyncio.sleep(1 if CLEANUP_FAST else 60)\n"
        "        print('cleaned', flush=True)\n"
        "run_async(main())\n"
    ).replace("CLEANUP_FAST", str(signals == 1))
    windows = sys.platform == "win32"
    stop = signal.CTRL_BREAK_EVENT if windows else signal.SIGTERM
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if windows else 0,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "ready"
        proc.send_signal(stop)
        assert proc.stdout.readline().strip() == "cleaning"
        if signals == 2:
            proc.send_signal(stop)
        started = time.monotonic()
        proc.wait(timeout=10)
        assert time.monotonic() - started < 5
        assert proc.returncode == 143
        rest = proc.stdout.read()
        assert ("cleaned" in rest) is (signals == 1)
    finally:
        if proc.poll() is None:
            proc.kill()


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("Исправь тест \U0001f44b".encode(), "Исправь тест \U0001f44b"),
        ("﻿задача".encode(), "задача"),  # UTF-8 BOM (PowerShell)
    ],
)
def test_task_on_stdin_is_read_as_utf8(payload: bytes, expected: str) -> None:
    code = "from karox6.cli import read_piped_text; print(ascii(read_piped_text()))"
    env = {**os.environ, "PYTHONIOENCODING": "cp1251"}  # a Windows pipe
    env.pop("PYTHONUTF8", None)
    out = subprocess.run(
        [sys.executable, "-c", code], input=payload, capture_output=True, env=env, check=True
    )
    assert out.stdout.decode().strip() == ascii(expected)


def test_mcp_servers_start_after_the_key_left_the_environment(tmp_path, monkeypatch):
    import asyncio

    from karox6 import cli

    monkeypatch.setenv("KX_TEST_KEY", "secret-value")
    seen: dict[str, object] = {}

    class Stub:
        closed = False

        async def aclose(self) -> None:
            Stub.closed = True

    def fake_provider(args):
        os.environ.pop(args.api_key_env, None)
        return Stub()

    async def fake_mcp(args, home):
        seen["key"] = os.environ.get("KX_TEST_KEY")
        raise ValueError("bad mcp.json")

    monkeypatch.setattr(cli, "_provider", fake_provider)
    monkeypatch.setattr(cli, "_mcp_tools", fake_mcp)
    args = cli._parser().parse_args(
        [
            "run",
            "--model",
            "m",
            "--api-key-env",
            "KX_TEST_KEY",
            "--home",
            str(tmp_path),
            "--cwd",
            str(tmp_path),
            "task",
        ]
    )
    with pytest.raises(ValueError, match="bad mcp"):
        asyncio.run(cli._run_async(args, "task"))
    assert seen["key"] is None
    assert Stub.closed


def test_text_output_without_sse_prints_the_reply(capsys):
    from karox6.cli import _TextRenderer
    from karox6.core import events as ev

    render = _TextRenderer()
    render(ev.AssistantMessage(step=1, text="the answer"))
    render(ev.TextDelta("streamed"))
    render(ev.AssistantMessage(step=2, text="streamed"))
    assert capsys.readouterr().out == "the answer\nstreamed\n"


def test_max_cost_without_prices_is_refused(tmp_path):
    from karox6 import cli

    args = cli._parser().parse_args(["run", "--model", "m", "--max-cost", "1", "task"])
    with pytest.raises(ValueError, match="price"):
        cli._session_config(args, tmp_path, tmp_path, stream_deltas=False)


def test_config_theme_is_the_chat_default():
    """`theme = "light"` in config.toml used to be dead: set_defaults ran before
    add_argument("--theme", default=None), and argparse's own default shadowed it."""
    args = cli._parser({"theme": "light"}).parse_args(["chat", "--model", "m"])
    assert args.theme == "light"
    args = cli._parser({"theme": "light"}).parse_args(["chat", "--model", "m", "--theme", "dark"])
    assert args.theme == "dark"  # a flag still wins
    assert not hasattr(cli._parser().parse_args(["chat", "--model", "m"]), "theme")


def test_update_source_is_set_when_the_check_is_off(tmp_path, monkeypatch):
    """update_check = false (or a source tree) skips the daily check, but /update must
    still install from the configured repository — not the hard-coded default."""
    from karox6 import update

    monkeypatch.delenv("KAROX_UPDATE_CHECK", raising=False)
    args = cli._parser(
        {
            "update_repo": "https://example.test/mine.git",
            "update_branch": "mine/main",
            "update_check": False,
        }
    ).parse_args(["chat", "--model", "m"])
    app = types.SimpleNamespace(update_source=None)
    cli._start_update_check(args, tmp_path, app)
    assert app.update_source == update.Source("https://example.test/mine.git", "mine/main")


def test_update_check_env_one_overrides_a_config_false(tmp_path, monkeypatch):
    """KAROX_UPDATE_CHECK=1 forces the daily check on over `update_check = false` and a
    source-tree run."""
    from karox6 import update

    done = threading.Event()
    monkeypatch.setattr(update, "check_cached", lambda *a, **k: done.set() or None)
    monkeypatch.setenv("KAROX_UPDATE_CHECK", "1")
    monkeypatch.delenv("KAROX_UPDATE_REPO", raising=False)
    monkeypatch.delenv("KAROX_UPDATE_BRANCH", raising=False)
    args = cli._parser({"update_check": False}).parse_args(["chat", "--model", "m"])
    app = types.SimpleNamespace(update_source=None)

    async def start():
        cli._start_update_check(args, tmp_path, app)

    asyncio.run(start())
    assert done.wait(timeout=10)  # the daemon thread fetched despite update_check=false
    assert app.update_source == update.Source()  # no repo/branch in config or env
