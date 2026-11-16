import asyncio
import shlex
import sys
from pathlib import Path

import pytest

from karox6.tools.jobs import JobKillTool, JobListTool, JobOutputTool
from karox6.tools.shell import ShellTool, detect_shell


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
