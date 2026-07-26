"""Provider-independent local action boundary for KaroX vNext."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import signal
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from .models import Capability, CoreCommand, CoreResult, EvidenceRecord
from .policy import CapabilityPolicy
from .security import (
    child_process_environment,
    contains_credential,
    redact,
    redact_content,
)
from .sessions import MutationLease, SessionError, SessionRecord, SessionStore


class CoreError(RuntimeError):
    pass


class InvalidPath(CoreError):
    pass


class InvalidCommand(CoreError):
    pass


WILDCARD_ARGUMENT = "*"

# Options that hand a program text to the executable instead of a file to work
# on.  A rule's literal prefix is what the user actually approved; the wildcard
# tail is not, so it must not be able to turn "run my test suite" into "run
# whatever you like" by appending an interpreter's own code-execution flag.
_CODE_EXECUTION_OPTIONS = frozenset(
    {"-c", "-e", "--code", "--command", "--eval", "--exec", "--execute"}
)
# Short options may carry their value in the same token (``python -cCODE``) and
# may be bundled with other short options (``python -Bc CODE``), so a whole-token
# comparison alone would let the very flag above straight back in.
_CODE_EXECUTION_SHORT_LETTERS = frozenset({"c", "e"})


def _is_plain_argument(value: str) -> bool:
    """True when a wildcard tail argument cannot make the child run new code."""
    if not value.startswith("-"):
        return True
    name = value.split("=", 1)[0].lower()
    if name in _CODE_EXECUTION_OPTIONS:
        return False
    if value.startswith("--"):
        return True
    return not _CODE_EXECUTION_SHORT_LETTERS.intersection(name[1:])


@dataclass(frozen=True)
class VerificationRule:
    """One user-approved check command.

    An entry whose last element is ``*`` is a prefix rule: the literal prefix
    must match position for position and every remaining argument must satisfy
    :func:`_is_plain_argument`.  Any other entry keeps the exact-argv meaning it
    had before prefix rules existed.
    """

    prefix: tuple[str, ...]
    wildcard: bool

    @classmethod
    def parse(cls, entry: Iterable[str]) -> "VerificationRule":
        values = tuple(entry)
        if not values or not all(
            isinstance(item, str) and item for item in values
        ):
            raise CoreError(
                "a verification command must contain non-empty strings"
            )
        wildcard = values[-1] == WILDCARD_ARGUMENT
        prefix = values[:-1] if wildcard else values
        if WILDCARD_ARGUMENT in prefix:
            raise CoreError(
                f"{WILDCARD_ARGUMENT!r} is only meaningful as the last argument "
                "of a verification command"
            )
        # Without a literal prefix the rule would approve every executable, which
        # is the one thing this allowlist exists to prevent.
        if not prefix:
            raise CoreError("a verification command must name an executable")
        return cls(prefix, wildcard)

    def as_tuple(self) -> tuple[str, ...]:
        return self.prefix + ((WILDCARD_ARGUMENT,) if self.wildcard else ())

    def matches(self, argv: Sequence[str]) -> bool:
        if not self.wildcard:
            return tuple(argv) == self.prefix
        if tuple(argv[: len(self.prefix)]) != self.prefix:
            return False
        return all(_is_plain_argument(item) for item in argv[len(self.prefix) :])


_WINDOWS_JOB_LIMIT_KILL_ON_CLOSE = 0x2000
_WINDOWS_JOB_EXTENDED_LIMIT_INFORMATION = 9
_WINDOWS_PROCESS_TERMINATE = 0x0001
_WINDOWS_PROCESS_SET_QUOTA = 0x0100


@lru_cache(maxsize=1)
def _windows_job_api() -> Optional[tuple[Any, Any]]:
    """Win32 Job Object entry points, or None where they are unavailable."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class _BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimits),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
        ]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        return kernel32, _ExtendedLimits
    except Exception:
        return None


def _new_process_group_kwargs() -> Dict[str, Any]:
    # Windows has no process groups that survive the parent for this purpose, so
    # containment there comes from the Job Object rather than from spawn flags.
    return {} if os.name == "nt" else {"start_new_session": True}


