"""The baseline tool: run one command against HEAD in a temporary isolated clone.

Never touches the user's working tree, index, refs, stashes, or ``.git/config``.
Creates an isolated temporary checkout via ``git clone --shared --no-checkout -q``
followed by ``git checkout -q --detach <HEAD sha>`` with hooks and fsmonitor disabled,
runs the command there with a hard tree-kill timeout, kills any lingering descendants
before cleanup, removes links safely (aborting deletion if a link cannot be removed),
and removes the temporary directory with ``shutil.rmtree``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
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
    normalize_exit_code,
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
WORKSPACE_PATH_WARNING = (
    "Warning: command contains the absolute workspace path and may run on the real working "
    "tree instead of the temporary HEAD checkout."
)
_GIT_TIMEOUT_S = 120
_TEMP_PREFIX = "karox-baseline-"
_TEMP_DIR_RE = re.compile(r"^karox-baseline-(\d+)-")
_STRIP_GIT_ENV = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_COMMON_DIR",
    }
)
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*=")
_PIP_NAME_RE = re.compile(r"^pip(?:\d+(?:\.\d+)*)?$")
_PYTHON_NAME_RE = re.compile(r"^(?:python(?:\d+(?:\.\d+)*)?|py)$")

_WRAPPER_TOOLS = frozenset(
    {
        "sudo",
        "doas",
        "env",
        "command",
        "builtin",
        "exec",
        "nice",
        "nohup",
        "time",
        "timeout",
        "corepack",
    }
)
_WRAPPER_VALUE_FLAGS = frozenset(
    {
        "-u",
        "--user",
        "-g",
        "--group",
        "-C",
        "-n",
        "--signal",
        "-s",
        "-k",
        "--kill-after",
    }
)
_PKG_INSTALL_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "npm": frozenset({"ci", "install", "i", "add"}),
    "pnpm": frozenset({"ci", "install", "i", "add"}),
    "yarn": frozenset({"ci", "install", "i", "add"}),
    "pip": frozenset({"install"}),
    "uv": frozenset({"pip", "sync", "add"}),
    "poetry": frozenset({"install", "add"}),
}
_PKG_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "npm": frozenset({"-C", "--prefix", "--userconfig", "--globalconfig"}),
    "pnpm": frozenset({"-C", "--dir", "--filter", "-F", "--config"}),
    "yarn": frozenset({"--cwd", "--modules-folder", "--cache-folder"}),
    "pip": frozenset(
        {
            "-c",
            "--constraint",
            "--proxy",
            "--retries",
            "--timeout",
            "--cache-dir",
            "--log",
            "--python",
        }
    ),
    "uv": frozenset({"--directory", "--project", "--config-file", "--cache-dir", "--python", "-p"}),
    "poetry": frozenset({"-C", "--directory", "-P", "--project"}),
}


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


def _strip_git_env(env: Mapping[str, str]) -> dict[str, str]:
    """Remove git repository override variables that would redirect git to the real tree."""
    if sys.platform == "win32":
        return {k: v for k, v in env.items() if k.upper() not in _STRIP_GIT_ENV}
    return {k: v for k, v in env.items() if k not in _STRIP_GIT_ENV}


def _git(cwd: Path, *args: str) -> _GitOut:
    env = _strip_git_env(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_PAGER"] = "cat"
    env["GIT_LFS_SKIP_SMUDGE"] = "1"
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


def _validate_repo(cwd: Path) -> tuple[Path | None, str | None, str | None]:
    """Verify ``cwd`` is inside a git work tree with at least one commit (HEAD)."""
    try:
        inside = _git(cwd, "rev-parse", "--is-inside-work-tree")
    except (OSError, subprocess.SubprocessError) as exc:
        return None, None, f"Error: git is not available ({exc})."
    if inside.returncode != 0 or inside.text.strip() != "true":
        return None, None, "Error: workspace is not a git work tree."

    top = _git(cwd, "rev-parse", "--show-toplevel")
    if top.returncode != 0 or not top.text.strip():
        return None, None, f"Error: could not determine git repository root ({top.err})."
    repo_root = Path(top.text.strip())

    head = _git(repo_root, "rev-parse", "--verify", "HEAD")
    head_sha = head.text.strip()
    if head.returncode != 0 or not head_sha:
        return None, None, "Error: repository has no commits (HEAD does not exist)."

    return repo_root, head_sha, None


def _prepare_checkout(
    repo_root: Path, head_sha: str, temp_parent: Path, wt_dir: Path
) -> str | None:
    """Create an isolated shared clone of ``repo_root`` detached at ``head_sha``."""
    hooks_dir = temp_parent / "hooks"
    try:
        hooks_dir.mkdir(exist_ok=True)
    except OSError as exc:
        return f"Error: could not create temporary hooks dir ({exc})."

    git_opts = (
        "-c",
        f"core.hooksPath={hooks_dir.as_posix()}",
        "-c",
        "core.fsmonitor=false",
    )
    try:
        clone = _git(
            temp_parent,
            *git_opts,
            "clone",
            "--shared",
            "--no-checkout",
            "-q",
            str(repo_root),
            str(wt_dir),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"Error: git clone failed ({exc})."
    if clone.returncode != 0:
        detail = clone.err or clone.text.strip()
        return f"Error: git clone failed: {detail}"

    checkout_opts = list(git_opts)
    # Match the repo's checkout formatting (e.g. core.autocrlf=false on Windows)
    # without sharing .git/config with the temporary clone.
    for key in ("core.autocrlf", "core.eol", "core.symlinks"):
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            cfg = _git(repo_root, "config", "--get", key)
            val = cfg.text.strip()
            if cfg.returncode == 0 and val:
                checkout_opts.extend(["-c", f"{key}={val}"])

    try:
        checkout = _git(
            temp_parent,
            "-C",
            str(wt_dir),
            *checkout_opts,
            "checkout",
            "-q",
            "--detach",
            head_sha,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"Error: git checkout failed ({exc})."
    if checkout.returncode != 0:
        detail = checkout.err or checkout.text.strip()
        return f"Error: git checkout failed: {detail}"
    return None


def _is_junction(path: Path) -> bool:
    isjunc = getattr(os.path, "isjunction", None)
    if isjunc is None:
        return False
    try:
        return bool(isjunc(path))
    except OSError:
        return False


def _is_link(path: Path) -> bool:
    try:
        return path.is_symlink() or _is_junction(path)
    except OSError:
        return False


def _link_node_modules(src_root: Path, dst_root: Path) -> Path | None:
    """Link ``src_root/node_modules`` into ``dst_root/node_modules`` if missing; skip on error."""
    src = src_root / "node_modules"
    dst = dst_root / "node_modules"
    if not src.is_dir() or dst.exists() or _is_link(dst):
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


def _remove_link_only(path: Path) -> bool:
    """Remove a symlink or Windows junction without ever touching its target.

    Returns False if removal fails so cleanup stops instead of running ``shutil.rmtree``
    through a live link.
    """
    is_sym = False
    is_junc = False
    try:
        is_sym = path.is_symlink()
        is_junc = _is_junction(path)
    except OSError:
        return False

    if not is_sym and not is_junc:
        try:
            exists = path.exists()
        except OSError:
            return False
        # Already gone: nothing to remove. If a real file/dir replaced it, refuse to rmdir it here.
        return not exists

    try:
        if is_sym:
            path.unlink()
        else:
            os.rmdir(path)
    except OSError:
        return False

    try:
        return not path.is_symlink() and not _is_junction(path) and not path.exists()
    except OSError:
        return False


def _remove_all_links(root: Path, known_links: list[Path]) -> bool:
    """Remove recorded links and any symlinks/junctions inside ``root`` before rmtree."""
    for link in known_links:
        if not _remove_link_only(link):
            return False

    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            entry_path = Path(entry.path)
            try:
                is_link = entry.is_symlink() or _is_junction(entry_path)
            except OSError:
                return False
            if is_link:
                if not _remove_link_only(entry_path):
                    return False
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry_path)
            except OSError:
                continue
    return True


def _handle_remove_readonly(func: Callable[..., Any], path: str, _exc: BaseException) -> None:
    with contextlib.suppress(OSError):
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        func(path)


def _rmtree_force(path: Path) -> bool:
    if not path.exists():
        return True
    attempts = 5 if sys.platform == "win32" else 1
    for attempt in range(attempts):
        try:
            shutil.rmtree(path, onexc=_handle_remove_readonly)
            return True
        except OSError:
            if not path.exists():
                return True
            if attempt + 1 < attempts:
                time.sleep(0.05)
    return not path.exists()


def _cleanup_temp_dir(temp_parent: Path, created_links: list[Path]) -> bool:
    """Remove all links first (stopping on failure), then delete ``temp_parent``."""
    if not temp_parent.exists() and not _is_link(temp_parent):
        return True
    if not _remove_all_links(temp_parent, created_links):
        return False
    return _rmtree_force(temp_parent)


def _is_pid_alive(pid: int) -> bool:
    """Best-effort check whether process ``pid`` is currently running."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        process_query_limited = 0x1000
        still_active = 259
        error_access_denied = 5
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined,unused-ignore]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel32.OpenProcess(process_query_limited, False, pid)
            if not handle:
                return ctypes.get_last_error() == error_access_denied
            try:
                code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return True
                return int(code.value) == still_active
            finally:
                kernel32.CloseHandle(handle)
        except (OSError, AttributeError, ValueError):
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True


