from __future__ import annotations

import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from karox6.cli import main
from karox6.mcp.supervise import Supervisor, without_flag

# A stand-in server: run N exits with the code and after the seconds of line N of the plan.
PLANNED = """
import sys, time
from pathlib import Path
plan, label = Path(sys.argv[1]), sys.argv[2]
runs = plan.with_suffix(".runs")
done = runs.read_text().split() if runs.exists() else []
runs.write_text(" ".join([*done, label]))
code, seconds = plan.read_text().splitlines()[len(done)].split()
time.sleep(float(seconds))
sys.exit(int(code))
"""

# A stand-in server with cleanup: like `karox mcp`, it stops through run_async.
POLITE = """
import asyncio, sys
from pathlib import Path
from karox6.cli import run_async
out = Path(sys.argv[1])
async def main():
    out.write_text("ready")
    try:
        await asyncio.sleep(60)
    finally:
        out.write_text("cleaned")
run_async(main())
"""

# A server that ignores the polite request.
STUBBORN = """
import signal, sys, time
from pathlib import Path
signal.signal(signal.SIGBREAK if sys.platform == "win32" else signal.SIGTERM, lambda *a: None)
Path(sys.argv[1]).write_text("ready")
for _ in range(600):
    time.sleep(0.1)
"""


def planned(tmp_path: Path, *runs: str) -> tuple[Path, list[str], list[str]]:
    plan = tmp_path / "plan.txt"
    plan.write_text("\n".join(runs), encoding="utf-8")
    first = [sys.executable, "-c", PLANNED, str(plan), "first"]
    return plan, first, [*first[:-1], "again"]


def started_runs(plan: Path) -> list[str]:
    return plan.with_suffix(".runs").read_text().split()


