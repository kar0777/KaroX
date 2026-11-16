from __future__ import annotations

import os
import subprocess
import sys

import pytest

from karox6 import __version__
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
