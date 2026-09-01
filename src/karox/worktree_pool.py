"""Guarded Git worktree isolation for parallel KaroX workers.

Worktrees live only below the KaroX runtime directory and are created detached
from HEAD.  This module never pushes, merges, rebases or rewrites history.  A
worktree containing changes cannot be removed automatically.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .paths import runtime_dir
from .sessions import _exclusive_file_lock


_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class WorktreePoolError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class WorkerWorktree:
    run_id: str
    worker_id: str
    path: Path
    base_revision: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "worker_id": self.worker_id,
            "path": str(self.path),
            "base_revision": self.base_revision,
        }


@dataclasses.dataclass(frozen=True)
class WorktreeStatus:
    worktree: WorkerWorktree
    changed_files: tuple[str, ...]
    clean: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "worktree": self.worktree.to_dict(),
            "changed_files": list(self.changed_files),
            "clean": self.clean,
        }


@dataclasses.dataclass(frozen=True)
class OverlapReport:
    workers: tuple[str, ...]
    changed_by_worker: dict[str, tuple[str, ...]]
    overlapping_files: dict[str, tuple[str, ...]]

    @property
    def merge_safe_by_path(self) -> bool:
        return not self.overlapping_files

    def to_dict(self) -> dict[str, Any]:
        return {
            "workers": list(self.workers),
            "changed_by_worker": {key: list(value) for key, value in self.changed_by_worker.items()},
            "overlapping_files": {key: list(value) for key, value in self.overlapping_files.items()},
            "merge_safe_by_path": self.merge_safe_by_path,
        }


class WorktreePool:
    def __init__(
        self,
        repository: Path,
        *,
        root: Optional[Path] = None,
        runner: Optional[Callable[..., subprocess.CompletedProcess[str]]] = None,
    ) -> None:
        self.repository = repository.expanduser().resolve(strict=True)
        if not (self.repository / ".git").exists():
            raise WorktreePoolError("worktree pool requires a Git repository")
        fingerprint = hashlib.sha256(os.path.normcase(str(self.repository)).encode("utf-8")).hexdigest()[:16]
        self.root = (root or (runtime_dir() / "vnext" / "worktrees" / fingerprint)).expanduser().resolve()
        self._lock_path = self.root / ".pool.lock"
        self._runner = runner or subprocess.run

    def _run(self, args: list[str], *, cwd: Optional[Path] = None) -> str:
        try:
            completed = self._runner(
                args,
                cwd=str(cwd or self.repository),
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=60,
                creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise WorktreePoolError(f"Git worktree command failed to start: {exc}") from exc
        if completed.returncode != 0:
            error = (completed.stderr or completed.stdout or "git command failed").strip()
            raise WorktreePoolError(error[:2000])
        return completed.stdout

    @staticmethod
    def _validate_id(value: str, label: str) -> str:
        if not isinstance(value, str) or _SAFE.fullmatch(value) is None:
            raise ValueError(f"{label} must contain 1-64 safe characters")
        return value

    def _path(self, run_id: str, worker_id: str) -> Path:
        run_id = self._validate_id(run_id, "run id")
        worker_id = self._validate_id(worker_id, "worker id")
        candidate = (self.root / run_id / worker_id).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise WorktreePoolError("worktree path escaped the KaroX runtime root") from exc
        return candidate

    def _metadata_path(self, run_id: str, worker_id: str) -> Path:
        run_id = self._validate_id(run_id, "run id")
        worker_id = self._validate_id(worker_id, "worker id")
        return self.root / ".metadata" / run_id / f"{worker_id}.base"

    def _write_base_revision_unlocked(
        self, run_id: str, worker_id: str, revision: str
    ) -> None:
        if not re.fullmatch(r"[0-9a-f]{40,64}", revision):
            raise WorktreePoolError("worker base revision is invalid")
        path = self._metadata_path(run_id, worker_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(revision + "\n", encoding="ascii")

    def _read_base_revision_unlocked(
        self, run_id: str, worker_id: str
    ) -> Optional[str]:
        try:
            value = self._metadata_path(run_id, worker_id).read_text(
                encoding="ascii"
            ).strip().lower()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise WorktreePoolError("worker worktree metadata is unreadable") from exc
        if not re.fullmatch(r"[0-9a-f]{40,64}", value):
            raise WorktreePoolError("worker worktree metadata is malformed")
        return value

    def head_revision(self) -> str:
        value = self._run(["git", "rev-parse", "HEAD"]).strip()
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", value):
            raise WorktreePoolError("Git returned an invalid HEAD revision")
        return value.lower()

    def _create_unlocked(
        self, *, run_id: str, worker_id: str, revision: Optional[str] = None
    ) -> WorkerWorktree:
        path = self._path(run_id, worker_id)
        if path.exists():
            raise WorktreePoolError(f"worker worktree already exists: {path}")
        revision = revision or self.head_revision()
        if not re.fullmatch(r"[0-9a-fA-F]{7,64}", revision):
            raise ValueError("worktree revision must be a Git object id")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._run(["git", "worktree", "add", "--detach", str(path), revision])
        base_revision = self._run(
            ["git", "rev-parse", "HEAD"], cwd=path
        ).strip().lower()
        if not re.fullmatch(r"[0-9a-f]{40,64}", base_revision):
            raise WorktreePoolError("worker worktree returned an invalid base revision")
        try:
            self._write_base_revision_unlocked(run_id, worker_id, base_revision)
        except Exception:
            # The worktree is still pristine at this point. Do not leave an
            # untracked pool entry whose original base cannot be recovered.
            self._run(["git", "worktree", "remove", str(path)])
            raise
        return WorkerWorktree(
            run_id=run_id,
            worker_id=worker_id,
            path=path,
            base_revision=base_revision,
        )

    def create(
        self, *, run_id: str, worker_id: str, revision: Optional[str] = None
    ) -> WorkerWorktree:
        """Create one worker worktree under a cross-process pool fence."""
        with _exclusive_file_lock(self._lock_path):
            return self._create_unlocked(
                run_id=run_id,
                worker_id=worker_id,
                revision=revision,
            )

    def _registered_worktree_paths(self) -> set[Path]:
        output = self._run(["git", "worktree", "list", "--porcelain"])
        paths: set[Path] = set()
        for line in output.splitlines():
            if not line.startswith("worktree "):
                continue
            raw = line[len("worktree ") :].strip()
            if raw:
                paths.add(Path(raw).expanduser().resolve())
        return paths

    def _open_unlocked(self, *, run_id: str, worker_id: str) -> WorkerWorktree:
        path = self._path(run_id, worker_id)
        if not path.is_dir():
            raise WorktreePoolError(f"worker worktree does not exist: {path}")
        if path not in self._registered_worktree_paths():
            raise WorktreePoolError(
                "refusing to adopt a directory Git does not report as a worktree"
            )
        current_revision = self._run(
            ["git", "rev-parse", "HEAD"], cwd=path
        ).strip().lower()
        if not re.fullmatch(r"[0-9a-f]{40,64}", current_revision):
            raise WorktreePoolError("worker worktree returned an invalid HEAD revision")
        # New worktrees persist their immutable fork point outside the worktree.
        # Legacy worktrees may not have metadata; for them HEAD is the safest
        # conservative fallback and is never written back automatically.
        base_revision = self._read_base_revision_unlocked(run_id, worker_id)
        if base_revision is None:
            base_revision = current_revision
        return WorkerWorktree(
            run_id=run_id,
            worker_id=worker_id,
            path=path,
            base_revision=base_revision,
        )

    def open(self, *, run_id: str, worker_id: str) -> WorkerWorktree:
        """Re-adopt one existing KaroX-owned worktree after process restart."""
        with _exclusive_file_lock(self._lock_path):
            return self._open_unlocked(run_id=run_id, worker_id=worker_id)

    def _ensure_unlocked(
        self, *, run_id: str, worker_id: str, revision: Optional[str] = None
    ) -> WorkerWorktree:
        path = self._path(run_id, worker_id)
        if path.exists():
            worktree = self._open_unlocked(run_id=run_id, worker_id=worker_id)
            if revision is not None and worktree.base_revision != revision.lower():
                raise WorktreePoolError(
                    "existing worker worktree is based on a different revision"
                )
            return worktree
        return self._create_unlocked(
            run_id=run_id,
            worker_id=worker_id,
            revision=revision,
        )

    def ensure(
        self, *, run_id: str, worker_id: str, revision: Optional[str] = None
    ) -> WorkerWorktree:
        """Create a worker worktree once, or safely re-adopt it after restart."""
        with _exclusive_file_lock(self._lock_path):
            return self._ensure_unlocked(
                run_id=run_id,
                worker_id=worker_id,
                revision=revision,
            )

    def _worktree_for_path_unlocked(self, path: Path) -> WorkerWorktree:
        resolved = path.expanduser().resolve(strict=True)
        try:
            relative = resolved.relative_to(self.root)
        except ValueError as exc:
            raise WorktreePoolError("worktree path is outside this KaroX pool") from exc
        if len(relative.parts) != 2:
            raise WorktreePoolError("worktree path has an invalid KaroX pool layout")
        run_id, worker_id = relative.parts
        return self._open_unlocked(run_id=run_id, worker_id=worker_id)

    def worktree_for_path(self, path: Path) -> WorkerWorktree:
        """Resolve a KaroX-owned registered path back to its worktree identity."""
        with _exclusive_file_lock(self._lock_path):
            return self._worktree_for_path_unlocked(path)

    def list_statuses(self, *, run_id: Optional[str] = None) -> tuple[WorktreeStatus, ...]:
        """List only Git-registered worktrees inside this repository's KaroX root."""

        wanted_run = self._validate_id(run_id, "run id") if run_id else None
        rows: list[WorktreeStatus] = []
        with _exclusive_file_lock(self._lock_path):
            for path in sorted(self._registered_worktree_paths(), key=str):
                try:
                    relative = path.relative_to(self.root)
                except ValueError:
                    continue
                if len(relative.parts) != 2:
                    continue
                candidate_run, worker_id = relative.parts
                try:
                    candidate_run = self._validate_id(candidate_run, "run id")
                    worker_id = self._validate_id(worker_id, "worker id")
                except ValueError:
                    continue
                if wanted_run is not None and candidate_run != wanted_run:
                    continue
                try:
                    worktree = self._open_unlocked(
                        run_id=candidate_run,
                        worker_id=worker_id,
                    )
                    rows.append(self._status_unlocked(worktree))
                except (OSError, WorktreePoolError):
                    continue
        return tuple(rows)

    def _status_unlocked(self, worktree: WorkerWorktree) -> WorktreeStatus:
        path = worktree.path.expanduser().resolve(strict=True)
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise WorktreePoolError(
                "refusing to inspect a worktree outside KaroX runtime"
            ) from exc
        # Compare the final filesystem state to the immutable fork point, not
        # merely to the current HEAD. This includes worker-local commits as well
        # as staged/unstaged changes. Disabling rename detection deliberately
        # exposes a rename as delete+add, which is exactly what composition needs.
        tracked = self._run(
            [
                "git",
                "diff",
                "--name-only",
                "-z",
                "--no-renames",
                worktree.base_revision,
                "--",
            ],
            cwd=path,
        )
        untracked = self._run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=path,
        )
        changed = list(
            dict.fromkeys(
                item
                for item in (*tracked.split("\x00"), *untracked.split("\x00"))
                if item
            )
        )
        return WorktreeStatus(
            worktree=worktree,
            changed_files=tuple(sorted(changed)),
            clean=not changed,
        )

    def status(self, worktree: WorkerWorktree) -> WorktreeStatus:
        with _exclusive_file_lock(self._lock_path):
            return self._status_unlocked(worktree)

    @staticmethod
    def _relative_change_path(raw: str) -> Path:
        path = Path(raw)
        if path.is_absolute() or not path.parts or any(part == ".." for part in path.parts):
            raise WorktreePoolError("worktree change escaped its repository")
        return path

    @staticmethod
    def _remove_path(path: Path) -> None:
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path)

    def _change_signature(self, worktree: WorkerWorktree, raw: str) -> tuple[str, str]:
        relative = self._relative_change_path(raw)
        path = worktree.path / relative
        if path.is_symlink():
            return "symlink", os.readlink(path)
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            executable = "1" if path.stat().st_mode & 0o111 else "0"
            return "file", f"{digest}:{executable}"
        if path.exists():
            return "directory", ""
        return "absent", ""

    def _copy_change(
        self,
        source: WorkerWorktree,
        target: WorkerWorktree,
        raw: str,
    ) -> None:
        relative = self._relative_change_path(raw)
        source_path = source.path / relative
        target_path = target.path / relative
        if source_path.is_symlink():
            self._remove_path(target_path)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(os.readlink(source_path), target_path)
            return
        if source_path.is_file():
            self._remove_path(target_path)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target_path)
            return
        if source_path.exists():
            raise WorktreePoolError(
                f"cannot auto-compose directory or submodule change: {raw}"
            )
        self._remove_path(target_path)

    def _reset_owned_worktree(self, worktree: WorkerWorktree) -> None:
        self._run(["git", "reset", "--hard", worktree.base_revision], cwd=worktree.path)
        self._run(["git", "clean", "-fd"], cwd=worktree.path)

    def compose(
        self,
        *,
        run_id: str,
        worker_id: str,
        sources: Iterable[WorkerWorktree],
    ) -> WorkerWorktree:
        """Compose compatible worker changes into a new isolated worktree.

        Sources must belong to one run and one base revision. Overlapping paths
        are accepted only when all sources have the exact same resulting content
        (or the same deletion). Divergent overlap fails closed. The primary user
        checkout is never modified.
        """
        rows = tuple(sources)
        if not rows:
            raise WorktreePoolError("composition requires at least one source worktree")
        with _exclusive_file_lock(self._lock_path):
            canonical: list[WorkerWorktree] = []
            seen_paths: set[Path] = set()
            for source in rows:
                current = self._worktree_for_path_unlocked(source.path)
                if current.run_id != run_id:
                    raise WorktreePoolError("cannot compose worktrees from another run")
                if current.path in seen_paths:
                    continue
                seen_paths.add(current.path)
                canonical.append(current)

            base_revisions = {item.base_revision for item in canonical}
            if len(base_revisions) != 1:
                raise WorktreePoolError(
                    "cannot auto-compose worktrees based on different revisions"
                )
            base_revision = next(iter(base_revisions))

            desired: dict[str, tuple[tuple[str, str], WorkerWorktree]] = {}
            conflicts: dict[str, list[str]] = {}
            for source in canonical:
                status = self._status_unlocked(source)
                for raw in status.changed_files:
                    signature = self._change_signature(source, raw)
                    prior = desired.get(raw)
                    if prior is None:
                        desired[raw] = (signature, source)
                        continue
                    if prior[0] != signature:
                        conflicts.setdefault(raw, [prior[1].worker_id]).append(
                            source.worker_id
                        )
            if conflicts:
                details = ", ".join(
                    f"{path} ({', '.join(sorted(set(owners)))})"
                    for path, owners in sorted(conflicts.items())
                )
                raise WorktreePoolError(
                    "parallel worktrees contain conflicting changes: " + details
                )

            target = self._ensure_unlocked(
                run_id=run_id,
                worker_id=worker_id,
                revision=base_revision,
            )
            if any(target.path == item.path for item in canonical):
                raise WorktreePoolError("composition target must differ from its sources")

            wanted = {raw: value[0] for raw, value in desired.items()}
            target_status = self._status_unlocked(target)
            if not target_status.clean:
                existing = {
                    raw: self._change_signature(target, raw)
                    for raw in target_status.changed_files
                }
                if existing == wanted:
                    return target
                raise WorktreePoolError(
                    "composition target already contains divergent worker changes"
                )

            try:
                for raw, (_signature, source) in sorted(desired.items()):
                    self._copy_change(source, target, raw)
                composed_status = self._status_unlocked(target)
                existing = {
                    raw: self._change_signature(target, raw)
                    for raw in composed_status.changed_files
                }
                if existing != wanted:
                    raise WorktreePoolError(
                        "composed worktree did not reproduce the source changes exactly"
                    )
            except Exception:
                self._reset_owned_worktree(target)
                raise
            return target

    def remove(self, worktree: WorkerWorktree) -> None:
        with _exclusive_file_lock(self._lock_path):
            status = self._status_unlocked(worktree)
            if not status.clean:
                raise WorktreePoolError(
                    "refusing to remove worker worktree with changes: "
                    + ", ".join(status.changed_files[:20])
                )
            self._run(["git", "worktree", "remove", str(worktree.path)])
            # Git normally removes the directory. Clean up an empty parent only.
            parent = worktree.path.parent
            try:
                if parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
            except OSError:
                pass

    def prune_stale_metadata(self) -> None:
        with _exclusive_file_lock(self._lock_path):
            self._run(["git", "worktree", "prune"])

    def overlap(self, statuses: Iterable[WorktreeStatus]) -> OverlapReport:
        rows = list(statuses)
        changed = {row.worktree.worker_id: row.changed_files for row in rows}
        owners: dict[str, list[str]] = {}
        for worker, files in changed.items():
            for path in files:
                owners.setdefault(path, []).append(worker)
        overlapping = {
            path: tuple(sorted(workers))
            for path, workers in sorted(owners.items())
            if len(workers) > 1
        }
        return OverlapReport(
            workers=tuple(sorted(changed)),
            changed_by_worker={key: tuple(changed[key]) for key in sorted(changed)},
            overlapping_files=overlapping,
        )

    def remove_runtime_tree_if_empty(self) -> None:
        try:
            if self.root.exists() and not any(self.root.rglob("*")):
                shutil.rmtree(self.root)
        except OSError:
            pass


__all__ = [
    "OverlapReport",
    "WorkerWorktree",
    "WorktreePool",
    "WorktreePoolError",
    "WorktreeStatus",
]
