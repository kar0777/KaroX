"""Deterministic repository context engine for high-level hosted inspection.

No model is called inside KaroX.  The engine combines ripgrep, Python AST,
repository paths, test/doc naming and import/call relationships, then stores the
full map as a session artifact while returning a compact ranked digest.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from .artifacts import ArtifactStore
from .paths import runtime_dir
from .security import redact

REPO_INSPECT_SCHEMA_VERSION = 2
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


class RepositoryContextEngine:
    def __init__(
        self,
        repository: Path,
        artifacts: ArtifactStore,
        *,
        policy_profile: str,
    ) -> None:
        self.repository = repository.expanduser().resolve(strict=True)
        self.artifacts = artifacts
        self.policy_profile = policy_profile
        self._git_directory = self._resolve_git_directory()
        self._common_git_directory = self._resolve_common_git_directory(self._git_directory)
        self.is_git_repository = self._git_directory is not None
        repo_key = hashlib.sha256(str(self.repository).encode("utf-8")).hexdigest()[:20]
        self.cache_root = (
            runtime_dir()
            / "vnext"
            / "repo-context-cache"
            / artifacts.session_id
            / repo_key
        )
        self.cache_root.mkdir(parents=True, exist_ok=True)

    def _git(
        self,
        *arguments: str,
        allow_failure: bool = False,
        preserve_whitespace: bool = False,
    ) -> str:
        completed = _run(
            self.repository,
            ["git", "-C", str(self.repository), *arguments],
            timeout=20,
            allow_failure=allow_failure,
        )
        if completed.returncode != 0:
            return ""
        return completed.stdout if preserve_whitespace else completed.stdout.strip()

    def _resolve_git_directory(self) -> Optional[Path]:
        marker = self.repository / ".git"
        if marker.is_dir():
            return marker
        try:
            raw = marker.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        if not raw.lower().startswith("gitdir:"):
            return None
        value = raw.split(":", 1)[1].strip()
        if not value:
            return None
        path = Path(value)
        if not path.is_absolute():
            path = self.repository / path
        try:
            return path.resolve(strict=True)
        except OSError:
            return None

    @staticmethod
    def _resolve_common_git_directory(git_directory: Optional[Path]) -> Optional[Path]:
        if git_directory is None:
            return None
        marker = git_directory / "commondir"
        try:
            raw = marker.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return git_directory
        if not raw:
            return git_directory
        path = Path(raw)
        if not path.is_absolute():
            path = git_directory / path
        try:
            return path.resolve(strict=True)
        except OSError:
            return git_directory

    @staticmethod
    def _valid_revision(value: str) -> Optional[str]:
        candidate = value.strip().lower()
        if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", candidate):
            return candidate
        return None

    def _head_revision(self) -> str:
        if not self.is_git_repository:
            return "not_applicable"
        git_directory = self._git_directory
        common_directory = self._common_git_directory
        if git_directory is None:
            return self._git("rev-parse", "--verify", "HEAD", allow_failure=True) or "unborn"
        try:
            head = (git_directory / "HEAD").read_text(
                encoding="utf-8", errors="replace"
            ).strip()
        except OSError:
            return self._git("rev-parse", "--verify", "HEAD", allow_failure=True) or "unborn"
        detached = self._valid_revision(head)
        if detached is not None:
            return detached
        if not head.startswith("ref:"):
            return self._git("rev-parse", "--verify", "HEAD", allow_failure=True) or "unborn"
        reference = head.split(":", 1)[1].strip().replace("\\", "/")
        if not reference or reference.startswith("/") or ".." in Path(reference).parts:
            return self._git("rev-parse", "--verify", "HEAD", allow_failure=True) or "unborn"
        roots = [git_directory]
        if common_directory is not None and common_directory != git_directory:
            roots.append(common_directory)
        for root in roots:
            try:
                revision = self._valid_revision(
                    (root / reference).read_text(encoding="ascii", errors="replace")
                )
            except OSError:
                revision = None
            if revision is not None:
                return revision
        if common_directory is not None:
            try:
                packed = (common_directory / "packed-refs").read_text(
                    encoding="ascii", errors="replace"
                )
            except OSError:
                packed = ""
            for line in packed.splitlines():
                if not line or line.startswith(("#", "^")):
                    continue
                value, separator, name = line.partition(" ")
                if separator and name.strip() == reference:
                    revision = self._valid_revision(value)
                    if revision is not None:
                        return revision
        return self._git("rev-parse", "--verify", "HEAD", allow_failure=True) or "unborn"

    def _status_snapshot(self, *, untracked_files: str) -> tuple[str, str]:
        if not self.is_git_repository:
            return "not_applicable", ""
        before = self._head_revision()
        status = self._git(
            "--no-optional-locks",
            "status",
            "--porcelain=v1",
            f"--untracked-files={untracked_files}",
            "-z",
            preserve_whitespace=True,
        )
        after = self._head_revision()
        if after != before:
            status = self._git(
                "--no-optional-locks",
                "status",
                "--porcelain=v1",
                f"--untracked-files={untracked_files}",
                "-z",
                preserve_whitespace=True,
            )
        return after, status

    def _non_git_identity(self, *, max_entries: int = 5_000) -> dict[str, Any]:
        """Return a bounded filesystem identity without invoking or creating Git."""

        digest = hashlib.sha256()
        visited = 0
        truncated = False
        stack = [self.repository]
        while stack and visited < max_entries:
            current = stack.pop()
            try:
                entries = sorted(
                    os.scandir(current), key=lambda item: os.path.normcase(item.name)
                )
            except OSError:
                continue
            child_directories: list[Path] = []
            for entry in entries:
                if visited >= max_entries:
                    truncated = True
                    break
                child = Path(entry.path)
                try:
                    relative = child.relative_to(self.repository).as_posix()
                except ValueError:
                    continue
                if any(
                    part.lower() in _IGNORED_STATUS_COMPONENTS
                    or part.lower().endswith(".egg-info")
                    for part in Path(relative).parts
                ):
                    continue
                try:
                    metadata = entry.stat(follow_symlinks=False)
                    is_symlink = entry.is_symlink()
                    is_directory = entry.is_dir(follow_symlinks=False) and not is_symlink
                except OSError:
                    continue
                kind = "symlink" if is_symlink else ("dir" if is_directory else "file")
                # Directory timestamps may change as a side-effect of indexing or
                # antivirus activity even though the visible tree did not. Child
                # paths already capture structural changes, so only regular-file
                # metadata belongs in the cache identity.
                identity = (
                    f"{relative}\0{kind}\0"
                    if is_directory
                    else f"{relative}\0{kind}\0{metadata.st_size}\0{metadata.st_mtime_ns}\0"
                )
                digest.update(identity.encode("utf-8", errors="surrogatepass"))
                visited += 1
                if is_directory:
                    child_directories.append(child)
            stack.extend(reversed(child_directories))
        if stack:
            truncated = True
        return {
            "revision": None,
            "dirty": [],
            "repository_kind": "directory",
            "directory_state": digest.hexdigest(),
            "entries_considered": visited,
            "truncated": truncated,
        }

    def _content_tree_digest(self, path: Path, *, status_code: str, relative: str) -> str:
        """Hash dirty content exactly while collapsing wholly-untracked trees.

        Directory traversal and digest ordering stay deterministic, but regular
        file contents in a sufficiently large tree are read in parallel. This
        preserves the exact SHA-256 identity (including same-size/same-mtime
        rewrites) while avoiding serial I/O across large generated/untracked
        trees that dominate warm ``repo.inspect`` cache validation on Windows.
        """
        ordered: list[tuple[str, Any]] = []
        files: list[Path] = []

        def ignored(value: str) -> bool:
            return any(
                part.lower() in _IGNORED_STATUS_COMPONENTS
                or part.lower().endswith(".egg-info")
                for part in Path(value).parts
            )

        def hash_file(file_path: Path) -> str:
            try:
                content = hashlib.sha256()
                with file_path.open("rb") as handle:
                    while True:
                        chunk = handle.read(1024 * 1024)
                        if not chunk:
                            break
                        content.update(chunk)
                return content.hexdigest()
            except OSError:
                return "unreadable"

        def visit(current: Path, current_relative: str) -> None:
            try:
                metadata = current.stat(follow_symlinks=False)
            except OSError:
                ordered.append(("marker", f"{status_code}\0{current_relative}\0missing"))
                return
            # Reuse the lstat above: Path.is_symlink()/is_dir() each issue
            # another metadata query for every dirty or untracked entry.
            if stat.S_ISLNK(metadata.st_mode):
                try:
                    target = os.readlink(current)
                except OSError:
                    target = "unreadable"
                ordered.append(
                    ("marker", f"{status_code}\0{current_relative}\0symlink\0{target}")
                )
                return
            if not stat.S_ISDIR(metadata.st_mode):
                file_index = len(files)
                files.append(current)
                ordered.append(
                    (
                        "file",
                        (status_code, current_relative, metadata.st_size, file_index),
                    )
                )
                return
            ordered.append(("marker", f"{status_code}\0{current_relative}\0dir"))
            try:
                entries = sorted(os.scandir(current), key=lambda item: os.path.normcase(item.name))
            except OSError:
                ordered.append(
                    ("marker", f"{status_code}\0{current_relative}\0unreadable")
                )
                return
            for entry in entries:
                child = Path(entry.path)
                try:
                    child_relative = child.relative_to(self.repository).as_posix()
                except ValueError:
                    continue
                if ignored(child_relative):
                    continue
                visit(child, child_relative)

        visit(path, relative)
        if len(files) < 8:
            hashes = [hash_file(file_path) for file_path in files]
        else:
            with ThreadPoolExecutor(max_workers=min(8, len(files))) as pool:
                hashes = list(pool.map(hash_file, files))

        digest = hashlib.sha256()
        for kind, value in ordered:
            if kind == "file":
                code, file_relative, size, file_index = value
                marker = (
                    f"{code}\0{file_relative}\0file\0{size}\0{hashes[file_index]}"
                )
            else:
                marker = str(value)
            digest.update(marker.encode("utf-8", errors="surrogatepass"))
            digest.update(b"\0")
        return digest.hexdigest()

    def _git_control_identity(self) -> dict[str, str]:
        """Hash Git control files that can change porcelain status without worktree IO.

        This is intentionally much cheaper than ``git status`` and is used only
        alongside an OS working-tree watcher.  The values are hashes/identifiers,
        never Git config contents, so credentials embedded in a remote URL cannot
        leak into plan results or telemetry.
        """

        def digest_file(path: Optional[Path]) -> str:
            if path is None:
                return "missing"
            try:
                if not path.is_file():
                    return "missing"
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    while True:
                        chunk = handle.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                return digest.hexdigest()
            except OSError:
                return "unreadable"

        git_directory = self._git_directory
        common_directory = self._common_git_directory
        values: dict[str, str] = {"revision": self._head_revision()}
        candidates = (
            ("index", git_directory / "index" if git_directory is not None else None),
            ("head", git_directory / "HEAD" if git_directory is not None else None),
            ("commondir", git_directory / "commondir" if git_directory is not None else None),
            (
                "worktree_config",
                git_directory / "config.worktree" if git_directory is not None else None,
            ),
            (
                "worktree_exclude",
                git_directory / "info" / "exclude" if git_directory is not None else None,
            ),
            (
                "worktree_sparse_checkout",
                git_directory / "info" / "sparse-checkout" if git_directory is not None else None,
            ),
            ("common_config", common_directory / "config" if common_directory is not None else None),
            (
                "common_packed_refs",
                common_directory / "packed-refs" if common_directory is not None else None,
            ),
            (
                "common_exclude",
                common_directory / "info" / "exclude" if common_directory is not None else None,
            ),
            (
                "common_sparse_checkout",
                common_directory / "info" / "sparse-checkout"
                if common_directory is not None
                else None,
            ),
        )
        for key, path in candidates:
            values[key] = digest_file(path)
        return values

    def _revision_identity(self) -> dict[str, Any]:
        """Return the strict per-path identity used by mutation safety checks."""
        if not self.is_git_repository:
            return self._non_git_identity()
        revision, status = self._status_snapshot(untracked_files="all")
        dirty: list[dict[str, str]] = []
        for _status_code, raw_path in _status_entries(status):
            relative = _safe_relative(self.repository, raw_path)
            if relative is None:
                continue
            if any(
                part.lower() in _IGNORED_STATUS_COMPONENTS
                or part.lower().endswith(".egg-info")
                for part in Path(relative).parts
            ):
                continue
            path = self.repository / relative
            try:
                digest = (
                    hashlib.sha256(path.read_bytes()).hexdigest()
                    if path.is_file()
                    else "missing"
                )
            except OSError:
                digest = "unreadable"
            dirty.append({"path": relative, "sha256": digest})
        dirty.sort(key=lambda item: item["path"])
        return {"revision": revision, "dirty": dirty}

    def _compact_content_revision_identity(self) -> dict[str, Any]:
        """Return content-aware identity with collapsed wholly-untracked trees."""
        if not self.is_git_repository:
            return self._non_git_identity()
        revision, status = self._status_snapshot(untracked_files="normal")
        dirty: list[dict[str, str]] = []
        for status_code, raw_path in _status_entries(status):
            relative = _safe_relative(self.repository, raw_path)
            if relative is None:
                continue
            if any(
                part.lower() in _IGNORED_STATUS_COMPONENTS
                or part.lower().endswith(".egg-info")
                for part in Path(relative).parts
            ):
                continue
            path = self.repository / relative
            dirty.append(
                {
                    "path": relative,
                    "sha256": self._content_tree_digest(
                        path,
                        status_code=status_code,
                        relative=relative,
                    ),
                }
            )
        dirty.sort(key=lambda item: item["path"])
        return {"revision": revision, "dirty": dirty}

    def _metadata_tree_digest(self, path: Path, *, status_code: str, relative: str) -> str:
        """Hash filesystem metadata for one dirty path without reading file bytes.

        Git's ``--untracked-files=normal`` collapses a wholly-untracked directory
        to one root entry. For that case we recursively hash only names, types,
        sizes and nanosecond mtimes. This preserves fast detection of ordinary
        edits/creates/deletes inside the untracked tree while avoiding both file
        content reads and the huge ``--untracked-files=all`` porcelain payload.
        Symlinks are never followed.
        """
        digest = hashlib.sha256()

        def update(marker: str) -> None:
            digest.update(marker.encode("utf-8", errors="surrogatepass"))
            digest.update(b"\0")

        try:
            metadata = path.stat(follow_symlinks=False)
        except OSError:
            update(f"{status_code}\0{relative}\0missing")
            return digest.hexdigest()

        if not path.is_dir() or path.is_symlink():
            kind = "symlink" if path.is_symlink() else "file"
            update(
                f"{status_code}\0{relative}\0{kind}\0"
                f"{metadata.st_size}\0{metadata.st_mtime_ns}"
            )
            return digest.hexdigest()

        root = path
        stack = [root]
        while stack:
            current = stack.pop()
            try:
                entries = sorted(os.scandir(current), key=lambda item: os.path.normcase(item.name))
            except OSError:
                try:
                    current_relative = current.relative_to(self.repository).as_posix()
                except ValueError:
                    current_relative = relative
                update(f"{status_code}\0{current_relative}\0unreadable")
                continue
            child_directories: list[Path] = []
            for entry in entries:
                child = Path(entry.path)
                try:
                    child_relative = child.relative_to(self.repository).as_posix()
                except ValueError:
                    continue
                try:
                    child_stat = entry.stat(follow_symlinks=False)
                    is_symlink = entry.is_symlink()
                    is_directory = entry.is_dir(follow_symlinks=False) and not is_symlink
                    kind = "symlink" if is_symlink else ("dir" if is_directory else "file")
                    update(
                        f"{status_code}\0{child_relative}\0{kind}\0"
                        f"{child_stat.st_size}\0{child_stat.st_mtime_ns}"
                    )
                    if is_directory:
                        child_directories.append(child)
                except OSError:
                    update(f"{status_code}\0{child_relative}\0unreadable")
            stack.extend(reversed(child_directories))
        return digest.hexdigest()

    def _fast_revision_identity(self) -> dict[str, Any]:
        """Return a compact metadata-based workspace-state identity.

        Tracked dirty paths remain individual entries. Wholly-untracked trees are
        represented by one root entry whose digest covers descendant metadata.
        This keeps read-only plan drift detection while avoiding recursive Git
        porcelain expansion and oversized plan artifacts.
        """
        if not self.is_git_repository:
            return self._non_git_identity()
        revision, status = self._status_snapshot(untracked_files="normal")
        work: list[tuple[str, str, Path]] = []
        for status_code, raw_path in _status_entries(status):
            relative = _safe_relative(self.repository, raw_path)
            if relative is None:
                continue
            if any(
                part.lower() in _IGNORED_STATUS_COMPONENTS
                or part.lower().endswith(".egg-info")
                for part in Path(relative).parts
            ):
                continue
            work.append((relative, status_code, self.repository / relative))

        def metadata_entry(item: tuple[str, str, Path]) -> dict[str, str]:
            relative, status_code, path = item
            return {
                "path": relative,
                "sha256": self._metadata_tree_digest(
                    path,
                    status_code=status_code,
                    relative=relative,
                ),
            }

        if len(work) >= 8:
            with ThreadPoolExecutor(max_workers=min(8, len(work))) as pool:
                dirty = list(pool.map(metadata_entry, work))
        else:
            dirty = [metadata_entry(item) for item in work]
        dirty.sort(key=lambda item: item["path"])
        return {"revision": revision, "dirty": dirty}

    def _cache_key(
        self,
        goal: str,
        depth: str,
        revision: Mapping[str, Any],
        *,
        include_dependency_hints: bool = False,
    ) -> str:
        payload = json.dumps(
            {
                "repository": str(self.repository),
                "revision": revision,
                "goal": goal,
                "depth": depth,
                "include_dependency_hints": bool(include_dependency_hints),
                "policy_profile": self.policy_profile,
                "schema_version": REPO_INSPECT_SCHEMA_VERSION,
                "ranking_version": 2,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _cache_get(self, key: str) -> Optional[dict[str, Any]]:
        path = self.cache_root / f"{key}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        artifact_id = payload.get("artifact_id")
        if not isinstance(artifact_id, str) or not self.artifacts.exists(artifact_id):
            return None
        payload["cache_hit"] = True
        return payload

    def _cache_put(self, key: str, payload: Mapping[str, Any]) -> None:
        try:
            _json_atomic(self.cache_root / f"{key}.json", payload)
        except OSError:
            # Context caching is an optimization. A transient filesystem error
            # must not make repository inspection itself fail.
            pass

    def _files(self, budget: InspectBudget) -> list[str]:
        if not self.is_git_repository:
            files: list[str] = []
            stack = [self.repository]
            max_candidates = max(1_000, budget.max_files * 50)
            considered = 0
            while stack and considered < max_candidates:
                current = stack.pop()
                try:
                    entries = sorted(
                        os.scandir(current), key=lambda item: os.path.normcase(item.name)
                    )
                except OSError:
                    continue
                child_directories: list[Path] = []
                for entry in entries:
                    if considered >= max_candidates:
                        break
                    child = Path(entry.path)
                    try:
                        relative = child.relative_to(self.repository).as_posix()
                        is_symlink = entry.is_symlink()
                        is_directory = entry.is_dir(follow_symlinks=False) and not is_symlink
                    except (OSError, ValueError):
                        continue
                    if any(
                        part.lower() in _IGNORED_STATUS_COMPONENTS
                        or part.lower().endswith(".egg-info")
                        for part in Path(relative).parts
                    ):
                        continue
                    considered += 1
                    if is_directory:
                        child_directories.append(child)
                        continue
                    if is_symlink or child.suffix.lower() not in _TEXT_SUFFIXES:
                        continue
                    try:
                        if entry.stat(follow_symlinks=False).st_size > budget.max_file_bytes:
                            continue
                    except OSError:
                        continue
                    files.append(relative)
                stack.extend(reversed(child_directories))
            return sorted(dict.fromkeys(files))
        listed = self._git("ls-files", "-co", "--exclude-standard")
        git_files: list[str] = []
        for raw in listed.splitlines():
            git_relative = _safe_relative(self.repository, raw)
            if git_relative is None:
                continue
            path = self.repository / git_relative
            if path.suffix.lower() not in _TEXT_SUFFIXES or not path.is_file():
                continue
            try:
                if path.stat().st_size > budget.max_file_bytes:
                    continue
            except OSError:
                continue
            git_files.append(git_relative)
        return sorted(dict.fromkeys(git_files))

    def _git_grep_search(
        self,
        tokens: tuple[str, ...],
        budget: InspectBudget,
    ) -> Optional[list[dict[str, Any]]]:
        """Use Git to shortlist token-matching files when ripgrep is unavailable."""
        if not self.is_git_repository:
            return None
        if not tokens:
            return []
        argv = [
            "git",
            "-C",
            str(self.repository),
            "grep",
            "--untracked",
            "-I",
            "-l",
            "-z",
            "--full-name",
            "-F",
            "-i",
        ]
        for token in tokens:
            argv.extend(("-e", token))
        argv.append("--")
        completed = _run(
            self.repository,
            argv,
            timeout=45,
            allow_failure=True,
        )
        if completed.returncode == 1:
            return []
        if completed.returncode != 0:
            return None
        raw_candidates = (
            completed.stdout.split("\x00")
            if "\x00" in completed.stdout
            else completed.stdout.splitlines()
        )
        expression = re.compile(
            "|".join(re.escape(token) for token in _content_search_tokens(tokens)),
            re.IGNORECASE,
        )
        matches: list[dict[str, Any]] = []
        seen: set[str] = set()
        per_file_limit = _per_file_match_limit(budget)
        raw_limit = _raw_match_limit(budget)
        identifier_tokens = _identifier_tokens(tokens)
        for raw in raw_candidates:
            relative = _safe_relative(self.repository, raw.strip())
            if relative is None or relative in seen:
                continue
            seen.add(relative)
            path = self.repository / relative
            file_matches = 0
            for line_number, line in enumerate(
                _read_lines(path, budget.max_file_bytes), start=1
            ):
                if not expression.search(line):
                    continue
                lowered_line = line.lower()
                identifier_hit = any(token in lowered_line for token in identifier_tokens)
                if file_matches >= per_file_limit and not identifier_hit:
                    continue
                matches.append(
                    {
                        "path": relative,
                        "line": line_number,
                        "text": str(redact(line))[:1000],
                    }
                )
                file_matches += 1
                if len(matches) >= raw_limit:
                    return _select_diverse_matches(matches, tokens, budget)
        return _select_diverse_matches(matches, tokens, budget)

    def _targeted_identifier_matches(
        self,
        matches: Iterable[Mapping[str, Any]],
        tokens: tuple[str, ...],
        budget: InspectBudget,
    ) -> list[dict[str, Any]]:
        """Recover late exact identifiers from already relevant files only.

        A second repository-wide search roughly doubled search latency on large
        Windows worktrees. The broad pass already tells us which files are
        semantically relevant, so exact snake/dotted/hyphen identifiers are
        refined inside a small ranked subset instead.
        """
        identifiers = _identifier_tokens(tokens)
        if not identifiers:
            return []
        groups: dict[str, dict[str, Any]] = {}
        for item in matches:
            path = str(item.get("path", ""))
            if not path:
                continue
            group = groups.setdefault(path, {"coverage": set(), "count": 0})
            group["count"] = int(group["count"]) + 1
            lowered = str(item.get("text", "")).lower()
            coverage = group["coverage"]
            if isinstance(coverage, set):
                coverage.update(token for token in tokens if token in lowered)

        source_roots = ("src/", "lib/", "app/", "pkg/", "cmd/", "internal/", "packages/")

        def priority(item: tuple[str, dict[str, Any]]) -> tuple[float, str]:
            path, data = item
            lowered = path.lower()
            coverage = data.get("coverage")
            coverage_count = len(coverage) if isinstance(coverage, set) else 0
            score = sum(token in lowered for token in tokens) * 50.0
            score += coverage_count * 18.0 + min(int(data.get("count", 0)), 8)
            if path.startswith(source_roots):
                score += 36.0
            elif path.startswith("tests/"):
                score += 28.0
            if Path(path).suffix.lower() in _SYMBOL_SUFFIXES:
                score += 8.0
            return (-score, path)

        limit = min(80, max(20, budget.max_files * 2))
        candidates = [path for path, _data in sorted(groups.items(), key=priority)[:limit]]
        existing = {
            (str(item.get("path", "")), int(item.get("line", 0) or 0))
            for item in matches
        }

        def scan(relative: str) -> list[dict[str, Any]]:
            found: list[dict[str, Any]] = []
            for line_number, line in enumerate(
                _read_lines(self.repository / relative, budget.max_file_bytes), start=1
            ):
                lowered = line.lower()
                if not any(token in lowered for token in identifiers):
                    continue
                if (relative, line_number) in existing:
                    continue
                found.append(
                    {
                        "path": relative,
                        "line": line_number,
                        "text": str(redact(line))[:1000],
                    }
                )
                if len(found) >= 4:
                    break
            return found

        refined: list[dict[str, Any]] = []
        workers = min(8, max(1, len(candidates)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for found in pool.map(scan, candidates):
                refined.extend(found)
        return refined

    def _ripgrep(
        self,
        tokens: tuple[str, ...],
        budget: InspectBudget,
    ) -> list[dict[str, Any]]:
        if not tokens:
            return []
        rg = shutil.which("rg")
        if rg is None:
            git_matches = self._git_grep_search(tokens, budget)
            if git_matches is not None:
                return git_matches
            return self._python_search(tokens, budget)
        expression = "|".join(re.escape(token) for token in _content_search_tokens(tokens))
        completed = _run(
            self.repository,
            [
                rg,
                "--json",
                "--ignore-case",
                "--line-number",
                "--no-heading",
                "--max-count",
                str(_per_file_match_limit(budget)),
                "--glob",
                "!.git/**",
                "-e",
                expression,
                ".",
            ],
            timeout=45,
            allow_failure=True,
        )
        matches: list[dict[str, Any]] = []
        per_file_limit = _per_file_match_limit(budget)
        raw_limit = _raw_match_limit(budget)
        identifier_tokens = _identifier_tokens(tokens)
        per_file_counts: dict[str, int] = {}
        for line in completed.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "match":
                continue
            data = event.get("data") or {}
            path_text = ((data.get("path") or {}).get("text"))
            relative = _safe_relative(self.repository, str(path_text or ""))
            if relative is None:
                continue
            line_number = data.get("line_number")
            text = ((data.get("lines") or {}).get("text"))
            if not isinstance(line_number, int) or not isinstance(text, str):
                continue
            lowered_text = text.lower()
            identifier_hit = any(token in lowered_text for token in identifier_tokens)
            if per_file_counts.get(relative, 0) >= per_file_limit and not identifier_hit:
                continue
            matches.append(
                {
                    "path": relative,
                    "line": line_number,
                    "text": str(redact(text.rstrip("\r\n")))[:1000],
                }
            )
            per_file_counts[relative] = per_file_counts.get(relative, 0) + 1
            if len(matches) >= raw_limit:
                break

        if identifier_tokens:
            matches.extend(self._targeted_identifier_matches(matches, tokens, budget))
        return _select_diverse_matches(matches, tokens, budget)

    def _python_search(
        self,
        tokens: tuple[str, ...],
        budget: InspectBudget,
    ) -> list[dict[str, Any]]:
        expression = re.compile(
            "|".join(re.escape(token) for token in _content_search_tokens(tokens)),
            re.IGNORECASE,
        )
        matches: list[dict[str, Any]] = []
        per_file_limit = _per_file_match_limit(budget)
        raw_limit = _raw_match_limit(budget)
        identifier_tokens = _identifier_tokens(tokens)
        for relative in self._files(budget):
            file_matches = 0
            for line_number, line in enumerate(
                _read_lines(self.repository / relative, budget.max_file_bytes),
                start=1,
            ):
                if expression.search(line):
                    lowered_line = line.lower()
                    identifier_hit = any(token in lowered_line for token in identifier_tokens)
                    if file_matches >= per_file_limit and not identifier_hit:
                        continue
                    matches.append(
                        {"path": relative, "line": line_number, "text": str(redact(line))[:1000]}
                    )
                    file_matches += 1
                    if len(matches) >= raw_limit:
                        return _select_diverse_matches(matches, tokens, budget)
        return _select_diverse_matches(matches, tokens, budget)

    def _symbol_candidate_files(
        self,
        files: Iterable[str],
        matches: Iterable[Mapping[str, Any]],
        tokens: tuple[str, ...],
        budget: InspectBudget,
    ) -> list[str]:
        """Choose code files for symbol extraction before doing any AST work.

        Search results can be dominated by prose files that repeat generic task
        words. Symbols only exist in code, so parsing the first alphabetic match
        list wasted the focused budget and often produced zero definitions.
        """

        file_set = set(files)
        counts: dict[str, int] = {}
        coverage: dict[str, set[str]] = {}
        for item in matches:
            path = str(item.get("path", ""))
            if path not in file_set or Path(path).suffix.lower() not in _SYMBOL_SUFFIXES:
                continue
            counts[path] = counts.get(path, 0) + 1
            text = str(item.get("text", "")).lower()
            coverage.setdefault(path, set()).update(
                token for token in tokens if token in text or token in path.lower()
            )

        candidates = list(counts)
        if not candidates:
            candidates = [
                path
                for path in file_set
                if Path(path).suffix.lower() in _SYMBOL_SUFFIXES
                and any(token in path.lower() for token in tokens)
            ]

        preferred_roots = ("src/", "lib/", "app/", "pkg/", "cmd/", "internal/", "packages/")

        def priority(path: str) -> tuple[float, str]:
            path_hits = _path_relevance(path, tokens)
            score = path_hits * 30 + len(coverage.get(path, ())) * 10
            score += min(counts.get(path, 0), 8) * 2
            if path.startswith(preferred_roots):
                score += 36
            return (-float(score), path)

        return sorted(candidates, key=priority)[: budget.max_files]

    def _python_symbols(self, files: Iterable[str], tokens: tuple[str, ...]) -> dict[str, Any]:
        wanted = {token.lower().split(".")[-1] for token in tokens}
        definitions: list[dict[str, Any]] = []
        calls: list[dict[str, Any]] = []
        imports: list[dict[str, Any]] = []
        types: list[dict[str, Any]] = []
        for relative in files:
            path = self.repository / relative
            if Path(relative).suffix.lower() not in {".py", ".pyi"}:
                for line_number, line in enumerate(_read_lines(path, 2_000_000), start=1):
                    for match in _GENERIC_SYMBOL.finditer(line):
                        name = match.group("name")
                        keyword = match.group("kind")
                        lowered_name = name.lower()
                        entry = {
                            "path": relative,
                            "line": line_number,
                            "name": name,
                            "kind": "type" if keyword in {"class", "interface", "type", "struct", "enum", "trait"} else "function",
                            "goal_match": (
                                not wanted
                                or lowered_name in wanted
                                or any(token in lowered_name for token in wanted)
                            ),
                        }
                        definitions.append(entry)
                        if entry["kind"] == "type":
                            types.append(entry)
                continue
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(source, filename=relative)
            except (OSError, SyntaxError, ValueError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    lowered_name = node.name.lower()
                    entry = {
                        "path": relative,
                        "line": int(getattr(node, "lineno", 0)),
                        "name": node.name,
                        "kind": "class" if isinstance(node, ast.ClassDef) else "function",
                        "goal_match": (
                            not wanted
                            or lowered_name in wanted
                            or any(token in lowered_name for token in wanted)
                        ),
                    }
                    definitions.append(entry)
                    if isinstance(node, ast.ClassDef):
                        types.append(entry)
                elif isinstance(node, ast.Call):
                    name = ""
                    if isinstance(node.func, ast.Name):
                        name = node.func.id
                    elif isinstance(node.func, ast.Attribute):
                        name = node.func.attr
                    if name:
                        calls.append(
                            {
                                "path": relative,
                                "line": int(getattr(node, "lineno", 0)),
                                "name": name,
                                "goal_match": not wanted or name.lower() in wanted,
                            }
                        )
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        imports.append({"path": relative, "module": alias.name, "line": node.lineno})
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    imports.append({"path": relative, "module": module, "line": node.lineno})
        return {
            "definitions": definitions[:500],
            "calls": calls[:1000],
            "types": types[:300],
            "imports": imports[:1500],
        }

    def _lightweight_imports(
        self, files: Iterable[str], *, max_file_bytes: int
    ) -> list[dict[str, Any]]:
        """Index Python imports cheaply, independent of goal-ranked AST files.

        The fast path is one ``git grep`` over import lines. That keeps reverse
        caller discovery without reopening hundreds of files from Python. A
        bounded threaded scanner remains as a portability fallback when Git grep
        is unavailable or rejects the query.
        """

        python_files = [
            path for path in files if Path(path).suffix.lower() in {".py", ".pyi"}
        ]
        if not python_files:
            return []
        allowed = set(python_files)
        from_pattern = re.compile(r"^\s*from\s+([.A-Za-z_][.A-Za-z0-9_]*)\s+import\b")
        import_pattern = re.compile(r"^\s*import\s+(.+)$")

        def parse(relative: str, line_number: int, line: str) -> list[dict[str, Any]]:
            if relative not in allowed:
                return []
            match = from_pattern.match(line)
            if match is not None:
                module = match.group(1).strip().strip(".")
                return (
                    [{"path": relative, "module": module, "line": line_number}]
                    if module
                    else []
                )
            match = import_pattern.match(line)
            if match is None:
                return []
            result: list[dict[str, Any]] = []
            for raw in match.group(1).split(","):
                module = raw.strip().split()[0].strip().strip(".") if raw.strip() else ""
                if module:
                    result.append(
                        {"path": relative, "module": module, "line": line_number}
                    )
            return result

        collected: list[dict[str, Any]] = []
        indexed = False
        rg = shutil.which("rg")
        if rg is not None:
            completed = _run(
                self.repository,
                [
                    rg,
                    "--json",
                    "--line-number",
                    "--no-heading",
                    "--glob",
                    "*.py",
                    "--glob",
                    "*.pyi",
                    "-e",
                    r"^\s*(from\s+\S+\s+import|import\s+)",
                    ".",
                ],
                timeout=20.0,
                allow_failure=True,
            )
            if completed.returncode in {0, 1}:
                indexed = True
                for raw in completed.stdout.splitlines():
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") != "match":
                        continue
                    data = event.get("data") or {}
                    path_text = ((data.get("path") or {}).get("text"))
                    relative = _safe_relative(self.repository, str(path_text or ""))
                    line_number = data.get("line_number")
                    line = ((data.get("lines") or {}).get("text"))
                    if (
                        relative is None
                        or not isinstance(line_number, int)
                        or not isinstance(line, str)
                    ):
                        continue
                    collected.extend(parse(relative, line_number, line.rstrip("\r\n")))
                    if len(collected) >= 5_000:
                        break

        if not indexed:
            completed = _run(
                self.repository,
                [
                    "git",
                    "-C",
                    str(self.repository),
                    "grep",
                    "--untracked",
                    "-I",
                    "-n",
                    "-E",
                    r"^[[:space:]]*(from[[:space:]]+[^[:space:]]+[[:space:]]+import|import[[:space:]]+)",
                    "--",
                    "*.py",
                    "*.pyi",
                ],
                timeout=20.0,
                allow_failure=True,
            )
            if completed.returncode in {0, 1}:
                indexed = True
                for raw in completed.stdout.splitlines():
                    relative, separator, rest = raw.partition(":")
                    if not separator:
                        continue
                    raw_line, separator, line = rest.partition(":")
                    if not separator:
                        continue
                    try:
                        line_number = int(raw_line)
                    except ValueError:
                        continue
                    collected.extend(parse(relative, line_number, line))
                    if len(collected) >= 5_000:
                        break

        if not indexed:
            def scan(relative: str) -> list[dict[str, Any]]:
                entries: list[dict[str, Any]] = []
                for line_number, line in enumerate(
                    _read_lines(self.repository / relative, max_file_bytes), start=1
                ):
                    entries.extend(parse(relative, line_number, line))
                return entries

            workers = min(8, max(1, len(python_files)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for entries in pool.map(scan, python_files):
                    collected.extend(entries)
                    if len(collected) >= 5_000:
                        break

        deduplicated: list[dict[str, Any]] = []
        seen: set[tuple[str, str, int]] = set()
        for item in collected:
            import_key = (
                str(item["path"]),
                str(item["module"]),
                int(item["line"]),
            )
            if import_key in seen:
                continue
            seen.add(import_key)
            deduplicated.append(item)
            if len(deduplicated) >= 5_000:
                break
        return deduplicated

    def _resolve_import_target(self, importer: str, module: str) -> Optional[str]:
        """Resolve a Python import to a repository file when it is local.

        AST ``ImportFrom`` records intentionally stay lightweight and do not keep
        the relative-import level, so sibling resolution is tried before the
        common source roots. Missing/external modules simply produce no edge.
        """
        cleaned = module.strip().strip(".")
        if not cleaned:
            return None
        module_parts = tuple(part for part in cleaned.split(".") if part)
        if not module_parts:
            return None
        importer_parent = (self.repository / importer).parent
        relative = Path(*module_parts)
        candidates = [
            importer_parent / relative.with_suffix(".py"),
            importer_parent / relative / "__init__.py",
            self.repository / relative.with_suffix(".py"),
            self.repository / relative / "__init__.py",
        ]
        for source_root in ("src", "lib", "app", "pkg", "packages"):
            candidates.extend(
                [
                    self.repository / source_root / relative.with_suffix(".py"),
                    self.repository / source_root / relative / "__init__.py",
                ]
            )
        for candidate in candidates:
            try:
                if not candidate.is_file():
                    continue
                return candidate.resolve().relative_to(self.repository).as_posix()
            except (OSError, ValueError):
                continue
        return None

    def _dependency_hints(
        self,
        imports: Iterable[Mapping[str, Any]],
        root_paths: Iterable[str],
    ) -> dict[str, Any]:
        """Return one-hop local dependency/caller paths around ranked roots."""
        roots = {str(path) for path in root_paths if isinstance(path, str) and path}
        implementation: list[str] = []
        tests: list[str] = []
        edges = 0

        def add(path: str) -> None:
            if path in roots:
                return
            target = tests if path.startswith("tests/") else implementation
            if path not in target:
                target.append(path)

        for item in imports:
            importer = str(item.get("path", ""))
            module = str(item.get("module", ""))
            if not importer or not module:
                continue
            target = self._resolve_import_target(importer, module)
            if target is None:
                continue
            if importer in roots:
                add(target)
                edges += 1
            if target in roots:
                add(importer)
                edges += 1
        return {
            "implementation": implementation[:12],
            "tests": tests[:8],
            "edge_count": edges,
        }

    def _rank_files(
        self,
        files: Iterable[str],
        matches: Iterable[Mapping[str, Any]],
        symbols: Mapping[str, Any],
        tokens: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        scores: dict[str, float] = {path: 0.0 for path in files}
        reasons: dict[str, list[str]] = {path: [] for path in files}
        for path in files:
            path_hits = _path_relevance(path, tokens)
            if path_hits:
                scores[path] += path_hits * 20
                reasons[path].append(f"path-token-hits={path_hits}")
            if path.startswith("tests/") or "/test_" in path or path.endswith("_test.py"):
                scores[path] += 1.5
            # invalid typographic literal preserved in comment: path.startswith("docs/") or path.lower().endswith(“.md"):
            if path.startswith("docs/") or path.lower().endswith(".md"):
                scores[path] += 1.0
        match_counts: dict[str, int] = {}
        token_coverage: dict[str, set[str]] = {}
        identifier_tokens = tuple(
            token for token in tokens if any(marker in token for marker in ("_", ".", "-"))
        )
        identifier_coverage: dict[str, set[str]] = {}
        for item in matches:
            path = str(item.get("path", ""))
            if path in scores:
                match_counts[path] = match_counts.get(path, 0) + 1
                lowered_text = str(item.get("text", "")).lower()
                token_coverage.setdefault(path, set()).update(
                    token for token in tokens if token in lowered_text
                )
                identifier_coverage.setdefault(path, set()).update(
                    token for token in identifier_tokens if token in lowered_text
                )
        for path, count in match_counts.items():
            scores[path] += min(count, 8) * 1.5
            coverage = len(token_coverage.get(path, ()))
            identifier_hits = len(identifier_coverage.get(path, ()))
            scores[path] += coverage * 8
            scores[path] += identifier_hits * 36
            reasons[path].append(f"content-matches={count}")
            if coverage:
                reasons[path].append(f"goal-token-coverage={coverage}")
            if identifier_hits:
                reasons[path].append(f"identifier-token-coverage={identifier_hits}")
        for key, weight in (("definitions", 12), ("calls", 5), ("types", 8)):
            matched_per_file: dict[str, int] = {}
            for item in symbols.get(key, []):
                path = str(item.get("path", ""))
                if path in scores and item.get("goal_match") is True:
                    matched_per_file[path] = matched_per_file.get(path, 0) + 1
            for path, count in matched_per_file.items():
                scores[path] += min(count, 4) * weight
                reasons[path].append(f"{key}={count}")
        for path, score in scores.items():
            if score > 0 and path.startswith(
                ("src/", "lib/", "app/", "pkg/", "cmd/", "internal/", "packages/")
            ):
                scores[path] += 36
                reasons[path].append("primary-source")
        ranked: list[dict[str, Any]] = [
            {"path": path, "score": round(score, 3), "reasons": sorted(set(reasons[path]))}
            for path, score in scores.items()
            if score > 0
        ]
        ranked.sort(key=lambda item: (-float(item["score"]), str(item["path"])))
        return ranked

    def _excerpts(
        self,
        matches: list[dict[str, Any]],
        ranked: list[dict[str, Any]],
        budget: InspectBudget,
    ) -> list[dict[str, Any]]:
        rank_order = {str(item["path"]): index for index, item in enumerate(ranked)}
        ordered = sorted(
            matches,
            key=lambda item: (
                rank_order.get(str(item["path"]), 999999),
                int(item["line"]),
            ),
        )
        excerpts: list[dict[str, Any]] = []
        file_lines: dict[str, list[str]] = {}
        emitted_end: dict[str, int] = {}
        window = budget.context_lines * 2 + 1
        remaining_lines = budget.max_excerpts * window
        for match in ordered:
            path = str(match["path"])
            line_number = int(match["line"])
            if path not in file_lines:
                file_lines[path] = _read_lines(self.repository / path, budget.max_file_bytes)
            lines = file_lines[path]
            if not lines or not 1 <= line_number <= len(lines):
                continue
            start = max(1, line_number - budget.context_lines, emitted_end.get(path, 0) + 1)
            end = min(len(lines), line_number + budget.context_lines)
            if start > end:
                continue
            end = min(end, start + remaining_lines - 1)
            if end < line_number and line_number > emitted_end.get(path, 0):
                # The line budget cannot reach the match that justified this
                # excerpt: emitting the window anyway would spend context on a
                # reason the reader never gets to see, and nothing in the result
                # would say the line was cut. Skip it and let a later match with
                # a shorter window use what is left.
                continue
            previous = excerpts[-1] if excerpts else None
            merge = (previous is not None and previous["path"] == path
                     and previous["end"] + 1 == start
                     and end - previous["start"] + 1 <= window * 4)
            if not merge and len(excerpts) >= budget.max_excerpts:
                break
            content = [
                {"line": index, "text": str(redact(lines[index - 1]))[:2000]}
                for index in range(start, end + 1)
            ]
            if merge and previous is not None:
                previous["end"] = end
                previous["lines"].extend(content)
            else:
                excerpts.append({"path": path, "start": start, "end": end, "lines": content})
            emitted_end[path] = end
            remaining_lines -= len(content)
            if remaining_lines <= 0:
                break
        return excerpts

    def inspect(
        self,
        goal: str,
        depth: str = "focused",
        *,
        include_dependency_hints: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 10_000:
            raise ValueError("repo.inspect goal must be a non-empty string up to 10000 characters")
        if depth not in _BUDGETS:
            raise ValueError("repo.inspect depth must be focused, standard, or deep")
        budget = _BUDGETS[depth]
        normalized_goal = goal.strip()
        revision = self._compact_content_revision_identity()
        key = self._cache_key(
            normalized_goal,
            depth,
            revision,
            include_dependency_hints=include_dependency_hints,
        )
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        started = time.perf_counter()
        tokens = _tokens(normalized_goal)
        files_started = time.perf_counter()
        files = self._files(budget)
        files_ms = (time.perf_counter() - files_started) * 1000
        search_started = time.perf_counter()
        matches = self._ripgrep(tokens, budget)
        search_ms = (time.perf_counter() - search_started) * 1000
        candidate_files = self._symbol_candidate_files(files, matches, tokens, budget)
        if not candidate_files:
            candidate_files = [
                path
                for path in files
                if any(token in path.lower() for token in tokens)
            ][: budget.max_files]
        symbols_started = time.perf_counter()
        symbols = self._python_symbols(candidate_files, tokens)
        # Ordinary inspection stays fast. A broad caller/dependency index is
        # built only when an explicit recursive/research experiment asks for it;
        # the default project map must not pay several seconds for unused edges.
        if include_dependency_hints:
            import_edges = self._lightweight_imports(
                files, max_file_bytes=budget.max_file_bytes
            )
            merged_imports: list[dict[str, Any]] = []
            seen_imports: set[tuple[str, str, int]] = set()
            for item in [*symbols.get("imports", []), *import_edges]:
                path = str(item.get("path", ""))
                module = str(item.get("module", ""))
                line = int(item.get("line", 0) or 0)
                import_key = (path, module, line)
                if not path or not module or import_key in seen_imports:
                    continue
                seen_imports.add(import_key)
                merged_imports.append({"path": path, "module": module, "line": line})
                if len(merged_imports) >= 5_000:
                    break
            symbols["imports"] = merged_imports
        symbols_ms = (time.perf_counter() - symbols_started) * 1000
        rank_started = time.perf_counter()
        ranked = self._rank_files(files, matches, symbols, tokens)[: budget.max_files]
        excerpts = self._excerpts(matches, ranked, budget)
        tests = [item for item in ranked if str(item["path"]).startswith("tests/")][:20]
        docs = [item for item in ranked if str(item["path"]).startswith("docs/")][:20]
        preferred_source_roots = (
            "src/", "lib/", "app/", "pkg/", "cmd/", "internal/", "packages/"
        )
        change_candidates = [
            item
            for item in ranked
            if not str(item["path"]).startswith(("tests/", "docs/", "benchmarks/"))
            and not str(item["path"]).lower().endswith((".md", ".rst", ".txt"))
        ]
        change_points = sorted(
            change_candidates,
            key=lambda item: (
                0 if str(item["path"]).startswith(preferred_source_roots) else 1,
                -float(item["score"]),
                str(item["path"]),
            ),
        )[:20]
        dependency_hints = self._dependency_hints(
            symbols["imports"],
            (str(item["path"]) for item in change_points[:6]),
        )
        rank_ms = (time.perf_counter() - rank_started) * 1000
        full: dict[str, Any] = {
            "schema_version": REPO_INSPECT_SCHEMA_VERSION,
            "goal": normalized_goal,
            "depth": depth,
            "tokens": tokens,
            "repository_revision": revision,
            "ranked_files": ranked,
            "matches": matches,
            "symbols": symbols,
            "dependency_map": {
                "imports": symbols["imports"],
                "calls": symbols["calls"],
            },
            "dependency_hints": dependency_hints,
            "tests": tests,
            "docs": docs,
            "likely_change_points": change_points,
            "excerpts": excerpts,
            "metrics": {
                "files_considered": len(files),
                "candidate_files": len(candidate_files),
                "matches": len(matches),
                "files_ms": round(files_ms, 3),
                "search_ms": round(search_ms, 3),
                "symbols_ms": round(symbols_ms, 3),
                "rank_ms": round(rank_ms, 3),
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            },
        }
        encoded = json.dumps(redact(full), ensure_ascii=False, sort_keys=True).encode("utf-8")
        record = self.artifacts.put(
            encoded,
            name="repo-inspect.json",
            mime="application/json",
        )
        result = {
            "ok": True,
            "schema_version": REPO_INSPECT_SCHEMA_VERSION,
            "goal": normalized_goal,
            "depth": depth,
            "cache_hit": False,
            "summary": {
                "files_considered": len(files),
                "matches": len(matches),
                "definitions": len(symbols["definitions"]),
                "callers": len(symbols["calls"]),
                "tests": len(tests),
                "docs": len(docs),
                "duration_ms": full["metrics"]["duration_ms"],
            },
            "important_findings": ranked[:12],
            "likely_change_points": change_points[:10],
            "relevant_tests": tests[:10],
            "relevant_docs": docs[:10],
            "dependency_hints": dependency_hints,
            "excerpts": excerpts[:10],
            "artifact_id": record.artifact_id,
            "available_sections": [
                "ranked_files",
                "matches",
                "symbols.definitions",
                "symbols.calls",
                "symbols.types",
                "dependency_map",
                "dependency_hints",
                "tests",
                "docs",
                "likely_change_points",
                "excerpts",
                "metrics",
            ],
            "content_hash": record.sha256,
            "total_size": record.size,
            "persistence_policy": record.persistence_policy,
            "expires_at": record.expires_at,
        }
        self._cache_put(key, result)
        return result