def _cleanup_stale_temp_dirs(base_dir: Path | None = None) -> None:
    """Remove leftover ``karox-baseline-<pid>-*`` directories belonging to dead processes."""
    root = base_dir if base_dir is not None else Path(tempfile.gettempdir())
    own_pid = os.getpid()
    try:
        with os.scandir(root) as it:
            entries = list(it)
    except OSError:
        return
    for entry in entries:
        match = _TEMP_DIR_RE.match(entry.name)
        if not match:
            continue
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        pid = int(match.group(1))
        if pid == own_pid or _is_pid_alive(pid):
            continue
        _cleanup_temp_dir(Path(entry.path), [])


def _cmd_basename(token: str) -> str:
    name = token.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return re.sub(r"\.(?:exe|cmd|bat|ps1)$", "", name)


def _tokenize_shell(text: str) -> list[str]:
    lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|()<>\n")
    lexer.whitespace_split = True
    lexer.whitespace = " \t\r"
    lexer.escape = ""
    lexer.commenters = ""
    try:
        tokens = list(lexer)
        result: list[str] = []
        for token in tokens:
            if re.fullmatch(r"[;&|()\n]+", token):
                result.extend(re.findall(r"&&|\|\||[;&|()\n]", token))
            else:
                result.append(token)
        return result
    except ValueError:
        return re.findall(r"&&|\|\||[;&|()\n]|[^\s;&|()\n]+", text)


