"""Repository-context support: constants, budgets, match selection and IO helpers."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional


REPO_INSPECT_SCHEMA_VERSION = 3

_TEXT_SUFFIXES = frozenset(
    {
        ".py",
        ".pyi",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".md",
        ".rst",
        ".toml",
        ".yaml",
        ".yml",
        ".json",
        ".ini",
        ".cfg",
        ".txt",
        ".html",
        ".css",
        ".scss",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".kts",
        ".cs",
        ".c",
        ".h",
        ".hpp",
        ".cpp",
        ".cc",
        ".rb",
        ".php",
        ".swift",
        ".scala",
        ".vue",
        ".svelte",
        ".sql",
        ".proto",
        ".gradle",
        ".xml",
        ".sh",
        ".ps1",
    }
)

_SYMBOL_SUFFIXES = frozenset(
    {
        ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs",
        ".java", ".kt", ".kts", ".cs", ".c", ".h", ".hpp", ".cpp",
        ".cc", ".rb", ".php", ".swift", ".scala", ".vue", ".svelte",
        ".sh", ".ps1",
    }
)

_IGNORED_STATUS_COMPONENTS = frozenset(
    {
        # Same scratch trees the search skips: an inspection that walks a copy of
        # this repository reports the copy as the place the work belongs.
        ".tmp",
        ".work",
        ".mypy_cache",
        ".netlify",
        ".next",
        ".nuxt",
        ".output",
        ".pytest_cache",
        ".ruff_cache",
        ".svelte-kit",
        ".tox",
        ".turbo",
        ".venv",
        ".vite",
        "__pycache__",
        "build",
        "coverage",
        "dist",
        "node_modules",
        "out",
        "site-packages",
        "target",
        "venv",
    }
)

_IGNORED_STATUS_PREFIXES = ("adapt-v5-", "adapt-v6-")

def _ignored_component(part: str) -> bool:
    """True for a path component that is scratch, vendored or generated."""
    lowered = part.lower()
    return lowered in _IGNORED_STATUS_COMPONENTS or lowered.startswith(
        _IGNORED_STATUS_PREFIXES
    )

_GENERIC_SYMBOL = re.compile(
    r"\b(?P<kind>function|func|fn|def|class|interface|type|struct|enum|trait)\s+"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
)

_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "from",
        "with",
        "into",
        "find",
        "full",
        "flow",
        "where",
        "what",
        "how",
        "this",
        "that",
        "или",
        "как",
        "где",
        "найти",
        "полный",
        "через",
        "для",
        "что",
        "это",
        "из",
        "от",
        "до",
        "по",
    }
)

@dataclass(frozen=True)
class InspectBudget:
    max_files: int
    max_matches: int
    max_excerpts: int
    context_lines: int
    max_file_bytes: int

_BUDGETS = {
    "focused": InspectBudget(40, 200, 20, 3, 1_000_000),
    "standard": InspectBudget(100, 800, 50, 5, 2_000_000),
    "deep": InspectBudget(250, 2500, 120, 8, 4_000_000),
}

_EDIT_CONTEXT_MAX_BYTES = 16_384

_EDIT_CONTEXT_MAX_LINES = 120

def _per_file_match_limit(budget: InspectBudget) -> int:
    """Keep one noisy file from consuming the whole semantic-search budget."""
    return max(8, min(32, budget.max_matches // 20))

def _identifier_tokens(tokens: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        token for token in tokens if any(marker in token for marker in ("_", ".", "-"))
    )

def _path_relevance(path: str, tokens: tuple[str, ...]) -> float:
    """A descriptive backup directory must not outweigh the actual module name."""
    lowered = path.lower()
    name = lowered.rsplit("/", 1)[-1]
    return sum(1.0 if token in name else 0.2 if token in lowered else 0.0
               for token in tokens)

def _content_search_tokens(tokens: tuple[str, ...]) -> tuple[str, ...]:
    """Choose high-signal tokens for repository-wide content search.

    All goal tokens still participate in path/file ranking. The expensive full
    text scan avoids short generic words that mostly create JSON/output noise;
    exact code-like identifiers and longer terms are preferred, with a small
    fallback so short goals remain searchable.
    """
    selected: list[str] = []
    for token in _identifier_tokens(tokens):
        if token not in selected:
            selected.append(token)
    for token in tokens:
        if len(token) >= 7 and token not in selected:
            selected.append(token)
        if len(selected) >= 8:
            break
    for token in tokens:
        if token not in selected:
            selected.append(token)
        if len(selected) >= 4:
            break
    return tuple(selected[:8])

def _raw_match_limit(budget: InspectBudget) -> int:
    """Bound the pre-ranking pool while allowing broad file diversity.

    Search subprocesses already capture their complete stdout before this code
    runs, so stopping parsing after only 10x the final budget saves little I/O and
    reintroduces filesystem-order bias on large repositories. A wider bounded
    pool lets relevance ranking see late source files while the final model-facing
    match budget remains unchanged.
    """
    return min(20_000, max(budget.max_matches, budget.max_matches * 100))

def _select_diverse_matches(
    matches: Iterable[Mapping[str, Any]],
    tokens: tuple[str, ...],
    budget: InspectBudget,
) -> list[dict[str, Any]]:
    """Rank matching files before spending the final match budget.

    Search tools return filesystem/order-dependent streams. Cutting that stream
    at ``max_matches`` made an early noisy doc/test crowd out a later source file
    even when the source covered the actual task tokens. Grouping first and then
    round-robining across ranked files makes focused inspection deterministic by
    relevance rather than directory order.
    """

    groups: dict[str, list[dict[str, Any]]] = {}
    for item in matches:
        path = str(item.get("path", ""))
        if not path:
            continue
        groups.setdefault(path, []).append(dict(item))

    source_roots = ("src/", "lib/", "app/", "pkg/", "cmd/", "internal/", "packages/")

    identifier_tokens = tuple(
        token for token in tokens if any(marker in token for marker in ("_", ".", "-"))
    )

    def priority(item: tuple[str, list[dict[str, Any]]]) -> tuple[float, str]:
        path, items = item
        lowered_path = path.lower()
        path_hits = _path_relevance(path, tokens)
        coverage: set[str] = {
            token for token in tokens if token in lowered_path
        }
        identifier_coverage: set[str] = {
            token for token in identifier_tokens if token in lowered_path
        }
        for match in items:
            text = str(match.get("text", "")).lower()
            coverage.update(token for token in tokens if token in text)
            identifier_coverage.update(token for token in identifier_tokens if token in text)
        bonus = 0.0
        if path.startswith(source_roots):
            bonus += 36.0
        elif path.startswith("tests/"):
            bonus += 24.0
        if Path(path).suffix.lower() in _SYMBOL_SUFFIXES:
            bonus += 8.0
        if path.startswith("docs/") or Path(path).suffix.lower() in {".md", ".rst", ".txt"}:
            bonus -= 5.0
        score = (
            bonus
            + path_hits * 50.0
            + len(coverage) * 18.0
            + len(identifier_coverage) * 36.0
            + min(len(items), 8)
        )
        return (-score, path)

    for path, items in groups.items():
        lowered_path = path.lower()

        def line_priority(item: dict[str, Any]) -> tuple[int, int, int]:
            text = str(item.get("text", "")).lower()
            identifier_hits = sum(
                token in text or token in lowered_path for token in identifier_tokens
            )
            coverage = sum(token in text or token in lowered_path for token in tokens)
            line = item.get("line")
            line_number = line if isinstance(line, int) else 0
            return (-identifier_hits, -coverage, line_number)

        items.sort(key=line_priority)

    ordered = sorted(groups.items(), key=priority)
    selected: list[dict[str, Any]] = []
    round_index = 0
    while len(selected) < budget.max_matches:
        added = False
        for _path, items in ordered:
            if round_index >= len(items):
                continue
            selected.append(items[round_index])
            added = True
            if len(selected) >= budget.max_matches:
                break
        if not added:
            break
        round_index += 1
    return selected

def _run(
    repository: Path,
    argv: list[str],
    *,
    timeout: float = 30.0,
    allow_failure: bool = False,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        argv,
        cwd=repository,
        check=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0 and not allow_failure:
        raise RuntimeError("repository inspection command failed")
    return completed

def _tokens(goal: str) -> tuple[str, ...]:
    raw = re.findall(r"[A-Za-z_][A-Za-z0-9_.-]{2,}|[А-Яа-яЁё][А-Яа-яЁё0-9_-]{2,}", goal)
    values: list[str] = []
    for item in raw:
        lowered = item.lower().strip("._-")
        if lowered and lowered not in _STOPWORDS and lowered not in values:
            values.append(lowered)
    values.sort(key=lambda value: (-len(value), value))
    return tuple(values[:16])

def _safe_relative(repository: Path, value: str) -> Optional[str]:
    raw = value.replace("\\", "/")
    while raw.startswith("./"):
        raw = raw[2:]
    if not raw or raw.startswith("/") or raw.startswith("../") or "/../" in raw:
        return None
    # Compare against the resolved base: CI temp roots carry 8.3 short names
    # (Windows runners) or symlinked prefixes (/var on macOS), so joining
    # against the unresolved path would reject every repository-relative path.
    base = repository.expanduser().resolve()
    path = (base / raw).resolve()
    try:
        relative = path.relative_to(base).as_posix()
    except ValueError:
        return None
    return relative

def _status_entries(status: str) -> Iterable[tuple[str, str]]:
    """Decode porcelain v1 -z; rename/copy sources are a second NUL field.

    With -z, the destination comes first and literal " -> " is part of the
    filename, not the human-readable rename separator. Keep the origin in the
    identity too: changing which source was renamed/copied must invalidate it.
    """
    entries = iter(status.split("\0"))
    for entry in entries:
        if len(entry) < 4:
            continue
        code = entry[:2]
        yield code, entry[3:]
        if "R" in code or "C" in code:
            source = next(entries, "")
            if source:
                yield code, source

def _read_lines(path: Path, max_bytes: int) -> list[str]:
    try:
        if path.stat().st_size > max_bytes:
            return []
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []

def _json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
