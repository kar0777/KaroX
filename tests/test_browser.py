"""page_screenshot: a headless browser picture of a web page, with its console."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

from karox6.tools import browser
from karox6.tools.base import ToolContext
from karox6.tools.files import FileTracker
from karox6.tools.jobs import JobManager
from karox6.tools.shell import detect_shell


def _ctx(ws: Path) -> ToolContext:
    return ToolContext(
        workspace=ws,
        session_dir=ws / ".s",
        files=FileTracker(ws),
        jobs=JobManager(),
        shell=detect_shell(),
    )


def test_console_lines_come_from_the_browser_log():
    log = (
        '[1:2:1128/091525.782:INFO:CONSOLE:5] "boom", source: file:///C:/x/page.html (5)\n'
        "[1:2:1128/091525.790:WARNING:gpu_init.cc(1)] unrelated\n"
        '[1:2:1128/091525.799:INFO:CONSOLE(12)] "Uncaught ReferenceError: f is not defined",'
        " source: http://localhost:5173/main.js (12)\n"
    )
    assert browser.console_lines(log) == [
        "boom  [page.html (5)]",
        "Uncaught ReferenceError: f is not defined  [main.js (12)]",
    ]


def test_the_browser_comes_from_the_setting_or_is_off(monkeypatch, tmp_path):
    monkeypatch.setenv("KAROX_BROWSER", "0")
    assert browser.find_browser() is None
    fake = tmp_path / "chrome.exe"
    fake.write_text("")
    monkeypatch.setenv("KAROX_BROWSER", str(fake))
    assert browser.find_browser() == str(fake)
    from karox6.tools.registry import build_tools

    names = [t.name for t in build_tools(detect_shell())]
    assert "page_screenshot" in names
    monkeypatch.setenv("KAROX_BROWSER", "0")
    assert "page_screenshot" not in [t.name for t in build_tools(detect_shell())]


def test_bad_targets_are_refused(tmp_path):
    tool = browser.PageScreenshotTool("unused")
    missing = asyncio.run(tool.run({"target": "nope.html"}, _ctx(tmp_path)))
    assert missing.is_error
    assert "not a file" in missing.content
    assert asyncio.run(tool.run({"target": " "}, _ctx(tmp_path))).is_error


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32")  # type: ignore[attr-defined,unused-ignore]
        handle = kernel32.OpenProcess(0x00100000 | 0x1000, False, pid)  # SYNCHRONIZE | QUERY
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT: running
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _spawner(tmp_path: Path, wait: bool) -> list[str]:
    """A stand-in browser that starts a long-lived child and records its pid."""
    pid_file = tmp_path / "child.pid"
    code = (
        "import subprocess, sys, time\n"
        "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(c.pid))\n" + ("c.wait()\n" if wait else "")
    )
    return [sys.executable, "-c", code]


def _child_pid(tmp_path: Path) -> int:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        text = (tmp_path / "child.pid").read_text() if (tmp_path / "child.pid").exists() else ""
        if text.strip():
            return int(text)
        time.sleep(0.05)
    raise AssertionError("the child never started")


def test_a_browser_that_hangs_is_killed_with_its_children(monkeypatch, tmp_path):
    monkeypatch.setattr(browser, "TIME_LIMIT_S", 2.0)
    began = time.monotonic()
    code, _ = asyncio.run(browser._run(_spawner(tmp_path, wait=True), tmp_path))
    assert code is None
    assert time.monotonic() - began < 30
    pid = _child_pid(tmp_path)
    deadline = time.monotonic() + 5
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(pid)


def test_a_child_that_outlives_the_browser_is_killed_too(tmp_path):
    code, _ = asyncio.run(browser._run(_spawner(tmp_path, wait=False), tmp_path))
    assert code == 0
    pid = _child_pid(tmp_path)
    deadline = time.monotonic() + 5
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(pid)


def _leftovers() -> set[str]:
    return {p.name for p in Path(tempfile.gettempdir()).glob("karox-shot-*")}


def test_a_real_page_is_captured_with_its_console(monkeypatch, tmp_path):
    monkeypatch.delenv("KAROX_BROWSER", raising=False)
    found = browser.find_browser()
    if found is None:
        pytest.skip("no Chromium-family browser installed")
    page = tmp_path / "page.html"
    page.write_text(
        "<body style='background:#f00;margin:0'><script>"
        "console.log('hello from the page' + location.search); missingFunction();"
        "</script></body>",
        encoding="utf-8",
    )
    before = _leftovers()
    tool = browser.PageScreenshotTool(found)
    result = asyncio.run(
        tool.run(
            {"target": "page.html?view=top", "width": 320, "height": 240, "wait_ms": 200},
            _ctx(tmp_path),
        )
    )
    assert not result.is_error, result.content
    png = result.meta["image_png"]
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert "hello from the page?view=top" in result.content
    assert "missingFunction is not defined" in result.content
    assert "320x240" in result.content
    assert _leftovers() <= before  # the throwaway profile is gone