def _extract_substitutions(text: str, shell: str) -> list[str]:
    bash = shell == "bash"
    found: list[str] = []
    i, n, double = 0, len(text), False
    while i < n:
        char = text[i]
        if (char == "\\" and bash) or (char == "`" and not bash):
            i += 2
            continue
        if char == "'" and not double:
            end = text.find("'", i + 1)
            i = n if end < 0 else end + 1
            continue
        if char == '"':
            double = not double
        elif char == "`":
            end = text.find("`", i + 1)
            end = n if end < 0 else end
            found.append(text[i + 1 : end])
            i = end + 1
            continue
        elif text.startswith("$(", i):
            depth, j = 1, i + 2
            while j < n and depth:
                depth += {"(": 1, ")": -1}.get(text[j], 0)
                j += 1
            found.append(text[i + 2 : j - 1 if depth == 0 else n])
            i = j
            continue
        i += 1
    return found


def _first_subcommand(args: list[str], value_flags: frozenset[str]) -> str | None:
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            return None
        if arg in value_flags:
            i += 2
            continue
        if arg.startswith("-"):
            i += 1
            continue
        return arg.lower()
    return None


def _python_module_call(args: list[str]) -> tuple[str, list[str]] | None:
    """If ``args`` invokes ``python ... -m <module> ...``, return ``(module, rest)``."""
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "-m" and i + 1 < len(args):
            return args[i + 1].lower(), args[i + 2 :]
        if arg in {"-c", "-"}:
            return None
        if arg in {"-X", "-W", "--check-hash-based-pycs"}:
            i += 2
            continue
        if arg.startswith("-"):
            i += 1
            continue
        return None
    return None


