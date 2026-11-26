import asyncio
import os
import re
import shlex
import shutil
import sys
import time
from pathlib import Path

import pytest

from karox6.tools.jobs import JobKillTool, JobListTool, JobOutputTool
from karox6.tools.proc import ProcessRun
from karox6.tools.shell import ShellTool, detect_shell, is_msys_bash


def python(code):
    return f"{shlex.quote(sys.executable.replace(chr(92), '/'))} -c {shlex.quote(code)}"


def shell(ctx, **args):
    return asyncio.run(ShellTool(ctx.shell).run(args, ctx))


@pytest.mark.parametrize("code", [0, 3])
def test_exit(ctx, code):
    result = shell(ctx, command=f"exit {code}")
    assert f"[exit code: {code}]" in result.content
    assert result.meta["exit_code"] == code


def test_stdout_stderr(ctx):
    result = shell(ctx, command=python("import sys; print('out'); print('err', file=sys.stderr)"))
    assert "out" in result.content
    assert "err" in result.content


@pytest.mark.parametrize("absolute", [False, True])
def test_workdir(ctx, absolute):
    path = ctx.workspace / "sub"
    path.mkdir()
    result = shell(
        ctx,
        command=python("import os; print(os.getcwd())"),
        workdir=str(path) if absolute else "sub",
    )
    assert path.name in result.content
    assert result.meta["exit_code"] == 0


def test_bad_workdir(ctx):
    assert shell(ctx, command="echo hi", workdir="missing").is_error


def test_empty(ctx):
    assert shell(ctx, command="  ").is_error


def test_timeout_and_output(ctx):
    async def scenario():
        result = await ShellTool(ctx.shell).run(
            {"command": python("import time; time.sleep(3); print('later')"), "timeout": 1000}, ctx
        )
        assert result.meta["timed_out"]
        output = await JobOutputTool().run({"job_id": result.meta["job_id"], "wait": True}, ctx)
        assert "later" in output.content
        assert "exited with code 0" in output.content

    asyncio.run(scenario())


def test_job_output_timeout_implies_wait(ctx):
    async def scenario():
        started = await ShellTool(ctx.shell).run(
            {
                "command": python("import time; time.sleep(1); print('done')"),
                "run_in_background": True,
            },
            ctx,
        )
        job_id = started.meta["job_id"]
        peek = await JobOutputTool().run({"job_id": job_id, "timeout": 30000, "wait": False}, ctx)
        assert "running" in peek.content  # an explicit wait: false still returns at once
        waited = await JobOutputTool().run({"job_id": job_id, "timeout": 30000}, ctx)
        assert "done" in waited.content
        assert "exited with code 0" in waited.content

    asyncio.run(scenario())


def test_background_list_kill(ctx):
    async def scenario():
        result = await ShellTool(ctx.shell).run(
            {
                "command": python("import time; print('ready', flush=True); time.sleep(30)"),
                "run_in_background": True,
            },
            ctx,
        )
        job_id = result.meta["job_id"]
        job = ctx.jobs.get(job_id)
        assert job is not None
        for _ in range(100):
            if b"ready" in job.run.head:
                break
            await asyncio.sleep(0.05)
        assert b"ready" in job.run.head
        assert f"{job_id}: running" in (await JobListTool().run({}, ctx)).content
        assert "stopped" in (await JobKillTool().run({"job_id": job_id}, ctx)).content
        assert job.run.returncode is not None
        assert job.status == "killed"
        assert not ctx.jobs.drain_finished()

    asyncio.run(scenario())


def test_unknown_job(ctx):
    assert asyncio.run(JobOutputTool().run({"job_id": "999"}, ctx)).is_error


def test_drain_once(ctx):
    async def scenario():
        result = await ShellTool(ctx.shell).run(
            {"command": "echo finished", "run_in_background": True}, ctx
        )
        job = ctx.jobs.get(result.meta["job_id"])
        assert job is not None
        assert await job.run.wait(10)
        assert len(ctx.jobs.drain_finished()) == 1
        assert ctx.jobs.drain_finished() == []

    asyncio.run(scenario())


