"""Focused test-target selection from changed files and their import graph."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence


MAX_TEST_FILES = 4_000

MAX_TEST_TARGETS = 8

_TEST_FILE = re.compile(r"^test_.*\.py$|^.*_test\.py$")

_TEST_DIRS = frozenset({"tests", "test", "testing"})

_SKIP_DIRS = frozenset(
    {
        "node_modules",
        "__pycache__",
        "site-packages",
        "build",
        "dist",
        "venv",
        "env",
    }
)

def is_test_path(relative: str) -> bool:
    """Whether a repository-relative path names a Python test module.

    A ``test_*.py`` / ``*_test.py`` file anywhere qualifies, and so does any
    ``.py`` file below a ``tests``/``test``/``testing`` directory (Django's
    ``tests/app/tests.py``). Parent references never qualify: the worker
    resolves the path again and refuses one that leaves the repository.
    """

    text = str(relative or "").replace("\\", "/").strip("/")
    if not text.endswith(".py"):
        return False
    parts = [part for part in text.split("/") if part]
    if not parts or any(part in (".", "..") for part in parts):
        return False
    if _TEST_FILE.match(parts[-1]):
        return True
    return any(part in _TEST_DIRS for part in parts[:-1])

_IMPORT = re.compile(
    r"^\s*import\s+(?P<modules>[\w.]+(?:\s*,\s*[\w.]+)*)"
    r"|^\s*from\s+(?P<source>[\w.]+)\s+import\s+(?P<names>[^\n]+)",
    re.M,
)

_IMPORT_CACHE: dict[tuple[str, int, int], tuple[str, ...]] = {}

_IMPORT_CACHE_LIMIT = 2_048

MAX_IMPORT_BYTES = 512 * 1024

def select_test_targets(
    repository: Path | str,
    changed_paths: Iterable[str],
    *,
    limit: int = MAX_TEST_TARGETS,
) -> list[str]:
    """Test files that the changed modules can affect.

    Two signals, strongest first: the conventional name (``test_mod.py`` for
    ``mod.py``) and an import of the changed module by its dotted name. Within
    each signal the file closest to the changed module wins, so
    ``sympy/core/basic.py`` selects ``sympy/core/tests/test_basic.py`` before a
    same-named file in another package. The result is a bounded, sorted list of
    repository-relative paths; an empty list means "no test names this
    module", not "nothing can break".

    Only paths that `is_test_path` accepts are returned because that is the
    contract the pytest runner enforces; returning a path it would refuse
    turns a useful hint into a failed call.
    """

    root = Path(repository).expanduser().resolve(strict=False)
    if not root.is_dir():
        return []
    changed = _changed_stems(changed_paths)
    if not changed:
        return []
    files = _repository_test_files(root)
    if not files:
        return []
    changed_dirs = [
        tuple(Path(str(raw).replace("\\", "/")).parent.parts)
        for raw in changed_paths
        if isinstance(raw, str) and raw
    ]
    by_name: list[tuple[int, str]] = []
    by_import: list[tuple[int, str]] = []
    wanted_names = set()
    for stem, _ in changed:
        wanted_names.add(f"test_{stem}")
        wanted_names.add(f"{stem}_test")
    for path in files:
        relative = path.relative_to(root).as_posix()
        closeness = _closeness(relative, changed_dirs)
        if path.stem in wanted_names:
            by_name.append((closeness, relative))
            continue
        imports = _imports_of(path)
        if imports and _imports_touch(imports, changed):
            by_import.append((closeness, relative))
    selected: list[str] = []
    for group in (by_name, by_import):
        for _, item in sorted(group, key=lambda row: (-row[0], row[1])):
            if item not in selected:
                selected.append(item)
            if len(selected) >= limit:
                break
        if len(selected) >= limit:
            break
    return sorted(selected)

def _closeness(relative: str, changed_dirs: Sequence[tuple[str, ...]]) -> int:
    """Shared leading directories between a test file and any changed file."""

    parts = tuple(relative.split("/")[:-1])
    best = 0
    for directory in changed_dirs:
        shared = 0
        for left, right in zip(parts, directory):
            if left != right:
                break
            shared += 1
        best = max(best, shared)
    return best

def _repository_test_files(root: Path) -> list[Path]:
    """``test_*.py`` files anywhere a focused run accepts, bounded."""

    files: list[Path] = []
    for directory, subdirs, names in os.walk(root):
        subdirs[:] = sorted(
            name
            for name in subdirs
            if not name.startswith(".")
            and name not in _SKIP_DIRS
            and not name.endswith(".egg-info")
            and not os.path.islink(os.path.join(directory, name))
        )
        for name in sorted(names):
            if not _TEST_FILE.match(name):
                continue
            path = Path(directory) / name
            # safe_path refuses a symlink that leaves the repository, so
            # returning one here would make the focused run fail instead.
            if path.is_symlink():
                continue
            files.append(path)
            if len(files) >= MAX_TEST_FILES:
                return files
    return files

def _changed_stems(changed_paths: Iterable[str]) -> list[tuple[str, str]]:
    """(stem, dotted module) for every changed Python source file."""

    seen: list[tuple[str, str]] = []
    for raw in changed_paths:
        if not isinstance(raw, str) or not raw:
            continue
        path = Path(raw.replace("\\", "/"))
        if path.suffix.lower() != ".py" or path.name.startswith("test_"):
            continue
        if not path.name or path.name == "__init__.py":
            continue
        parts = [part for part in path.with_suffix("").parts if part not in (".", "")]
        if not parts:
            continue
        dotted = ".".join(parts[-2:])
        item = (path.stem, dotted)
        if item not in seen:
            seen.append(item)
    return seen

def _imports_of(path: Path) -> tuple[str, ...]:
    try:
        stat = path.stat()
    except OSError:
        return ()
    if path.is_symlink() or stat.st_size > MAX_IMPORT_BYTES:
        return ()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    cached = _IMPORT_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ()
    modules: list[str] = []
    for match in _IMPORT.finditer(source):
        if match.group("modules"):
            modules.extend(
                item.strip() for item in match.group("modules").split(",") if item.strip()
            )
        elif match.group("source"):
            source_name = match.group("source")
            modules.append(source_name)
            # `from pkg import alpha` imports pkg.alpha; without expanding the
            # names, a changed pkg/alpha.py selected only tests that imported
            # the full dotted path.
            names = (match.group("names") or "").strip().strip("()")
            for item in names.split(","):
                name = item.strip().split(" as ", 1)[0].strip()
                if name and name != "*" and re.fullmatch(r"[A-Za-z_]\w*", name):
                    modules.append(f"{source_name}.{name}")
    result = tuple(dict.fromkeys(modules))
    if len(_IMPORT_CACHE) >= _IMPORT_CACHE_LIMIT:
        _IMPORT_CACHE.clear()
    _IMPORT_CACHE[key] = result
    return result

def _imports_touch(imports: Sequence[str], changed: Sequence[tuple[str, str]]) -> bool:
    for module in imports:
        for _, dotted in changed:
            if module == dotted or module.endswith("." + dotted):
                return True
    return False

def render_targets(paths: Sequence[str]) -> str:
    """A copy-ready hint naming the tests a repair should rerun first."""
    if not paths:
        return ""
    listed = ", ".join(f'"{path}"' for path in paths)
    return f"Targeted rerun: tests.run suite=focused targets=[{listed}]"

def rerun_targets(
    report: Optional[Mapping[str, Any]], limit: int = 4
) -> list[str]:
    """The test files or node ids a focused rerun should use.

    The map's ``path`` is where the failure *raised*, which may be a source
    file; rerunning a source file is not a test. The node id still names the
    test, so its file wins when `is_test_path` accepts it.
    """

    targets: list[str] = []
    if not isinstance(report, Mapping):
        return targets
    rows = report.get("failures")
    if not isinstance(rows, list):
        return targets
    for row in rows:
        if not isinstance(row, dict):
            continue
        test_id = str(row.get("test_id") or "")
        node_file = test_id.split("::", 1)[0] if "::" in test_id else ""
        if is_test_path(node_file):
            candidate = test_id if test_id.startswith(node_file + "::") else node_file
        else:
            path = str(row.get("path") or "")
            if not is_test_path(path):
                continue
            candidate = path
        if candidate not in targets:
            targets.append(candidate)
        if len(targets) >= limit:
            break
    return targets