def _scan_pkg_install_segment(args: list[str], shell: str, depth: int) -> str | None:
    if not args or depth > 8:
        return None
    args = args[:]
    while args and _ASSIGN_RE.match(args[0]):
        args.pop(0)
    if not args:
        return None

    name = _cmd_basename(args[0])
    if name in _WRAPPER_TOOLS:
        idx = 1
        while idx < len(args):
            arg = args[idx]
            if _ASSIGN_RE.match(arg):
                idx += 1
            elif arg in _WRAPPER_VALUE_FLAGS:
                idx += 2
            elif arg.startswith("-"):
                idx += 1
            else:
                break
        if name == "timeout" and idx < len(args):
            idx += 1
        return _scan_pkg_install_segment(args[idx:], shell, depth + 1)

    if name in {"bash", "sh", "zsh", "dash", "ksh", "pwsh", "powershell", "cmd"}:
        for i, arg in enumerate(args[1:], 1):
            if arg.lower() in {"-c", "-lc", "-command", "/c"} and i + 1 < len(args):
                inner_shell = "pwsh" if name in {"pwsh", "powershell"} else "bash"
                return _package_install_command(" ".join(args[i + 1 :]), inner_shell, depth + 1)
        return None

    if name == "eval":
        return _package_install_command(" ".join(args[1:]), shell, depth + 1)

    if _PYTHON_NAME_RE.match(name):
        mod_call = _python_module_call(args[1:])
        if mod_call is None:
            return None
        mod, rest = mod_call
        tool = "pip" if _PIP_NAME_RE.match(mod) else mod
        if tool in _PKG_INSTALL_SUBCOMMANDS:
            sub = _first_subcommand(rest, _PKG_VALUE_FLAGS.get(tool, frozenset()))
            if sub in _PKG_INSTALL_SUBCOMMANDS[tool]:
                return f"{tool} {sub}"
        return None

    tool = "pip" if _PIP_NAME_RE.match(name) else name
    if tool in _PKG_INSTALL_SUBCOMMANDS:
        sub = _first_subcommand(args[1:], _PKG_VALUE_FLAGS.get(tool, frozenset()))
        if sub in _PKG_INSTALL_SUBCOMMANDS[tool]:
            return f"{tool} {sub}"
    return None


def _package_install_command(command: str, shell: str = "bash", depth: int = 0) -> str | None:
    """Return a short description if ``command`` looks like a package install, else None."""
    if depth > 8:
        return None
    for inner in _extract_substitutions(command, shell):
        if inner.strip():
            found = _package_install_command(inner, shell, depth + 1)
            if found is not None:
                return found
    segment: list[str] = []
    for token in _tokenize_shell(command):
        if token in {";", "&&", "||", "|", "&", "(", ")", "\n"}:
            if segment:
                found = _scan_pkg_install_segment(segment, shell, depth)
                if found is not None:
                    return found
                segment = []
        else:
            segment.append(token)
    if segment:
        return _scan_pkg_install_segment(segment, shell, depth)
    return None


def _command_references_workspace(command: str, *paths: Path) -> bool:
    """Return True when ``command`` explicitly contains an absolute workspace/repo path."""
    normalized_cmd = command.replace("\\", "/")
    flags = re.IGNORECASE if sys.platform == "win32" else 0
    variants: set[str] = set()
    for path in paths:
        for candidate in (path, path.resolve()):
            posix = candidate.as_posix().rstrip("/")
            if not posix:
                continue
            variants.add(posix)
            if sys.platform == "win32" and len(posix) >= 2 and posix[1] == ":":
                drive = posix[0].lower()
                rest = posix[2:]
                variants.add(f"/{drive}{rest}")
    for variant in variants:
        pattern = re.escape(variant) + r"(?=$|[\s/\\'\"`;|&):,])"
        if re.search(pattern, normalized_cmd, flags):
            return True
    return False


