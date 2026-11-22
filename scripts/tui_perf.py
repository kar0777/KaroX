"""Measure the chat UI in a real pseudo-terminal and print the numbers.

    python scripts/tui_perf.py [--runs 7] [--lines 6000]

The real `karox6 chat` process runs in a ConPTY (Windows) or a pty (POSIX) against a
scripted local model server (tests/fake_model.py), and the screen is read through a
terminal emulator (tests/term.py), so the times are what a user would see:

* first frame: process start -> welcome card, status bar and prompt on screen;
* ready for input: process start -> a key typed at once is echoed in the input line;
* a long session: N lines streamed, then PageUp / Ctrl+Home / Ctrl+End;
* typing while a reply streams: key sent -> echoed;
* bytes written to the terminal per second while streaming (a redraw of the whole screen
  per update would be hundreds of KB/s and flicker).
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from fake_model import FakeModel, Reply  # noqa: E402
from term import Terminal  # noqa: E402


def launch(model: FakeModel, tmp: Path, **kwargs: object) -> Terminal:
    ws, home = tmp / "ws", tmp / "home"
    ws.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("KAROX_")}
    env.pop("PYTHONUTF8", None)
    env.update(KAROX_HOME=str(home), KAROX_API_KEY="k", PYTHONPATH=str(ROOT / "src"))
    env.update(kwargs.pop("env", {}))  # type: ignore[arg-type]
    argv = [sys.executable, "-m", "karox6", "chat", "--model", "fake", "--base-url", model.url]
    argv += ["--home", str(home), "--cwd", str(ws), "--no-mcp"]
    return Terminal(argv, env=env, cwd=str(ws), **kwargs)  # type: ignore[arg-type]


def ms(seconds: float) -> str:
    return f"{seconds * 1000:.0f} ms"


def startup(tmp: Path, runs: int, env: dict[str, str]) -> tuple[float, float]:
    frames, ready = [], []
    with FakeModel([]) as model:
        for _ in range(runs):
            marks = ("Type a task to start.", "approvals ask", "› x")
            t = launch(model, tmp, rows=30, cols=100, watch=marks, env=env)
            try:
                t.send("x")  # typed at once: echoed when the app starts reading keys
                frames.append(max(t.seen_at(marks[0]), t.seen_at(marks[1])))
                ready.append(t.seen_at(marks[2]))
                t.key("backspace", "ctrl-d")
                t.wait_exit(15)
            finally:
                t.close()
    return statistics.median(frames), statistics.median(ready)


def long_session(tmp: Path, lines: int) -> None:
    big = "\n".join(f"line {i}: " + "word " * (i % 9 + 1) for i in range(1, lines + 1))
    slow = " ".join(f"tok{i}" for i in range(600))
    replies = [Reply(text=big, chunk_size=2000), Reply(text=slow, chunk_size=8, chunk_delay=0.02)]
    with FakeModel(replies) as model:
        t = launch(model, tmp, rows=40, cols=120)
        try:
            t.wait("Type a task to start.", 30)
            start, written = time.perf_counter(), len(t.raw)
            t.line("big")
            t.wait(f"line {lines}:", 300)
            t.wait("✓ completed", 60)
            took = time.perf_counter() - start
            sent = len(t.raw) - written
            print(f"{lines} lines streamed and shown: {took:.2f} s, {sent:,} bytes to the terminal")
            for name, key, marker in (
                ("PageUp", "pageup", "lines below"),
                ("Ctrl+Home (top)", "ctrl-home", "KaroX 6"),
                ("Ctrl+End (bottom)", "ctrl-end", "✓ completed"),
            ):
                before = time.perf_counter()
                t.send(
                    {"pageup": "\x1b[5~", "ctrl-home": "\x1b[1;5H", "ctrl-end": "\x1b[1;5F"}[key]
                )
                t.wait(marker, 10)
                print(f"{name}: screen updated in {ms(time.perf_counter() - before)}")
            t.line("slow")
            t.wait("tok3 ", 30)
            written, began = len(t.raw), time.perf_counter()
            echo = []
            typed = ""
            for ch in "abcdefghijklmnopqrstuvwxy":
                typed += ch
                sent_at = time.perf_counter()
                t.send(ch)
                t.wait(lambda _s, want=typed: t.lines()[-3].rstrip(" │").endswith("› " + want), 5)
                echo.append(time.perf_counter() - sent_at)
                time.sleep(0.05)
            rate = (len(t.raw) - written) / (time.perf_counter() - began)
            echo.sort()
            print(
                f"key -> echo while a reply streams: median {ms(statistics.median(echo))}, "
                f"p90 {ms(echo[int(len(echo) * 0.9)])}, max {ms(echo[-1])}"
            )
            print(f"terminal output while streaming: {rate / 1024:.1f} KB/s")
            t.key("ctrl-c")
            t.wait("! stopped", 15)
        finally:
            t.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--runs", type=int, default=7)
    parser.add_argument("--lines", type=int, default=6000)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="karox-perf-", ignore_cleanup_errors=True) as raw:
        tmp = Path(raw)
        frame, ready = startup(tmp, args.runs, {})
        print(f"first frame: {ms(frame)} (median of {args.runs}); ready for input: {ms(ready)}")
        frame, ready = startup(tmp, args.runs, {"KAROX_NO_SPLASH": "1"})
        print(
            "without the start-up frame (KAROX_NO_SPLASH=1): "
            f"first frame {ms(frame)}, ready {ms(ready)}"
        )
        long_session(tmp, args.lines)


if __name__ == "__main__":
    main()
