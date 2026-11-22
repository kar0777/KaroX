"""The baseline tool: run one command against HEAD in a temporary detached worktree.

Never touches the user's working tree, index, stashes, or branch. Creates a temporary
checkout via ``git worktree add --detach <temp dir> HEAD``, runs the command there with
a hard tree-kill timeout, and always removes and prunes the temporary worktree in a
``finally`` block.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from karox6.context.spill import limit_output
from karox6.tools.base import ApprovalRequest, ToolContext, ToolResult
from karox6.tools.danger import check_command
from karox6.tools.proc import ProcessRun
from karox6.tools.shell import (
    INLINE_CHARS,
    ShellInfo,
    format_exit_marker,
    read_pipestatus,
    shell_env,
    wrap_command_for_pipestatus,
)

DEFAULT_TIMEOUT_MS = 120_000
MAX_TIMEOUT_MS = 300_000
BASELINE_HEADER = (
    "Baseline: this command ran in a temporary checkout of HEAD (your changes are untouched). "
    "Untracked and ignored files (build outputs, compiled extensions, virtualenvs) are not "
    "present there."
)
_GIT_TIMEOUT_S = 120


@dataclass(slots=True)
class _GitOut:
    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def text(self) -> str:
        return self.stdout.decode("utf-8", "replace")

    @property
    def err(self) -> str:
        return self.stderr.decode("utf-8", "replace").strip()


def _git(cwd: Path, *args: str) -> _GitOut:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_PAGER"] = "cat"
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=_GIT_TIMEOUT_S,
        check=False,
    )
    return _GitOut(proc.returncode, proc.stdout, proc.stderr)


def _validate_repo(cwd: Path) -> tuple[Path | None, str | None]:
    """Verify ``cwd`` is inside a git work tree with at least one commit (HEAD)."""
    try:
        inside = _git(cwd, "rev-parse", "--is-inside-work-tree")
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"Error: git is not available ({exc})."
    if inside.returncode != 0 or inside.text.strip() != "true":
        return None, "Error: workspace is not a git work tree."

    top = _git(cwd, "rev-parse", "--show-toplevel")
    if top.returncode != 0 or not top.text.strip():
        return None, f"Error: could not determine git repository root ({top.err})."
    repo_root = Path(top.text.strip())

    head = _git(repo_root, "rev-parse", "--verify", "HEAD")
    if head.returncode != 0:
        return None, "Error: repository has no commits (HEAD does not exist)."

    return repo_root, None


def _add_worktree(repo_root: Path, wt_dir: Path) -> str | None:
    try:
        res = _git(repo_root, "worktree", "add", "--detach", str(wt_dir), "HEAD")
    except (OSError, subprocess.SubprocessError) as exc:
        return f"Error: git worktree add failed ({exc})."
    if res.returncode != 0:
        detail = res.err or res.text.strip()
        return f"Error: git worktree add failed: {detail}"
    return None


def _link_node_modules(src_root: Path, dst_root: Path) -> Path | None:
    """Link ``src_root/node_modules`` into ``dst_root/node_modules`` if missing; skip on error."""
    src = src_root / "node_modules"
    dst = dst_root / "node_modules"
    if not src.is_dir() or dst.exists() or dst.is_symlink():
        return None
    try:
        if sys.platform == "win32":
            import _winapi

            _winapi.CreateJunction(str(src), str(dst))
        else:
            os.symlink(src, dst, target_is_directory=True)
    except OSError:
        return None
    return dst


def _remove_link_only(path: Path) -> None:
    """Remove a symlink or Windows junction without ever touching its target."""
    with contextlib.suppress(OSError):
        if path.is_symlink():
            path.unlink()
        elif path.exists():
            os.rmdir(path)


def _handle_remove_readonly(func: Callable[..., Any], path: str, _exc: BaseException) -> None:
    with contextlib.suppress(OSError):
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        func(path)


def _rmtree_force(path: Path) -> None:
    if not path.exists():
        return
    with contextlib.suppress(OSError):
        shutil.rmtree(path, onexc=_handle_remove_readonly)


def _cleanup_worktree(
    repo_root: Path,
    temp_parent: Path,
    wt_dir: Path,
    created_links: list[Path],
) -> None:
    """Always remove linked dirs, then ``git worktree remove --force`` + ``prune``."""
    for link in created_links:
        _remove_link_only(link)

    if wt_dir.exists():
        removed = False
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            res = _git(repo_root, "worktree", "remove", "--force", str(wt_dir))
            removed = res.returncode == 0 and not wt_dir.exists()
        if not removed and wt_dir.exists():
            _rmtree_force(wt_dir)

    with contextlib.suppress(OSError, subprocess.SubprocessError):
        _git(repo_root, "worktree", "prune")

    _rmtree_force(temp_parent)


def _pythonpath_env(wt_dir: Path, base_env: Mapping[str, str]) -> dict[str, str]:
    """Prepend the temporary checkout root (and its ``src/`` if present) to PYTHONPATH."""
    prepend = [str(wt_dir)]
    src_dir = wt_dir / "src"
    if src_dir.is_dir():
        prepend.append(str(src_dir))

    existing: str | None = None
    for key, val in base_env.items():
        if key == "PYTHONPATH" or (sys.platform == "win32" and key.upper() == "PYTHONPATH"):
            existing = val
            break
    if existing is None:
        existing = os.environ.get("PYTHONPATH")

    parts = [*prepend, existing] if existing else prepend
    return {"PYTHONPATH": os.pathsep.join(parts)}


async def _await_shielded(task: asyncio.Future[Any]) -> Any:
    """Run ``task`` to completion even if the caller receives ``CancelledError``."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