def wait_for(path: Path, text: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text() == text:
            return
        time.sleep(0.02)
    raise AssertionError(f"{path} never said {text!r}")


def in_thread(sup: Supervisor) -> tuple[threading.Thread, list[int]]:
    result: list[int] = []
    thread = threading.Thread(target=lambda: result.append(sup.run()), daemon=True)
    thread.start()
    return thread, result


def test_restarts_with_backoff_until_exit_code_2(tmp_path: Path) -> None:
    plan, first, again = planned(tmp_path, "1 0", "0 0", "1 0", "1 0", "2 0")
    lines: list[str] = []
    sup = Supervisor(
        first,
        restart_command=again,
        first_delay=0.01,
        max_delay=0.04,
        poll=0.01,
        write=lines.append,
    )
    assert sup.run() == 2
    assert started_runs(plan) == ["first", "again", "again", "again", "again"]
    assert lines == [
        "karox mcp: the server exited with code 1; restarting in 0.01 s\n",
        "karox mcp: the server exited with code 0; restarting in 0.02 s\n",
        "karox mcp: the server exited with code 1; restarting in 0.04 s\n",
        "karox mcp: the server exited with code 1; restarting in 0.04 s\n",
        "karox mcp: the server exited with code 2 (bad arguments or configuration); "
        "not restarting it\n",
    ]


def test_bad_arguments_are_not_retried(tmp_path: Path) -> None:
    plan, first, _ = planned(tmp_path, "2 0", "0 0")
    assert Supervisor(first, poll=0.01, write=lambda _: None).run() == 2
    assert started_runs(plan) == ["first"]


def test_a_child_that_ran_long_enough_resets_the_backoff(tmp_path: Path) -> None:
    _, first, _ = planned(tmp_path, "1 0", "1 0", "1 1.3", "2 0")
    lines: list[str] = []
    sup = Supervisor(
        first, first_delay=0.01, max_delay=10, healthy_after=1.0, poll=0.01, write=lines.append
    )
    assert sup.run() == 2
    assert [line.rsplit(" in ", 1)[-1] for line in lines[:3]] == [
        "0.01 s\n",
        "0.02 s\n",
        "0.01 s\n",
    ]


def test_stop_lets_the_child_clean_up(tmp_path: Path) -> None:
    out = tmp_path / "state"
    sup = Supervisor([sys.executable, "-c", POLITE, str(out)], poll=0.02)
    thread, result = in_thread(sup)
    wait_for(out, "ready")
    sup.stop(143)
    thread.join(30)
    assert result == [143]
    assert out.read_text() == "cleaned"


@pytest.mark.parametrize(("stop_timeout", "requests"), [(0.5, 1), (60, 2)])
def test_a_child_that_ignores_the_request_is_killed(
    tmp_path: Path, stop_timeout: float, requests: int
) -> None:
    """After stop_timeout, or at once when asked to stop again."""
    out = tmp_path / "state"
    sup = Supervisor(
        [sys.executable, "-c", STUBBORN, str(out)], stop_timeout=stop_timeout, poll=0.02
    )
    thread, result = in_thread(sup)
    wait_for(out, "ready")
    started = time.monotonic()
    sup.stop()
    if requests == 2:
        time.sleep(0.3)  # the polite request is out and ignored
        sup.stop()
    thread.join(30)
    assert result == [0]
    assert time.monotonic() - started < 10


def test_stop_during_the_wait_starts_nothing_more(tmp_path: Path) -> None:
    plan, first, _ = planned(tmp_path, "1 0", "1 0")
    lines: list[str] = []
    sup = Supervisor(first, first_delay=60, poll=0.02, write=lines.append)
    thread, result = in_thread(sup)
    deadline = time.monotonic() + 30
    while not lines and time.monotonic() < deadline:
        time.sleep(0.02)
    sup.stop()
    thread.join(10)
    assert result == [0]
    assert started_runs(plan) == ["first"]


def _stop_signals() -> list[tuple[int, int]]:
    if sys.platform == "win32":
        return [(signal.CTRL_BREAK_EVENT, 143)]
    return [(signal.SIGTERM, 143), (signal.SIGINT, 0)]


@pytest.mark.parametrize(("signum", "expected"), _stop_signals())
def test_a_signalled_supervisor_stops_its_child_politely(
    tmp_path: Path, signum: int, expected: int
) -> None:
    """The real thing: Ctrl+C / SIGTERM (CTRL_BREAK on Windows) on the supervisor process
    reaches the child as a request it can clean up after."""
    out = tmp_path / "state"
    code = (
        "import sys\n"
        "from karox6.mcp.supervise import Supervisor\n"
        "sys.exit(Supervisor([sys.executable, '-c', sys.argv[1], sys.argv[2]], poll=0.05).run())\n"
    )
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        [sys.executable, "-c", code, POLITE, str(out)],
        stdin=subprocess.DEVNULL,
        creationflags=flags,
    )
    try:
        wait_for(out, "ready")
        proc.send_signal(signum)
        assert proc.wait(timeout=30) == expected
        assert out.read_text() == "cleaned"
    finally:
        if proc.poll() is None:
            proc.kill()


def test_without_flag_also_drops_abbreviations() -> None:
    argv = ["mcp", "--sup", "--http", "8765", "--supervise", "--append-system-prompt=--supervise"]
    assert without_flag(argv, "--supervise") == [
        "mcp",
        "--http",
        "8765",
        "--append-system-prompt=--supervise",
    ]


def test_supervise_runs_the_same_command_without_it(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[list[str], list[str]]] = []

    def fake_run(self: Supervisor) -> int:
        seen.append((self.command, self.restart_command))
        return 0

    monkeypatch.setattr(Supervisor, "run", fake_run)
    argv = ["mcp", "--http", "8765", "--direct", "--supervise", "--new-token", "--no-mcp"]
    assert main(argv) == 0
    base = [sys.executable, "-m", "karox6", "mcp", "--http", "8765", "--direct"]
    assert seen == [([*base, "--new-token", "--no-mcp"], [*base, "--no-mcp"])]


def test_supervise_needs_http(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["mcp", "--direct", "--supervise", "--no-mcp"])
    assert exc.value.code == 2
    assert "--supervise needs --http" in capsys.readouterr().err