def _merge_pythonpath(env: dict[str, str], wt_dir: Path) -> dict[str, str]:
    """Prepend the temporary checkout root (and its ``src/`` if present) to PYTHONPATH."""
    prepend = [str(wt_dir)]
    src_dir = wt_dir / "src"
    if src_dir.is_dir():
        prepend.append(str(src_dir))

    existing: str | None = None
    keys_to_drop: list[str] = []
    for key, val in env.items():
        if key == "PYTHONPATH" or (sys.platform == "win32" and key.upper() == "PYTHONPATH"):
            existing = val
            keys_to_drop.append(key)
    for key in keys_to_drop:
        env.pop(key, None)

    parts = [*prepend, existing] if existing else prepend
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


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

        pkg_install = _package_install_command(command, shell=self.shell.kind)
        if pkg_install is not None:
            return ToolResult(
                f"Error: baseline is for running checks against HEAD, not package installation "
                f"({pkg_install}). Run package installs in the workspace instead.",
                is_error=True,
            )

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

        repo_root, head_sha, err = await asyncio.to_thread(_validate_repo, base_cwd)
        if err is not None or repo_root is None or head_sha is None:
            return ToolResult(err or "Error: could not validate git repository.", is_error=True)

        try:
            rel_cwd = base_cwd.resolve().relative_to(repo_root.resolve())
        except ValueError:
            return ToolResult(
                f"Error: working directory {base_cwd} could not be mapped inside git repository "
                f"root {repo_root}.",
                is_error=True,
            )

        await asyncio.to_thread(_cleanup_stale_temp_dirs)

        temp_parent = Path(tempfile.mkdtemp(prefix=f"{_TEMP_PREFIX}{os.getpid()}-"))
        wt_dir = temp_parent / "wt"
        created_links: list[Path] = []
        clone_task: asyncio.Future[str | None] | None = None
        run: ProcessRun | None = None
        tree_killed = False

        text = ""
        marker = ""
        meta: dict[str, Any] = {}
        start_error: str | None = None
        clone_error: str | None = None
        subdir_error: str | None = None
        log_path: Path | None = None

        try:
            clone_task = asyncio.ensure_future(
                asyncio.to_thread(_prepare_checkout, repo_root, head_sha, temp_parent, wt_dir)
            )
            clone_error = await asyncio.shield(clone_task)
            if clone_error is None:
                candidate_cwd = wt_dir / rel_cwd
                if not candidate_cwd.is_dir():
                    subdir_error = (
                        f"Error: subdirectory '{rel_cwd.as_posix()}' does not exist in HEAD."
                    )
                else:
                    run_cwd = candidate_cwd

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
                    cmd_env = _strip_git_env(shell_env({**ctx.env, **extra_env}))
                    _merge_pythonpath(cmd_env, wt_dir)

                    try:
                        run = await ProcessRun.start(
                            self.shell.argv(wrapped_command, script=log_path.with_suffix(".sh")),
                            cwd=run_cwd,
                            env=cmd_env,
                            log_path=log_path,
                            command=command,
                            isolate_tree=True,
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
                            # Always kill the command's process tree before cleanup so leftover
                            # children/grandchildren cannot keep files open in the checkout.
                            await run.kill(force_tree=True)
                            tree_killed = True
                        except BaseException:
                            await run.kill_on_cancel(force_tree=True)
                            tree_killed = True
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
                            exit_code = normalize_exit_code(run.returncode, pipestatus)
                            marker, reported_pipe = format_exit_marker(run.returncode, pipestatus)
                            meta["exit_code"] = exit_code
                            if reported_pipe is not None:
                                meta["pipeline_exit_codes"] = reported_pipe
        finally:
            if clone_task is not None:
                if not clone_task.done():
                    with contextlib.suppress(BaseException):
                        await _await_shielded(clone_task)
                elif not clone_task.cancelled():
                    with contextlib.suppress(BaseException):
                        clone_task.exception()
            if run is not None and not tree_killed:
                with contextlib.suppress(BaseException):
                    await run.kill_on_cancel(force_tree=True)
            cleanup_task = asyncio.ensure_future(
                asyncio.to_thread(_cleanup_temp_dir, temp_parent, created_links)
            )
            await _await_shielded(cleanup_task)

        if clone_error is not None:
            return ToolResult(clone_error, is_error=True)
        if subdir_error is not None:
            return ToolResult(subdir_error, is_error=True)
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

        parts: list[str] = []
        if _command_references_workspace(command, ctx.workspace, base_cwd, repo_root):
            parts.append(WORKSPACE_PATH_WARNING)
        parts.append(BASELINE_HEADER)
        if inline:
            parts.append(inline.rstrip("\n"))
        if marker:
            parts.append(marker)
        return ToolResult("\n".join(parts), is_error=False, meta=meta)
