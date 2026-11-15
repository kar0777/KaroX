"""Search tools: glob (find files by path pattern) and grep (search contents).

ripgrep is used when it is on PATH. Without it (common in task containers) files are
listed with ``git ls-files`` inside a repository (so .gitignore is respected) or a
directory walk that skips VCS, virtualenv, cache and node_modules folders, and
contents are searched with Python's ``re``. Result shapes follow DSH (MIT).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

from karox6.context.spill import write_spill
from karox6.tools.base import ToolContext, ToolResult

GLOB_MAX = 100
GREP_MAX = 250
GREP_LINE_MAX = 500
_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".jj",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    ".idea",
}
_MAX_FILE_BYTES = 4 * 1024 * 1024


@lru_cache(maxsize=1)
def ripgrep() -> str | None:
    if os.environ.get("KAROX_NO_RG"):
        return None
    return shutil.which("rg")


# --- glob pattern matching (gitignore-like: no slash = match the basename anywhere) -------


@lru_cache(maxsize=256)
def glob_regex(pattern: str) -> re.Pattern[str]:
    pat = pattern.replace("\\", "/").strip()
    if pat.startswith("./"):
        pat = pat[2:]
    anchored = "/" in pat.rstrip("/")
    if pat.startswith("/"):
        pat = pat[1:]
    out = []
    i = 0
    while i < len(pat):
        ch = pat[i]
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pat.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        elif ch == "{":
            end = pat.find("}", i)
            if end > i:
                alts = pat[i + 1 : end].split(",")
                out.append("(?:" + "|".join(re.escape(a) for a in alts) + ")")
                i = end + 1
                continue
            out.append(re.escape(ch))
        elif ch == "[":
            end = pat.find("]", i + 1)
            if end > i:
                body = pat[i + 1 : end]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body + "]")
                i = end + 1
                continue
            out.append(re.escape(ch))
        else:
            out.append(re.escape(ch))
        i += 1
    body = "".join(out)
    prefix = "" if anchored else "(?:.*/)?"
    return re.compile(f"^{prefix}{body}$", re.IGNORECASE if os.name == "nt" else 0)


def _list_files(root: Path, *, include_ignored: bool) -> list[str]:
    """Paths relative to ``root`` (forward slashes)."""
    if not include_ignored:
        listed = _git_ls_files(root)
        if listed is not None:
            return listed
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        for name in filenames:
            out.append(name if rel_dir == "." else f"{rel_dir}/{name}")
    return out


def _git_ls_files(root: Path) -> list[str] | None:
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    files = [p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p]
    return [p for p in files if (root / p).is_file()]


def _resolve_root(args: dict[str, Any], ctx: ToolContext) -> Path:
    raw = args.get("path")
    return ctx.resolve(str(raw)) if raw else ctx.workspace


def _shown(path: Path, ctx: ToolContext) -> str:
    try:
        rel = path.relative_to(ctx.workspace).as_posix()
        return rel or "."
    except ValueError:
        return path.as_posix()


class GlobTool:
    name = "glob"
    description = (
        "Find files whose paths match a glob pattern (`**/*.py`, `src/**/test_*.py`, "
        "`*.{js,ts}`; a pattern without `/` matches file names at any depth). Returns up to "
        f"{GLOB_MAX} paths, newest first; .gitignore'd files are skipped unless nothing else "
        "matches. Use this tool, not shell find, to discover files."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern."},
            "path": {"type": "string", "description": "Directory to search (default: workspace)."},
        },
        "required": ["pattern"],
    }
    read_only = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        root = _resolve_root(args, ctx)
        if not root.is_dir():
            return ToolResult(f"Error: {_shown(root, ctx)} is not a directory.", is_error=True)
        pattern = str(args["pattern"])
        matches = await asyncio.to_thread(_glob, root, pattern)
        if not matches:
            return ToolResult("No files found.")
        base = (
            root.relative_to(ctx.workspace).as_posix()
            if root.is_relative_to(ctx.workspace)
            else root.as_posix()
        )
        prefix = "" if base in ("", ".") else base.rstrip("/") + "/"
        paths = [prefix + m for m in matches]
        if len(paths) <= GLOB_MAX:
            return ToolResult("\n".join(paths))
        spill = write_spill(ctx.spill_dir, "glob", "\n".join(paths))
        return ToolResult(
            "\n".join(paths[:GLOB_MAX])
            + f"\n\n({len(paths)} files matched; showing the {GLOB_MAX} newest. Full list: "
            f"{spill}. Narrow the pattern or path.)"
        )


def _glob(root: Path, pattern: str) -> list[str]:
    regex = glob_regex(pattern)
    for include_ignored in (False, True):
        files = [f for f in _list_files(root, include_ignored=include_ignored) if regex.match(f)]
        if files:
            break
    stamped = []
    for rel in files:
        try:
            stamped.append(((root / rel).stat().st_mtime, rel))
        except OSError:
            continue
    stamped.sort(key=lambda item: (-item[0], item[1]))
    return [rel for _, rel in stamped]


class GrepTool:
    name = "grep"
    description = (
        "Search file contents with a regular expression (ripgrep/Python syntax; `(?i)` for "
        "case-insensitive). Returns matching lines with line numbers, grouped by file, up to "
        f"{GREP_MAX} matches; `include` limits the search to files matching one glob (e.g. "
        "`*.py`). Skips .gitignore'd and binary files. Use this tool, not shell grep or rg; "
        "use read on a matched file when you need surrounding context."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regular expression."},
            "path": {
                "type": "string",
                "description": "File or directory to search (default: workspace).",
            },
            "include": {"type": "string", "description": "Glob for file names, e.g. `*.py`."},
        },
        "required": ["pattern"],
    }
    read_only = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        root = _resolve_root(args, ctx)
        if not root.exists():
            return ToolResult(f"Error: {_shown(root, ctx)} does not exist.", is_error=True)
        pattern = str(args["pattern"])
        include = str(args.get("include") or "") or None
        rg = ripgrep()
        if rg:
            result = await _grep_rg(rg, root, pattern, include, ctx)
            if result is not None:
                return result
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return ToolResult(f"Error: invalid regular expression: {exc}", is_error=True)
        hits = await asyncio.to_thread(_grep_py, root, regex, include)
        return _format_hits(hits, root if root.is_dir() else root.parent, ctx)


async def _grep_rg(
    rg: str, root: Path, pattern: str, include: str | None, ctx: ToolContext
) -> ToolResult | None:
    argv = [rg, "--no-config", "--json", "--regexp", pattern]
    if include:
        argv += ["--glob", include]
    argv.append(".")
    cwd = root if root.is_dir() else root.parent
    if not root.is_dir():
        argv[-1] = root.name
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
        out, err = await asyncio.wait_for(proc.communicate(), 60)
    except (OSError, TimeoutError):
        return None
    if proc.returncode not in (0, 1):
        message = err.decode("utf-8", "replace").strip()
        if "regex" in message.lower():
            return ToolResult(f"Error: invalid regular expression: {message}", is_error=True)
        return None
    hits: list[tuple[str, int, str]] = []
    for line in out.decode("utf-8", "replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "match":
            continue
        data = event["data"]
        path_text = data["path"].get("text")
        if path_text is None:
            continue
        rel = path_text.replace("\\", "/")
        rel = rel[2:] if rel.startswith("./") else rel
        if not root.is_dir():
            rel = root.name
        text = data["lines"].get("text") or "(line is not valid UTF-8)"
        hits.append((rel, int(data["line_number"]), text.rstrip("\r\n")))
    base = root if root.is_dir() else root.parent
    return _format_hits(hits, base, ctx)


def _grep_py(root: Path, regex: re.Pattern[str], include: str | None) -> list[tuple[str, int, str]]:
    if root.is_file():
        files, base = [root.name], root.parent
    else:
        files, base = _list_files(root, include_ignored=False), root
    inc = glob_regex(include) if include else None
    hits: list[tuple[str, int, str]] = []
    for rel in files:
        if inc is not None and not inc.match(rel):
            continue
        path = base / rel
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8000]:
            continue
        text = data.decode("utf-8", "replace")
        if not regex.search(text):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                hits.append((rel, number, line))
    return hits


def _format_hits(hits: list[tuple[str, int, str]], base: Path, ctx: ToolContext) -> ToolResult:
    if not hits:
        return ToolResult("No matches found.")
    prefix_path = _shown(base, ctx)
    prefix = "" if prefix_path == "." else prefix_path.rstrip("/") + "/"

    def render(items: list[tuple[str, int, str]]) -> str:
        blocks: list[str] = []
        current = None
        for rel, number, line in items:
            if rel != current:
                if current is not None:
                    blocks.append("")
                blocks.append(prefix + rel)
                current = rel
            shown = line if len(line) <= GREP_LINE_MAX else line[:GREP_LINE_MAX] + " (truncated)"
            blocks.append(f"Line {number}: {shown}")
        return "\n".join(blocks)

    if len(hits) <= GREP_MAX:
        return ToolResult(render(hits))
    spill = write_spill(ctx.spill_dir, "grep", render(hits))
    return ToolResult(
        render(hits[:GREP_MAX])
        + f"\n\n({len(hits)} matches; showing the first {GREP_MAX}. Full list: {spill}. "
        "Narrow the pattern, path or include.)"
    )
