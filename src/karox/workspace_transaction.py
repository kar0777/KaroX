"""Atomic repository transactions used by the reloadable developer worker."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .core import CoreError, InvalidCommand
from .models import EvidenceRecord
from .security import contains_credential


MAX_BATCH_OPERATIONS = 200


@dataclass(frozen=True)
class FileState:
    exists: bool
    content: bytes
    mode: Optional[int]


def verify_expected(path: Path, state: FileState, expected: Optional[str]) -> None:
    if expected is None:
        return
    if not isinstance(expected, str) or len(expected) != 64:
        raise InvalidCommand("expected_sha256 must be a sha256 hex digest")
    try:
        int(expected, 16)
    except ValueError as exc:
        raise InvalidCommand("expected_sha256 must be a sha256 hex digest") from exc
    found = hashlib.sha256(state.content).hexdigest() if state.exists else None
    if found != expected.lower():
        raise InvalidCommand(
            f"file changed since it was read: {path}; expected {expected.lower()} "
            f"but found {found}"
        )


class WorkspaceTransaction:
    """Validate first, then commit all file operations or roll them back."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.repository = runtime.repository
        self._states: dict[Path, FileState] = {}
        self._virtual: dict[Path, FileState] = {}
        self._directory_states: dict[Path, bool] = {}
        self._actions: list[tuple[str, dict[str, Any]]] = []
        self._created_dirs: list[Path] = []

    def _capture(self, path: Path) -> FileState:
        current = self._states.get(path)
        if current is not None:
            return current
        if path.exists():
            if not path.is_file():
                raise CoreError(f"target is not a regular file: {path}")
            size = path.stat().st_size
            if size > self.runtime.MAX_FILE_BYTES:
                raise CoreError(
                    f"file is larger than {self.runtime.MAX_FILE_BYTES} bytes: {path}"
                )
            current = FileState(
                True,
                path.read_bytes(),
                stat.S_IMODE(path.stat().st_mode),
            )
        else:
            current = FileState(False, b"", None)
        self._states[path] = current
        self._virtual[path] = current
        return current

    def _current(self, path: Path) -> FileState:
        if path not in self._virtual:
            self._capture(path)
        return self._virtual[path]

    def read_text(self, relative: str) -> str:
        """Read the transaction's current virtual UTF-8 state for one file."""
        path = self.runtime.safe_path(relative, for_write=True)
        state = self._current(path)
        if not state.exists:
            raise FileNotFoundError(path)
        try:
            return state.content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CoreError("repo.apply_patch supports UTF-8 text only") from exc

    def exists(self, relative: str) -> bool:
        path = self.runtime.safe_path(relative, for_write=True)
        return self._current(path).exists

    def _capture_directory(self, path: Path) -> bool:
        if path in self._directory_states:
            return self._directory_states[path]
        exists = path.exists()
        if exists and not path.is_dir():
            raise InvalidCommand(
                f"mkdir target exists and is not a directory: "
                f"{path.relative_to(self.repository).as_posix()}"
            )
        self._directory_states[path] = exists
        return exists

    def stage_write(
        self,
        relative: str,
        content: str,
        *,
        expected_sha256: Optional[str] = None,
        allow_secret_literal: bool = False,
    ) -> None:
        encoded = content.encode("utf-8")
        if len(encoded) > self.runtime.MAX_FILE_BYTES:
            raise CoreError(
                f"content for {relative} is larger than "
                f"{self.runtime.MAX_FILE_BYTES} bytes"
            )
        if contains_credential(content) and not allow_secret_literal:
            raise CoreError(
                f"write to {relative} blocked by credential scanner; set "
                "allow_secret_literal only for an intentional fixture or example"
            )
        path = self.runtime.safe_path(relative, for_write=True)
        self._capture(path)
        state = self._current(path)
        verify_expected(path, state, expected_sha256)
        self._actions.append(
            (
                "write",
                {
                    "path": path,
                    "relative": path.relative_to(self.repository).as_posix(),
                    "content": encoded,
                },
            )
        )
        self._virtual[path] = FileState(True, encoded, state.mode)

    def stage_delete(
        self,
        relative: str,
        *,
        expected_sha256: Optional[str] = None,
        missing_ok: bool = False,
    ) -> None:
        path = self.runtime.safe_path(relative, for_write=True)
        self._capture(path)
        state = self._current(path)
        if not state.exists and not missing_ok:
            raise FileNotFoundError(path)
        verify_expected(path, state, expected_sha256)
        self._actions.append(
            (
                "delete",
                {
                    "path": path,
                    "relative": path.relative_to(self.repository).as_posix(),
                },
            )
        )
        self._virtual[path] = FileState(False, b"", None)

    def stage_move(
        self,
        source: str,
        destination: str,
        *,
        expected_sha256: Optional[str] = None,
        overwrite: bool = False,
    ) -> None:
        source_path = self.runtime.safe_path(source, for_write=True)
        destination_path = self.runtime.safe_path(destination, for_write=True)
        if source_path == destination_path:
            raise InvalidCommand("move source and destination are identical")
        self._capture(source_path)
        self._capture(destination_path)
        source_state = self._current(source_path)
        destination_state = self._current(destination_path)
        if not source_state.exists:
            raise FileNotFoundError(source_path)
        if destination_state.exists and not overwrite:
            raise InvalidCommand(f"move destination already exists: {destination}")
        verify_expected(source_path, source_state, expected_sha256)
        self._actions.append(
            (
                "move",
                {
                    "source": source_path,
                    "destination": destination_path,
                    "source_relative": source_path.relative_to(
                        self.repository
                    ).as_posix(),
                    "destination_relative": destination_path.relative_to(
                        self.repository
                    ).as_posix(),
                },
            )
        )
        self._virtual[source_path] = FileState(False, b"", None)
        self._virtual[destination_path] = source_state

    def stage_mkdir(self, relative: str) -> None:
        path = self.runtime.safe_path(relative, for_write=True)
        self._capture_directory(path)
        self._actions.append(
            (
                "mkdir",
                {
                    "path": path,
                    "relative": path.relative_to(self.repository).as_posix(),
                },
            )
        )

    def preview(self) -> dict[str, Any]:
        changes: list[dict[str, Any]] = []
        for action, payload in self._actions:
            item: dict[str, Any] = {"op": action}
            if "relative" in payload:
                item["path"] = payload["relative"]
            if "source_relative" in payload:
                item["source"] = payload["source_relative"]
                item["destination"] = payload["destination_relative"]
            if action == "write":
                item["bytes"] = len(payload["content"])
                item["sha256"] = hashlib.sha256(payload["content"]).hexdigest()
            changes.append(item)
        return {
            "dry_run": True,
            "operation_count": len(changes),
            "changes": changes,
        }

    @staticmethod
    def _atomic_write(path: Path, content: bytes, mode: Optional[int]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=path.parent
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                if mode is not None and hasattr(os, "fchmod"):
                    os.fchmod(handle.fileno(), mode)
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _remember_created_parents(self, path: Path) -> None:
        missing: list[Path] = []
        cursor = path.parent
        while cursor != self.repository and not cursor.exists():
            missing.append(cursor)
            cursor = cursor.parent
        for directory in reversed(missing):
            directory.mkdir()
            self._created_dirs.append(directory)

    def _ensure_directory(self, path: Path) -> bool:
        if path.exists():
            if not path.is_dir():
                raise CoreError(f"directory target is not a directory: {path}")
            return False
        self._remember_created_parents(path / ".karox-directory-placeholder")
        return True

    def _revalidate_path(self, path: Path) -> Path:
        relative = path.relative_to(self.repository).as_posix()
        current = self.runtime.safe_path(relative, for_write=True)
        if current != path:
            raise InvalidCommand(
                "repository path changed while the atomic transaction was being prepared: "
                f"{relative}"
            )
        return current

    def _live_state(self, path: Path) -> FileState:
        self._revalidate_path(path)
        if not path.exists():
            return FileState(False, b"", None)
        if not path.is_file():
            raise CoreError(f"target is not a regular file: {path}")
        size = path.stat().st_size
        if size > self.runtime.MAX_FILE_BYTES:
            raise CoreError(
                f"file is larger than {self.runtime.MAX_FILE_BYTES} bytes: {path}"
            )
        return FileState(
            True,
            path.read_bytes(),
            stat.S_IMODE(path.stat().st_mode),
        )

    def _verify_live_snapshots(self) -> None:
        for path, expected in self._states.items():
            current = self._live_state(path)
            if current != expected:
                relative = path.relative_to(self.repository).as_posix()
                raise InvalidCommand(
                    "repository changed while the atomic transaction was being prepared: "
                    f"{relative}"
                )
        for path, expected_exists in self._directory_states.items():
            current_exists = path.exists()
            if current_exists and not path.is_dir():
                relative = path.relative_to(self.repository).as_posix()
                raise InvalidCommand(
                    "repository changed while the atomic transaction was being prepared: "
                    f"{relative}"
                )
            if current_exists != expected_exists:
                relative = path.relative_to(self.repository).as_posix()
                raise InvalidCommand(
                    "repository changed while the atomic transaction was being prepared: "
                    f"{relative}"
                )

    def commit(self) -> dict[str, Any]:
        operations: list[dict[str, Any]] = []
        changed_files: list[str] = []
        self._verify_live_snapshots()
        try:
            for action, payload in self._actions:
                if action == "mkdir":
                    path = payload["path"]
                    self._revalidate_path(path)
                    changed = self._ensure_directory(path)
                    operations.append(
                        {
                            "op": "mkdir",
                            "path": payload["relative"],
                            "changed": changed,
                        }
                    )
                    continue

                if action == "write":
                    path = payload["path"]
                    state = self._live_state(path)
                    digest = hashlib.sha256(payload["content"]).hexdigest()
                    previous = (
                        hashlib.sha256(state.content).hexdigest()
                        if state.exists
                        else None
                    )
                    changed = previous != digest
                    if changed:
                        self._remember_created_parents(path)
                        self._atomic_write(path, payload["content"], state.mode)
                        changed_files.append(payload["relative"])
                    operations.append(
                        {
                            "op": "write",
                            "path": payload["relative"],
                            "changed": changed,
                            "bytes": len(payload["content"]),
                            "previous_sha256": previous,
                            "sha256": digest,
                        }
                    )
                    continue

                if action == "delete":
                    path = payload["path"]
                    state = self._live_state(path)
                    changed = state.exists and path.exists()
                    if changed:
                        path.unlink()
                        changed_files.append(payload["relative"])
                    operations.append(
                        {
                            "op": "delete",
                            "path": payload["relative"],
                            "changed": changed,
                            "previous_sha256": (
                                hashlib.sha256(state.content).hexdigest()
                                if state.exists
                                else None
                            ),
                            "sha256": None,
                        }
                    )
                    continue

                if action == "move":
                    source = payload["source"]
                    destination = payload["destination"]
                    live_source = self._live_state(source)
                    self._revalidate_path(destination)
                    if not live_source.exists:
                        raise FileNotFoundError(source)
                    self._remember_created_parents(destination)
                    if destination.exists():
                        destination.unlink()
                    os.replace(source, destination)
                    digest = hashlib.sha256(live_source.content).hexdigest()
                    changed_files.extend(
                        [
                            payload["source_relative"],
                            payload["destination_relative"],
                        ]
                    )
                    operations.append(
                        {
                            "op": "move",
                            "source": payload["source_relative"],
                            "destination": payload["destination_relative"],
                            "changed": True,
                            "sha256": digest,
                        }
                    )
                    continue

                raise RuntimeError(f"unknown staged operation: {action}")
        except Exception as exc:
            rollback_failures = self.rollback()
            if rollback_failures:
                raise CoreError(
                    "repository transaction failed and rollback was incomplete: "
                    + "; ".join(rollback_failures)
                ) from exc
            raise

        post_state: dict[str, Optional[str]] = {}
        for path in self._states:
            relative = path.relative_to(self.repository).as_posix()
            post_state[relative] = (
                self.runtime._file_sha256(path) if path.is_file() else None
            )
        directory_paths = set(self._directory_states).union(self._created_dirs)
        post_directories = {
            path.relative_to(self.repository).as_posix(): path.is_dir()
            for path in sorted(directory_paths)
        }
        unique_changed = sorted(set(changed_files))
        return {
            "dry_run": False,
            "operation_count": len(operations),
            "operations": operations,
            "changed_files": unique_changed,
            "post_state": post_state,
            "post_directories": post_directories,
            "_evidence": [
                EvidenceRecord(
                    kind="repo_transaction",
                    summary=(
                        f"Applied {len(operations)} repository operation(s)"
                    ),
                    metadata={
                        "operation_count": len(operations),
                        "changed_files": unique_changed,
                    },
                )
            ],
        }

    def rollback(self) -> list[str]:
        failures: list[str] = []
        for path, state in reversed(list(self._states.items())):
            try:
                self._revalidate_path(path)
                if state.exists:
                    self._atomic_write(path, state.content, state.mode)
                elif path.exists() and path.is_file():
                    path.unlink()
            except Exception as exc:
                relative = path.relative_to(self.repository).as_posix()
                failures.append(f"{relative}: {type(exc).__name__}: {exc}")
        for directory in reversed(self._created_dirs):
            try:
                self._revalidate_path(directory)
                directory.rmdir()
            except Exception as exc:
                if directory.exists():
                    relative = directory.relative_to(self.repository).as_posix()
                    failures.append(f"{relative}: {type(exc).__name__}: {exc}")
        return failures
