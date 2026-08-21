"""Run the canonical CI coverage gate and persist machine-readable evidence.

The gate used to hand the console to ``coverage run`` for its entire ten-plus
minute life while printing nothing of its own: on a slow machine the operator
stared at thousands of unittest dots with no way to tell progress from a hang,
and an interrupt could leave the child tree (and its SQLite/coverage locks)
behind. This runner keeps the child's output flowing line by line, prints one
bounded heartbeat with elapsed time and time-since-last-output, and stops the
whole child tree -- closing its pipe -- on Ctrl+C.

The measured commands are exactly the documented canonical ones; nothing about
coverage semantics, discovery, or the threshold changes here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / "scratch"
DATA_FILE = SCRATCH / ".coverage.current"
JSON_REPORT = SCRATCH / "coverage_current.json"
XML_REPORT = SCRATCH / "coverage_current.xml"

# One line every 30s keeps a long run observably alive without drowning the
# real output; the stall note appears only after three quiet minutes, which a
# full TUI test class can legitimately reach under coverage instrumentation.
HEARTBEAT_SECONDS = 30.0
STALL_NOTE_SECONDS = 180.0


def _format_duration(seconds: float) -> str:
    minutes, secs = divmod(int(max(seconds, 0.0)), 60)
    return f"{minutes:02d}:{secs:02d}"


class _Heartbeat:
    """Print elapsed/last-output status while one phase runs."""

    def __init__(self, phase: str) -> None:
        self._phase = phase
        self._started = time.monotonic()
        self._last_output = self._started
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="coverage-gate-heartbeat", daemon=True
        )

    def __enter__(self) -> "_Heartbeat":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def output_seen(self) -> None:
        with self._lock:
            self._last_output = time.monotonic()

    def _run(self) -> None:
        while not self._stop.wait(HEARTBEAT_SECONDS):
            now = time.monotonic()
            with self._lock:
                quiet = now - self._last_output
            line = (
                f"[coverage-gate] {self._phase}: running, "
                f"elapsed {_format_duration(now - self._started)}, "
                f"last output {int(quiet)}s ago"
            )
            if quiet >= STALL_NOTE_SECONDS:
                line += " -- no output for a while; a stall here is worth a look"
            print(line, flush=True)


def _terminate_tree(process: subprocess.Popen[str]) -> None:
    """Stop the child and everything it started, then release its pipe.

    ``taskkill /T`` is the only reliable whole-tree stop on Windows: the test
    child spawns git, worker interpreters, and bridge processes of its own,
    and terminating just the group leader is exactly how interrupted runs
    used to orphan them (and their coverage/SQLite file locks).
    """
    if process.poll() is None:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                check=False,
            )
        else:
            try:
                process.terminate()
            except OSError:
                pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
    stream = process.stdout
    if stream is not None:
        try:
            stream.close()
        except OSError:
            pass


def _run(argv: list[str], env: dict[str, str], phase: str) -> int:
    print("+ " + subprocess.list2cmdline(argv), flush=True)
    process = subprocess.Popen(
        argv,
        cwd=ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    )
    try:
        with _Heartbeat(phase) as heartbeat:
            stream = process.stdout
            assert stream is not None
            for line in stream:
                sys.stdout.write(line)
                sys.stdout.flush()
                heartbeat.output_seen()
            code = process.wait()
    except KeyboardInterrupt:
        print(
            f"\n[coverage-gate] interrupted during '{phase}'; "
            "stopping the child process tree",
            flush=True,
        )
        _terminate_tree(process)
        raise
    finally:
        stream = process.stdout
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
    return int(code)


def main() -> int:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    for path in (JSON_REPORT, XML_REPORT):
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    env = os.environ.copy()
    env["COVERAGE_FILE"] = str(DATA_FILE)
    python = sys.executable
    started = time.monotonic()

    phases: list[tuple[str, list[str]]] = [
        ("erase", [python, "-m", "coverage", "erase"]),
        (
            "coverage tests",
            [
                python,
                "-m",
                "coverage",
                "run",
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
                "-p",
                "test_*.py",
            ],
        ),
        ("combine", [python, "-m", "coverage", "combine"]),
        ("json report", [python, "-m", "coverage", "json", "-o", str(JSON_REPORT)]),
        ("xml report", [python, "-m", "coverage", "xml", "-o", str(XML_REPORT)]),
    ]
    try:
        for phase, argv in phases:
            code = _run(argv, env, phase)
            if code != 0:
                print(
                    f"[coverage-gate] phase '{phase}' exited {code} after "
                    f"{_format_duration(time.monotonic() - started)}",
                    flush=True,
                )
                return code

        payload = json.loads(JSON_REPORT.read_text(encoding="utf-8"))
        totals = payload.get("totals", {})
        percent = totals.get("percent_covered")
        print(f"COVERAGE_TOTAL={percent}", flush=True)

        # Keep the threshold enforcement last so JSON/XML evidence remains
        # available even when the gate is red.
        code = _run([python, "-m", "coverage", "report"], env, "threshold report")
        print(
            f"[coverage-gate] finished in "
            f"{_format_duration(time.monotonic() - started)}, exit={code}",
            flush=True,
        )
        return code
    except KeyboardInterrupt:
        print(
            f"[coverage-gate] interrupted after "
            f"{_format_duration(time.monotonic() - started)}; the child tree was "
            "stopped, so no git/bridge/worker orphans or coverage locks remain "
            "from this runner.",
            flush=True,
        )
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
