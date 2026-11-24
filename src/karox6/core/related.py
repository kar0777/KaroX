"""Find untouched production Python files that reference symbols changed in a diff.

Used once at the first finish attempt in a git repository to surface likely sibling
implementations, callers, or subclasses that the agent may have missed.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from karox6.core.loopguard import _run_git, is_test_file
from karox6.tools.files import GENERATED_DIRS, is_generated

RELATED_TIMEOUT_S = 2.0
MAX_RESULTS = 5
MAX_SYMBOLS_PER_LINE = 4
MAX_SCAN_FILES = 20_000
MAX_FILE_BYTES = 1_048_576  # 1 MiB
MAX_SEARCH_SYMBOLS = 200
MAX_GREP_ARG_CHARS = 8_000

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

# Common Python keywords, builtins, generic dunders, and ubiquitous method/attr names
# that appear in almost every file and carry no cross-file signal.
STOPWORDS = frozenset(
    {
        # Keywords / literals
        "False",
        "None",
        "True",
        "and",
        "as",
        "assert",
        "async",
        "await",
        "break",
        "class",
        "continue",
        "def",
        "del",
        "elif",
        "else",
        "except",
        "finally",
        "for",
        "from",
        "global",
        "if",
        "import",
        "in",
        "is",
        "lambda",
        "nonlocal",
        "not",
        "or",
        "pass",
        "raise",
        "return",
        "try",
        "while",
        "with",
        "yield",
        "match",
        "case",
        "self",
        "cls",
        "args",
        "kwargs",
        "other",
        # Common builtins & exceptions
        "abs",
        "all",
        "any",
        "bool",
        "bytes",
        "callable",
        "chr",
        "classmethod",
        "complex",
        "dict",
        "dir",
        "divmod",
        "enumerate",
        "eval",
        "exec",
        "filter",
        "float",
        "format",
        "frozenset",
        "getattr",
        "globals",
        "hasattr",
        "hash",
        "help",
        "hex",
        "id",
        "input",
        "int",
        "isinstance",
        "issubclass",
        "iter",
        "len",
        "list",
        "locals",
        "map",
        "max",
        "min",
        "next",
        "object",
        "oct",
        "open",
        "ord",
        "pow",
        "print",
        "property",
        "range",
        "repr",
        "reversed",
        "round",
        "set",
        "setattr",
        "slice",
        "sorted",
        "staticmethod",
        "str",
        "sum",
        "super",
        "tuple",
        "type",
        "vars",
        "zip",
        "BaseException",
        "Exception",
        "ValueError",
        "TypeError",
        "KeyError",
        "IndexError",
        "AttributeError",
        "RuntimeError",
        "NotImplementedError",
        "OSError",
        "IOError",
        "ImportError",
        "FileNotFoundError",
        "StopIteration",
        "AssertionError",
        "ZeroDivisionError",
        "Warning",
        "UserWarning",
        "DeprecationWarning",
        "FutureWarning",
        # Typing / stdlib common names
        "Any",
        "Optional",
        "Union",
        "List",
        "Dict",
        "Set",
        "Tuple",
        "Sequence",
        "Mapping",
        "Callable",
        "Iterable",
        "Iterator",
        "Generator",
        "NamedTuple",
        "Literal",
        "Final",
        "ClassVar",
        "Protocol",
        "TypeVar",
        "Generic",
        "cast",
        "overload",
        "Path",
        "dataclass",
        "field",
        # Generic container / string / IO / logging methods & attrs
        "get",
        "pop",
        "add",
        "remove",
        "discard",
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
        "reverse",
        "join",
        "split",
        "rsplit",
        "splitlines",
        "strip",
        "lstrip",
        "rstrip",
        "replace",
        "find",
        "rfind",
        "startswith",
        "endswith",
        "lower",
        "upper",
        "title",
        "encode",
        "decode",
        "read",
        "write",
        "close",
        "flush",
        "seek",
        "tell",
        "exists",
        "is_file",
        "is_dir",
        "mkdir",
        "unlink",
        "resolve",
        "parent",
        "parents",
        "name",
        "stem",
        "suffix",
        "parts",
        "debug",
        "info",
        "warning",
        "warn",
        "error",
        "critical",
        "exception",
        "logger",
        "logging",
        "log",
        "data",
        "value",
        "result",
        "item",
        "node",
        "path",
        "file",
        "line",
        "lines",
        "text",
        "string",
        "code",
        "mode",
        "kind",
        "size",
        "length",
        "width",
        "height",
        "shape",
        "dtype",
        "dims",
        "attrs",
        "config",
        "options",
        "params",
        "state",
        "status",
        "message",
        "errors",
        "default",
        "defaults",
        "context",
        "metadata",
        "extra",
        "run",
        "call",
        "apply",
        "check",
        "parse",
        "load",
        "dump",
        "dumps",
        "loads",
        "save",
        "create",
        "delete",
        "reset",
        "init",
        "setup",
        "build",
        "render",
        "start",
        "stop",
        "send",
        "recv",
        "emit",
        "handle",
        "process",
        "execute",
        "validate",
        "convert",
        "transform",
        "serialize",
        "deserialize",
        "search",
        "sub",
        "group",
        "groups",
        "compile",
        # Generic dunders
        "__init__",
        "__new__",
        "__del__",
        "__repr__",
        "__str__",
        "__bytes__",
        "__format__",
        "__lt__",
        "__le__",
        "__eq__",
        "__ne__",
        "__gt__",
        "__ge__",
        "__hash__",
        "__bool__",
        "__getattr__",
        "__getattribute__",
        "__setattr__",
        "__delattr__",
        "__dir__",
        "__get__",
        "__set__",
        "__delete__",
        "__call__",
        "__len__",
        "__getitem__",
        "__setitem__",
        "__delitem__",
        "__iter__",
        "__next__",
        "__reversed__",
        "__contains__",
        "__enter__",
        "__exit__",
        "__class__",
        "__dict__",
        "__name__",
        "__qualname__",
        "__module__",
        "__doc__",
        "__annotations__",
        "__slots__",
        "__all__",
        "__file__",
        "__version__",
        "__post_init__",
        "__copy__",
        "__deepcopy__",
    }
)

_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$")
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
_DEF_CLASS_RE = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)")
_TOPLEVEL_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=]+)?=(?!=)\s*")
_ATTR_RE = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)")
_STRING_RE = re.compile(
    r'("""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'|"[^"\\]*(?:\\.[^"\\]*)*"|\'[^\'\\]*(?:\\.[^\'\\]*)*\')'
)
_WORD_RE = re.compile(r"\w+")


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
    return len(sym) >= min_len and sym not in STOPWORDS


