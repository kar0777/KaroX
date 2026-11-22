import asyncio
import dataclasses
import os
import shlex
import shutil
import sys
import time
from pathlib import Path

import pytest

from karox6.tools.jobs import JobKillTool, JobListTool, JobOutputTool
from karox6.tools.proc import ProcessRun
from karox6.tools.shell import (
    ShellInfo,
    ShellTool,
    detect_shell,
    format_exit_marker,
    is_msys_bash,
    wrap_command_for_pipestatus,
)


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


def test_pipeline_exit_codes_reported_when_stage_fails(ctx):
    if ctx.shell.kind != "bash":
        pytest.skip("bash pipeline statuses")
    result = shell(ctx, command="false | cat")
    assert result.meta["exit_code"] == 0
    assert result.meta["pipeline_exit_codes"] == [1, 0]
    assert result.content.endswith("[exit code: 0] [pipeline exit codes: 1 0]")


def test_pipeline_exit_codes_omitted_when_all_stages_succeed(ctx):
    if ctx.shell.kind != "bash":
        pytest.skip("bash pipeline statuses")
    result = shell(ctx, command="true | cat")
    assert result.meta["exit_code"] == 0
    assert "pipeline_exit_codes" not in result.meta
    assert "[pipeline exit codes:" not in result.content
    assert result.content.endswith("[exit code: 0]")


def test_pipeline_exit_codes_multi_stage_and_explicit_exit_unchanged(ctx):
    if ctx.shell.kind != "bash":
        pytest.skip("bash pipeline statuses")
    multi = shell(ctx, command="false | true | (exit 2)")
    assert multi.meta["exit_code"] == 2
    assert multi.meta["pipeline_exit_codes"] == [1, 0, 2]
    assert multi.content.endswith("[exit code: 2] [pipeline exit codes: 1 0 2]")

    exited = shell(ctx, command="false | cat; exit 3")
    assert exited.meta["exit_code"] == 3
    assert "pipeline_exit_codes" not in exited.meta
    assert "[pipeline exit codes:" not in exited.content
    assert exited.content.endswith("[exit code: 3]")

    with_e_and_ifs = shell(ctx, command="set -e; IFS=,:; false | cat")
    assert with_e_and_ifs.meta["exit_code"] == 0
    assert with_e_and_ifs.meta["pipeline_exit_codes"] == [1, 0]
    assert with_e_and_ifs.content.endswith("[exit code: 0] [pipeline exit codes: 1 0]")


def test_pipeline_wrapper_and_description_skip_pwsh(tmp_path):
    bash_tool = ShellTool(ShellInfo("bash", "/bin/bash", "bash"))
    sh_tool = ShellTool(ShellInfo("bash", "/bin/sh", "sh"))
    pwsh_tool = ShellTool(ShellInfo("pwsh", "pwsh", "pwsh"))

    assert "[pipeline exit codes:" in bash_tool.description
    assert "[pipeline exit codes:" in sh_tool.description
    assert "[pipeline exit codes:" not in pwsh_tool.description

    cmd, env = wrap_command_for_pipestatus("false | cat", pwsh_tool.shell, tmp_path / "ps")
    assert cmd == "false | cat"
    assert env == {}

    sh_cmd, sh_env = wrap_command_for_pipestatus("false | cat", sh_tool.shell, tmp_path / "ps")
    assert "BASH_VERSION" in sh_cmd
    assert sh_cmd.endswith("false | cat")
    assert "KAROX_PIPESTATUS_FILE" in sh_env

    bash_cmd, bash_env = wrap_command_for_pipestatus(
        "false | cat", bash_tool.shell, tmp_path / "ps"
    )
    assert bash_cmd.endswith("false | cat")
    assert "trap " in bash_cmd
    assert "KAROX_PIPESTATUS_FILE" in bash_env


@pytest.mark.parametrize("suffix", ["&&", "||", "|"])
def test_pipeline_wrapper_does_not_execute_incomplete_operator_commands(ctx, suffix):
    if ctx.shell.kind != "bash":
        pytest.skip("bash syntax")
    result = shell(ctx, command=f"echo SHOULD_NOT_RUN {suffix}")
    assert result.meta["exit_code"] != 0
    assert "SHOULD_NOT_RUN" not in result.content
    assert "pipeline_exit_codes" not in result.meta