def _kill_posix_process_group(pid: int) -> None:
    try:
        group = os.getpgid(pid)
    except OSError:
        return
    # If the child is not its own group leader then start_new_session did not
    # take effect and the group is ours, so signalling it would kill KaroX and
    # everything else sharing the terminal.
    if group != pid:
        return
    try:
        os.killpg(group, signal.SIGTERM)
    except OSError:
        return
    time.sleep(0.2)
    try:
        os.killpg(group, signal.SIGKILL)
    except OSError:
        pass


def _kill_windows_process_tree(pid: int) -> None:
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


class ProcessTree:
    """A spawned child together with the descendants it goes on to create.

    Waiting on a timeout and killing only the direct child leaves the workers of
    a parallel test run alive, still holding the captured output handle and any
    file locks they took, so the next check inherits a broken workspace.  This
    keeps the whole tree reachable so it can be swept in one go.  It bounds
    cleanup, nothing else: the child still runs with the repository as its
    working directory and with whatever access the operating system gives it.
    """

    KILL_GRACE_SECONDS = 5.0

    def __init__(self, process: "subprocess.Popen[bytes]") -> None:
        self._process = process
        self._job: Optional[Any] = None
        api = _windows_job_api()
        if api is None:
            return
        import ctypes

        kernel32, extended_limits = api
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return
        limits = extended_limits()
        limits.BasicLimitInformation.LimitFlags = _WINDOWS_JOB_LIMIT_KILL_ON_CLOSE
        # Assignment can only happen once the process exists, so a child that
        # spawns before the handle is opened is not covered. That window is a few
        # microseconds against an interpreter start-up of tens of milliseconds.
        handle = kernel32.OpenProcess(
            _WINDOWS_PROCESS_SET_QUOTA | _WINDOWS_PROCESS_TERMINATE,
            False,
            process.pid,
        )
        assigned = False
        if handle:
            assigned = bool(
                kernel32.SetInformationJobObject(
                    job,
                    _WINDOWS_JOB_EXTENDED_LIMIT_INFORMATION,
                    ctypes.byref(limits),
                    ctypes.sizeof(limits),
                )
            ) and bool(kernel32.AssignProcessToJobObject(job, handle))
            kernel32.CloseHandle(handle)
        if assigned:
            self._job = job
        else:
            kernel32.CloseHandle(job)

    def terminate(self) -> None:
        if self._job is not None:
            api = _windows_job_api()
            if api is not None:
                api[0].TerminateJobObject(self._job, 1)
        elif os.name == "nt":
            _kill_windows_process_tree(self._process.pid)
        else:
            _kill_posix_process_group(self._process.pid)
        try:
            self._process.kill()
        except OSError:
            pass
        try:
            self._process.wait(timeout=self.KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass

    def close(self) -> None:
        if self._job is None:
            return
        api = _windows_job_api()
        if api is not None:
            # The job carries KILL_ON_JOB_CLOSE, so releasing the last handle
            # also collects anything the finished check left running.
            api[0].CloseHandle(self._job)
        self._job = None


@dataclass(frozen=True)
class CapturedStream:
    text: str
    sha256: str
    truncated: bool
    total_bytes: int
    elided_bytes: int


@dataclass(frozen=True)
class CheckPlan:
    argv: List[str]
    requested_timeout: float
    effective_timeout: float
    clamped_by: Optional[str]
    verification_eligible: bool


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    capability: Capability
    mutates: bool
    input_schema: Dict[str, Any]
    additional_capabilities: tuple[Capability, ...] = ()
    external_schema: bool = False
    # A mutating tool normally replays its stored outcome for a repeated
    # idempotency key. A check must not: its answer describes a working tree that
    # may have changed since, and replaying it reports a stale pass as fresh.
    replayable: bool = True


class CoreRuntime:
    MAX_FILE_BYTES = 2_000_000
    # A read may return less than the file holds, but never without saying so.
    MAX_READ_CONTENT_CHARS = 1_000_000
    MAX_OUTPUT_BYTES = 1_000_000
    MAX_AUDIT_BYTES = 10_000_000
    MAX_COMMIT_MESSAGE_BYTES = 4_000
    MAX_COMMIT_PATHS = 100
    MAX_SEARCH_FILES = 2_000
    MAX_SEARCH_RESULTS = 200
    MAX_SEARCH_LINE_BYTES = 2_000
    MIN_PROCESS_TIMEOUT_SECONDS = 0.1
    MAX_PROCESS_TIMEOUT_SECONDS = 3600.0
    DEFAULT_CHECK_TIMEOUT_SECONDS = 120.0

    def __init__(
        self,
        repository: Path,
        policy: CapabilityPolicy,
        sessions: SessionStore,
        audit_path: Optional[Path] = None,
        mcp_binding: Optional[Any] = None,
        verification_commands: Optional[Iterable[Iterable[str]]] = None,
    ) -> None:
        self.repository = repository.expanduser().resolve(strict=True)
        if not self.repository.is_dir():
            raise CoreError(f"repository is not a directory: {self.repository}")
        self.policy = policy
        self.sessions = sessions
        self.audit_path = audit_path.expanduser().resolve() if audit_path else None
        self._mcp_binding = mcp_binding
        self._verification_rules: Optional[tuple[VerificationRule, ...]] = (
            None
            if verification_commands is None
            else tuple(
                VerificationRule.parse(item) for item in verification_commands
            )
        )
        self._verification_commands = (
            None
            if self._verification_rules is None
            else frozenset(rule.as_tuple() for rule in self._verification_rules)
        )
        self._handlers: Mapping[
            str, Callable[[Dict[str, Any], float], Dict[str, Any]]
        ] = {
            "repo.read_file": self._read_file,
            "repo.write_file": self._write_file,
            "repo.list_files": self._list_files,
            "checks.run": self._run_check,
            "git.status": self._git_status,
            "git.diff": self._git_diff,
            "repo.search": self._search,
            "git.commit": self._git_commit,
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
                        "allow_secret_literal": {
                            "type": "boolean",
                            "description": (
                                "Write text that looks like a credential on "
                                "purpose, such as a secret-scanner fixture or a "
                                "documentation example. Recorded in the result "
                                "and the evidence."
                            ),
                        },
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
                replayable=False,
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
            "repo.search": ToolDefinition(
                "repo.search",
                "Search repository text files for a literal or regular expression.",
                Capability.REPO_READ,
                False,
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "pattern": {"type": "string"},
                        "regex": {"type": "boolean"},
                        "case_sensitive": {"type": "boolean"},
                        "max_results": {"type": "number"},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            ),
            "git.commit": ToolDefinition(
                "git.commit",
                "Commit an explicit list of repository paths. Never pushes.",
                Capability.GIT_COMMIT,
                True,
                {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string"},
                        "paths": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["message", "paths"],
                    "additionalProperties": False,
                },
            ),
        }
        if self._mcp_binding is not None:
            dynamic = list(self._mcp_binding.definitions())
            names = [item.name for item in dynamic]
            if len(names) != len(set(names)):
                raise CoreError("dynamic MCP tools contain duplicate names")
            collisions = set(names).intersection(self._definitions)
            if collisions:
                raise CoreError(
                    f"dynamic MCP tools collide with Core tools: {sorted(collisions)}"
                )
            self._definitions.update({item.name: item for item in dynamic})

    def tools(self) -> List[ToolDefinition]:
        return list(self._definitions.values())

    @property
    def verification_commands(self) -> Optional[frozenset[tuple[str, ...]]]:
        return self._verification_commands

    def execute(
        self,
        command: CoreCommand,
        capability_token: Optional[str] = None,
        lease: Optional[MutationLease] = None,
    ) -> CoreResult:
        definition = self._definitions.get(command.name)
        handler = self._handlers.get(command.name)
        is_mcp = (
            definition is not None
            and handler is None
            and self._mcp_binding is not None
            and command.name.startswith("mcp.")
        )
        if definition is None or (handler is None and not is_mcp):
            raise InvalidCommand(f"unknown Core command: {command.name}")
        self._validate_arguments(definition, command.arguments)
        try:
            input_digest = command.input_digest()
        except (TypeError, ValueError) as exc:
            raise InvalidCommand(
                "Core command arguments must contain strict JSON values"
            ) from exc
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
                "input_digest": input_digest,
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
            if definition.replayable:
                replay = self.sessions.begin_idempotent(
                    record,
                    lease,
                    command.idempotency_key,
                    input_digest,
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
            if is_mcp:
                data = self._mcp_binding.execute(
                    command.name,
                    dict(command.arguments),
                    record,
                )
            else:
                assert handler is not None
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
        process_result = command.name in {
            "checks.run",
            "git.status",
            "git.diff",
            "git.commit",
        }
        result = CoreResult(
            ok=not (
                process_result
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
            if definition.replayable:
                self.sessions.complete_idempotent(
                    record,
                    lease,
                    command.idempotency_key,
                    result.to_dict(),
                )
            else:
                # There is no idempotency entry to complete, but the evidence and
                # the check log this call just appended still have to reach disk.
                self.sessions.save(record, record.revision, lease)
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
        if definition.external_schema:
            CoreRuntime._validate_external_schema(
                definition.input_schema, "arguments"
            )
            CoreRuntime._validate_external_value(arguments, definition.input_schema, "arguments")
            return
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
            if expected_name == "number" and not math.isfinite(float(value)):
                if name == "timeout_seconds":
                    raise InvalidCommand(
                        "timeout_seconds must be positive and finite"
                    )
                raise InvalidCommand(f"{name} must be finite")
            item_type = properties[name].get("items", {}).get("type")
            if item_type and isinstance(value, list):
                item_kind = kinds.get(item_type)
                if item_kind is None or not all(
                    isinstance(item, item_kind) for item in value
                ):
                    raise InvalidCommand(f"{name} items must be {item_type}")

    @staticmethod
    def _validate_external_schema(schema: Any, label: str) -> None:
        if not isinstance(schema, dict):
            raise InvalidCommand(f"{label} schema must be an object")
        if "type" in schema:
            declared = schema["type"]
            if isinstance(declared, str):
                type_names = [declared]
            elif (
                isinstance(declared, list)
                and declared
                and all(isinstance(item, str) for item in declared)
                and len(set(declared)) == len(declared)
            ):
                type_names = declared
            else:
                raise InvalidCommand(f"{label} schema type is malformed")
            supported = {
                "null", "boolean", "integer", "number", "string", "array", "object"
            }
            unsupported = set(type_names).difference(supported)
            if unsupported:
                raise InvalidCommand(
                    f"{label} schema uses unsupported types: {sorted(unsupported)}"
                )
        if "properties" in schema:
            properties = schema["properties"]
            if not isinstance(properties, dict):
                raise InvalidCommand(f"{label} schema properties must be an object")
            for name, child in properties.items():
                if not isinstance(name, str):
                    raise InvalidCommand(
                        f"{label} schema property names must be strings"
                    )
                CoreRuntime._validate_external_schema(
                    child, f"{label}.properties[{name!r}]"
                )
        if "required" in schema:
            required = schema["required"]
            if (
                not isinstance(required, list)
                or not all(isinstance(item, str) for item in required)
                or len(set(required)) != len(required)
            ):
                raise InvalidCommand(
                    f"{label} schema required must contain unique strings"
                )
        if "items" in schema:
            CoreRuntime._validate_external_schema(
                schema["items"], f"{label}.items"
            )
        if "additionalProperties" in schema:
            additional = schema["additionalProperties"]
            if not isinstance(additional, bool):
                CoreRuntime._validate_external_schema(
                    additional, f"{label}.additionalProperties"
                )

    @staticmethod
    def _validate_external_value(value: Any, schema: Any, label: str) -> None:
        """Enforce the safe JSON-Schema subset understood by Core.

        Unknown keywords are intentionally ignored so a valid remote schema is
        not rejected merely because Core does not implement every draft feature.
        """
        if not isinstance(schema, dict):
            raise InvalidCommand(f"{label} schema must be an object")
        declared = schema.get("type")
        type_names: list[str] = []
        if "type" in schema:
            if isinstance(declared, str):
                type_names = [declared]
            elif (
                isinstance(declared, list)
                and declared
                and all(isinstance(item, str) for item in declared)
            ):
                type_names = declared
            else:
                raise InvalidCommand(f"{label} schema type is malformed")
            supported = {
                "null", "boolean", "integer", "number", "string", "array", "object"
            }
            unsupported = set(type_names).difference(supported)
            if unsupported:
                raise InvalidCommand(
                    f"{label} schema uses unsupported types: {sorted(unsupported)}"
                )
            matches = any(
                CoreRuntime._external_type_matches(value, item) for item in type_names
            )
            if not matches:
                expected = " or ".join(type_names)
                raise InvalidCommand(f"{label} must be {expected}")
        if isinstance(value, dict):
            properties = schema.get("properties")
            properties = properties if isinstance(properties, dict) else {}
            required = schema.get("required")
            if isinstance(required, list) and all(isinstance(item, str) for item in required):
                missing = set(required).difference(value)
                if missing:
                    raise InvalidCommand(f"missing arguments: {sorted(missing)}")
            if schema.get("additionalProperties") is False:
                unknown = set(value).difference(properties)
                if unknown:
                    raise InvalidCommand(f"unknown arguments: {sorted(unknown)}")
            additional = schema.get("additionalProperties")
            for name, item in value.items():
                child_schema = properties.get(name)
                if not isinstance(child_schema, dict) and isinstance(additional, dict):
                    child_schema = additional
                if isinstance(child_schema, dict):
                    CoreRuntime._validate_external_value(item, child_schema, name)
        elif isinstance(value, list):
            items = schema.get("items")
            if isinstance(items, dict):
                for index, item in enumerate(value):
                    CoreRuntime._validate_external_value(item, items, f"{label}[{index}]")

    @staticmethod
    def _external_type_matches(value: Any, type_name: str) -> bool:
        if type_name == "null":
            return value is None
        if type_name == "boolean":
            return isinstance(value, bool)
        if type_name == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if type_name == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return False
            return not isinstance(value, float) or math.isfinite(value)
        kinds = {"string": str, "array": list, "object": dict}
        kind = kinds.get(type_name)
        # External schemas are an authorization boundary: an unknown declared
        # type must never turn a mixed-type declaration into an allow-all rule.
        return False if kind is None else isinstance(value, kind)

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
        if command.name == "checks.run":
            record.checks.append(
                {
                    "correlation_id": command.correlation_id,
                    "ok": result.ok,
                    "argv": result.data.get("argv", []),
                    "exit_code": result.data.get("exit_code"),
                    "timed_out": result.data.get("timed_out", False),
                    "effective_timeout": result.data.get("effective_timeout"),
                    "timeout_clamped_by": result.data.get("timeout_clamped_by"),
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
        elif command_name == "git.commit":
            self._prepare_commit(arguments)

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
            raw_content = path.read_bytes()
            content = raw_content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CoreError("repo.read_file supports UTF-8 text only") from exc
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
        return payload

    def _write_file(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        relative, encoded, path = self._prepare_write(arguments)
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
                        },
                    )
                ],
            }
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                if previous_mode is not None and hasattr(os, "fchmod"):
                    os.fchmod(handle.fileno(), previous_mode)
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
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
            "previous_sha256": previous_digest,
            "sha256": digest,
            "secret_literal_allowed": bypassed,
            "_evidence": [
                EvidenceRecord(
                    kind="file_write",
                    summary=(
                        f"Wrote {relative} with the credential scanner overridden"
                        if bypassed
                        else f"Wrote {relative}"
                    ),
                    artifact_sha256=digest,
                    metadata={
                        "path": relative,
                        "bytes": len(encoded),
                        "changed": True,
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
        self, arguments: Dict[str, Any]
    ) -> tuple[str, bytes, Path]:
        relative = self._required(arguments, "path", str)
        content = self._required(arguments, "content", str)
        encoded = content.encode("utf-8")
        if len(encoded) > self.MAX_FILE_BYTES:
            raise CoreError(f"content is larger than {self.MAX_FILE_BYTES} bytes")
        if contains_credential(content) and not self._secret_literal_allowed(
            arguments
        ):
            raise CoreError(
                "write blocked by credential scanner; pass allow_secret_literal "
                "to author a fixture or documentation example on purpose"
            )
        path = self.safe_path(relative, for_write=True)
        return relative, encoded, path

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
        timeout = min(
            max(float(timeout_seconds), self.MIN_PROCESS_TIMEOUT_SECONDS),
            self.MAX_PROCESS_TIMEOUT_SECONDS,
        )
        env = child_process_environment()
        started = time.perf_counter()
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            process = subprocess.Popen(
                argv,
                cwd=self.repository,
                env=env,
                stdout=stdout_file,
                stderr=stderr_file,
                shell=False,
                **_new_process_group_kwargs(),
            )
            tree = ProcessTree(process)
            try:
                try:
                    exit_code: Optional[int] = process.wait(timeout=timeout)
                    timed_out = False
                except subprocess.TimeoutExpired:
                    timed_out = True
                    exit_code = None
                    tree.terminate()
                except BaseException:
                    # An interrupt while waiting must not orphan the tree either.
                    tree.terminate()
                    raise
            finally:
                tree.close()
            stdout = self._bounded_stream(stdout_file)
            stderr = self._bounded_stream(stderr_file)
        return {
            "argv": redact(argv),
            "exit_code": exit_code,
            "stdout": str(redact(stdout.text)),
            "stderr": str(redact(stderr.text)),
            "timed_out": timed_out,
            "stdout_sha256": stdout.sha256,
            "stderr_sha256": stderr.sha256,
            "stdout_truncated": stdout.truncated,
            "stderr_truncated": stderr.truncated,
            "stdout_bytes": stdout.total_bytes,
            "stderr_bytes": stderr.total_bytes,
            "stdout_elided_bytes": stdout.elided_bytes,
            "stderr_elided_bytes": stderr.elided_bytes,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        }

    def _bounded_stream(self, handle: Any) -> CapturedStream:
        handle.flush()
        handle.seek(0)
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = handle.read(64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        limit = max(0, int(self.MAX_OUTPUT_BYTES))
        if size <= limit:
            handle.seek(0)
            body = handle.read(size)
            return CapturedStream(
                body.decode("utf-8", errors="ignore"),
                digest.hexdigest(),
                False,
                size,
                0,
            )
        # A compiler or a test runner states the root cause first and then repeats
        # it in a summary, so returning only the tail throws away the half of the
        # output that explains the failure.
        head_budget = (limit + 1) // 2
        tail_budget = limit - head_budget
        handle.seek(0)
        head = handle.read(head_budget)
        tail = b""
        if tail_budget:
            handle.seek(size - tail_budget)
            tail = handle.read(tail_budget)
        elided = size - head_budget - tail_budget
        text = (
            head.decode("utf-8", errors="ignore")
            + f"\n[karox: {elided} bytes elided from the middle of this stream]\n"
            + tail.decode("utf-8", errors="ignore")
        )
        return CapturedStream(text, digest.hexdigest(), True, size, elided)

    def _run_check(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        plan = self._prepare_check(arguments, deadline_seconds)
        result = self._run(plan.argv, plan.effective_timeout)
        result["verification_eligible"] = plan.verification_eligible
        # A caller that asked for ten minutes and silently got the host's deadline
        # cannot tell a genuine hang from a clamp, so both numbers are reported.
        result["requested_timeout"] = plan.requested_timeout
        result["effective_timeout"] = plan.effective_timeout
        result["timeout_clamped_by"] = plan.clamped_by
        if result["timed_out"]:
            detail = (
                f"the check was stopped after {plan.effective_timeout:g}s and its "
                "process tree was terminated"
            )
            if plan.clamped_by is not None:
                detail += (
                    f"; the requested {plan.requested_timeout:g}s was reduced by "
                    f"{plan.clamped_by}"
                )
            result["detail"] = detail
        display_argv = result["argv"]
        result["_evidence"] = [
            EvidenceRecord(
                kind="check",
                summary=("Passed" if result["exit_code"] == 0 else "Failed")
                + f": {' '.join(display_argv)}",
                command=display_argv,
                exit_code=result["exit_code"],
                metadata={
                    "timed_out": result["timed_out"],
                    "effective_timeout": plan.effective_timeout,
                    "timeout_clamped_by": plan.clamped_by,
                },
            )
        ]
        return result

    def _prepare_check(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> CheckPlan:
        raw = self._required(arguments, "argv", list)
        argv = self._validate_process(raw)
        eligible = self._verification_rules is not None and any(
            rule.matches(argv) for rule in self._verification_rules
        )
        # Refusing here rather than after the run keeps a rejected command from
        # reserving a durable idempotency intent that then needs reconciliation.
        if self._verification_rules is not None and not eligible:
            raise InvalidCommand(
                "check command is not in the user-approved verification set"
            )
        requested = float(
            arguments.get("timeout_seconds", self.DEFAULT_CHECK_TIMEOUT_SECONDS)
        )
        if not math.isfinite(requested) or requested <= 0:
            raise InvalidCommand("timeout_seconds must be positive")
        effective = requested
        clamped_by: Optional[str] = None
        if effective > float(deadline_seconds):
            effective = float(deadline_seconds)
            clamped_by = "request_deadline"
        if effective > self.MAX_PROCESS_TIMEOUT_SECONDS:
            effective = self.MAX_PROCESS_TIMEOUT_SECONDS
            clamped_by = "runtime_maximum"
        if effective < self.MIN_PROCESS_TIMEOUT_SECONDS:
            effective = self.MIN_PROCESS_TIMEOUT_SECONDS
            clamped_by = "runtime_minimum"
        return CheckPlan(argv, requested, effective, clamped_by, eligible)

    def _git(self, arguments: List[str], deadline_seconds: float) -> Dict[str, Any]:
        return self._run(["git", *arguments], min(60.0, deadline_seconds))

    @staticmethod
    def _git_evidence(kind: str, result: Dict[str, Any]) -> EvidenceRecord:
        digest = result.get("stdout_sha256") or hashlib.sha256(
            result["stdout"].encode("utf-8")
        ).hexdigest()
        result["sha256"] = digest
        return EvidenceRecord(
            kind=kind,
            summary=("Read" if result["exit_code"] == 0 else "Failed to read")
            + f" {kind.replace('_', ' ')}",
            command=result["argv"],
            exit_code=result["exit_code"],
            artifact_sha256=digest,
            metadata={"timed_out": result["timed_out"]},
        )

    def _git_status(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        if arguments:
            raise InvalidCommand("git.status takes no arguments")
        result = self._git(["status", "--short", "--branch"], deadline_seconds)
        result["_evidence"] = [self._git_evidence("git_status", result)]
        return result

    def _git_diff(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        staged = arguments.get("staged", False)
        if not isinstance(staged, bool):
            raise InvalidCommand("staged must be boolean")
        command = ["diff", "--no-ext-diff"]
        if staged:
            command.append("--cached")
        result = self._git(command, deadline_seconds)
        result["_evidence"] = [self._git_evidence("git_diff", result)]
        return result

    def _prepare_commit(self, arguments: Dict[str, Any]) -> tuple[str, List[str]]:
        message = self._required(arguments, "message", str)
        if not message.strip():
            raise InvalidCommand("commit message must not be empty")
        if len(message.encode("utf-8")) > self.MAX_COMMIT_MESSAGE_BYTES:
            raise InvalidCommand(
                f"commit message is longer than {self.MAX_COMMIT_MESSAGE_BYTES} bytes"
            )
        if any(ord(item) < 32 and item != "\n" for item in message):
            raise InvalidCommand("commit message contains control characters")
        if contains_credential(message):
            raise InvalidCommand("commit message blocked by credential scanner")
        raw_paths = self._required(arguments, "paths", list)
        if not raw_paths or len(raw_paths) > self.MAX_COMMIT_PATHS:
            raise InvalidCommand(
                f"paths must contain 1-{self.MAX_COMMIT_PATHS} entries"
            )
        relatives: List[str] = []
        for item in raw_paths:
            if not isinstance(item, str):
                raise InvalidCommand("paths items must be string")
            resolved = self.safe_path(item)
            relative = resolved.relative_to(self.repository).as_posix()
            if relative in relatives:
                raise InvalidCommand(f"duplicate commit path: {relative}")
            relatives.append(relative)
        return message, relatives

    def _commit_evidence(
        self,
        result: Dict[str, Any],
        relatives: List[str],
        committed: bool,
        commit_sha: Optional[str],
    ) -> EvidenceRecord:
        return EvidenceRecord(
            kind="git_commit",
            summary=("Committed " if committed else "Failed to commit ")
            + f"{len(relatives)} path(s)",
            command=result["argv"],
            exit_code=result["exit_code"],
            artifact_sha256=commit_sha,
            metadata={
                "timed_out": result["timed_out"],
                "paths": list(relatives),
                "committed": committed,
            },
        )

    def _git_commit(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        message, relatives = self._prepare_commit(arguments)
        staged = self._git(["add", "--", *relatives], deadline_seconds)
        if staged["timed_out"] or staged["exit_code"] != 0:
            staged["paths"] = list(relatives)
            staged["committed"] = False
            staged["commit_sha"] = None
            staged["stage_failed"] = True
            staged["_evidence"] = [
                self._commit_evidence(staged, relatives, False, None)
            ]
            return staged
        result = self._git(
            [
                "commit",
                "--no-verify",
                "--only",
                "--message",
                message,
                "--",
                *relatives,
            ],
            deadline_seconds,
        )
        committed = not result["timed_out"] and result["exit_code"] == 0
        commit_sha: Optional[str] = None
        if committed:
            revision = self._git(["rev-parse", "HEAD"], deadline_seconds)
            if revision["exit_code"] == 0:
                commit_sha = revision["stdout"].strip() or None
        result["paths"] = list(relatives)
        result["committed"] = committed
        result["commit_sha"] = commit_sha
        result["stage_failed"] = False
        result["_evidence"] = [
            self._commit_evidence(result, relatives, committed, commit_sha)
        ]
        return result

    def _search(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        query = self._required(arguments, "query", str)
        if not query or len(query) > 1_000 or "\x00" in query:
            raise InvalidCommand("query must contain 1-1000 characters")
        use_regex = arguments.get("regex", False)
        if not isinstance(use_regex, bool):
            raise InvalidCommand("regex must be boolean")
        case_sensitive = arguments.get("case_sensitive", False)
        if not isinstance(case_sensitive, bool):
            raise InvalidCommand("case_sensitive must be boolean")
        requested = arguments.get("max_results", self.MAX_SEARCH_RESULTS)
        if isinstance(requested, bool) or not isinstance(requested, (int, float)):
            raise InvalidCommand("max_results must be number")
        if not math.isfinite(float(requested)) or float(requested) < 1:
            raise InvalidCommand("max_results must be a positive number")
        limit = min(int(requested), self.MAX_SEARCH_RESULTS)
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            matcher = re.compile(query if use_regex else re.escape(query), flags)
        except re.error as exc:
            raise InvalidCommand(f"invalid regular expression: {exc}") from exc

        listing = self._list_files(
            {"pattern": arguments.get("pattern", "**/*")}, deadline_seconds
        )
        candidates = listing["files"][: self.MAX_SEARCH_FILES]
        truncated = bool(listing["truncated"]) or len(listing["files"]) > len(
            candidates
        )
        matches: List[Dict[str, Any]] = []
        scanned = 0
        skipped = 0
        for relative in candidates:
            if len(matches) >= limit:
                truncated = True
                break
            try:
                path = self.safe_path(relative)
                if path.stat().st_size > self.MAX_FILE_BYTES:
                    skipped += 1
                    continue
                text = path.read_bytes().decode("utf-8")
            except (InvalidPath, OSError, UnicodeDecodeError):
                skipped += 1
                continue
            scanned += 1
            for number, line in enumerate(text.splitlines(), start=1):
                if not matcher.search(line):
                    continue
                encoded = line.encode("utf-8")
                clipped = len(encoded) > self.MAX_SEARCH_LINE_BYTES
                if clipped:
                    line = encoded[: self.MAX_SEARCH_LINE_BYTES].decode(
                        "utf-8", errors="ignore"
                    )
                matches.append(
                    {
                        "path": relative,
                        "line": number,
                        # A matched line is repository content a caller may quote
                        # back, so it follows the same byte-faithful rule as a
                        # read rather than the display rule used for audit rows.
                        "text": str(redact_content(line)),
                        "clipped": clipped,
                    }
                )
                if len(matches) >= limit:
                    truncated = True
                    break
        return {
            "query": query,
            "regex": use_regex,
            "case_sensitive": case_sensitive,
            "files_scanned": scanned,
            "files_skipped": skipped,
            "match_count": len(matches),
            "matches": matches,
            "truncated": truncated,
        }

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