def _strip_comments_and_strings(code_line: str) -> str:
    cleaned = _STRING_RE.sub(" ", code_line)
    idx = cleaned.find("#")
    if idx != -1:
        cleaned = cleaned[:idx]
    return cleaned


def _parse_diff(
    diff_text: str,
    allowed_prod: set[str],
) -> tuple[set[str], dict[str, _DiffFileInfo]]:
    """Parse unified diff -> (all_diff_files, prod_py_file_infos)."""
    all_diff_files: set[str] = set()
    prod_files: dict[str, _DiffFileInfo] = {}
    cur_file: _DiffFileInfo | None = None
    old_line = 0
    new_line = 0
    in_docstring_delim: str | None = None
    saw_git_header = False

    def select_file(path: str) -> _DiffFileInfo | None:
        if path in allowed_prod:
            return prod_files.setdefault(path, _DiffFileInfo(path=path))
        return None

    for raw in diff_text.splitlines():
        if raw.startswith("diff --git "):
            saw_git_header = True
            cur_file = None
            in_docstring_delim = None
            m = _DIFF_GIT_RE.match(raw)
            if m:
                a_path, b_path = _norm_path(m.group(1)), _norm_path(m.group(2))
                all_diff_files.add(a_path)
                all_diff_files.add(b_path)
                cur_file = select_file(b_path) or select_file(a_path)
            continue

        if raw.startswith("+++ "):
            target = raw[4:].split("\t", 1)[0].strip()
            if target != "/dev/null":
                b_path = _norm_path(target[2:] if target.startswith("b/") else target)
                all_diff_files.add(b_path)
                if not saw_git_header:
                    in_docstring_delim = None
                    cur_file = select_file(b_path)
            continue

        if raw.startswith("--- "):
            target = raw[4:].split("\t", 1)[0].strip()
            if target != "/dev/null":
                a_path = _norm_path(target[2:] if target.startswith("a/") else target)
                all_diff_files.add(a_path)
                if not saw_git_header and cur_file is None:
                    in_docstring_delim = None
                    cur_file = select_file(a_path)
            continue

        if cur_file is None:
            continue

        if raw.startswith(("index ", "new file", "deleted file", "old mode", "new mode")):
            continue

        hm = _HUNK_RE.match(raw)
        if hm:
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
            return
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

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start = getattr(node, "lineno", None)
                end = getattr(node, "end_lineno", None)
                if start is None or end is None:
                    continue
                if any(start <= ln <= end for ln in target_lines):
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

    _, prod_files = _parse_diff(diff_text, changed_prod)
    if not prod_files:
        return []

    return _collect_symbols(ws_root, prod_files, deadline=deadline)