def test_pipeline_wrapper_preserves_trailing_backslash_and_unterminated_heredoc(ctx):
    if ctx.shell.kind != "bash":
        pytest.skip("bash syntax")
    bs = shell(ctx, command="echo hi \\")
    assert bs.meta["exit_code"] == 0, bs.content
    assert "hi \\" in bs.content

    heredoc = shell(ctx, command="cat <<EOF\nhello")
    assert heredoc.meta["exit_code"] == 0, heredoc.content
    assert "hello" in heredoc.content
    assert "__karox" not in heredoc.content
    assert "KAROX_PIPESTATUS_FILE" not in heredoc.content
    assert "PIPESTATUS" not in heredoc.content


def test_pipeline_wrapper_hidden_from_set_x_and_safe_against_user_functions(ctx):
    if ctx.shell.kind != "bash":
        pytest.skip("bash syntax")
    traced = shell(ctx, command="set -x; false | cat")
    assert traced.meta["exit_code"] == 0
    assert traced.meta["pipeline_exit_codes"] == [1, 0]
    assert "+ false" in traced.content
    assert "+ cat" in traced.content
    assert "__karox" not in traced.content
    assert "KAROX_PIPESTATUS_FILE" not in traced.content
    assert "PIPESTATUS" not in traced.content

    hijack = shell(
        ctx,
        command=(
            "printf() { echo HIJACK_PRINTF; }; "
            "set() { echo HIJACK_SET; }; "
            "exit() { echo HIJACK_EXIT; }; "
            "false | cat"
        ),
    )
    assert hijack.meta["exit_code"] == 0
    assert hijack.meta["pipeline_exit_codes"] == [1, 0]
    assert "HIJACK_" not in hijack.content


def test_pipeline_side_channel_deleted_on_background_job_handoff(ctx):
    if ctx.shell.kind != "bash":
        pytest.skip("bash pipeline statuses")

    capped = dataclasses.replace(ctx, command_wait_cap_s=1)

    async def scenario():
        result = await ShellTool(ctx.shell).run(
            {
                "command": f"false | {python('import time; time.sleep(2); print(123)')}",
                "timeout": 60_000,
            },
            capped,
        )
        assert result.meta["timed_out"]
        shell_dir = ctx.session_dir / "shell"
        assert list(shell_dir.glob("*.pipestatus")) == []
        output = await JobOutputTool().run({"job_id": result.meta["job_id"], "wait": True}, ctx)
        assert "123" in output.content
        assert "exited with code 0" in output.content
        assert list(shell_dir.glob("*.pipestatus")) == []

    asyncio.run(scenario())


def test_pipeline_sh_flavor_actually_executes(ctx):
    sh_exe = shutil.which("sh")
    if sh_exe is None and ctx.shell.kind == "bash":
        candidate = Path(ctx.shell.executable).resolve().parent.parent / "usr" / "bin" / "sh.exe"
        if candidate.is_file():
            sh_exe = str(candidate)
    if sh_exe is None and Path("/bin/sh").is_file():
        sh_exe = "/bin/sh"
    if sh_exe is None:
        pytest.skip("sh executable not installed")

    sh_info = ShellInfo("bash", sh_exe, "sh")
    sh_ctx = dataclasses.replace(ctx, shell=sh_info)

    ok_res = shell(sh_ctx, command="echo sh-works | cat")
    assert ok_res.meta["exit_code"] == 0, ok_res.content
    assert "sh-works" in ok_res.content
    assert "pipeline_exit_codes" not in ok_res.meta

    fail_pipe = shell(sh_ctx, command="false | cat")
    assert fail_pipe.meta["exit_code"] == 0, fail_pipe.content
    # When sh is bash (e.g. Git Bash sh.exe), PIPESTATUS is captured; on pure dash it stays clean.
    if "pipeline_exit_codes" in fail_pipe.meta:
        assert fail_pipe.meta["pipeline_exit_codes"] == [1, 0]

    explicit = shell(sh_ctx, command="false | cat; exit 3")
    assert explicit.meta["exit_code"] == 3, explicit.content
    assert "pipeline_exit_codes" not in explicit.meta


