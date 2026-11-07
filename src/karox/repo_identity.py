"""Repository revision identity and the bounded context cache."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Mapping, Optional
from .repo_support import (  # noqa: F401
    REPO_INSPECT_SCHEMA_VERSION as REPO_INSPECT_SCHEMA_VERSION,
    _TEXT_SUFFIXES as _TEXT_SUFFIXES,
    _SYMBOL_SUFFIXES as _SYMBOL_SUFFIXES,
    _IGNORED_STATUS_COMPONENTS as _IGNORED_STATUS_COMPONENTS,
    _IGNORED_STATUS_PREFIXES as _IGNORED_STATUS_PREFIXES,
    _ignored_component as _ignored_component,
    _GENERIC_SYMBOL as _GENERIC_SYMBOL,
    _STOPWORDS as _STOPWORDS,
    InspectBudget as InspectBudget,
    _BUDGETS as _BUDGETS,
    _EDIT_CONTEXT_MAX_BYTES as _EDIT_CONTEXT_MAX_BYTES,
    _EDIT_CONTEXT_MAX_LINES as _EDIT_CONTEXT_MAX_LINES,
    _per_file_match_limit as _per_file_match_limit,
    _identifier_tokens as _identifier_tokens,
    _path_relevance as _path_relevance,
    _content_search_tokens as _content_search_tokens,
    _raw_match_limit as _raw_match_limit,
    _select_diverse_matches as _select_diverse_matches,
    _run as _run,
    _tokens as _tokens,
    _safe_relative as _safe_relative,
    _status_entries as _status_entries,
    _read_lines as _read_lines,
    _json_atomic as _json_atomic,
)


class RepoIdentityMixin:
    """Methods composed into RepositoryContextEngine; see repo_context.py."""

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
                    _ignored_component(part)
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
                _ignored_component(part)
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
                _ignored_component(part)
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
                _ignored_component(part)
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
                _ignored_component(part)
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
