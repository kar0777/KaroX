"""The baseline tool: run one command against HEAD with uncommitted changes set aside.

Saves a durable backup before touching the working tree (a stash commit pinned under
``refs/karox/baseline/<timestamp>`` plus copies of untracked non-ignored files in a
private temp directory), stashes changes with ``git stash push --include-untracked``,
runs the command with a hard tree-kill timeout, and always restores in a ``finally``
block without ever running ``git reset --hard``, ``git checkout -- .``, or ``git clean``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import tempfile
import time
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
BASELINE_STASH_MESSAGE = "karox-baseline"
LEFTOVERS_STASH_MESSAGE = "karox-baseline-leftovers"
BASELINE_HEADER = "Baseline (HEAD, your changes set aside and restored):"
_GIT_TIMEOUT_S = 60


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


@dataclass(slots=True)
class _BackupState:
    repo_root: Path
    saved_ref: str
    stash_oid: str
    pre_status: bytes
    clean_status: bytes
    pre_stash_oids: list[str]
    temp_dir: Path
    untracked_files: list[str]
    ident_args: tuple[str, ...]


@dataclass(slots=True)
class _RestoreOutcome:
    ok: bool
    leftovers_stashed: bool
    note: str
    stash_ref: str | None = None
    saved_ref: str = ""
    temp_dir: Path | None = None


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


def _status_porcelain_z(repo_root: Path) -> bytes:
    res = _git(repo_root, "status", "--porcelain=v1", "--untracked-files=all", "-z")
    return res.stdout if res.returncode == 0 else b""


def _has_unmerged(status_z: bytes) -> bool:
    entries = status_z.split(b"\0")
    skip_next = False
    for entry in entries:
        if skip_next:
            skip_next = False
            continue
        if len(entry) < 4:
            continue
        xy = entry[:2].decode("ascii", "replace")
        if "U" in xy or xy in ("AA", "DD"):
            return True
        if xy[0] in "RC":
            skip_next = True
    return False


def _in_progress_operation(git_dir: Path, status_z: bytes) -> str | None:
    if (git_dir / "MERGE_HEAD").exists():
        return "merge"
    if (
        (git_dir / "REBASE_HEAD").exists()
        or (git_dir / "rebase-merge").exists()
        or (git_dir / "rebase-apply").exists()
    ):
        return "rebase"
    if (git_dir / "CHERRY_PICK_HEAD").exists():
        return "cherry-pick"
    if _has_unmerged(status_z):
        return "merge/conflict"
    return None


def _stash_entries(repo_root: Path) -> list[tuple[str, str, str]]:
    """Return ``[(oid, gd_ref, subject), ...]`` for ``git stash list``."""
    res = _git(repo_root, "stash", "list", "--format=%H%x00%gd%x00%s")
    if res.returncode != 0 or not res.stdout:
        return []
    out: list[tuple[str, str, str]] = []
    for line in res.text.splitlines():
        parts = line.split("\0", 2)
        if len(parts) == 3:
            out.append((parts[0], parts[1], parts[2]))
    return out


def _find_stash_ref(repo_root: Path, oid: str) -> str | None:
    for entry_oid, gd_ref, _subject in _stash_entries(repo_root):
        if entry_oid == oid:
            return gd_ref
    return None


def _list_untracked(repo_root: Path) -> list[str]:
    res = _git(repo_root, "ls-files", "--others", "--exclude-standard", "-z")
    if res.returncode != 0 or not res.stdout:
        return []
    return [raw.decode("utf-8", "surrogateescape") for raw in res.stdout.split(b"\0") if raw]


def _copy_entry(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_symlink():
        if dst.exists() or dst.is_symlink():
            dst.unlink()
        os.symlink(os.readlink(src), dst)
    else:
        shutil.copy2(src, dst)


def _copy_untracked_to_temp(repo_root: Path, files: list[str], temp_dir: Path) -> None:
    for rel in files:
        src = repo_root / rel
        if src.exists() or src.is_symlink():
            _copy_entry(src, temp_dir / rel)


def _restore_untracked_from_temp(repo_root: Path, files: list[str], temp_dir: Path) -> list[str]:
    errors: list[str] = []
    for rel in files:
        src = temp_dir / rel
        if not src.exists() and not src.is_symlink():
            continue
        try:
            _copy_entry(src, repo_root / rel)
        except OSError as exc:
            errors.append(f"{rel}: {exc}")
    return errors


def _committer_ident_args(repo_root: Path) -> tuple[str, ...]:
    res = _git(repo_root, "var", "GIT_COMMITTER_IDENT")
    if res.returncode == 0 and res.text.strip():
        return ()
    return ("-c", "user.name=karox", "-c", "user.email=karox@local")


def _new_saved_ref() -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    nanos = time.time_ns() % 1_000_000_000
    return f"refs/karox/baseline/{stamp}-{nanos:09d}"


def _prepare_and_stash(workspace: Path) -> tuple[_BackupState | None, str | None]:
    """Validate git state, save durable backup, and stash uncommitted changes."""
    try:
        inside = _git(workspace, "rev-parse", "--is-inside-work-tree")
    except OSError as exc:
        return None, f"Error: git is not available ({exc})."
    if inside.returncode != 0 or inside.text.strip() != "true":
        return None, "Error: workspace is not a git work tree."

    top = _git(workspace, "rev-parse", "--show-toplevel")
    if top.returncode != 0 or not top.text.strip():
        return None, f"Error: could not determine git repository root ({top.err})."
    repo_root = Path(top.text.strip())

    git_dir_res = _git(workspace, "rev-parse", "--git-dir")
    if git_dir_res.returncode != 0 or not git_dir_res.text.strip():
        return None, f"Error: could not determine git directory ({git_dir_res.err})."
    raw_git_dir = Path(git_dir_res.text.strip())
    git_dir = raw_git_dir if raw_git_dir.is_absolute() else (workspace / raw_git_dir)

    head = _git(repo_root, "rev-parse", "--verify", "HEAD")
    if head.returncode != 0:
        return None, "Error: repository has no commits (HEAD does not exist)."

    pre_status_res = _git(repo_root, "status", "--porcelain=v1", "--untracked-files=all", "-z")
    if pre_status_res.returncode != 0:
        return None, f"Error: git status failed ({pre_status_res.err})."
    pre_status = pre_status_res.stdout

    in_progress = _in_progress_operation(git_dir, pre_status)
    if in_progress is not None:
        return None, f"Error: cannot run baseline while a git {in_progress} is in progress."

    if not pre_status:
        return (
            None,
            "Error: workspace has no uncommitted changes; just run the command normally.",
        )

    ident_args = _committer_ident_args(repo_root)
    pre_stash_oids = [oid for oid, _gd, _subj in _stash_entries(repo_root)]
    saved_ref = _new_saved_ref()

    # 1. Durable backup BEFORE touching the working tree.
    created = _git(repo_root, *ident_args, "stash", "create")
    if created.returncode != 0:
        return None, f"Error: git stash create failed before touching the tree: {created.err}"
    create_oid = created.text.strip()
    if create_oid:
        pinned = _git(repo_root, "update-ref", saved_ref, create_oid)
        if pinned.returncode != 0:
            return None, f"Error: could not record backup ref {saved_ref}: {pinned.err}"

    untracked_files = _list_untracked(repo_root)
    temp_dir = Path(tempfile.mkdtemp(prefix="karox-baseline-"))
    try:
        _copy_untracked_to_temp(repo_root, untracked_files, temp_dir)
    except OSError as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        return None, f"Error: could not back up untracked files before stashing: {exc}"

    # 2. Set the changes aside.
    pushed = _git(
        repo_root,
        *ident_args,
        "stash",
        "push",
        "--include-untracked",
        "--message",
        BASELINE_STASH_MESSAGE,
    )
    if pushed.returncode != 0:
        shutil.rmtree(temp_dir, ignore_errors=True)
        detail = pushed.err or pushed.text.strip()
        return None, f"Error: git stash push failed (aborted before running command): {detail}"

    top_stash = _git(repo_root, "rev-parse", "--verify", "stash@{0}")
    if top_stash.returncode != 0 or not top_stash.text.strip():
        shutil.rmtree(temp_dir, ignore_errors=True)
        return None, "Error: git stash push did not create a stash entry (aborted)."
    stash_oid = top_stash.text.strip()

    # Pin the full stash commit (including untracked files) under the backup ref.
    _git(repo_root, "update-ref", saved_ref, stash_oid)

    clean_status = _status_porcelain_z(repo_root)
    return (
        _BackupState(
            repo_root=repo_root,
            saved_ref=saved_ref,
            stash_oid=stash_oid,
            pre_status=pre_status,
            clean_status=clean_status,
            pre_stash_oids=pre_stash_oids,
            temp_dir=temp_dir,
            untracked_files=untracked_files,
            ident_args=ident_args,
        ),
        None,
    )


def _restore_workspace(state: _BackupState) -> _RestoreOutcome:
    """Restore stashed changes using only safe stash operations (never reset/checkout/clean)."""
    repo = state.repo_root
    leftovers_stashed = False
    applied = False
    errors: list[str] = []

    after_status = _status_porcelain_z(repo)
    if after_status == state.clean_status:
        pop_ref = _find_stash_ref(repo, state.stash_oid)
        pop_args = ("stash", "pop", "--index", pop_ref) if pop_ref else ("stash", "pop", "--index")
        popped = _git(repo, *pop_args)
        if popped.returncode == 0:
            applied = True
        else:
            errors.append(f"git stash pop --index failed: {popped.err or popped.text.strip()}")

    if not applied:
        # Exact recovery path: stash whatever the command left as karox-baseline-leftovers,
        # then git stash apply --index <stash-ref>, then drop the karox-baseline stash.
        leftover_ok = True
        if _status_porcelain_z(repo) != state.clean_status:
            stashed_leftovers = _git(
                repo,
                *state.ident_args,
                "stash",
                "push",
                "--include-untracked",
                "--message",
                LEFTOVERS_STASH_MESSAGE,
            )
            if stashed_leftovers.returncode == 0:
                leftovers_stashed = True
            else:
                leftover_ok = False
                errors.append(
                    "git stash push (karox-baseline-leftovers) failed: "
                    f"{stashed_leftovers.err or stashed_leftovers.text.strip()}"
                )

        if leftover_ok:
            stash_ref = _find_stash_ref(repo, state.stash_oid)
            apply_target = stash_ref or state.stash_oid
            reapplied = _git(repo, "stash", "apply", "--index", apply_target)
            if reapplied.returncode == 0:
                if stash_ref is not None:
                    dropped = _git(repo, "stash", "drop", stash_ref)
                    if dropped.returncode != 0:
                        errors.append(
                            f"git stash drop {stash_ref} failed: "
                            f"{dropped.err or dropped.text.strip()}"
                        )
                    else:
                        applied = True
                else:
                    applied = True
            else:
                errors.append(
                    f"git stash apply --index {apply_target} failed: "
                    f"{reapplied.err or reapplied.text.strip()}"
                )

    if applied:
        post_status = _status_porcelain_z(repo)
        post_entries = _stash_entries(repo)
        post_oids = [oid for oid, _gd, _subj in post_entries]
        status_ok = post_status == state.pre_status
        if leftovers_stashed:
            stash_ok = (
                len(post_oids) == len(state.pre_stash_oids) + 1
                and LEFTOVERS_STASH_MESSAGE in post_entries[0][2]
                and post_oids[1:] == state.pre_stash_oids
                and state.stash_oid not in post_oids
            )
        else:
            stash_ok = post_oids == state.pre_stash_oids

        if status_ok and stash_ok:
            shutil.rmtree(state.temp_dir, ignore_errors=True)
            if leftovers_stashed:
                note = (
                    "Restore confirmed: git status matches pre-run state and "
                    f"baseline leftovers were saved in stash ({LEFTOVERS_STASH_MESSAGE})."
                )
            else:
                note = "Restore confirmed: git status and git stash list match pre-run state."
            return _RestoreOutcome(
                ok=True,
                leftovers_stashed=leftovers_stashed,
                note=note,
                saved_ref=state.saved_ref,
            )

        mismatch_parts: list[str] = []
        if not status_ok:
            mismatch_parts.append("git status --porcelain does not match pre-run state")
        if not stash_ok:
            mismatch_parts.append("git stash list does not match pre-run state")
        errors.append("verification mismatch: " + "; ".join(mismatch_parts))

    # Disaster recovery: leave everything in stashes/refs, copy back untracked files from temp dir.
    copy_errors = _restore_untracked_from_temp(repo, state.untracked_files, state.temp_dir)
    if copy_errors:
        errors.append("untracked copy errors: " + "; ".join(copy_errors))
    current_stash_ref = _find_stash_ref(repo, state.stash_oid) or state.stash_oid
    detail = " | ".join(errors) if errors else "unknown restore error"
    note = (
        f"Error: failed to cleanly restore uncommitted changes ({detail}). "
        f"Nothing was deleted; stashes and refs were preserved for manual recovery: "
        f"stash ref={current_stash_ref} (oid {state.stash_oid}), "
        f"saved ref={state.saved_ref}, "
        f"untracked backup dir={state.temp_dir}."
    )
    return _RestoreOutcome(
        ok=False,
        leftovers_stashed=leftovers_stashed,
        note=note,
        stash_ref=current_stash_ref,
        saved_ref=state.saved_ref,
        temp_dir=state.temp_dir,
    )


async def _restore_shielded(state: _BackupState) -> _RestoreOutcome:
    """Run ``_restore_workspace`` to completion even if the calling task is being cancelled."""
    task = asyncio.ensure_future(asyncio.to_thread(_restore_workspace, state))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    outcome = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return outcome


class BaselineTool:
    name = "baseline"
    description = (
        "Run a test or check command against the last commit with your uncommitted changes "
        "set aside, then restore them: shows which failures already existed before your change. "
        "Use instead of git stash."
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

        raw_timeout = args.get("timeout", args.get("timeoutMs", args.get("timeout_ms")))
        timeout_ms = DEFAULT_TIMEOUT_MS
        if raw_timeout is not None:
            try:
                timeout_ms = int(raw_timeout)
            except (TypeError, ValueError):
                timeout_ms = DEFAULT_TIMEOUT_MS
        timeout_ms = min(max(timeout_ms, 1_000), MAX_TIMEOUT_MS)

        danger = check_command(
            command, shell=self.shell.kind, cwd=ctx.workspace, workspace=ctx.workspace
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

        state, err = await asyncio.to_thread(_prepare_and_stash, ctx.workspace)
        if err is not None or state is None:
            return ToolResult(err or "Error: could not prepare baseline run.", is_error=True)

        log_dir = ctx.session_dir / "baseline"
        index = len(list(log_dir.glob("*.log"))) + 1 if log_dir.exists() else 1
        log_path = log_dir / f"base-{index:04d}.log"
        pipestatus_path = log_path.with_suffix(".pipestatus") if self.shell.kind == "bash" else None
        wrapped_command, extra_env = wrap_command_for_pipestatus(
            command, self.shell, pipestatus_path
        )
        run_env = {**ctx.env, **extra_env} if extra_env else ctx.env

        text = ""
        marker = ""
        meta: dict[str, Any] = {"saved_ref": state.saved_ref}
        start_error: str | None = None
        restore: _RestoreOutcome | None = None

        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path.touch()
            try:
                run = await ProcessRun.start(
                    self.shell.argv(wrapped_command, script=log_path.with_suffix(".sh")),
                    cwd=ctx.workspace,
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
            restore = await _restore_shielded(state)

        assert restore is not None
        meta["restored"] = restore.ok
        if restore.leftovers_stashed:
            meta["leftovers_stashed"] = True
        if restore.stash_ref is not None:
            meta["stash_ref"] = restore.stash_ref

        if start_error is not None:
            return ToolResult(f"{start_error}\n{restore.note}", is_error=True, meta=meta)

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
        parts.append(restore.note)
        return ToolResult("\n".join(parts), is_error=not restore.ok, meta=meta)
