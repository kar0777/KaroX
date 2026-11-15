"""Core file safety, read/write/list, mutation evidence and audit."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from .atomic_io import atomic_write, new_file_mode
from .core_types import (
    CoreError,
    InvalidCommand,
    InvalidPath,
    _path_label,
)
from .models import CoreCommand, CoreResult, EvidenceRecord
from .security import (
    contains_credential,
    redact,
    redact_content,
)
from .sessions import SessionRecord
from .text_lines import LATIN_1, decode_source
from .core_support import (  # noqa: F401
    WRITE_MODE_CREATE as WRITE_MODE_CREATE,
    WRITE_MODE_APPEND as WRITE_MODE_APPEND,
    VerificationRule as VerificationRule,
    _ripgrep_executable as _ripgrep_executable,
)


class CoreFilesMixin:
    """Methods composed into CoreRuntime; see core.py."""

    def _record_mutation(
        self, record: SessionRecord, command: CoreCommand, result: CoreResult
    ) -> None:
        record.evidence.extend(item.to_dict() for item in result.evidence)
        if command.name == "repo.write_file":
            path = result.data.get("path")
            if (
                result.data.get("changed") is True
                and isinstance(path, str)
                and path not in record.changed_files
            ):
                record.changed_files.append(path)
            if (
                result.data.get("changed") is True
                and isinstance(path, str)
                and result.data.get("previous_sha256") is None
                and path not in record.created_files
            ):
                record.created_files.append(path)
        if command.name == "checks.run":
            entry = {
                "correlation_id": command.correlation_id,
                "ok": result.ok,
                # Reported to clients and kept in session.json: a token in a
                # command line must not be (persistence review, 2026-11-15).
                "argv": redact(result.data.get("argv", [])),
                "exit_code": result.data.get("exit_code"),
                "timed_out": result.data.get("timed_out", False),
                "effective_timeout": result.data.get("effective_timeout"),
                "timeout_clamped_by": result.data.get("timeout_clamped_by"),
            }
            if result.data.get("probe") is True:
                entry["probe"] = True
            record.checks.append(entry)

    def _preflight_mutation(
        self,
        command_name: str,
        arguments: Dict[str, Any],
        deadline_seconds: float,
    ) -> None:
        if command_name == "repo.write_file":
            self._prepare_write(arguments)
        elif command_name == "checks.run":
            self._prepare_check(arguments, deadline_seconds)
        elif command_name == "git.commit":
            self._prepare_commit(arguments)
        elif command_name == "git.push":
            self._prepare_push(arguments, deadline_seconds)

    def safe_path(self, relative: str, for_write: bool = False) -> Path:
        if not isinstance(relative, str) or not relative.strip() or "\x00" in relative:
            raise InvalidPath("path must be a non-empty string")
        raw = Path(relative)
        if raw.is_absolute() or raw.drive:
            # Naming the file by its absolute path means the file inside the
            # repository, not an escape. Resolving that here costs one comparison
            # and removes the blind retries a bare rejection produced: the caller
            # only received "path must be repository-relative", with no value to
            # correct, so it sent the same shape again -- three turns of context
            # in one observed run.
            try:
                contained = raw.resolve(strict=False).relative_to(self.repository)
            except (OSError, ValueError):
                raise InvalidPath(
                    "path escapes the repository: " + _path_label(relative)
                ) from None
            raw = contained
        elif any(part == ".." for part in raw.parts):
            raise InvalidPath(
                "path must be repository-relative: " + _path_label(relative)
            )
        lexical = self.repository / raw
        cursor = self.repository
        for part in raw.parts:
            if part in {"", "."}:
                continue
            cursor = cursor / part
            if cursor.exists() or cursor.is_symlink():
                self._reject_link(cursor)
        candidate = lexical.resolve(strict=False)
        try:
            candidate.relative_to(self.repository)
        except ValueError as exc:
            raise InvalidPath(
                "path escapes the repository: " + _path_label(relative)
            ) from exc
        relative_parts = candidate.relative_to(self.repository).parts
        if relative_parts and relative_parts[0].lower() in {".git", ".karox"}:
            raise InvalidPath(
                "runtime and Git metadata are not tool-accessible: "
                + _path_label(relative)
            )
        if for_write:
            ancestor = candidate.parent
            while not ancestor.exists() and ancestor != self.repository:
                ancestor = ancestor.parent
            resolved_ancestor = ancestor.resolve(strict=True)
            try:
                resolved_ancestor.relative_to(self.repository)
            except ValueError as exc:
                raise InvalidPath("write parent escapes through a link") from exc
        return candidate

    @staticmethod
    def _reject_link(path: Path) -> None:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise InvalidPath(f"cannot inspect path component: {path}") from exc
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        attributes = getattr(metadata, "st_file_attributes", 0)
        if path.is_symlink() or attributes & reparse_flag:
            # Naming the component is what lets the caller pick a real path
            # instead of guessing which part of its path was refused.
            raise InvalidPath(
                f"symlink and reparse-point paths are not allowed: {path.name}"
            )

    @staticmethod
    def _required(arguments: Dict[str, Any], name: str, kind: type) -> Any:
        value = arguments.get(name)
        if not isinstance(value, kind):
            raise InvalidCommand(f"{name} must be {kind.__name__}")
        return value

    def _read_file(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        path = self.safe_path(self._required(arguments, "path", str))
        if not path.is_file():
            raise FileNotFoundError(path)
        size = path.stat().st_size
        if size > self.MAX_FILE_BYTES:
            raise CoreError(f"file is larger than {self.MAX_FILE_BYTES} bytes")
        raw_content = path.read_bytes()
        try:
            content, encoding = decode_source(raw_content)
        except UnicodeDecodeError as exc:
            raise CoreError("repo.read_file reads text files only; this one holds binary data") from exc
        # Content is the caller's working material, so it is returned byte for
        # byte. Pattern redaction here used to rewrite token-shaped literals
        # inside real source, which made an exact-match edit anchor copied from
        # the read unmatchable and turned a read-then-write into corruption.
        returned = str(redact_content(content))
        truncated = len(returned) > self.MAX_READ_CONTENT_CHARS
        if truncated:
            returned = returned[: self.MAX_READ_CONTENT_CHARS]
        payload = {
            "path": path.relative_to(self.repository).as_posix(),
            "content": returned,
            "bytes": size,
            "sha256": hashlib.sha256(raw_content).hexdigest(),
            "truncated": truncated,
            # A caller that echoes content back must be able to tell whether it
            # holds the whole file. Reporting only the whole-file digest beside a
            # shortened body previously lost data with no error at all.
            "content_sha256": hashlib.sha256(returned.encode("utf-8")).hexdigest(),
            "secret_like": contains_credential(content),
        }
        if truncated:
            payload["detail"] = (
                f"only the first {self.MAX_READ_CONTENT_CHARS} characters are "
                "included; use repo.read_lines for a specific range and do not "
                "write this content back as a whole file"
            )
        if encoding == LATIN_1:
            payload["encoding"] = LATIN_1
            payload.setdefault(
                "detail",
                "the file is not UTF-8 and was read as Latin-1; repo.edit_file keeps "
                "that encoding, a whole-file write would store UTF-8",
            )
        return payload

    def _write_file(
        self, arguments: Dict[str, Any], deadline_seconds: float, *, encoding: str = "utf-8"
    ) -> Dict[str, Any]:
        relative, encoded, path, mode = self._prepare_write(arguments, encoding)
        appended_bytes = 0
        if mode == WRITE_MODE_APPEND:
            # An append names a file that is already there: it adds to existing
            # content, so a missing file is a mistake in the call rather than a
            # create. The combined result is what the ceilings and the digest
            # apply to, and the atomic replace below still writes it whole, so a
            # failed append can never leave a half-written file.
            if not path.is_file():
                raise FileNotFoundError(
                    f"repo.write_file mode=append needs an existing file: {relative}"
                )
            appended_bytes = len(encoded)
            encoded = path.read_bytes() + encoded
            if len(encoded) > self.MAX_FILE_BYTES:
                raise CoreError(
                    f"file would be larger than {self.MAX_FILE_BYTES} bytes"
                )
        digest = hashlib.sha256(encoded).hexdigest()
        previous_digest: Optional[str] = None
        previous_mode: Optional[int] = None
        changed = True
        if path.exists():
            if not path.is_file():
                raise CoreError("repo.write_file target is not a regular file")
            previous_mode = stat.S_IMODE(path.stat().st_mode)
            previous_digest = self._file_sha256(path)
            changed = previous_digest != digest or path.stat().st_size != len(encoded)
        if not changed:
            return {
                "path": path.relative_to(self.repository).as_posix(),
                "bytes": len(encoded),
                "changed": False,
                "mode": mode,
                "previous_sha256": previous_digest,
                "sha256": digest,
                "_evidence": [
                    EvidenceRecord(
                        kind="file_write",
                        summary=f"No change to {relative}",
                        artifact_sha256=digest,
                        metadata={
                            "path": relative,
                            "bytes": len(encoded),
                            "changed": False,
                            "mode": mode,
                        },
                    )
                ],
            }
        atomic_write(path, encoded, new_file_mode() if previous_mode is None else previous_mode)
        # A write that went around the credential scanner says so in both the
        # result and the durable evidence, so a reviewer can find every one of
        # them rather than having to infer which writes used the escape hatch.
        bypassed = self._secret_literal_allowed(arguments) and contains_credential(
            arguments.get("content", "")
        )
        return {
            "path": path.relative_to(self.repository).as_posix(),
            "bytes": len(encoded),
            "changed": True,
            "mode": mode,
            "appended_bytes": appended_bytes,
            "previous_sha256": previous_digest,
            "sha256": digest,
            "secret_literal_allowed": bypassed,
            "_evidence": [
                EvidenceRecord(
                    kind="file_write",
                    summary=(
                        f"Appended {appended_bytes} bytes to {relative}"
                        if mode == WRITE_MODE_APPEND
                        else (
                            f"Wrote {relative} with the credential scanner overridden"
                            if bypassed
                            else f"Wrote {relative}"
                        )
                    ),
                    artifact_sha256=digest,
                    metadata={
                        "path": relative,
                        "bytes": len(encoded),
                        "changed": True,
                        "mode": mode,
                        "appended_bytes": appended_bytes,
                        "secret_literal_allowed": bypassed,
                    },
                )
            ],
        }

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _prepare_write(
        self, arguments: Dict[str, Any], encoding: str = "utf-8"
    ) -> tuple[str, bytes, Path, str]:
        relative = self._required(arguments, "path", str)
        content = self._required(arguments, "content", str)
        encoded = content.encode(encoding)
        if len(encoded) > self.MAX_FILE_BYTES:
            raise CoreError(f"content is larger than {self.MAX_FILE_BYTES} bytes")
        mode = arguments.get("mode", WRITE_MODE_CREATE)
        if mode not in {WRITE_MODE_CREATE, WRITE_MODE_APPEND}:
            raise InvalidCommand(
                f"mode must be {WRITE_MODE_CREATE!r} or {WRITE_MODE_APPEND!r}"
            )
        if contains_credential(content) and not self._secret_literal_allowed(
            arguments
        ):
            raise CoreError(
                "write blocked by credential scanner; pass allow_secret_literal "
                "to author a fixture or documentation example on purpose"
            )
        path = self.safe_path(relative, for_write=True)
        return relative, encoded, path, mode

    @staticmethod
    def _secret_literal_allowed(arguments: Dict[str, Any]) -> bool:
        """Whether this call deliberately writes token-shaped text.

        The scanner matches the *shape* of a credential, not a credential KaroX
        holds, so it also refuses the fixtures of a secret scanner, a rotation
        runbook and any documentation that shows an example key. There was no
        way to say "yes, on purpose", which made those files unwritable rather
        than the repository safer.

        The flag has to be passed on the individual call, and the call that used
        it says so in its result and its evidence, so a reviewer can find every
        write that went around the scanner instead of inferring it.
        """
        value = arguments.get("allow_secret_literal", False)
        if not isinstance(value, bool):
            raise InvalidCommand("allow_secret_literal must be true or false")
        return value

    def _list_files(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        pattern = arguments.get("pattern", "**/*")
        raw_pattern = Path(pattern) if isinstance(pattern, str) else None
        if (
            raw_pattern is None
            or not pattern
            or len(pattern) > 1000
            or "\x00" in pattern
            or raw_pattern.is_absolute()
            or raw_pattern.drive
            or ".." in raw_pattern.parts
        ):
            raise InvalidCommand(
                "pattern must be repository-relative: "
                + _path_label(str(raw_pattern))
            )
        items: List[str] = []
        try:
            matches = self.repository.glob(pattern)
        except (OSError, ValueError) as exc:
            raise InvalidCommand(f"invalid glob pattern: {exc}") from exc
        for path in matches:
            if not path.is_file():
                continue
            try:
                lexical_relative = path.relative_to(self.repository).as_posix()
                safe = self.safe_path(lexical_relative)
                relative = safe.relative_to(self.repository)
            except (InvalidPath, ValueError):
                continue
            if relative.parts and relative.parts[0].lower() in {".git", ".karox"}:
                continue
            items.append(relative.as_posix())
            if len(items) >= 2000:
                break
        return {"files": sorted(items), "truncated": len(items) >= 2000}

    def _audit(self, event: str, data: Dict[str, Any]) -> None:
        if self.audit_path is None:
            return
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        if (
            self.audit_path.exists()
            and self.audit_path.stat().st_size >= self.MAX_AUDIT_BYTES
        ):
            rotated = self.audit_path.with_suffix(self.audit_path.suffix + ".1")
            try:
                rotated.unlink()
            except FileNotFoundError:
                pass
            os.replace(self.audit_path, rotated)
        record = {
            "timestamp": time.time(),
            "event": event,
            "data": redact(data),
        }
        with self.audit_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
