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


def test_list_empty(ctx):
    assert asyncio.run(JobListTool().run({}, ctx)).content == "No background jobs."


def test_kill_unknown(ctx):
    assert asyncio.run(JobKillTool().run({"job_id": "999"}, ctx)).is_error
