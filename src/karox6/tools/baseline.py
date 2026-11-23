"""The baseline tool: run one command against HEAD in a temporary isolated clone.

Never touches the user's working tree, index, refs, stashes, or ``.git/config``.
Creates an isolated temporary checkout via ``git clone --shared --no-checkout -q``,
checks out ``HEAD`` detached with hooks and fsmonitor disabled, removes the ``origin``
remote so pushes cannot reach the user's repository, runs the command with a hard
tree-kill timeout, removes the temporary directory, and reports any cleanup failure.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shlex
import shutil
import socket
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
    read_pipestatus,
    shell_env,
    wrap_command_for_pipestatus,
)

DEFAULT_TIMEOUT_MS = 120_000
MAX_TIMEOUT_MS = 300_000
BASELINE_HEADER = (
    "Baseline: this command ran in a temporary checkout of HEAD (your changes are untouched). "
    "Untracked and ignored files (build outputs, compiled extensions, virtualenvs) are not "
    "present there. Any node_modules is the live one from your working tree, not HEAD's."
)
_WORKSPACE_PATH_ERROR = (
    "Error: baseline refuses commands containing the absolute workspace path "
    "(they would access the real working tree instead of the temporary HEAD checkout). "
    "Use relative paths instead."
)
_GIT_TIMEOUT_S = 120
_TEMP_PREFIX = "karox-baseline-"
_STALE_SUFFIX_RE = re.compile(r"^(\d+)-[^-]+$")
_STALE_MIN_AGE_S = 3600.0
_STRIP_GIT_ENV = frozenset(
    {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR"}
)
_STRIP_CMD_ENV = _STRIP_GIT_ENV | frozenset({"PWD", "OLDPWD"})
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
    {"-u", "--user", "-g", "--group", "-C", "-n", "--signal", "-s", "-k", "--kill-after"}
)
_JS_MUTATING = frozenset(
    {
        "install",
        "i",
        "ci",
        "add",
        "remove",
        "rm",
        "uninstall",
        "prune",
        "update",
        "up",
        "dedupe",
        "rebuild",
        "link",
        "unlink",
    }
)
_ALWAYS_REFUSED_TOOLS = frozenset({"npx", "bunx", "pip-sync"})
_PKG_INSTALL_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "npm": _JS_MUTATING | frozenset({"exec"}),
    "pnpm": _JS_MUTATING | frozenset({"dlx"}),
    "yarn": _JS_MUTATING | frozenset({"dlx"}),
    "bun": _JS_MUTATING,
    "prisma": frozenset({"generate"}),
    "pip": frozenset({"install", "uninstall"}),
    "pipenv": frozenset({"install", "sync", "update", "uninstall"}),
    "uv": frozenset({"pip", "sync", "add", "remove", "lock", "venv"}),
    "poetry": frozenset({"install", "add", "remove", "update", "sync", "lock"}),
    "pdm": frozenset({"install", "add", "remove", "update", "sync", "lock"}),
    "conda": frozenset({"install", "remove", "uninstall", "update", "upgrade", "create", "env"}),
    "pipx": frozenset(
        {
            "install",
            "uninstall",
            "upgrade",
            "upgrade-all",
            "reinstall",
            "reinstall-all",
            "inject",
            "uninject",
        }
    ),
}
_PKG_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "npm": frozenset(
        {
            "-C",
            "--prefix",
            "--userconfig",
            "--globalconfig",
            "-w",
            "--workspace",
            "--registry",
            "--cache",
        }
    ),
    "pnpm": frozenset({"-C", "--dir", "--filter", "-F", "--config", "--workspace-concurrency"}),
    "yarn": frozenset({"--cwd", "--modules-folder", "--cache-folder"}),
    "bun": frozenset({"--cwd", "-c", "--config", "--env-file"}),
    "prisma": frozenset({"--schema"}),
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
    "pipenv": frozenset({"--python"}),
    "uv": frozenset({"--directory", "--project", "--config-file", "--cache-dir", "--python", "-p"}),
    "poetry": frozenset({"-C", "--directory", "-P", "--project"}),
    "pdm": frozenset({"-p", "--project", "-c", "--config"}),
    "conda": frozenset({"-n", "--name", "-p", "--prefix"}),
    "pipx": frozenset({"--python"}),
}
_SETUP_PY_MUTATING = frozenset({"install", "develop", "easy_install"})


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


def _strip_env(env: Mapping[str, str], keys: frozenset[str]) -> dict[str, str]:
    if sys.platform == "win32":
        return {k: v for k, v in env.items() if k.upper() not in keys}
    return {k: v for k, v in env.items() if k not in keys}


def _strip_git_env(env: Mapping[str, str]) -> dict[str, str]:
    """Remove git repository override variables that would redirect git to the real tree."""
    return _strip_env(env, _STRIP_GIT_ENV)


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
    for key in ("core.autocrlf", "core.eol", "core.symlinks", "core.longpaths"):
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

    try:
        rm_origin = _git(temp_parent, "-C", str(wt_dir), *git_opts, "remote", "remove", "origin")
    except (OSError, subprocess.SubprocessError) as exc:
        return f"Error: could not disconnect temporary clone remote ({exc})."
    if rm_origin.returncode != 0:
        detail = rm_origin.err or rm_origin.text.strip()
        return f"Error: could not disconnect temporary clone remote: {detail}"
    return None


def _safe_hostname() -> str:
    raw = socket.gethostname() or "host"
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", raw).strip("._-")
    return safe or "host"


def _is_link(path: Path) -> bool:
    isjunc = getattr(os.path, "isjunction", None)
    try:
        return path.is_symlink() or bool(isjunc is not None and isjunc(path))
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _make_writable(path: Path, *, is_dir: bool = False) -> None:
    if _is_link(path):
        return
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        return
    bits = stat.S_IWUSR | (stat.S_IXUSR if (is_dir or stat.S_ISDIR(st.st_mode)) else 0)
    os.chmod(path, stat.S_IMODE(st.st_mode) | bits)


def _handle_remove_readonly(
    func: Callable[..., Any],
    path: str,
    _exc: BaseException,
    *,
    root: Path | None = None,
) -> None:
    p = Path(path)
    with contextlib.suppress(OSError):
        if sys.platform != "win32":
            parent = p.parent
            if parent != p and (root is None or parent == root or root in parent.parents):
                with contextlib.suppress(OSError):
                    _make_writable(parent, is_dir=True)
        if _is_link(p):
            if func in (os.unlink, os.rmdir):
                func(path)
            return
        _make_writable(p)
        if func not in (os.open, os.scandir, os.listdir, os.lstat):
            func(path)


def _rmtree_force(path: Path) -> bool:
    if not path.exists():
        return True
    attempts = 5 if sys.platform == "win32" else 2

    def onexc(func: Callable[..., Any], target: str, exc: BaseException) -> None:
        _handle_remove_readonly(func, target, exc, root=path)

    for attempt in range(attempts):
        try:
            shutil.rmtree(path, onexc=onexc)
            return True
        except OSError:
            if not path.exists():
                return True
            if attempt + 1 < attempts:
                time.sleep(0.05)
    return not path.exists()


def _cleanup_temp_dir(temp_parent: Path) -> bool:
    """Remove ``temp_parent`` unless it is itself a symlink or Windows junction."""
    if _is_link(temp_parent):
        return False
    if not temp_parent.exists():
        return True
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


def _cleanup_stale_temp_dirs(
    base_dir: Path | None = None, *, min_age_s: float = _STALE_MIN_AGE_S
) -> None:
    """Remove leftover ``karox-baseline-<host>-<pid>-<rand>`` dirs from dead processes."""
    root = base_dir if base_dir is not None else Path(tempfile.gettempdir())
    prefix = f"{_TEMP_PREFIX}{_safe_hostname()}-"
    own_pid = os.getpid()
    now = time.time()
    getuid = getattr(os, "getuid", None)
    own_uid = getuid() if getuid is not None else None
    try:
        with os.scandir(root) as it:
            entries = list(it)
    except OSError:
        return
    for entry in entries:
        if not entry.name.startswith(prefix):
            continue
        match = _STALE_SUFFIX_RE.fullmatch(entry.name[len(prefix) :])
        if not match:
            continue
        entry_path = Path(entry.path)
        try:
            if _is_link(entry_path) or not entry.is_dir(follow_symlinks=False):
                continue
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if own_uid is not None and st.st_uid != own_uid:
            continue
        if now - st.st_mtime <= min_age_s:
            continue
        pid = int(match.group(1))
        if pid == own_pid or _is_pid_alive(pid):
            continue
        _cleanup_temp_dir(entry_path)


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


def _python_target(args: list[str]) -> tuple[str, str, list[str]] | None:
    """Return ``('module', mod, rest)`` or ``('script', basename, rest)`` for a python call."""
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "-m" and i + 1 < len(args):
            return "module", args[i + 1].lower(), args[i + 2 :]
        if arg in {"-c", "-"}:
            return None
        if arg in {"-X", "-W", "--check-hash-based-pycs"}:
            i += 2
            continue
        if arg.startswith("-"):
            i += 1
            continue
        return "script", _cmd_basename(arg), args[i + 1 :]
    return None


def _check_pkg_tool(tool: str, rest: list[str]) -> str | None:
    if tool in _ALWAYS_REFUSED_TOOLS:
        return tool
    if tool not in _PKG_INSTALL_SUBCOMMANDS:
        return None
    sub = _first_subcommand(rest, _PKG_VALUE_FLAGS.get(tool, frozenset()))
    if sub in _PKG_INSTALL_SUBCOMMANDS[tool]:
        return f"{tool} {sub}"
    if (
        tool == "yarn"
        and sub is None
        and not any(a in {"-v", "--version", "-h", "--help"} for a in rest)
    ):
        return "yarn"
    return None


def _check_setup_py(rest: list[str]) -> str | None:
    for arg in rest:
        if arg == "--":
            break
        if not arg.startswith("-") and arg.lower() in _SETUP_PY_MUTATING:
            return f"setup.py {arg.lower()}"
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

    if name == "setup.py":
        return _check_setup_py(args[1:])

    if _PYTHON_NAME_RE.match(name):
        target = _python_target(args[1:])
        if target is None:
            return None
        kind, value, rest = target
        if kind == "module":
            return _check_pkg_tool("pip" if _PIP_NAME_RE.match(value) else value, rest)
        return _check_setup_py(rest) if value == "setup.py" else None

    tool = "pip" if _PIP_NAME_RE.match(name) else name
    return _check_pkg_tool(tool, args[1:])


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


def _home_prefixes() -> list[str]:
    homes: list[Path] = []
    with contextlib.suppress(RuntimeError, OSError):
        homes.append(Path.home())
    for env_key in ("HOME", "USERPROFILE"):
        raw = os.environ.get(env_key)
        if raw:
            homes.append(Path(raw))
    prefixes: list[str] = []
    seen: set[str] = set()
    for home in homes:
        candidates: list[Path] = [home]
        with contextlib.suppress(OSError):
            candidates.append(home.resolve())
        for cand in candidates:
            posix = re.sub(r"[\\/]+", "/", cand.as_posix()).rstrip("/")
            if not posix:
                continue
            forms = [posix]
            if sys.platform == "win32" and len(posix) >= 2 and posix[1] == ":":
                forms.append(f"/{posix[0].lower()}{posix[2:]}")
            for form in forms:
                key = form.lower() if sys.platform == "win32" else form
                if key not in seen:
                    seen.add(key)
                    prefixes.append(form)
    return prefixes


def _command_references_workspace(command: str, *paths: Path) -> bool:
    """Return True when ``command`` explicitly contains an absolute workspace/repo path."""
    normalized_cmd = re.sub(r"[\\/]+", "/", command)
    flags = re.IGNORECASE if sys.platform == "win32" else 0
    home_prefixes = _home_prefixes()
    variants: set[str] = set()
    for path in paths:
        for candidate in (path, path.resolve()):
            posix = re.sub(r"[\\/]+", "/", candidate.as_posix()).rstrip("/")
            if not posix:
                continue
            cand_forms = [posix]
            if sys.platform == "win32" and len(posix) >= 2 and posix[1] == ":":
                drive = posix[0].lower()
                rest = posix[2:]
                cand_forms.append(f"/{drive}{rest}")
            for form in cand_forms:
                variants.add(form)
                cmp_form = form.lower() if sys.platform == "win32" else form
                for home_posix in home_prefixes:
                    cmp_home = home_posix.lower() if sys.platform == "win32" else home_posix
                    if cmp_form.startswith(cmp_home + "/"):
                        rel = form[len(home_posix) + 1 :]
                        variants.add(f"~/{rel}")
                        variants.add(f"$HOME/{rel}")
                        variants.add(f"${{HOME}}/{rel}")
                        if sys.platform == "win32":
                            variants.add(f"$env:HOME/{rel}")
                            variants.add(f"$env:USERPROFILE/{rel}")
                            variants.add(f"%USERPROFILE%/{rel}")
    for variant in variants:
        pattern = re.escape(variant) + r"(?=$|[\s/\\'\"`;|&):,])"
        if re.search(pattern, normalized_cmd, flags):
            return True
    return False


def _prepend_env_path(env: dict[str, str], name: str, entries: list[str]) -> None:
    """Prepend ``entries`` to a PATH-like environment variable (case-insensitive on Windows)."""
    if not entries:
        return
    existing: str | None = None
    for key in [
        k for k in env if k == name or (sys.platform == "win32" and k.upper() == name.upper())
    ]:
        existing = env.pop(key)
    parts = [*entries, existing] if existing else list(entries)
    env[name] = os.pathsep.join(parts)


def _merge_pythonpath(env: dict[str, str], wt_dir: Path) -> dict[str, str]:
    """Prepend the temporary checkout root (and its ``src/`` if present) to PYTHONPATH."""
    prepend = [str(wt_dir)]
    src_dir = wt_dir / "src"
    if src_dir.is_dir():
        prepend.append(str(src_dir))
    _prepend_env_path(env, "PYTHONPATH", prepend)
    return env


def _merge_node_env(env: dict[str, str], *search_roots: Path) -> dict[str, str]:
    """Expose ``node_modules`` via ``PATH`` and ``NODE_PATH``.

    Note: this ``node_modules`` is the live one from your working tree, not HEAD's.
    """
    bin_dirs: list[str] = []
    node_dirs: list[str] = []
    seen: set[Path] = set()
    for root in search_roots:
        nm = root / "node_modules"
        if not nm.is_dir():
            continue
        resolved = nm.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        node_dirs.append(str(nm))
        bin_dir = nm / ".bin"
        if bin_dir.is_dir():
            bin_dirs.append(str(bin_dir))
    _prepend_env_path(env, "PATH", bin_dirs)
    _prepend_env_path(env, "NODE_PATH", node_dirs)
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
        "change. Any node_modules is the live one from your working tree, not HEAD's. "
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

        if _command_references_workspace(command, ctx.workspace, base_cwd):
            return ToolResult(_WORKSPACE_PATH_ERROR, is_error=True)

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

        if _command_references_workspace(command, repo_root):
            return ToolResult(_WORKSPACE_PATH_ERROR, is_error=True)

        try:
            rel_cwd = base_cwd.resolve().relative_to(repo_root.resolve())
        except ValueError:
            return ToolResult(
                f"Error: working directory {base_cwd} could not be mapped inside git repository "
                f"root {repo_root}.",
                is_error=True,
            )

        await asyncio.to_thread(_cleanup_stale_temp_dirs)

        temp_parent = Path(
            tempfile.mkdtemp(prefix=f"{_TEMP_PREFIX}{_safe_hostname()}-{os.getpid()}-")
        )
        wt_dir = temp_parent / "wt"
        clone_task: asyncio.Future[str | None] | None = None
        run: ProcessRun | None = None
        tree_killed = False
        cleanup_ok = True

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

                    log_dir = ctx.session_dir / "baseline"
                    log_dir.mkdir(parents=True, exist_ok=True)
                    fd, raw_log = tempfile.mkstemp(dir=log_dir, prefix="base-", suffix=".log")
                    os.close(fd)
                    log_path = Path(raw_log)

                    pipestatus_path = (
                        log_path.with_suffix(".pipestatus") if self.shell.kind == "bash" else None
                    )
                    wrapped_command, extra_env = wrap_command_for_pipestatus(
                        command, self.shell, pipestatus_path
                    )
                    cmd_env = _strip_env(shell_env({**ctx.env, **extra_env}), _STRIP_CMD_ENV)
                    _merge_pythonpath(cmd_env, wt_dir)
                    _merge_node_env(cmd_env, base_cwd, repo_root)

                    try:
                        run = await ProcessRun.start(
                            self.shell.argv(wrapped_command, script=log_path.with_suffix(".sh")),
                            cwd=run_cwd,
                            env=cmd_env,
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
                            # Kill the process tree only while the command is still active,
                            # never after a normal exit.
                            if not finished or run.active:
                                await run.kill()
                                tree_killed = True
                        except BaseException:
                            if run.active:
                                await run.kill_on_cancel()
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
                            marker, reported_pipe = format_exit_marker(run.returncode, pipestatus)
                            meta["exit_code"] = run.returncode
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
            if run is not None and not tree_killed and run.active:
                with contextlib.suppress(BaseException):
                    await run.kill_on_cancel()
            cleanup_task = asyncio.ensure_future(asyncio.to_thread(_cleanup_temp_dir, temp_parent))
            cleanup_ok = bool(await _await_shielded(cleanup_task))

        cleanup_warning: str | None = None
        if not cleanup_ok:
            cleanup_warning = (
                f"Warning: failed to remove temporary checkout directory: {temp_parent}"
            )
            meta["cleanup_failed"] = True
            meta["cleanup_path"] = str(temp_parent)

        for err_msg in (clone_error, subdir_error, start_error):
            if err_msg is not None:
                msg = f"{err_msg}\n{cleanup_warning}" if cleanup_warning else err_msg
                return ToolResult(msg, is_error=True, meta=meta)

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

        parts: list[str] = [BASELINE_HEADER]
        if inline:
            parts.append(inline.rstrip("\n"))
        if marker:
            parts.append(marker)
        if cleanup_warning:
            parts.append(cleanup_warning)
        return ToolResult("\n".join(parts), is_error=False, meta=meta)