def test_spill(ctx):
    result = shell(ctx, command=python("print('A'*16000 + 'M'*16000 + 'Z'*16000)"))
    assert "Output truncated" in result.content
    assert "A" * 100 in result.content
    assert "Z" * 100 in result.content
    assert (
        Path(result.meta["spill_path"]).read_text().strip()
        == "A" * 16000 + "M" * 16000 + "Z" * 16000
    )


def test_stdin_closed(ctx):
    result = shell(ctx, command=python("input()"))
    assert "EOFError" in result.content
    assert result.meta["exit_code"] != 0


@pytest.mark.parametrize(("name", "value"), [("NO_COLOR", "1"), ("PAGER", "cat")])
def test_environment(ctx, name, value):
    result = shell(ctx, command=python(f"import os; print(os.environ[{name!r}])"))
    assert result.content.startswith(value + "\n")


def test_deny_approval(ctx):
    requests = []

    async def deny(request):
        requests.append(request)
        return False

    ctx.approve = deny
    result = shell(ctx, command="rm -rf /")
    assert result.is_error
    assert "Not executed" in result.content
    assert len(requests) == 1


def test_detect_shell():
    assert Path(detect_shell().executable).is_file()


def test_override(ctx):
    assert detect_shell({"KAROX_SHELL": ctx.shell.executable}).executable == ctx.shell.executable


def test_shutdown(ctx):
    async def scenario():
        result = await ShellTool(ctx.shell).run(
            {
                "command": python("import time; print('ready', flush=True); time.sleep(30)"),
                "run_in_background": True,
            },
            ctx,
        )
        job = ctx.jobs.get(result.meta["job_id"])
        assert job is not None
        for _ in range(100):
            if b"ready" in job.run.head:
                break
            await asyncio.sleep(0.05)
        assert b"ready" in job.run.head
        await ctx.jobs.shutdown()
        assert job.run.returncode is not None
        assert job.status == "killed"

    asyncio.run(scenario())


@pytest.mark.parametrize("count", [1, 2, 4])
def test_backslashes_reach_the_shell_unchanged(ctx, count):
    """Git Bash on Windows halved backslash runs taken from its command line."""
    if ctx.shell.kind != "bash":
        pytest.skip("bash quoting")
    backslash = chr(92)
    for literal in (f"'a{backslash * count}x'", f"'C:{backslash * count}Users'"):
        result = shell(ctx, command=f"printf '%s|' {literal}")
        assert result.meta["exit_code"] == 0, result.content
        assert result.content.count(backslash) == count, result.content


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (r"C:\Program Files\Git\bin\bash.exe", True),
        (r"C:\msys64\usr\bin\bash.exe", True),
        ("C:/cygwin64/bin/bash", True),
        (r"C:\Windows\System32\bash.exe", False),  # the WSL launcher
        (r"C:\Users\u\AppData\Local\Microsoft\WindowsApps\bash.exe", False),
        (r"C:\tools\zsh.exe", False),
        ("bash", False),
    ],
)
def test_msys_bash_from_karox_shell_gets_the_script_file(path, expected):
    """KAROX_SHELL pointing at Git Bash must get the backslash-safe launch too."""
    assert is_msys_bash(path) is expected


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_kill_reaches_a_child_the_shell_left_behind(ctx):
    async def scenario():
        run = await ProcessRun.start(
            ["bash", "-c", "sleep 300 & echo $!"],
            cwd=ctx.workspace,
            env=dict(os.environ),
            log_path=ctx.workspace / "left.log",
            command="left behind",
        )
        await asyncio.wait_for(run.exited(), 10)
        for _ in range(100):
            if run.head.strip():
                break
            await asyncio.sleep(0.02)
        assert not run.reader.done()  # the sleep still holds the output pipe
        await run.kill()
        return run, int(run.head.decode().strip())

    run, child = asyncio.run(scenario())
    for _ in range(100):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(child, 9)
        pytest.fail("the child the shell left behind survived kill()")
    assert run.killed is False  # the shell itself had exited on its own


