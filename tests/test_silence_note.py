"""A running command that printed nothing for a while is reported with advice, never capped."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

from karox6.tools import proc
from karox6.tools.proc import silence_note


def _run(command: str, *, silent_for: float, printed: bool, done: bool = False) -> Any:
    now = time.monotonic()
    last = now - silent_for if printed else None
    return SimpleNamespace(
        command=command,
        returncode=0 if done else None,
        active=not done,
        last_output=last,
        silent_s=silent_for,
    )


def test_a_quiet_piped_suite_is_explained() -> None:
    note = silence_note(_run("pytest -q tests | tail -20", silent_for=600, printed=False))
    assert "printed nothing for 600 s" in note
    assert "prints only when its input ends" in note


def test_a_quiet_unpiped_command_only_reports_the_silence() -> None:
    note = silence_note(_run("make build", silent_for=300, printed=True))
    assert note == " no new output for 300 s."


def test_no_note_for_recent_output_or_a_finished_command() -> None:
    assert silence_note(_run("pytest | tail", silent_for=10, printed=True)) == ""
    assert silence_note(_run("pytest | tail", silent_for=900, printed=False, done=True)) == ""


def test_a_pipe_inside_a_word_is_not_a_filter() -> None:
    note = silence_note(_run("echo a|tailor", silent_for=600, printed=False))
    assert "filter" not in note


def test_only_end_only_filters_are_named() -> None:
    def filtered(command: str) -> bool:
        return "filter" in silence_note(_run(command, silent_for=600, printed=False))

    assert filtered("pytest |& tail -20")
    assert filtered("pytest 2>&1 | sort | uniq -c")
    assert filtered("make test | wc -l")
    assert not filtered("pytest -q | grep FAIL")  # grep streams its matches
    assert not filtered("make -s test || tail -80 test.log")  # `||` is not a pipe
    assert not filtered("server | tail -f -n 5")  # follows, streams
    assert not filtered("server | tail --follow=name x")
    assert not filtered("pytest | tail -n +1")  # from line 1 on: streams
    assert not filtered("pytest | tail +3")
    assert filtered("pytest | tail -n 30")


def test_real_process_tracks_its_last_output(tmp_path: Any, monkeypatch: Any) -> None:
    import asyncio
    import sys

    async def main() -> tuple[float | None, float | None]:
        run = await proc.ProcessRun.start(
            [sys.executable, "-c", "print('x', flush=True)"],
            cwd=tmp_path,
            env=dict(__import__("os").environ),
            log_path=tmp_path / "cmd.log",
            command="python -c print",
        )
        before = run.last_output
        await run.wait(30)
        await run.exited()
        return before, run.last_output

    _before, after = asyncio.run(main())
    assert after is not None