def test_pipeline_wrapper_same_line_preserves_line_numbers_and_comments(ctx, tmp_path):
    bash_shell = ShellInfo("bash", "/bin/bash", "bash")
    sh_shell = ShellInfo("bash", "/bin/sh", "sh")
    wrapped_bash, _ = wrap_command_for_pipestatus("echo hi", bash_shell, tmp_path / "ps")
    wrapped_sh, _ = wrap_command_for_pipestatus("echo hi", sh_shell, tmp_path / "ps")
    assert "\n" not in wrapped_bash
    assert "\n" not in wrapped_sh

    if ctx.shell.kind != "bash":
        pytest.skip("bash syntax")

    lineno = shell(ctx, command='echo "L1=$LINENO"\necho "L2=$LINENO"')
    assert lineno.meta["exit_code"] == 0, lineno.content
    assert "L1=1" in lineno.content
    assert "L2=2" in lineno.content

    comment = shell(ctx, command='# leading comment\necho "L2=$LINENO"')
    assert comment.meta["exit_code"] == 0, comment.content
    assert "L2=2" in comment.content

    syntax_err = shell(ctx, command="echo ok\nif then")
    assert syntax_err.meta["exit_code"] != 0
    assert "line 2:" in syntax_err.content


def test_pipeline_ignores_sigpipe_141_in_non_last_stages(ctx):
    assert format_exit_marker(0, [141, 0]) == ("[exit code: 0]", None)
    assert format_exit_marker(0, [141, 141, 0]) == ("[exit code: 0]", None)
    assert format_exit_marker(0, [1, 141, 0]) == (
        "[exit code: 0] [pipeline exit codes: 1 141 0]",
        [1, 141, 0],
    )
    assert format_exit_marker(141, [0, 141]) == (
        "[exit code: 141] (128+13, likely signal 13) [pipeline exit codes: 0 141]",
        [0, 141],
    )

    if ctx.shell.kind != "bash":
        pytest.skip("bash pipeline statuses")

    sigpipe_sim = shell(ctx, command="(exit 141) | true")
    assert sigpipe_sim.meta["exit_code"] == 0
    assert "pipeline_exit_codes" not in sigpipe_sim.meta
    assert "[pipeline exit codes:" not in sigpipe_sim.content

    head_pipe = shell(ctx, command="yes | head -n 1")
    assert head_pipe.meta["exit_code"] == 0
    assert "pipeline_exit_codes" not in head_pipe.meta
    assert "[pipeline exit codes:" not in head_pipe.content


def test_signal_range_keeps_raw_exit_code_with_hint(ctx):
    assert format_exit_marker(130, [130]) == ("[exit code: 130] (128+2, likely signal 2)", None)
    assert format_exit_marker(139, [139]) == ("[exit code: 139] (128+11, likely signal 11)", None)
    # Explicit `exit 139` skips writing pipestatus, so it stays unchanged with no hint.
    assert format_exit_marker(139, None) == ("[exit code: 139]", None)
    # Boundary and out-of-range codes get no hint.
    assert format_exit_marker(128, [128]) == ("[exit code: 128]", None)
    assert format_exit_marker(193, [193]) == ("[exit code: 193]", None)
    assert format_exit_marker(255, [255]) == ("[exit code: 255]", None)

    if ctx.shell.kind != "bash":
        pytest.skip("bash signal reporting")

    explicit = shell(ctx, command="exit 139")
    assert explicit.meta["exit_code"] == 139
    assert explicit.content.endswith("[exit code: 139]")

    py_130 = shell(ctx, command=python("import sys; sys.exit(130)"))
    assert py_130.meta["exit_code"] == 130
    assert py_130.content.endswith("[exit code: 130] (128+2, likely signal 2)")

    sh_137 = shell(ctx, command="sh -c 'exit 137'")
    assert sh_137.meta["exit_code"] == 137
    assert sh_137.content.endswith("[exit code: 137] (128+9, likely signal 9)")

    if sys.platform != "win32":
        segv = shell(ctx, command=python("import os, signal; os.kill(os.getpid(), signal.SIGSEGV)"))
        assert segv.meta["exit_code"] == 139
        assert segv.content.endswith("[exit code: 139] (128+11, likely signal 11)")
    else:
        sig_wait = shell(ctx, command="sleep 10 & p=$!; kill -11 $p; wait $p")
        assert sig_wait.meta["exit_code"] == 139, sig_wait.content
        assert sig_wait.content.endswith("[exit code: 139] (128+11, likely signal 11)"), (
            sig_wait.content
        )


def test_pipeline_wrapper_user_exit_trap_and_exec(ctx):
    if ctx.shell.kind != "bash":
        pytest.skip("bash syntax")

    user_trap = shell(ctx, command="trap 'echo USER_TRAP' EXIT; false | cat")
    assert user_trap.meta["exit_code"] == 0
    assert "USER_TRAP" in user_trap.content

    via_exec = shell(ctx, command="exec echo via-exec")
    assert via_exec.meta["exit_code"] == 0
    assert "via-exec" in via_exec.content
