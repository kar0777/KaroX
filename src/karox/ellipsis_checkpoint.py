"""Local Git-aware checkpoints for explicit Ellipsis-session rollback.

A checkpoint never touches the working tree. Git object snapshots preserve
tracked worktree and index state; pre-existing untracked files are copied to a
restrictive local KaroX runtime directory. Rollback is limited to paths KaroX
recorded as changed by the session.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from .models import repository_fingerprint
from .paths import runtime_dir
from .sessions import SessionStore, current_mutation_lease


class CheckpointError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkspaceCheckpoint:
    schema_version: int
    checkpoint_id: str
    session_id: str
    repository_fingerprint: str
    created_at: float
    worktree_source: str
    index_tree: str
    tracked_paths: tuple[str, ...]
    untracked_paths: tuple[str, ...]
    untracked_bytes: int

    def public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "checkpoint_id": self.checkpoint_id,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "tracked_count": len(self.tracked_paths),
            "untracked_count": len(self.untracked_paths),
            "untracked_bytes": self.untracked_bytes,
        }


def _git(repository: Path, argv: list[str], timeout: float = 60.0) -> str:
    try:
        completed = subprocess.run(
            ["git", *argv],
            cwd=repository,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CheckpointError("local Git checkpoint command failed") from exc
    if completed.returncode != 0:
        raise CheckpointError(
            f"local Git checkpoint command failed with exit code {completed.returncode}"
        )
    return completed.stdout.strip()


def _git_z(repository: Path, argv: list[str]) -> tuple[str, ...]:
    try:
        completed = subprocess.run(
            ["git", *argv],
            cwd=repository,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CheckpointError("cannot enumerate checkpoint paths") from exc
    if completed.returncode != 0:
        raise CheckpointError(
            f"cannot enumerate checkpoint paths (exit {completed.returncode})"
        )
    return tuple(
        item.decode("utf-8", errors="surrogateescape")
        for item in completed.stdout.split(b"\0")
        if item
    )


def _safe_relative(repository: Path, value: str) -> Path:
    raw = Path(value)
    if raw.is_absolute() or raw.drive or ".." in raw.parts or "\0" in value:
        raise CheckpointError("checkpoint path is not repository-relative")
    # Resolve the base first: CI temp roots carry 8.3 short names (Windows
    # runners) or symlinked prefixes (/var on macOS), so joining against the
    # unresolved path would reject every repository-relative path.
    base = repository.expanduser().resolve()
    candidate = (base / raw).resolve(strict=False)
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise CheckpointError("checkpoint path escapes the repository") from exc
    cursor = base
    for part in raw.parts:
        cursor = cursor / part
        if cursor.exists() and cursor.is_symlink():
            raise CheckpointError("checkpoint does not follow symbolic links")
    return candidate


class WorkspaceCheckpointStore:
    MAX_UNTRACKED_BYTES = 50_000_000
    MAX_PATHS = 100_000

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = (
            root or runtime_dir() / "vnext" / "ellipsis" / "checkpoints"
        ).resolve()

    def directory(self, checkpoint_id: str) -> Path:
        if not checkpoint_id or any(
            not (character.isalnum() or character in "._-")
            for character in checkpoint_id
        ):
            raise CheckpointError("checkpoint ID is malformed")
        return self.root / checkpoint_id

    def create(
        self,
        repository: Path,
        *,
        session_id: str,
        sessions: SessionStore,
    ) -> WorkspaceCheckpoint:
        repository = repository.expanduser().resolve(strict=True)
        top = Path(_git(repository, ["rev-parse", "--show-toplevel"])).resolve(
            strict=True
        )
        if os.path.normcase(str(top)) != os.path.normcase(str(repository)):
            raise CheckpointError("checkpoint repository must be the Git root")
        tracked = _git_z(repository, ["ls-files", "-z"])
        untracked = _git_z(
            repository, ["ls-files", "--others", "--exclude-standard", "-z"]
        )
        if len(tracked) + len(untracked) > self.MAX_PATHS:
            raise CheckpointError("repository has too many paths for a safe checkpoint")
        total = 0
        for relative in untracked:
            path = _safe_relative(repository, relative)
            if not path.is_file():
                continue
            total += path.stat().st_size
            if total > self.MAX_UNTRACKED_BYTES:
                raise CheckpointError(
                    "pre-existing untracked files exceed the checkpoint size limit"
                )
        index_tree = _git(repository, ["write-tree"])
        try:
            worktree_source = _git(
                repository, ["stash", "create", f"karox-{session_id}"]
            )
        except CheckpointError:
            # ``git stash create`` requires HEAD. Fresh repositories used by
            # coding agents often have no first commit yet, but an unborn repo
            # is still checkpointable when its tracked worktree matches the
            # index; untracked files are backed up separately below. A Git tree
            # is a valid ``git restore --source`` object, so the index tree is a
            # lossless worktree source in exactly that case.
            worktree_source = ""
        if not worktree_source:
            try:
                worktree_source = _git(repository, ["rev-parse", "HEAD"])
            except CheckpointError:
                try:
                    _git(repository, ["diff", "--quiet", "--"])
                except CheckpointError as exc:
                    raise CheckpointError(
                        "cannot safely checkpoint an unborn repository with "
                        "tracked worktree changes"
                    ) from exc
                worktree_source = index_tree
        checkpoint_id = f"cp-{int(time.time())}-{uuid.uuid4().hex[:10]}"
        directory = self.directory(checkpoint_id)
        backup_root = directory / "untracked"
        directory.mkdir(parents=True, exist_ok=False)
        try:
            for relative in untracked:
                source = _safe_relative(repository, relative)
                if not source.is_file():
                    continue
                destination = _safe_relative(backup_root, relative)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
            record = WorkspaceCheckpoint(
                schema_version=1,
                checkpoint_id=checkpoint_id,
                session_id=session_id,
                repository_fingerprint=repository_fingerprint(repository),
                created_at=time.time(),
                worktree_source=worktree_source,
                index_tree=index_tree,
                tracked_paths=tracked,
                untracked_paths=untracked,
                untracked_bytes=total,
            )
            self._write(directory / "checkpoint.json", asdict(record))
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        inherited = current_mutation_lease(session_id)
        if inherited is not None:
            # Hosted Core already owns the session mutation fence while Smart
            # Stop asks for this on-demand checkpoint. Re-acquiring the same
            # non-reentrant lease would make the safety adapter fail and prompt
            # the user for a deletion that is actually reversible. Reuse only
            # the ContextVar-scoped inherited lease; unrelated requests cannot
            # observe it and remain fenced by SessionStore.acquire().
            sessions.validate_lease(inherited)
            session = sessions.load(session_id)
            sessions.validate_repository(session, repository)
            revision = session.revision
            session.checkpoints.append(record.public_dict())
            sessions.save(session, revision, inherited)
        else:
            with sessions.mutate(
                session_id, f"checkpoint-{os.getpid()}", ttl_seconds=60.0
            ) as session:
                sessions.validate_repository(session, repository)
                session.checkpoints.append(record.public_dict())
        return record

    @staticmethod
    def _write(path: Path, payload: dict[str, Any]) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def load(self, checkpoint_id: str) -> WorkspaceCheckpoint:
        path = self.directory(checkpoint_id) / "checkpoint.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise CheckpointError("checkpoint does not exist") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise CheckpointError("checkpoint metadata is unreadable") from exc
        if not isinstance(payload, dict):
            raise CheckpointError("checkpoint metadata is malformed")
        try:
            payload["tracked_paths"] = tuple(payload["tracked_paths"])
            payload["untracked_paths"] = tuple(payload["untracked_paths"])
            return WorkspaceCheckpoint(**payload)
        except (KeyError, TypeError, ValueError) as exc:
            raise CheckpointError("checkpoint metadata is malformed") from exc

    def rollback(
        self,
        repository: Path,
        *,
        session_id: str,
        checkpoint_id: str,
        sessions: SessionStore,
    ) -> dict[str, Any]:
        repository = repository.expanduser().resolve(strict=True)
        checkpoint = self.load(checkpoint_id)
        if checkpoint.session_id != session_id:
            raise CheckpointError("checkpoint belongs to another session")
        if checkpoint.repository_fingerprint != repository_fingerprint(repository):
            raise CheckpointError("checkpoint belongs to another repository")
        session = sessions.load(session_id)
        sessions.validate_repository(session, repository)
        changed = tuple(dict.fromkeys(session.changed_files))
        tracked = set(checkpoint.tracked_paths)
        untracked = set(checkpoint.untracked_paths)
        restored: list[str] = []
        deleted: list[str] = []
        skipped: list[str] = []
        backup_root = self.directory(checkpoint_id) / "untracked"
        for relative in changed:
            target = _safe_relative(repository, relative)
            if relative in untracked:
                source = _safe_relative(backup_root, relative)
                if not source.is_file():
                    skipped.append(relative)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                restored.append(relative)
            elif relative in tracked:
                _git(
                    repository,
                    [
                        "restore",
                        f"--source={checkpoint.worktree_source}",
                        "--worktree",
                        "--",
                        relative,
                    ],
                )
                _git(
                    repository,
                    [
                        "restore",
                        f"--source={checkpoint.index_tree}",
                        "--staged",
                        "--",
                        relative,
                    ],
                )
                restored.append(relative)
            elif target.exists():
                if target.is_file():
                    target.unlink()
                    deleted.append(relative)
                    parent = target.parent
                    while parent != repository:
                        try:
                            parent.rmdir()
                        except OSError:
                            break
                        parent = parent.parent
                else:
                    skipped.append(relative)
            else:
                deleted.append(relative)
        decision = {
            "kind": "explicit_rollback",
            "checkpoint_id": checkpoint_id,
            "timestamp": time.time(),
            "restored": restored,
            "deleted": deleted,
            "skipped": skipped,
        }
        # Stop revokes the Karo session before rollback by design. Revocation
        # blocks every remote mutation, but it must not block the user's local,
        # explicit restore. Record the decision only while a lease can still be
        # acquired; the returned result remains the evidence for a revoked one.
        if not session.revoked:
            with sessions.mutate(
                session_id, f"rollback-{os.getpid()}", ttl_seconds=120.0
            ) as mutable:
                mutable.decisions.append(decision)
        return {
            "checkpoint_id": checkpoint_id,
            "restored": restored,
            "deleted": deleted,
            "skipped": skipped,
            "session_was_revoked": session.revoked,
        }
