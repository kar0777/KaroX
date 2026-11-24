"""Find untouched production Python files that reference symbols changed in a diff.

Used once at the first finish attempt in a git repository to surface likely sibling
implementations, callers, or subclasses that the agent may have missed.
"""

from __future__ import annotations

import ast
import bisect
import builtins
import keyword
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from karox6.core.loopguard import _run_git, is_test_file
from karox6.tools.files import is_generated

RELATED_TIMEOUT_S = 2.0
MAX_RESULTS = 5
MAX_SYMBOLS_PER_LINE = 4
MAX_FILE_BYTES = 1_048_576  # 1 MiB
MAX_DIFF_CHARS = 2 * 1024 * 1024  # 2 MiB
MAX_SEARCH_SYMBOLS = 200

RELATED_FILES_NOTICE = (
    "Before you finish: these files reference names you changed but you did not edit them:\n"
    "{lines}\n"
    "Check whether any of them needs the same change for the task "
    "(another implementation, a caller, a sibling class). "
    "If none does, finish with your summary."
)

EXCLUDED_DIRS = frozenset(
    {
        "test",
        "tests",
        "testing",
        "__tests__",
        "doc",
        "docs",
        "examples",
        "example",
        "benchmarks",
        "benchmark",
        "tutorials",
        ".git",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        ".venv",
        "venv",
        "site-packages",
        ".tox",
        ".nox",
        ".eggs",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)

# Short list (<= 60 words) of ubiquitous variable, container, IO, logging, and stdlib
# attribute names that carry no useful cross-file signal.
_COMMON_STOPWORDS = frozenset(
    {
        "self",
        "cls",
        "args",
        "kwargs",
        "other",
        "get",
        "pop",
        "add",
        "remove",
        "clear",
        "copy",
        "update",
        "items",
        "keys",
        "values",
        "append",
        "extend",
        "insert",
        "index",
        "count",
        "sort",
        "join",
        "split",
        "splitlines",
        "strip",
        "replace",
        "find",
        "startswith",
        "endswith",
        "lower",
        "upper",
        "encode",
        "decode",
        "read",
        "write",
        "close",
        "exists",
        "resolve",
        "parent",
        "name",
        "debug",
        "info",
        "warning",
        "error",
        "logger",
        "logging",
        "data",
        "value",
        "result",
        "item",
        "node",
        "path",
        "file",
        "text",
        "monotonic",
        "environ",
        "isoformat",
        "dumps",
        "loads",
        "dirname",
    }
)

STOPWORDS = (
    frozenset(keyword.kwlist)
    | frozenset(keyword.softkwlist)
    | frozenset(dir(builtins))
    | frozenset(dir(object))
    | _COMMON_STOPWORDS
)

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
_DEF_CLASS_RE = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)")
_TOPLEVEL_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=]+)?=(?!=)\s*")
_ATTR_RE = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)")
_STRING_RE = re.compile(
    r'("""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'|"[^"\\]*(?:\\.[^"\\]*)*"|\'[^\'\\]*(?:\\.[^\'\\]*)*\')'
)


@dataclass(slots=True)
class _DiffFileInfo:
    path: str
    hunk_header_syms: set[str] = field(default_factory=set)
    def_class_syms: set[str] = field(default_factory=set)
    toplevel_assign_syms: set[str] = field(default_factory=set)
    attr_syms: set[str] = field(default_factory=set)
    changed_new_lines: set[int] = field(default_factory=set)
    changed_old_lines: set[int] = field(default_factory=set)
    deletion_new_anchors: set[int] = field(default_factory=set)
    ast_enclosing_syms: set[str] = field(default_factory=set)
    ast_ok: bool = False


def _git(ws_root: Path, *args: str, timeout: float) -> subprocess.CompletedProcess[bytes] | None:
    return _run_git(ws_root, "-c", "core.quotepath=off", *args, timeout=timeout)


def _norm_path(path: str) -> str:
    p = path.strip()
    if len(p) >= 2 and p[0] == '"' and p[-1] == '"':
        p = p[1:-1]
    p = p.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.rstrip("/")


def _norm_ignore(ignore: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(d for d in (_norm_path(item) for item in ignore) if d)


def _in_ignore(rel: str, ignore: tuple[str, ...]) -> bool:
    return any(rel == d or rel.startswith(d + "/") for d in ignore)


def _inside_workspace(ws_root: Path, rel: str) -> bool:
    try:
        return (ws_root / rel).resolve().is_relative_to(ws_root)
    except (OSError, ValueError):
        return False


def is_production_python(
    rel_path: str,
    workspace: Path | None = None,
    *,
    ignore: tuple[str, ...] = (),
    _ws_root: Path | None = None,
) -> bool:
    """True for workspace-internal production `.py` files (not tests, docs, venvs, etc.)."""
    raw = rel_path.strip()
    if not raw:
        return False
    if Path(raw).is_absolute():
        return False
    p = _norm_path(raw)
    if not p or p.startswith("/") or not p.endswith(".py"):
        return False
    parts = p.split("/")
    if any(seg in ("", ".", "..") for seg in parts):
        return False
    if is_test_file(p) or is_generated(p):
        return False
    if any(seg.lower() in EXCLUDED_DIRS for seg in parts[:-1]):
        return False
    norm_ign = _norm_ignore(ignore) if ignore else ()
    if norm_ign and _in_ignore(p, norm_ign):
        return False
    ws_root = _ws_root if _ws_root is not None else (workspace.resolve() if workspace else None)
    return not (ws_root is not None and not _inside_workspace(ws_root, p))


def is_valid_symbol(sym: str, *, min_len: int = 3) -> bool:
    return min_len <= len(sym) <= 100 and sym not in STOPWORDS


def _strip_comments_and_strings(code_line: str) -> str:
    cleaned = _STRING_RE.sub(" ", code_line)
    idx = cleaned.find("#")
    if idx != -1:
        cleaned = cleaned[:idx]
    return cleaned


def _strip_diff_target(raw_target: str, prefix: str) -> str | None:
    target = raw_target.split("\t", 1)[0].strip()
    if not target or target == "/dev/null":
        return None
    if len(target) >= 2 and target[0] == '"' and target[-1] == '"':
        target = target[1:-1]
    if target.startswith(prefix):
        target = target[len(prefix) :]
    norm = _norm_path(target)
    return norm or None


def _parse_diff_git_symmetric(raw: str) -> str | None:
    """Extract path from `diff --git a/<p> b/<p>` when both sides match (handles spaces)."""
    rest = raw[len("diff --git ") :]
    if not rest.startswith("a/"):
        return None
    mid = len(rest) // 2
    if len(rest) % 2 == 1 and rest[mid : mid + 3] == " b/" and rest[2:mid] == rest[mid + 3 :]:
        norm = _norm_path(rest[2:mid])
        return norm or None
    return None


def _parse_diff(
    diff_text: str,
    allowed_prod: set[str],
    *,
    deadline: float | None = None,
    max_chars: int = MAX_DIFF_CHARS,
) -> tuple[set[str], dict[str, _DiffFileInfo]]:
    """Parse unified diff -> (all_diff_files, prod_py_file_infos)."""
    if len(diff_text) > max_chars:
        diff_text = diff_text[:max_chars]

    all_diff_files: set[str] = set()
    prod_files: dict[str, _DiffFileInfo] = {}
    cur_file: _DiffFileInfo | None = None
    in_header = True
    old_line = 0
    new_line = 0
    in_docstring_delim: str | None = None

    def select_file(path: str) -> _DiffFileInfo | None:
        if path in allowed_prod:
            return prod_files.setdefault(path, _DiffFileInfo(path=path))
        return None

    for idx, raw in enumerate(diff_text.splitlines()):
        if (idx & 255) == 0 and deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("related_files timed out parsing diff")

        if raw.startswith("diff --git "):
            cur_file = None
            in_header = True
            in_docstring_delim = None
            sym_path = _parse_diff_git_symmetric(raw)
            if sym_path is not None:
                all_diff_files.add(sym_path)
            continue

        if in_header and raw.startswith("rename from "):
            p = _norm_path(raw[len("rename from ") :])
            if p:
                all_diff_files.add(p)
            continue

        if in_header and raw.startswith("rename to "):
            p = _norm_path(raw[len("rename to ") :])
            if p:
                all_diff_files.add(p)
            continue

        if in_header and raw.startswith("--- "):
            in_docstring_delim = None
            a_path = _strip_diff_target(raw[4:], "a/")
            if a_path is not None:
                all_diff_files.add(a_path)
                cur_file = select_file(a_path)
            else:
                cur_file = None
            continue

        if in_header and raw.startswith("+++ "):
            in_docstring_delim = None
            b_path = _strip_diff_target(raw[4:], "b/")
            if b_path is not None:
                all_diff_files.add(b_path)
                cur_file = select_file(b_path)
            continue

        if cur_file is None:
            continue

        if in_header and raw.startswith(
            ("index ", "new file", "deleted file", "old mode", "new mode")
        ):
            continue

        hm = _HUNK_RE.match(raw)
        if hm:
            in_header = False
            old_line = int(hm.group(1))
            new_line = int(hm.group(3))
            header_ctx = hm.group(5).strip()
            in_docstring_delim = None
            dm = _DEF_CLASS_RE.match(header_ctx)
            if dm:
                cur_file.hunk_header_syms.add(dm.group(1))
            continue

        if not raw:
            old_line += 1
            new_line += 1
            continue

        prefix = raw[0]
        content = raw[1:]

        if prefix == " ":
            old_line += 1
            new_line += 1
            continue
        if prefix == "+":
            cur_file.changed_new_lines.add(new_line)
            new_line += 1
        elif prefix == "-":
            cur_file.changed_old_lines.add(old_line)
            if new_line > 0:
                cur_file.deletion_new_anchors.add(new_line)
            old_line += 1
        else:
            continue

        stripped = content.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # Skip obvious triple-quoted docstring lines within a hunk.
        if in_docstring_delim is not None:
            if in_docstring_delim in stripped:
                in_docstring_delim = None
            continue
        for delim in ('"""', "'''"):
            if stripped.startswith(delim):
                if stripped.count(delim) == 1:
                    in_docstring_delim = delim
                stripped = ""
                break
        if not stripped:
            continue

        is_import = stripped.startswith(("import ", "from "))

        # (1) Added or removed def / class name.
        dm = _DEF_CLASS_RE.match(content)
        if dm:
            cur_file.def_class_syms.add(dm.group(1))

        # (2) Module-level assignment target (no leading whitespace).
        if not is_import and content and not content[0].isspace():
            am = _TOPLEVEL_ASSIGN_RE.match(content)
            if am:
                cur_file.toplevel_assign_syms.add(am.group(1))

        # (3) Attribute / method names on changed code lines (outside strings/comments/imports).
        if not is_import:
            code_only = _strip_comments_and_strings(content)
            for attr in _ATTR_RE.findall(code_only):
                cur_file.attr_syms.add(attr)

    return all_diff_files, prod_files


def _add_ast_enclosing_symbols(
    ws_root: Path,
    prod_files: dict[str, _DiffFileInfo],
    *,
    deadline: float | None = None,
) -> None:
    """Use AST of the new file on disk to find enclosing def/class for changed lines."""
    for rel_path, info in prod_files.items():
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("related_files timed out during AST scan")
        if not _inside_workspace(ws_root, rel_path):
            continue
        fpath = ws_root / rel_path
        try:
            st = fpath.stat()
            if not fpath.is_file() or st.st_size > MAX_FILE_BYTES:
                continue
            src = fpath.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(src)
        except (OSError, SyntaxError, ValueError):
            continue

        info.ast_ok = True
        target_lines = info.changed_new_lines | info.deletion_new_anchors
        if not target_lines:
            continue

        sorted_lines = sorted(target_lines)
        for idx, node in enumerate(ast.walk(tree)):
            if (idx & 255) == 0 and deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("related_files timed out walking AST")
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start = getattr(node, "lineno", None)
                end = getattr(node, "end_lineno", None)
                if start is None or end is None:
                    continue
                pos = bisect.bisect_left(sorted_lines, start)
                if pos < len(sorted_lines) and sorted_lines[pos] <= end:
                    info.ast_enclosing_syms.add(node.name)


def _collect_symbols(
    ws_root: Path,
    prod_files: dict[str, _DiffFileInfo],
    *,
    deadline: float | None = None,
) -> list[str]:
    _add_ast_enclosing_symbols(ws_root, prod_files, deadline=deadline)

    def_syms: set[str] = set()
    attr_syms: set[str] = set()
    for info in prod_files.values():
        if info.ast_ok:
            enclosing = info.ast_enclosing_syms
            if not enclosing and info.changed_old_lines and not info.changed_new_lines:
                enclosing = info.hunk_header_syms
        else:
            enclosing = info.hunk_header_syms

        for s in enclosing | info.def_class_syms | info.toplevel_assign_syms:
            if is_valid_symbol(s):
                def_syms.add(s)
        for s in info.attr_syms:
            if is_valid_symbol(s):
                attr_syms.add(s)

    ordered = sorted(def_syms) + sorted(attr_syms - def_syms)
    return ordered[:MAX_SEARCH_SYMBOLS]


def extract_symbols(
    workspace: Path,
    changed: list[str],
    diff_text: str,
    *,
    ignore: tuple[str, ...] = (),
    deadline: float | None = None,
) -> list[str]:
    """Extract deterministic, filtered symbol names from changed production Python diffs."""
    ws_root = workspace.resolve()
    changed_prod = {
        p
        for p in (_norm_path(c) for c in changed)
        if is_production_python(p, ws_root, ignore=ignore, _ws_root=ws_root)
    }
    if not changed_prod or not diff_text.strip():
        return []

    try:
        _, prod_files = _parse_diff(diff_text, changed_prod, deadline=deadline)
        if not prod_files:
            return []
        return _collect_symbols(ws_root, prod_files, deadline=deadline)
    except TimeoutError:
        return []


def _git_grep_symbols(
    ws_root: Path,
    symbols: list[str],
    excluded: set[str],
    ignore: tuple[str, ...],
    deadline: float,
) -> dict[str, dict[str, int]]:
    """Search tracked `.py` files with `git -c core.quotepath=off grep -I -o -w -F`."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("related_files timed out before git grep")
    e_args: list[str] = []
    for sym in symbols:
        e_args.extend(("-e", sym))
    try:
        proc = _git(
            ws_root,
            "grep",
            "--no-color",
            "-I",
            "-o",
            "-w",
            "-F",
            *e_args,
            "--",
            "*.py",
            timeout=remaining,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("git grep timed out") from exc
    if proc is None or proc.returncode not in (0, 1) or proc.returncode == 1:
        return {}
    if time.monotonic() >= deadline:
        raise TimeoutError("related_files timed out after git grep")

    sym_set = set(symbols)
    norm_ign = _norm_ignore(ignore) if ignore else ()
    prod_cache: dict[str, bool] = {}
    file_hits: dict[str, dict[str, int]] = {}

    for idx, raw_line in enumerate(proc.stdout.decode("utf-8", "replace").splitlines()):
        if (idx & 255) == 0 and time.monotonic() >= deadline:
            raise TimeoutError("related_files timed out parsing git grep")
        sep = raw_line.rfind(":")
        if sep <= 0:
            continue
        rel = _norm_path(raw_line[:sep])
        if not rel or rel in excluded:
            continue
        ok = prod_cache.get(rel)
        if ok is None:
            ok = is_production_python(rel, ws_root, ignore=norm_ign, _ws_root=ws_root)
            prod_cache[rel] = ok
        if not ok:
            continue
        tok = raw_line[sep + 1 :]
        if tok not in sym_set:
            continue
        counts = file_hits.get(rel)
        if counts is None:
            counts = file_hits[rel] = {}
        counts[tok] = counts.get(tok, 0) + 1
    return file_hits


def _shared_dir_depth(path: str, edited_dirs: tuple[str, ...]) -> int:
    p_dir = os.path.dirname(path)
    if not p_dir or not edited_dirs:
        return 0
    best = 0
    for e_dir in edited_dirs:
        try:
            common = os.path.commonpath((p_dir, e_dir))
        except ValueError:
            continue
        if common and common != ".":
            best = max(best, len(Path(common).parts))
    return best


def related_files(
    workspace: Path,
    changed: list[str],
    diff_text: str,
    *,
    limit: int = MAX_RESULTS,
    timeout_s: float = RELATED_TIMEOUT_S,
    ignore: tuple[str, ...] = (),
) -> list[tuple[str, list[str]]]:
    """Return top-`limit` untouched production `.py` files referencing changed symbols.

    Each result is ``(rel_path, matched_symbols)`` ranked by:
    distinct matched symbols desc, shared directory depth with any changed file desc,
    total occurrences desc, path asc.
    """
    if limit <= 0 or timeout_s <= 0 or not changed or not diff_text.strip():
        return []
    deadline = time.monotonic() + timeout_s
    ws_root = workspace.resolve()
    norm_changed = {_norm_path(c) for c in changed if _norm_path(c)}
    changed_prod = {
        p
        for p in norm_changed
        if is_production_python(p, ws_root, ignore=ignore, _ws_root=ws_root)
    }
    if not changed_prod:
        return []

    try:
        diff_paths, prod_files = _parse_diff(diff_text, changed_prod, deadline=deadline)
        if not prod_files:
            return []
        excluded = norm_changed | diff_paths
        symbols = _collect_symbols(ws_root, prod_files, deadline=deadline)
        if not symbols:
            return []
        file_hits = _git_grep_symbols(ws_root, symbols, excluded, ignore, deadline)
    except (TimeoutError, OSError):
        return []

    if not file_hits:
        return []

    edited_dirs = tuple({d for ep in changed_prod if (d := os.path.dirname(ep))})
    ranked = sorted(
        file_hits.items(),
        key=lambda item: (
            -len(item[1]),
            -_shared_dir_depth(item[0], edited_dirs),
            -sum(item[1].values()),
            item[0],
        ),
    )
    return [(path, sorted(counts)) for path, counts in ranked[:limit]]


def format_related_notice(items: list[tuple[str, list[str]]]) -> str:
    """Format the one-shot pre-finish notice for related files."""
    lines = "\n".join(
        f"- {path} ({', '.join(symbols[:MAX_SYMBOLS_PER_LINE])})" for path, symbols in items
    )
    return RELATED_FILES_NOTICE.format(lines=lines)


def _workspace_prefix(ws_root: Path, deadline: float) -> str:
    """Return git repo prefix (e.g. ``'sub/'``) when ``ws_root`` is a repo subdirectory."""
    if (ws_root / ".git").exists():
        return ""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return ""
    try:
        proc = _git(ws_root, "rev-parse", "--show-prefix", timeout=remaining)
    except subprocess.TimeoutExpired:
        return ""
    if proc is None or proc.returncode != 0:
        return ""
    prefix = _norm_path(proc.stdout.decode("utf-8", "replace"))
    return f"{prefix}/" if prefix else ""


def _relativize_changed(ws_root: Path, files: list[str], prefix: str) -> list[str]:
    out: list[str] = []
    for raw in files:
        p = _norm_path(raw)
        if not p:
            continue
        if prefix and p.startswith(prefix) and not (ws_root / p).exists():
            p = p[len(prefix) :]
        if p:
            out.append(p)
    return out


def _git_untracked_files(ws_root: Path, prod_files: list[str], deadline: float) -> set[str]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return set()
    try:
        proc = _git(
            ws_root,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            *prod_files,
            timeout=remaining,
        )
    except subprocess.TimeoutExpired:
        return set()
    if proc is None or proc.returncode != 0:
        return set()
    return {_norm_path(p) for p in proc.stdout.decode("utf-8", "replace").split("\0") if p}


def _git_diff_for_files(ws_root: Path, prod_files: list[str], deadline: float) -> str:
    """Unified diff of tracked `prod_files` vs HEAD plus synthetic diffs for untracked ones."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return ""
    text = ""
    try:
        proc = _git(
            ws_root,
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--relative",
            "HEAD",
            "--",
            *prod_files,
            timeout=remaining,
        )
    except subprocess.TimeoutExpired:
        return ""
    if proc is not None and proc.returncode == 0:
        text = proc.stdout[:MAX_DIFF_CHARS].decode("utf-8", "replace")

    untracked = _git_untracked_files(ws_root, prod_files, deadline)
    chunks = [text] if text else []
    total_chars = len(text)
    for rel in prod_files:
        if time.monotonic() >= deadline:
            return ""
        if total_chars >= MAX_DIFF_CHARS:
            break
        if rel not in untracked or not _inside_workspace(ws_root, rel):
            continue
        fpath = ws_root / rel
        try:
            st = fpath.stat()
            if not fpath.is_file() or st.st_size > MAX_FILE_BYTES:
                continue
            added = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        added_lines = added.splitlines()
        if not added_lines:
            continue
        body = "".join(f"+{line}\n" for line in added_lines)
        chunk = (
            f"diff --git a/{rel} b/{rel}\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            f"+++ b/{rel}\n"
            f"@@ -0,0 +1,{len(added_lines)} @@\n"
            f"{body}"
        )
        chunks.append(chunk)
        total_chars += len(chunk)
    return "\n".join(chunks)


def find_related_files(
    workspace: Path,
    changed: list[str],
    *,
    limit: int = MAX_RESULTS,
    timeout_s: float = RELATED_TIMEOUT_S,
    ignore: tuple[str, ...] = (),
) -> list[tuple[str, list[str]]]:
    """Git helper: collect diff for `changed` and return related production `.py` files.

    Time-bounded to `timeout_s` (default 2 s). Never raises; returns `[]` on timeout or error.
    """
    try:
        if not changed or limit <= 0 or timeout_s <= 0:
            return []
        deadline = time.monotonic() + timeout_s
        ws_root = workspace.resolve()
        prefix = _workspace_prefix(ws_root, deadline)
        files = _relativize_changed(ws_root, changed, prefix)
        if not files:
            return []
        prod_changed = sorted(
            {
                p
                for p in files
                if is_production_python(p, ws_root, ignore=ignore, _ws_root=ws_root)
            }
        )
        if not prod_changed:
            return []
        diff_text = _git_diff_for_files(ws_root, prod_changed, deadline)
        if not diff_text.strip():
            return []
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return []
        return related_files(
            ws_root,
            files,
            diff_text,
            limit=limit,
            timeout_s=remaining,
            ignore=ignore,
        )
    except Exception:
        return []
