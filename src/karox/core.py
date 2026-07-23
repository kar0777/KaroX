"""Provider-independent local action boundary for KaroX vNext."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from .models import Capability, CoreCommand, CoreResult, EvidenceRecord
from .policy import CapabilityPolicy
from .security import child_process_environment, contains_credential, redact
from .sessions import MutationLease, SessionError, SessionRecord, SessionStore


class CoreError(RuntimeError):
    pass


class InvalidPath(CoreError):
    pass


class InvalidCommand(CoreError):
    pass


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    capability: Capability
    mutates: bool
    input_schema: Dict[str, Any]
    additional_capabilities: tuple[Capability, ...] = ()


class CoreRuntime:
    MAX_FILE_BYTES = 2_000_000
    MAX_OUTPUT_BYTES = 1_000_000

    def __init__(
        self,
        repository: Path,
        policy: CapabilityPolicy,
        sessions: SessionStore,
        audit_path: Optional[Path] = None,
    ) -> None:
        self.repository = repository.expanduser().resolve(strict=True)
        if not self.repository.is_dir():
            raise CoreError(f"repository is not a directory: {self.repository}")
        self.policy = policy
        self.sessions = sessions
        self.audit_path = audit_path.expanduser().resolve() if audit_path else None
        self._handlers: Mapping[
            str, Callable[[Dict[str, Any], float], Dict[str, Any]]
        ] = {
            "repo.read_file": self._read_file,
            "repo.write_file": self._write_file,
            "repo.list_files": self._list_files,
            "checks.run": self._run_check,
            "git.status": self._git_status,
            "git.diff": self._git_diff,
        }
        self._definitions = {
            "repo.read_file": ToolDefinition(
                "repo.read_file",
                "Read a UTF-8 text file inside the repository.",
                Capability.REPO_READ,
                False,
                {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            ),
            "repo.write_file": ToolDefinition(
                "repo.write_file",
                "Atomically write a UTF-8 text file inside the repository.",
                Capability.REPO_WRITE,
                True,
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                    "additionalProperties": False,
                },
            ),
            "repo.list_files": ToolDefinition(
                "repo.list_files",
                "List repository files with an optional glob.",
                Capability.REPO_READ,
                False,
                {
                    "type": "object",
                    "properties": {"pattern": {"type": "string"}},
                    "additionalProperties": False,
                },
            ),
            "checks.run": ToolDefinition(
                "checks.run",
                "Run a bounded test, lint, or build command without a shell.",
                Capability.CHECKS_RUN,
                True,
                {
                    "type": "object",
                    "properties": {
                        "argv": {"type": "array", "items": {"type": "string"}},
                        "timeout_seconds": {"type": "number"},
                    },
                    "required": ["argv"],
                    "additionalProperties": False,
                },
                (Capability.PROCESS_RUN,),
            ),
            "git.status": ToolDefinition(
                "git.status",
                "Read porcelain Git status.",
                Capability.GIT_READ,
                False,
                {"type": "object", "properties": {}, "additionalProperties": False},
            ),
            "git.diff": ToolDefinition(
                "git.diff",
                "Read the current Git diff.",
                Capability.GIT_READ,
                False,
                {
                    "type": "object",
                    "properties": {"staged": {"type": "boolean"}},
                    "additionalProperties": False,
                },
            ),
        }

    def tools(self) -> List[ToolDefinition]:
        return list(self._definitions.values())

    def execute(
        self,
        command: CoreCommand,
        capability_token: Optional[str] = None,
        lease: Optional[MutationLease] = None,
    ) -> CoreResult:
        definition = self._definitions.get(command.name)
        handler = self._handlers.get(command.name)
        if definition is None or handler is None:
            raise InvalidCommand(f"unknown Core command: {command.name}")
        self._validate_arguments(definition, command.arguments)
        record = self._load_session(command)
        decision = self.policy.require(
            command.origin, definition.capability, capability_token
        )
        for capability in definition.additional_capabilities:
            self.policy.require(command.origin, capability, capability_token)
        started = time.perf_counter()
        self._audit(
            "core.command.started",
            {
                "session_id": command.session_id,
                "origin": command.origin.key,
                "command": command.name,
                "capability": definition.capability.value,
                "correlation_id": command.correlation_id,
                "decision": decision.reason,
                "input_digest": command.input_digest(),
            },
        )
        if definition.mutates:
            if lease is None:
                raise SessionError("mutating Core commands require a mutation lease")
            if not command.idempotency_key:
                raise InvalidCommand("mutating Core commands require an idempotency key")
            self.sessions.validate_lease(lease)
            if lease.session_id != command.session_id:
                raise SessionError("mutation lease belongs to a different session")
            # Reject deterministic input failures before reserving the durable
            # idempotency intent. Once an intent exists, only execution failures
            # that may have caused a side effect require reconciliation.
            self._preflight_mutation(
                command.name,
                command.arguments,
                float(command.deadline_seconds),
            )
            replay = self.sessions.begin_idempotent(
                record,
                lease,
                command.idempotency_key,
                command.input_digest(),
            )
            if replay is not None:
                result = CoreResult.from_dict(replay)
                result.idempotent_replay = True
                self._audit(
                    "core.command.replayed",
                    {
                        "session_id": command.session_id,
                        "origin": command.origin.key,
                        "command": command.name,
                        "correlation_id": command.correlation_id,
                        "idempotency_key": command.idempotency_key,
                    },
                )
                return result
        try:
            data = handler(dict(command.arguments), float(command.deadline_seconds))
        except Exception as exc:
            self._audit(
                "core.command.failed",
                {
                    "session_id": command.session_id,
                    "origin": command.origin.key,
                    "command": command.name,
                    "correlation_id": command.correlation_id,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            raise
        evidence = list(data.pop("_evidence", []))
        result = CoreResult(
            ok=not (
                command.name == "checks.run"
                and (data.get("timed_out") or data.get("exit_code") != 0)
            ),
            command=command.name,
            data=data,
            correlation_id=command.correlation_id,
            mutation=definition.mutates,
            evidence=evidence,
        )
        if definition.mutates:
            assert lease is not None and command.idempotency_key is not None
            self._record_mutation(record, command, result)
            self.sessions.complete_idempotent(
                record,
                lease,
                command.idempotency_key,
                result.to_dict(),
            )
        self._audit(
            "core.command.completed",
            {
                "session_id": command.session_id,
                "origin": command.origin.key,
                "command": command.name,
                "correlation_id": command.correlation_id,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "evidence_ids": [item.evidence_id for item in evidence],
            },
        )
        return result

    def _load_session(self, command: CoreCommand) -> SessionRecord:
        record = self.sessions.load(command.session_id)
        self.sessions.validate_repository(record, self.repository)
        if record.revoked:
            raise SessionError("session access has been revoked")
        if record.access_profile != self.policy.profile.value:
            raise SessionError("session and runtime access profiles differ")
        return record

    @staticmethod
    def _validate_arguments(
        definition: ToolDefinition, arguments: Dict[str, Any]
    ) -> None:
        if not isinstance(arguments, dict):
            raise InvalidCommand("Core command arguments must be an object")
        schema = definition.input_schema
        properties = schema.get("properties", {})
        unknown = set(arguments).difference(properties)
        if unknown:
            raise InvalidCommand(f"unknown arguments: {sorted(unknown)}")
        missing = set(schema.get("required", [])).difference(arguments)
        if missing:
            raise InvalidCommand(f"missing arguments: {sorted(missing)}")
        kinds = {
            "string": str,
            "array": list,
            "number": (int, float),
            "boolean": bool,
            "object": dict,
        }
        for name, value in arguments.items():
            expected_name = properties[name].get("type")
            expected = kinds.get(expected_name)
            if expected is None:
                raise InvalidCommand(f"unsupported schema type for {name}")
            if expected_name == "number" and isinstance(value, bool):
                raise InvalidCommand(f"{name} must be number")
            if not isinstance(value, expected):
                raise InvalidCommand(f"{name} must be {expected_name}")
            item_type = properties[name].get("items", {}).get("type")
            if item_type and isinstance(value, list):
                item_kind = kinds.get(item_type)
                if item_kind is None or not all(
                    isinstance(item, item_kind) for item in value
                ):
                    raise InvalidCommand(f"{name} items must be {item_type}")

    def _record_mutation(
        self, record: SessionRecord, command: CoreCommand, result: CoreResult
    ) -> None:
        record.evidence.extend(item.to_dict() for item in result.evidence)
        if command.name == "repo.write_file":
            path = result.data.get("path")
            if isinstance(path, str) and path not in record.changed_files:
                record.changed_files.append(path)
        if command.name == "checks.run":
            record.checks.append(
                {
                    "correlation_id": command.correlation_id,
                    "ok": result.ok,
                    "argv": result.data.get("argv", []),
                    "exit_code": result.data.get("exit_code"),
                    "timed_out": result.data.get("timed_out", False),
                }
            )

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

    def safe_path(self, relative: str, for_write: bool = False) -> Path:
        if not isinstance(relative, str) or not relative.strip() or "\x00" in relative:
            raise InvalidPath("path must be a non-empty string")
        raw = Path(relative)
        if raw.is_absolute() or raw.drive or any(part == ".." for part in raw.parts):
            raise InvalidPath("path must be repository-relative")
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
            raise InvalidPath("path escapes the repository") from exc
        relative_parts = candidate.relative_to(self.repository).parts
        if relative_parts and relative_parts[0].lower() in {".git", ".karox"}:
            raise InvalidPath("runtime and Git metadata are not tool-accessible")
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
            raise InvalidPath("symlink and reparse-point paths are not allowed")

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
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise CoreError("repo.read_file supports UTF-8 text only") from exc
        return {
            "path": path.relative_to(self.repository).as_posix(),
            "content": content,
            "bytes": size,
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }

    def _write_file(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        relative, encoded, path = self._prepare_write(arguments)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        digest = hashlib.sha256(encoded).hexdigest()
        return {
            "path": path.relative_to(self.repository).as_posix(),
            "bytes": len(encoded),
            "sha256": digest,
            "_evidence": [
                EvidenceRecord(
                    kind="file_write",
                    summary=f"Wrote {relative}",
                    artifact_sha256=digest,
                    metadata={"path": relative, "bytes": len(encoded)},
                )
            ],
        }

    def _prepare_write(
        self, arguments: Dict[str, Any]
    ) -> tuple[str, bytes, Path]:
        relative = self._required(arguments, "path", str)
        content = self._required(arguments, "content", str)
        encoded = content.encode("utf-8")
        if len(encoded) > self.MAX_FILE_BYTES:
            raise CoreError(f"content is larger than {self.MAX_FILE_BYTES} bytes")
        if contains_credential(content):
            raise CoreError("write blocked by credential scanner")
        path = self.safe_path(relative, for_write=True)
        return relative, encoded, path

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
            raise InvalidCommand("pattern must be repository-relative")
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

    @staticmethod
    def _validate_process(argv: Iterable[str]) -> List[str]:
        values = list(argv)
        if not values or len(values) > 100 or not all(isinstance(item, str) for item in values):
            raise InvalidCommand("argv must contain 1-100 strings")
        if any("\x00" in item or len(item) > 10_000 for item in values):
            raise InvalidCommand("argv contains an invalid value")
        lowered = [item.lower() for item in values]
        executable = lowered[0].replace("\\", "/").rsplit("/", 1)[-1]
        for suffix in (".exe", ".cmd", ".bat", ".com"):
            if executable.endswith(suffix):
                executable = executable[: -len(suffix)]
                break
        joined = " ".join(lowered[:4])
        denied = (
            executable
            in {
                "bash",
                "cmd",
                "dash",
                "fish",
                "git",
                "nu",
                "powershell",
                "pwsh",
                "scp",
                "sftp",
                "sh",
                "ssh",
                "wsl",
                "zsh",
            }
            or " publish" in f" {joined}"
            or " login" in f" {joined}"
            or " logout" in f" {joined}"
        )
        if denied:
            raise InvalidCommand("publishing, authentication, and remote Git commands are blocked")
        return values

    def _run(self, argv: List[str], timeout_seconds: float) -> Dict[str, Any]:
        timeout = min(max(float(timeout_seconds), 0.1), 3600.0)
        env = child_process_environment()
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                argv,
                cwd=self.repository,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
            )
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            completed = None
            timed_out = True
            stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        if completed is not None:
            stdout, stderr = completed.stdout, completed.stderr
            exit_code: Optional[int] = completed.returncode
        else:
            exit_code = None
        stdout = stdout[-self.MAX_OUTPUT_BYTES :]
        stderr = stderr[-self.MAX_OUTPUT_BYTES :]
        return {
            "argv": redact(argv),
            "exit_code": exit_code,
            "stdout": redact(stdout),
            "stderr": redact(stderr),
            "timed_out": timed_out,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        }

    def _run_check(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        argv, timeout = self._prepare_check(arguments, deadline_seconds)
        result = self._run(argv, timeout)
        display_argv = result["argv"]
        result["_evidence"] = [
            EvidenceRecord(
                kind="check",
                summary=("Passed" if result["exit_code"] == 0 else "Failed")
                + f": {' '.join(display_argv)}",
                command=display_argv,
                exit_code=result["exit_code"],
                metadata={"timed_out": result["timed_out"]},
            )
        ]
        return result

    def _prepare_check(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> tuple[List[str], float]:
        raw = self._required(arguments, "argv", list)
        argv = self._validate_process(raw)
        requested_timeout = arguments.get("timeout_seconds", 120.0)
        timeout = float(requested_timeout)
        if not math.isfinite(timeout) or timeout <= 0:
            raise InvalidCommand("timeout_seconds must be positive")
        return argv, min(timeout, deadline_seconds)

    def _git(self, arguments: List[str]) -> Dict[str, Any]:
        result = self._run(["git", *arguments], 60.0)
        if result["timed_out"]:
            raise CoreError("Git command timed out")
        return result

    def _git_status(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        if arguments:
            raise InvalidCommand("git.status takes no arguments")
        return self._git(["status", "--short", "--branch"])

    def _git_diff(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        staged = arguments.get("staged", False)
        if not isinstance(staged, bool):
            raise InvalidCommand("staged must be boolean")
        command = ["diff", "--no-ext-diff"]
        if staged:
            command.append("--cached")
        result = self._git(command)
        result["sha256"] = hashlib.sha256(result["stdout"].encode("utf-8")).hexdigest()
        return result

    def _audit(self, event: str, data: Dict[str, Any]) -> None:
        if self.audit_path is None:
            return
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": time.time(),
            "event": event,
            "data": redact(data),
        }
        with self.audit_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