class BaselineTool:
    name = "baseline"
    description = (
        "Run a test or check command against the last commit in a temporary checkout of HEAD, "
        "leaving your working tree untouched: shows which failures already existed before your "
        "change. Use instead of git stash."
    )
    read_only = False

    def __init__(self, shell: ShellInfo) -> None:
        self.shell = shell
        self.parameters: dict[str, Any] = {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The command to run against HEAD."},
                "timeout": {
                    "type": "integer",
                    "description": (
                        f"Timeout in ms (default {DEFAULT_TIMEOUT_MS}, max {MAX_TIMEOUT_MS})."
                    ),
                },
            },
            "required": ["command"],
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = str(args["command"])
        if not command.strip():
            return ToolResult("Error: empty command.", is_error=True)

        base_cwd = ctx.workspace
        if args.get("workdir"):
            base_cwd = ctx.resolve(str(args["workdir"]))
            if not base_cwd.is_dir():
                return ToolResult(f"Error: workdir {base_cwd} is not a directory.", is_error=True)

        raw_timeout = args.get("timeout", args.get("timeoutMs", args.get("timeout_ms")))
        timeout_ms = DEFAULT_TIMEOUT_MS
        if raw_timeout is not None:
            try:
                timeout_ms = int(raw_timeout)
            except (TypeError, ValueError):
                timeout_ms = DEFAULT_TIMEOUT_MS
        timeout_ms = min(max(timeout_ms, 1_000), MAX_TIMEOUT_MS)

        danger = check_command(
            command, shell=self.shell.kind, cwd=base_cwd, workspace=ctx.workspace
        )
        if danger is not None:
            approved = await ctx.approve(
                ApprovalRequest(
                    tool=self.name, summary=command, rule=danger.rule, reason=danger.reason
                )
            )
            if not approved:
                return ToolResult(
                    f"Not executed: this command matches the destructive-command rule "
                    f"'{danger.rule}' ({danger.reason}) and the user did not confirm it.",
                    is_error=True,
                    meta={"blocked_rule": danger.rule},
                )

        repo_root, err = await asyncio.to_thread(_validate_repo, base_cwd)
        if err is not None or repo_root is None:
            return ToolResult(err or "Error: could not validate git repository.", is_error=True)

        try:
            rel_cwd = base_cwd.resolve().relative_to(repo_root.resolve())
        except ValueError:
            rel_cwd = Path(".")

        temp_parent = Path(tempfile.mkdtemp(prefix="karox-baseline-"))
        wt_dir = temp_parent / "wt"
        created_links: list[Path] = []
        add_task: asyncio.Future[str | None] | None = None

        text = ""
        marker = ""
        meta: dict[str, Any] = {}
        start_error: str | None = None
        add_error: str | None = None
        log_path: Path | None = None

        try:
            add_task = asyncio.ensure_future(asyncio.to_thread(_add_worktree, repo_root, wt_dir))
            add_error = await add_task
            if add_error is None:
                candidate_cwd = wt_dir / rel_cwd
                run_cwd = candidate_cwd if candidate_cwd.is_dir() else wt_dir

                link = _link_node_modules(base_cwd, run_cwd)
                if link is not None:
                    created_links.append(link)
                if repo_root.resolve() != base_cwd.resolve():
                    root_link = _link_node_modules(repo_root, wt_dir)
                    if root_link is not None:
                        created_links.append(root_link)

                log_dir = ctx.session_dir / "baseline"
                log_dir.mkdir(parents=True, exist_ok=True)
                index = len(list(log_dir.glob("*.log"))) + 1
                log_path = log_dir / f"base-{index:04d}.log"
                log_path.touch()

                pipestatus_path = (
                    log_path.with_suffix(".pipestatus") if self.shell.kind == "bash" else None
                )
                wrapped_command, extra_env = wrap_command_for_pipestatus(
                    command, self.shell, pipestatus_path
                )
                run_env = {**ctx.env, **_pythonpath_env(wt_dir, ctx.env), **extra_env}

                try:
                    run = await ProcessRun.start(
                        self.shell.argv(wrapped_command, script=log_path.with_suffix(".sh")),
                        cwd=run_cwd,
                        env=shell_env(run_env),
                        log_path=log_path,
                        command=command,
                    )
                except OSError as exc:
                    start_error = f"Error: could not start {self.shell.executable}: {exc}"
                else:
                    waited_s = ctx.wait_cap_s(timeout_ms / 1000)
                    timed_out = False
                    try:
                        finished = await run.wait(waited_s)
                        if not finished:
                            timed_out = True
                            await run.kill()
                        elif run.active:
                            await run.kill()
                    except BaseException:
                        await run.kill_on_cancel()
                        with contextlib.suppress(OSError):
                            if pipestatus_path is not None:
                                pipestatus_path.unlink(missing_ok=True)
                        raise

                    text, _complete = run.output_text()
                    if timed_out:
                        with contextlib.suppress(OSError):
                            if pipestatus_path is not None:
                                pipestatus_path.unlink(missing_ok=True)
                        marker = f"[timed out after {int(waited_s * 1000)} ms; process killed]"
                        meta["timed_out"] = True
                    else:
                        pipestatus = read_pipestatus(pipestatus_path)
                        marker, reported_pipe = format_exit_marker(run.returncode, pipestatus)
                        meta["exit_code"] = run.returncode
                        if reported_pipe is not None:
                            meta["pipeline_exit_codes"] = reported_pipe
        finally:
            if add_task is not None and not add_task.done():
                with contextlib.suppress(BaseException):
                    await _await_shielded(add_task)
            cleanup_task = asyncio.ensure_future(
                asyncio.to_thread(_cleanup_worktree, repo_root, temp_parent, wt_dir, created_links)
            )
            await _await_shielded(cleanup_task)

        if add_error is not None:
            return ToolResult(add_error, is_error=True)
        if start_error is not None:
            return ToolResult(start_error, is_error=True, meta=meta)

        assert log_path is not None
        inline, spill = limit_output(
            text,
            spill_dir=ctx.spill_dir,
            name=self.name,
            max_chars=INLINE_CHARS,
            full_path=log_path,
        )
        if spill is not None:
            meta["spill_path"] = str(spill)

        parts = [BASELINE_HEADER]
        if inline:
            parts.append(inline.rstrip("\n"))
        if marker:
            parts.append(marker)
        return ToolResult("\n".join(parts), is_error=False, meta=meta)