def test_command_that_leaves_a_child_running_returns_when_the_shell_exits(ctx):
    """`server &` keeps the output pipe open; the call must not wait for its timeout."""
    if ctx.shell.kind != "bash":
        pytest.skip("bash syntax")

    async def scenario():
        started = time.monotonic()
        command = "sleep 30 & echo started"
        result = await ShellTool(ctx.shell).run({"command": command, "timeout": 20000}, ctx)
        took = time.monotonic() - started
        assert took < 5, f"took {took:.1f}s"
        assert result.meta.get("exit_code") == 0, result.content
        assert "started" in result.content
        # The child is tracked as a background job, so the session can stop it.
        job = ctx.jobs.get(result.meta["job_id"])
        assert job is not None
        assert job.status == "running"
        assert ctx.jobs.running() == [job]
        assert ctx.jobs.drain_finished() == []
        await ctx.jobs.shutdown()
        if sys.platform != "win32":  # Windows: the job object ends it with KaroX
            assert ctx.jobs.running() == []

    asyncio.run(scenario())


def test_command_longer_than_a_windows_command_line(ctx):
    if ctx.shell.kind != "bash":
        pytest.skip("bash syntax")
    result = shell(ctx, command=f"printf '%s' '{'x' * 40_000}' | wc -c")
    assert result.meta["exit_code"] == 0, result.content[-300:]
    assert "40000" in result.content


def test_cancelled_command_is_killed(ctx, monkeypatch):
    """Cancelling the run (Ctrl+C, karox_cancel) must not leave the command running."""
    from karox6.tools import shell as shell_module

    started = []
    real_start = shell_module.ProcessRun.start

    async def spy(*args, **kwargs):
        run = await real_start(*args, **kwargs)
        started.append(run)
        return run

    monkeypatch.setattr(shell_module.ProcessRun, "start", spy)

    async def scenario():
        command = python("import time; print('ready', flush=True); time.sleep(30)")
        task = asyncio.create_task(ShellTool(ctx.shell).run({"command": command}, ctx))
        for _ in range(200):
            if started and b"ready" in started[0].head:
                break
            await asyncio.sleep(0.05)
        assert started
        assert b"ready" in started[0].head
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return started[0]

    run = asyncio.run(scenario())
    assert run.killed
    assert run.returncode is not None


def test_list_empty(ctx):
    assert asyncio.run(JobListTool().run({}, ctx)).content == "No background jobs."


def test_kill_unknown(ctx):
    assert asyncio.run(JobKillTool().run({"job_id": "999"}, ctx)).is_error


def test_finished_reported_jobs_are_forgotten_beyond_a_limit():
    from types import SimpleNamespace

    from karox6.tools.jobs import KEEP_FINISHED, JobManager

    jobs = JobManager()
    done = SimpleNamespace(active=False, returncode=0, killed=False, command="true")
    for _ in range(KEEP_FINISHED + 10):
        jobs.register(done)  # type: ignore[arg-type]
        jobs.drain_finished()
    running = jobs.register(SimpleNamespace(active=True, returncode=None, command="sleep"))
    assert len(jobs._jobs) == KEEP_FINISHED + 1
    assert jobs.get("1") is None
    assert jobs.get(str(KEEP_FINISHED + 10)) is not None
    assert jobs.get(running.id) is running


def test_shell_wait_stops_at_the_run_deadline(ctx):
    async def scenario():
        ctx.deadline = time.monotonic() + 1.0
        started = time.monotonic()
        result = await ShellTool(ctx.shell).run(
            {"command": python("import time; time.sleep(4)"), "timeout": 60_000}, ctx
        )
        assert result.meta["timed_out"]
        assert time.monotonic() - started < 3.5
        await JobKillTool().run({"job_id": result.meta["job_id"]}, ctx)

    asyncio.run(scenario())


def test_deadline_cap_never_lengthens_a_wait(ctx):
    ctx.deadline = time.monotonic() + 100
    assert ctx.wait_cap_s(0.01) == 0.01
    ctx.deadline = time.monotonic() - 5
    assert ctx.wait_cap_s(30) == 0.05


def test_job_kill_stops_a_child_after_the_shell_exited(ctx):
    class Lingering:
        returncode = 0
        active = True
        killed = False
        kills = 0

        async def kill(self):
            self.kills += 1
            self.active = False

    run = Lingering()
    job = ctx.jobs.register(run)
    result = asyncio.run(JobKillTool().run({"job_id": job.id}, ctx))
    assert run.kills == 1
    assert "stopped" in result.content
    again = asyncio.run(JobKillTool().run({"job_id": job.id}, ctx))
    assert "already finished" in again.content


