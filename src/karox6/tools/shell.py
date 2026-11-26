"""The shell tool: a real shell for the agent.

bash on Unix; on Windows bash from Git for Windows when installed, else PowerShell.
Each call runs in a fresh process in the workspace (or ``workdir``), stdin closed,
merged stdout/stderr, an ``[exit code: N]`` marker, a timeout that moves a still
running command to a background job instead of killing it, and tree-kill on stop.
Tool contract follows DSH's bash/pwsh tools (MIT, see THIRD_PARTY_NOTICES.md).
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from karox6.context.spill import limit_output
from karox6.tools.base import ApprovalRequest, ToolContext, ToolResult
from karox6.tools.danger import check_command
from karox6.tools.proc import ProcessRun, silence_note

DEFAULT_TIMEOUT_MS = 120_000
MAX_TIMEOUT_MS = 600_000
INLINE_CHARS = 30_000

ENV_OVERRIDES = {
    "NO_COLOR": "1",
    "TERM": "dumb",
    "PAGER": "cat",
    "GIT_PAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
    "PYTHONUNBUFFERED": "1",
    "PYTHONIOENCODING": "utf-8",
}


@dataclass(frozen=True, slots=True)
class ShellInfo:
    kind: Literal["bash", "pwsh"]
    executable: str
    flavor: str  # bash | git-bash | sh | pwsh | powershell

    def argv(self, command: str, script: Path | None = None) -> list[str]:
        """``script``: where Git Bash on Windows gets the command as a file. Its runtime
        parses the Windows command line itself and halves every run of backslashes inside
        quotes (a regex, a Windows path, a printf format arrived broken), and a command
        line is capped at 32767 characters; a script file has neither problem."""
        if self.kind == "bash":
            if script is not None and self.flavor == "git-bash":
                script.parent.mkdir(parents=True, exist_ok=True)
                script.write_bytes(command.encode("utf-8", "replace"))
                return [self.executable, script.as_posix()]
            return [self.executable, "-c", command]
        # Redirected PowerShell output uses the console code page (OEM), but the output is
        # decoded as UTF-8: Cyrillic names came back as U+FFFD. Switch the session to UTF-8.
        return [
            self.executable,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command if _PWSH_MUST_LEAD.match(command) else PWSH_UTF8_PREFIX + command,
        ]


# BOM-less: [Text.Encoding]::UTF8 would prefix piped input to native programs with a BOM.
PWSH_UTF8_PREFIX = (
    "[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding $false;"
    "$OutputEncoding=New-Object System.Text.UTF8Encoding $false; "
)
# Statements PowerShell only accepts at the very start of a script.
_PWSH_MUST_LEAD = re.compile(r"^\s*(param\s*\(|using\s|#requires\b)", re.IGNORECASE)


def detect_shell(environ: dict[str, str] | None = None) -> ShellInfo:
    env = dict(os.environ if environ is None else environ)
    override = env.get("KAROX_SHELL")
    if override:
        name = Path(override).name.lower()
        if "pwsh" in name or "powershell" in name:
            return ShellInfo("pwsh", override, "pwsh" if "pwsh" in name else "powershell")
        if sys.platform == "win32" and is_msys_bash(shutil.which(override) or override):
            return ShellInfo("bash", override, "git-bash")
        return ShellInfo("bash", override, "bash")
    if sys.platform != "win32":
        found = shutil.which("bash")
        if found:
            return ShellInfo("bash", found, "bash")
        return ShellInfo("bash", shutil.which("sh") or "/bin/sh", "sh")
    git_bash = _find_git_bash(env)
    if git_bash:
        return ShellInfo("bash", git_bash, "git-bash")
    for name in ("pwsh", "powershell"):
        found = shutil.which(name)
        if found:
            return ShellInfo("pwsh", found, name)
    return ShellInfo("pwsh", "powershell.exe", "powershell")


def is_msys_bash(path: str) -> bool:
    """A Windows bash on the MSYS/Cygwin runtime (Git Bash, MSYS2, Cygwin), which takes
    its command as a script file; not the WSL launcher (System32, WindowsApps), which
    could not open a Windows path."""
    lowered = path.replace("/", "\\").lower()
    name = lowered.rsplit("\\", 1)[-1]
    wsl = "\\windows\\system32\\" in lowered or "\\windowsapps\\" in lowered
    return name in ("bash", "bash.exe") and "\\" in lowered and not wsl


def _find_git_bash(env: dict[str, str]) -> str | None:
    """Locate Git for Windows' ``bin\\bash.exe``.

    The ``bin`` wrapper (not ``usr\\bin\\bash.exe``) sets up PATH so coreutils work
    when KaroX itself was not started from Git Bash.
    """
    upper = {k.upper(): v for k, v in env.items()}  # Windows env names are case-insensitive
    roots: list[Path] = []
    for tool in ("git", "bash"):
        found = shutil.which(tool)
        if found and not _is_wsl_launcher(found):
            roots.append(_git_root_of(Path(found)))
    for var in ("PROGRAMFILES", "PROGRAMW6432", "PROGRAMFILES(X86)"):
        if upper.get(var):
            roots.append(Path(upper[var]) / "Git")
    if upper.get("LOCALAPPDATA"):
        roots.append(Path(upper["LOCALAPPDATA"]) / "Programs" / "Git")
    for root in roots:
        for candidate in (root / "bin" / "bash.exe", root / "usr" / "bin" / "bash.exe"):
            if candidate.is_file():
                return str(candidate)
    return None


def _git_root_of(executable: Path) -> Path:
    """Map Git\\cmd\\git.exe, Git\\mingw64\\bin\\git.exe, Git\\usr\\bin\\bash.exe to Git."""
    path = executable.resolve().parent  # the bin/cmd directory
    while path.name.lower() in ("bin", "cmd", "mingw64", "mingw32", "clangarm64", "usr"):
        if path.parent == path:
            break
        path = path.parent
    return path


def _is_wsl_launcher(path: str) -> bool:
    lowered = path.lower().replace("/", "\\")
    return "\\system32\\" in lowered or "\\windowsapps\\" in lowered


def shell_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.update(ENV_OVERRIDES)
    if extra:
        env.update(extra)
    return env


class ShellTool:
    read_only = False

    def __init__(self, shell: ShellInfo) -> None:
        self.shell = shell
        self.name = "bash" if shell.kind == "bash" else "pwsh"
        self.description = _description(shell)
        self.parameters: dict[str, Any] = {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The command to run."},
                "description": {
                    "type": "string",
                    "description": "Five to ten words on what the command does.",
                },
                "timeout": {
                    "type": "integer",
                    "description": f"Timeout in ms (default {DEFAULT_TIMEOUT_MS}, "
                    f"max {MAX_TIMEOUT_MS}).",
                },
                "workdir": {
                    "type": "string",
                    "description": "Directory to run in (default: the workspace root).",
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": "Start as a background job and return at once "
                    "(servers, watchers, long builds).",
                },
            },
            "required": ["command"],
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = str(args["command"])
        if not command.strip():
            return ToolResult("Error: empty command.", is_error=True)
        cwd = ctx.workspace
        if args.get("workdir"):
            cwd = ctx.resolve(str(args["workdir"]))
            if not cwd.is_dir():
                return ToolResult(f"Error: workdir {cwd} is not a directory.", is_error=True)
        raw_timeout = args.get("timeout", args.get("timeoutMs", args.get("timeout_ms")))
        timeout_ms = DEFAULT_TIMEOUT_MS
        if raw_timeout is not None:
            try:
                timeout_ms = int(raw_timeout)
            except (TypeError, ValueError):
                timeout_ms = DEFAULT_TIMEOUT_MS
        timeout_ms = min(max(timeout_ms, 1_000), MAX_TIMEOUT_MS)
        if ctx.command_wait_cap_s is not None:
            # Past the cap the command keeps running as a job: the model gets control back
            # (to read its output so far, wait more or kill a hang) instead of blocking.
            timeout_ms = min(timeout_ms, max(ctx.command_wait_cap_s, 1) * 1000)

        danger = check_command(command, shell=self.shell.kind, cwd=cwd, workspace=ctx.workspace)
        if danger is not None:
            approved = await ctx.approve(
                ApprovalRequest(
                    tool=self.name, summary=command, rule=danger.rule, reason=danger.reason
                )
            )
            if not approved:
                return ToolResult(
                    f"Not executed: this command matches the destructive-command rule "
                    f"'{danger.rule}' ({danger.reason}) and the user did not confirm it. "
                    "Do not retry it in another form; use a narrower command (for example "
                    "delete specific paths inside the workspace) or leave this step to the user.",
                    is_error=True,
                    meta={"blocked_rule": danger.rule},
                )

        log_dir = ctx.session_dir / "shell"
        index = len(list(log_dir.glob("*.log"))) + 1 if log_dir.exists() else 1
        log_path = log_dir / f"cmd-{index:04d}.log"
        try:
            # Claim the number before the first await: a concurrent call (MCP direct mode)
            # would otherwise count the same logs and share this log and script file.
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path.touch()
            run = await ProcessRun.start(
                self.shell.argv(command, script=log_path.with_suffix(".sh")),
                cwd=cwd,
                env=shell_env(ctx.env),
                log_path=log_path,
                command=command,
            )
        except OSError as exc:
            return ToolResult(
                f"Error: could not start {self.shell.executable}: {exc}", is_error=True
            )

        if args.get("run_in_background"):
            job = ctx.jobs.register(run)
            return ToolResult(
                f"Started background job {job.id} (pid {run.pid}). Output is written to "
                f"{log_path}. Use job_output with job_id {job.id} to read it and job_kill "
                "to stop it.",
                meta={"job_id": job.id, "pid": run.pid},
            )

        try:
            # A wait never outlasts the run's time limit; the command keeps running as a
            # background job, as on any timeout.
            waited_s = ctx.wait_cap_s(timeout_ms / 1000)
            finished = await run.wait(waited_s)
        except BaseException:  # the run was cancelled: the command must not outlive it
            await run.kill_on_cancel()
            raise
        text, _complete = run.output_text()
        if finished:
            marker = f"[exit code: {run.returncode}]"
            meta: dict[str, Any] = {"exit_code": run.returncode}
            if run.active:  # a process it started (`server &`) still writes to the output
                job = ctx.jobs.register(run)
                job.read_offset = run.total_bytes
                marker = (
                    f"[exit code: {run.returncode}; a process it started is still running and "
                    f"writing to this output: background job {job.id}. Use job_output to "
                    "read it, job_kill to stop it.]"
                )
                meta["job_id"] = job.id
        else:
            job = ctx.jobs.register(run)
            job.read_offset = run.total_bytes
            marker = (
                f"[still running after {int(waited_s * 1000)} ms; moved to background job "
                f"{job.id}. "
                "Use job_output to read more output or wait for it, job_kill to stop it.]"
                f"{silence_note(run)}"
            )
            meta = {"job_id": job.id, "timed_out": True}
        inline, spill = limit_output(
            text,
            spill_dir=ctx.spill_dir,
            name=self.name,
            max_chars=INLINE_CHARS,
            full_path=log_path,
        )
        if spill is not None:
            meta["spill_path"] = str(spill)
        body = inline if inline.endswith("\n") or not inline else inline + "\n"
        return ToolResult(f"{body}{marker}", meta=meta)


def _description(shell: ShellInfo) -> str:
    common = (
        " Each call runs in a fresh process with stdin closed, in the workspace root unless "
        "`workdir` is given (prefer `workdir` over `cd`). Output is stdout and stderr merged, "
        "followed by an `[exit code: N]` marker; check it and investigate failures before "
        f"moving on. Default timeout {DEFAULT_TIMEOUT_MS} ms (max {MAX_TIMEOUT_MS}); a command "
        "still running at its timeout keeps running as a background job instead of being "
        "killed. Use `run_in_background` for servers and watchers, then job_output/job_kill. "
        "To wait for a long command, call job_output with wait: true on its job: it returns "
        "as soon as the command ends, while `sleep N` always waits N seconds and a command "
        "detached with nohup or `&` is not tracked as a job. "
        "Long output keeps its head and tail inline and is saved in full to a file whose path "
        "is reported, so run tests and builds without piping them through `tail` or `head`; "
        "read that file for omitted lines instead of rerunning the command. Never start "
        "interactive programs (editors, pagers, prompts). Before any "
        "delete or move, check that the target path is the intended one."
    )
    if shell.kind == "bash":
        intro = "Execute a bash command (`bash -c`) and return its output."
        if shell.flavor == "git-bash":
            intro += (
                " This is Git Bash on Windows: use POSIX shell syntax; write Windows paths as "
                "`/c/Users/...` or `C:/Users/...`."
            )
        elif shell.flavor == "sh":
            intro = "Execute a POSIX sh command (`sh -c`) and return its output."
        return intro + common
    return (
        "Execute a PowerShell command (`-Command`) and return its output. Paths use native "
        "Windows form (`C:\\...`); read environment variables with `$env:NAME`. On Windows a "
        "force-killed command settles as `[exit code: 1]`." + common
    )