def _batch_symbols(symbols: list[str], max_chars: int = MAX_GREP_ARG_CHARS) -> list[list[str]]:
    batches: list[list[str]] = []
    cur: list[str] = []
    chars = 0
    for sym in symbols:
        cost = len(sym) + 4  # "-e " + sym + space
        if cur and chars + cost > max_chars:
            batches.append(cur)
            cur = [sym]
            chars = cost
        else:
            cur.append(sym)
            chars += cost
    if cur:
        batches.append(cur)
    return batches


def _git_grep_symbols(
    ws_root: Path,
    symbols: list[str],
    excluded: set[str],
    ignore: tuple[str, ...],
    deadline: float,
) -> dict[str, dict[str, int]] | None:
    """Search tracked `.py` files with `git grep -w -F -n`. Returns None to trigger fallback."""
    file_hits: dict[str, dict[str, int]] = {}
    for batch in _batch_symbols(symbols):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("related_files timed out before git grep")
        e_args: list[str] = []
        for sym in batch:
            e_args.extend(("-e", sym))
        try:
            proc = _run_git(
                ws_root,
                "grep",
                "--no-color",
                "-w",
                "-F",
                "-n",
                *e_args,
                "--",
                "*.py",
                timeout=remaining,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("git grep timed out") from exc
        if proc is None or proc.returncode not in (0, 1):
            return None
        if proc.returncode == 1:
            continue

        batch_set = set(batch)
        for raw_line in proc.stdout.decode("utf-8", "replace").splitlines():
            parts = raw_line.split(":", 2)
            if len(parts) < 3:
                continue
            rel = _norm_path(parts[0])
            if rel in excluded:
                continue
            if not is_production_python(rel, ws_root, ignore=ignore, _ws_root=ws_root):
                continue
            counts = file_hits.get(rel)
            for tok in _WORD_RE.findall(parts[2]):
                if tok in batch_set:
                    if counts is None:
                        counts = file_hits.setdefault(rel, {})
                    counts[tok] = counts.get(tok, 0) + 1
    return file_hits


def _python_scan_symbols(
    ws_root: Path,
    symbols: list[str],
    excluded: set[str],
    ignore: tuple[str, ...],
    deadline: float,
) -> dict[str, dict[str, int]]:
    """Pure Python fallback scan limited to 20k files and 1 MB per file."""
    sym_set = set(symbols)
    norm_ign = _norm_ignore(ignore)
    file_hits: dict[str, dict[str, int]] = {}
    scanned = 0

    for dirpath, dirnames, filenames in os.walk(ws_root, topdown=True):
        if time.monotonic() >= deadline:
            raise TimeoutError("related_files scan timed out")
        dir_rel = _norm_path(os.path.relpath(dirpath, ws_root))
        if dir_rel == ".":
            dir_rel = ""

        # Prune excluded and ignored directories in-place.
        kept: list[str] = []
        for d in dirnames:
            dl = d.lower()
            if dl in EXCLUDED_DIRS or d in GENERATED_DIRS:
                continue
            sub_rel = f"{dir_rel}/{d}" if dir_rel else d
            if norm_ign and _in_ignore(sub_rel, norm_ign):
                continue
            kept.append(d)
        dirnames[:] = kept

        for fname in filenames:
            scanned += 1
            if scanned > MAX_SCAN_FILES:
                return file_hits
            if (scanned & 31) == 0 and time.monotonic() >= deadline:
                raise TimeoutError("related_files scan timed out")
            if not fname.endswith(".py"):
                continue
            rel = f"{dir_rel}/{fname}" if dir_rel else fname
            if rel in excluded:
                continue
            if not is_production_python(rel, ws_root, ignore=ignore, _ws_root=ws_root):
                continue
            fpath = ws_root / rel
            try:
                st = fpath.stat()
                if not fpath.is_file() or st.st_size > MAX_FILE_BYTES:
                    continue
                content = fpath.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            counts: dict[str, int] = {}
            for tok in _WORD_RE.findall(content):
                if tok in sym_set:
                    counts[tok] = counts.get(tok, 0) + 1
            if counts:
                file_hits[rel] = counts
    return file_hits


def _shared_dir_depth(path: str, edited_prod_paths: set[str]) -> int:
    p_parts = path.split("/")[:-1]
    best = 0
    for ep in edited_prod_paths:
        e_parts = ep.split("/")[:-1]
        depth = 0
        for a, b in zip(p_parts, e_parts, strict=False):
            if a == b:
                depth += 1
            else:
                break
        best = max(best, depth)
    return best


def related_files(
    workspace: Path,
    changed: list[str],
    diff_text: str,
    *,
    limit: int = MAX_RESULTS,
    timeout_s: float = RELATED_TIMEOUT_S,
    ignore: tuple[str, ...] = (),
    use_git: bool = True,
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

    diff_paths, prod_files = _parse_diff(diff_text, changed_prod)
    if not prod_files:
        return []
    excluded = norm_changed | diff_paths

    symbols = _collect_symbols(ws_root, prod_files, deadline=deadline)
    if not symbols:
        return []

    try:
        file_hits: dict[str, dict[str, int]] | None = None
        if use_git:
            file_hits = _git_grep_symbols(ws_root, symbols, excluded, ignore, deadline)
        if file_hits is None:
            file_hits = _python_scan_symbols(ws_root, symbols, excluded, ignore, deadline)
    except (TimeoutError, OSError):
        return []

    if not file_hits:
        return []

    ranked = sorted(
        file_hits.items(),
        key=lambda item: (
            -len(item[1]),
            -_shared_dir_depth(item[0], changed_prod),
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


def _git_changed_files(ws_root: Path, deadline: float) -> list[str] | None:
    """Tracked and untracked changed paths from `git status`, or None outside a repo."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    try:
        proc = _run_git(
            ws_root,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            timeout=remaining,
        )
    except subprocess.TimeoutExpired:
        return None
    if proc is None or proc.returncode != 0:
        return None
    entries = proc.stdout.decode("utf-8", "replace").split("\0")
    out: set[str] = set()
    skip_next = False
    for entry in entries:
        if skip_next:
            skip_next = False
            continue
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        norm = _norm_path(path)
        if norm:
            out.add(norm)
        if status[0] in "RC":
            skip_next = True
    return sorted(out)


def _git_diff_for_files(ws_root: Path, prod_files: list[str], deadline: float) -> str:
    """Unified diff of tracked `prod_files` vs HEAD plus synthetic diffs for untracked ones."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return ""
    text = ""
    try:
        proc = _run_git(
            ws_root,
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "HEAD",
            "--",
            *prod_files,
            timeout=remaining,
        )
    except subprocess.TimeoutExpired:
        return ""
    if proc is not None and proc.returncode == 0:
        text = proc.stdout.decode("utf-8", "replace")

    shown: set[str] = set()
    for line in text.splitlines():
        if line.startswith("diff --git "):
            m = _DIFF_GIT_RE.match(line)
            if m:
                shown.add(_norm_path(m.group(2)))
                shown.add(_norm_path(m.group(1)))

    chunks = [text] if text else []
    for rel in prod_files:
        if time.monotonic() >= deadline:
            return ""
        if rel in shown or not _inside_workspace(ws_root, rel):
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
        chunks.append(
            f"diff --git a/{rel} b/{rel}\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            f"+++ b/{rel}\n"
            f"@@ -0,0 +1,{len(added_lines)} @@\n"
            f"{body}"
        )
    return "\n".join(chunks)


def find_related_files(
    workspace: Path,
    changed: list[str] | None = None,
    *,
    limit: int = MAX_RESULTS,
    timeout_s: float = RELATED_TIMEOUT_S,
    ignore: tuple[str, ...] = (),
) -> list[tuple[str, list[str]]]:
    """Git helper: collect diff for `changed` (or working tree) and return related files.

    Time-bounded to `timeout_s` (default 2 s). Never raises; returns `[]` on timeout or error.
    """
    try:
        deadline = time.monotonic() + timeout_s
        ws_root = workspace.resolve()
        files = _git_changed_files(ws_root, deadline) if changed is None else list(changed)
        if not files:
            return []
        prod_changed = sorted(
            {
                p
                for p in (_norm_path(c) for c in files)
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
            use_git=True,
        )
    except Exception:
        return []