def test_command_wait_cap_turns_a_long_wait_into_a_job(ctx):
    import dataclasses
    import time as _time

    capped = dataclasses.replace(ctx, command_wait_cap_s=1)

    async def scenario():
        began = _time.monotonic()
        result = await ShellTool(ctx.shell).run(
            {"command": python("import time; time.sleep(3); print('later')"), "timeout": 60_000},
            capped,
        )
        assert _time.monotonic() - began < 2.5  # not the 60 s the model asked for
        assert result.meta["timed_out"]
        output = await JobOutputTool().run({"job_id": result.meta["job_id"], "wait": True}, ctx)
        assert "later" in output.content

    asyncio.run(scenario())


def test_pwsh_argv_switches_output_to_utf8():
    from karox6.tools.shell import PWSH_UTF8_PREFIX, ShellInfo

    info = ShellInfo("pwsh", "pwsh", "pwsh")
    assert info.argv("Get-ChildItem")[-1] == PWSH_UTF8_PREFIX + "Get-ChildItem"
    assert "UTF8Encoding $false" in PWSH_UTF8_PREFIX  # no BOM on piped input
    for lead in ("param($x) $x", "  using namespace System", "#Requires -Version 5"):
        assert info.argv(lead)[-1] == lead  # must stay the first statement


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell")
def test_pwsh_prints_cyrillic_and_keeps_exit_code(ctx):
    from karox6.tools.shell import ShellInfo

    exe = shutil.which("powershell") or shutil.which("pwsh")
    if exe is None:
        pytest.skip("no PowerShell")
    ctx.shell = ShellInfo("pwsh", exe, "powershell")
    command = "Write-Output 'проекты'; exit 3"
    result = asyncio.run(ShellTool(ctx.shell).run({"command": command}, ctx))
    assert "проекты" in result.content
    assert "[exit code: 3]" in result.content


def test_job_read_keeps_a_split_utf8_character_for_the_next_read(tmp_path):
    from karox6.tools.jobs import JobManager, incomplete_utf8_tail

    assert incomplete_utf8_tail("aб".encode()[:-1]) == 1
    assert incomplete_utf8_tail("😀".encode()[:3]) == 3
    assert incomplete_utf8_tail("aб".encode()) == 0
    assert incomplete_utf8_tail(b"abc") == 0
    assert incomplete_utf8_tail(bytes([97, 98, 0xFF])) == 0  # invalid: never held back
    assert incomplete_utf8_tail(bytes([97, 98, 0xC0])) == 0

    log = tmp_path / "job.log"
    word = "проекты".encode()
    log.write_bytes(word[:5])  # ends in the middle of the third letter

    class Run:
        active = True
        log_path = log
        command = "x"

        def _flush_log(self):
            pass

    class Job:
        id = 1
        read_offset = 0
        held_bytes = 0
        run = Run()

    manager = JobManager()
    job = Job()
    first = asyncio.run(manager.read_new(job, tmp_path))
    assert first == "пр"
    stalled = Job()  # no continuation arrives: the held byte is shown, not kept forever
    assert asyncio.run(manager.read_new(stalled, tmp_path)) == "пр"
    assert asyncio.run(manager.read_new(stalled, tmp_path)) == "\N{REPLACEMENT CHARACTER}"
    log.write_bytes(word)
    job.run.active = False
    second = asyncio.run(manager.read_new(job, tmp_path))
    assert second == "оекты"


def test_job_output_reports_elapsed_and_time_left(ctx):
    import time as _time

    ctx.deadline = _time.monotonic() + 500

    async def scenario():
        started = await ShellTool(ctx.shell).run(
            {"command": python("import time; time.sleep(5)"), "run_in_background": True}, ctx
        )
        out = await JobOutputTool().run({"job_id": started.meta["job_id"]}, ctx)
        await JobKillTool().run({"job_id": started.meta["job_id"]}, ctx)
        return out.content

    content = asyncio.run(scenario())
    assert re.search(r"running for \d+ s; \d+ s left in this run", content)
