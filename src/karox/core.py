"""Provider-independent local action boundary for KaroX vNext."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from .event_bus import EventBus, EventKind, EventLevel
from .atomic_io import atomic_write
from .core_git import GitToolsMixin
from .core_process import (
    WILDCARD_ARGUMENT,
    ProcessToolsMixin,
    is_plain_argument,
    probe_interpreters,
)
from .core_search import SearchToolsMixin
from .core_types import (
    CoreError,
    InvalidCommand,
    InvalidPath,
    ToolDefinition,
    _path_label,
)
from .models import Capability, CoreCommand, CoreResult, EvidenceRecord
from .policy import CapabilityPolicy
from .risk_engine import ConfirmationRejected, RiskEngine, SmartStopRequired
from .risk_mapping import action_for_command
from .security import (
    contains_credential,
    redact,
    redact_content,
)
from .sessions import MutationLease, SessionError, SessionRecord, SessionStore















# How repo.write_file treats the file it names. ``create`` replaces the whole
# file; ``append`` adds to the end of a file that already exists, so a large new
# file can be written in pieces instead of being reproduced in one response.
WRITE_MODE_CREATE = "create"
WRITE_MODE_APPEND = "append"





@dataclass(frozen=True)
class VerificationRule:
    """One user-approved check command.

    An entry whose last element is ``*`` is a prefix rule: the literal prefix
    must match position for position and every remaining argument must satisfy
    :func:`karox.core_process.is_plain_argument`.  Any other entry keeps the exact-argv meaning it
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
        return all(is_plain_argument(item) for item in argv[len(self.prefix) :])


@lru_cache(maxsize=1)
def _ripgrep_executable() -> Optional[str]:
    """Resolve ripgrep once per process; PATH is stable for a running bridge."""
    return shutil.which("rg")


class CoreRuntime(GitToolsMixin, SearchToolsMixin, ProcessToolsMixin):
    MAX_FILE_BYTES = 2_000_000

    # A read may return less than the file holds, but never without saying so.
    MAX_READ_CONTENT_CHARS = 1_000_000
    MAX_OUTPUT_BYTES = 1_000_000
    MAX_AUDIT_BYTES = 10_000_000
    MAX_COMMIT_MESSAGE_BYTES = 4_000
    MAX_COMMIT_PATHS = 100
    MAX_SEARCH_FILES = 2_000
    # On Windows, spawning git/rg costs roughly 90-140 ms even for tiny repos.
    # Probe only a small tree in-process; if it exceeds this bound (or contains
    # a symlink/scan ambiguity), fall back to the existing native backend.
    SMALL_SEARCH_FILE_PROBE_LIMIT = 384
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
        risk: Optional[RiskEngine] = None,
        events: Optional[EventBus] = None,
        session_repository_validator: Optional[Callable[[SessionRecord, Path], None]] = None,
        allow_probes: bool = False,
    ) -> None:
        self.repository = repository.expanduser().resolve(strict=True)
        if not self.repository.is_dir():
            raise CoreError(f"repository is not a directory: {self.repository}")
        self.policy = policy
        self.sessions = sessions
        self.audit_path = audit_path.expanduser().resolve() if audit_path else None
        self._mcp_binding = mcp_binding
        self._session_repository_validator = session_repository_validator
        # Tool availability is stable for one runtime process. Resolve ripgrep
        # once during runtime construction so the first user search does not pay
        # the very high Windows PATH-discovery cost when rg is absent.
        self._ripgrep_path: Optional[str] = _ripgrep_executable()
        # Capability policy answers whether this origin may ever do this kind of
        # thing; the RiskEngine answers whether this specific instance is safe
        # to do now. Both are required, and neither replaces the other.
        self._risk = risk
        self._events = events
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
        # The set answers "is this argv approved"; no order survives a set, and a
        # caller that has to pick one approved check (the harness does, when the
        # model never asked for one) must pick the first the user gave rather than
        # the lexicographically first, which is arbitrary.
        self._verification_command_order: tuple[tuple[str, ...], ...] = (
            ()
            if self._verification_rules is None
            else tuple(rule.as_tuple() for rule in self._verification_rules)
        )
        # Reproduction probes: the approved Python interpreter may also run a
        # snippet, a test module or a repository script through checks.run.
        # They are never verification evidence (core_process.probe_python_command).
        self._allow_probes = bool(allow_probes)
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
            "lsp.diagnostics": self._lsp_diagnostics,
            "git.commit": self._git_commit,
            "git.push": self._git_push,
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
                        "mode": {
                            "type": "string",
                            "enum": [WRITE_MODE_CREATE, WRITE_MODE_APPEND],
                            "description": (
                                "create replaces the whole file and is the "
                                "default. append adds content to the end of a "
                                "file that already exists, which is how a large "
                                "new file is written without reproducing all of "
                                "it in one response."
                            ),
                        },
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
                        "query": {
                            "type": "string",
                            "description": (
                                "The text, or the regular expression when "
                                "regex=true, to find inside files."
                            ),
                        },
                        "pattern": {
                            "type": "string",
                            "description": (
                                "Glob limiting which files are searched, "
                                "repository-relative, for example "
                                "'src/**/*.py'. Defaults to every file."
                            ),
                        },
                        "path": {
                            "type": "string",
                            "description": (
                                "Repository-relative file or directory to "
                                "search in, for example 'src/pkg'. Shorthand "
                                "for a pattern; ignored when pattern is given."
                            ),
                        },
                        "regex": {"type": "boolean"},
                        "case_sensitive": {"type": "boolean"},
                        "max_results": {"type": "number"},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            ),
            "lsp.diagnostics": ToolDefinition(
                "lsp.diagnostics",
                "Read bounded diagnostics from an allowlisted installed language server for one repository file.",
                Capability.DIAGNOSTICS_READ,
                False,
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "timeout_seconds": {"type": "number"},
                    },
                    "required": ["path"],
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
            "git.push": ToolDefinition(
                "git.push",
                "Push current HEAD to an existing configured Git remote and branch. Requires a one-shot user approval and never force-pushes, changes remotes, or authenticates.",
                Capability.GIT_PUSH,
                True,
                {
                    "type": "object",
                    "properties": {
                        "remote": {"type": "string"},
                        "branch": {"type": "string"},
                    },
                    "required": ["remote", "branch"],
                    "additionalProperties": False,
                },
                replayable=False,
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

    def register_tool(
        self,
        definition: ToolDefinition,
        handler: Callable[[Dict[str, Any], float], Dict[str, Any]],
    ) -> None:
        """Attach one typed tool to this runtime.

        Construction-time wiring for surfaces the product builds around the
        core (browser, desktop): the definition joins ``tools()`` so a kernel
        advertises it, and the handler executes through the same ``execute``
        gate as the built-in tools -- capability decision, Smart Stop, audit
        and evidence. Registration after the first run is not supported, and
        a name collision is a hard error rather than a silent override.
        """

        if definition.name in self._definitions or definition.name in self._handlers:
            raise CoreError(f"Core tool already registered: {definition.name}")
        handlers = dict(self._handlers)
        handlers[definition.name] = handler
        self._handlers = handlers
        definitions = dict(self._definitions)
        definitions[definition.name] = definition
        self._definitions = definitions

    def tools(self) -> List[ToolDefinition]:
        return list(self._definitions.values())

    @property
    def verification_commands(self) -> Optional[frozenset[tuple[str, ...]]]:
        return self._verification_commands

    @property
    def verification_command_order(self) -> tuple[tuple[str, ...], ...]:
        """The approved commands in the order the user declared them."""
        return self._verification_command_order

    @property
    def probe_interpreters(self) -> tuple[str, ...]:
        """Approved Python interpreters that may run probes; empty when off."""
        if not self._allow_probes or self._verification_rules is None:
            return ()
        return probe_interpreters(self._verification_rules)

    def _apply_smart_stop(self, command: CoreCommand) -> None:
        """Run the source-independent risk gate for one command.

        ``CoreRuntime`` is the single place every agent reaches the machine, so
        it is the only correct place to enforce Smart Stop. An API model, a
        sponsor API, ChatGPT Web, an MCP client and a subagent all arrive here,
        and all get the same verdict for the same action.

        No engine means no gate, which keeps every existing embedder working
        exactly as before; a runtime that wants Smart Stop passes one in.
        """

        if self._risk is None:
            return
        action = action_for_command(command)
        try:
            assessment = self._risk.authorize(
                action, confirmation_token=command.confirmation_token
            )
        except SmartStopRequired as stop:
            self._publish_risk(stop.assessment, allowed=False, reason="confirmation_required")
            self._audit(
                "core.command.stopped",
                {
                    "session_id": command.session_id,
                    "origin": command.origin.key,
                    "command": command.name,
                    "correlation_id": command.correlation_id,
                    "risk": stop.assessment.level.value,
                    "reasons": list(stop.assessment.reasons),
                },
            )
            raise
        except ConfirmationRejected as rejected:
            self._publish_risk(
                self._risk.assess(action), allowed=False, reason=rejected.reason
            )
            self._audit(
                "core.command.confirmation_rejected",
                {
                    "session_id": command.session_id,
                    "origin": command.origin.key,
                    "command": command.name,
                    "correlation_id": command.correlation_id,
                    "reason": rejected.reason,
                },
            )
            raise
        self._publish_risk(assessment, allowed=True, reason="allowed")

    def _publish_risk(self, assessment: Any, *, allowed: bool, reason: str) -> None:
        """Announce a risk decision on the event stream, never the token."""

        if self._events is None:
            return
        try:
            self._events.publish(
                EventKind.RISK_DECISION,
                session_id=assessment.session_id,
                summary=f"{assessment.kind}: {assessment.level.value}",
                level=EventLevel.INFO if allowed else EventLevel.WARNING,
                data={
                    "allowed": allowed,
                    "reason": reason,
                    "risk": assessment.level.value,
                    "reasons": list(assessment.reasons),
                    "action_digest": assessment.action_digest,
                    "preview": dict(assessment.preview),
                },
            )
        except Exception:
            # Observability must never be able to block or fail an action.
            return

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
        command = self._normalize_command(command)
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
        self._apply_smart_stop(command)
        # Smart Stop may strengthen a mutation by creating a durable rollback
        # checkpoint. That safety write advances the session revision under the
        # same fenced lease; continue from the fresh record so idempotency
        # reservation cannot race its own checkpoint into StaleSessionRevision.
        # Reloading also revalidates repository/session revocation before the
        # side effect, which is the safer boundary even for a no-op policy pass.
        if definition.mutates:
            record = self._load_session(command)
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
                    self._validate_idempotent_replay(command, result)
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
        if self._session_repository_validator is None:
            self.sessions.validate_repository(record, self.repository)
        else:
            self._session_repository_validator(record, self.repository)
        if record.revoked:
            raise SessionError("session access has been revoked")
        if record.access_profile != self.policy.profile.value:
            raise SessionError("session and runtime access profiles differ")
        return record

    def _validate_idempotent_replay(
        self, command: CoreCommand, result: CoreResult
    ) -> None:
        """Refuse a stored file result after the repository moved past it.

        An immediate retry after a lost response must replay safely: the target
        file still has the digest produced by the first attempt.  A later edit,
        formatter, checkout, or second client may change the same file, though.
        Returning the old success in that state claims a mutation happened when
        it did not.  File mutations therefore replay only while their recorded
        post-state is still the current post-state.  Other replayable operations
        keep their existing semantics.
        """
        if command.name not in {"repo.write_file", "repo.edit_file"}:
            return
        path_value = result.data.get("path")
        expected_digest = result.data.get("sha256")
        if (
            not isinstance(path_value, str)
            or not isinstance(expected_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None
        ):
            raise InvalidCommand("stored idempotent file result is invalid")
        path = self.safe_path(path_value)
        current_digest = self._file_sha256(path) if path.is_file() else None
        if current_digest != expected_digest:
            raise InvalidCommand(
                "stored idempotent result no longer matches repository state; "
                "use a new idempotency key"
            )

    # Argument spellings models commonly use for the read/search tools. Measured
    # SWE runs (swer5-k5..swer8-k10) lost turns to refusals such as
    # "unknown arguments: ['end']" or "missing arguments: ['query']" where the
    # intent was unambiguous. Only unambiguous, read-shaped aliases are mapped;
    # anything else still reaches strict validation and is refused as before.
    _SEARCH_QUERY_ALIASES = ("text", "q", "search", "term", "string", "needle")
    _SEARCH_GLOB_ALIASES = ("glob", "include", "file_pattern", "files")
    _PATH_ALIASES = ("dir", "directory", "folder", "file", "file_path")
    _GLOB_CHARS = frozenset("*?[]{}")

    def _repo_relative_alias(self, value: Any) -> Any:
        """An absolute path inside the repository, as a repository-relative one."""
        if not isinstance(value, str) or not value:
            return value
        candidate = value.replace("\\", "/")
        try:
            root = Path(self.repository).resolve().as_posix().rstrip("/")
        except Exception:
            return value
        if candidate == root:
            return "."
        if candidate.startswith(root + "/"):
            return candidate[len(root) + 1 :] or "."
        return value

    def _normalize_command(self, command: CoreCommand) -> CoreCommand:
        arguments = command.arguments
        if not isinstance(arguments, dict):
            return command
        normalized = self._normalize_arguments(command.name, dict(arguments))
        if normalized == arguments:
            return command
        from dataclasses import replace as _dc_replace

        self._audit(
            "core.command.arguments_normalized",
            {
                "session_id": command.session_id,
                "command": command.name,
                "from": sorted(arguments),
                "to": sorted(normalized),
            },
        )
        return _dc_replace(command, arguments=normalized)

    def _unwrap_argument_wrapper(
        self, name: str, args: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Flatten a call that wrapped its fields in ``payload``/``arguments``.

        The race logged models copying ``browser.command``'s shape into a
        desktop tool: every declared field arrived inside ``payload``, so the
        call was refused as unknown arguments even though the intended action
        was unambiguous. The repair is deliberately narrow: exactly one
        undeclared key, named ``payload`` or ``arguments``, holding a non-empty
        object whose every key the tool declares. The flattened call still goes
        through the same validation, policy and risk gates as a flat one.
        """
        definition = self._definitions.get(name)
        if definition is None or not isinstance(args, dict) or not args:
            return args
        schema = definition.input_schema
        if not isinstance(schema, dict) or schema.get("additionalProperties") is not False:
            return args
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            return args
        unknown = set(args).difference(properties)
        if len(unknown) != 1:
            return args
        wrapper = next(iter(unknown))
        if wrapper not in {"payload", "arguments"}:
            return args
        inner = args[wrapper]
        if not isinstance(inner, dict) or not inner:
            return args
        if not set(inner).issubset(properties):
            return args
        merged = dict(inner)
        # A declared sibling (for example app_id beside the wrapper) wins: the
        # wrapper only supplies the fields the caller did not pass directly.
        merged.update({key: value for key, value in args.items() if key != wrapper})
        return merged

    def _normalize_arguments(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        args = self._unwrap_argument_wrapper(name, args)
        if name == "repo.search":
            if "query" not in args:
                for alias in self._SEARCH_QUERY_ALIASES:
                    if isinstance(args.get(alias), str):
                        args["query"] = args.pop(alias)
                        break
            if "query" not in args and isinstance(args.get("pattern"), str):
                # ripgrep habit: the search text passed as "pattern". Only when it
                # cannot be a glob, so a real file filter is never reinterpreted.
                pattern = args["pattern"]
                if pattern and not (set(pattern) & self._GLOB_CHARS) and "/" not in pattern:
                    args["query"] = args.pop("pattern")
            if "pattern" not in args:
                for alias in self._SEARCH_GLOB_ALIASES:
                    if isinstance(args.get(alias), str):
                        args["pattern"] = args.pop(alias)
                        break
            if "path" not in args:
                for alias in self._PATH_ALIASES:
                    if isinstance(args.get(alias), str):
                        args["path"] = args.pop(alias)
                        break
            for key in ("pattern", "path"):
                if key in args:
                    args[key] = self._repo_relative_alias(args[key])
            return args
        if name == "repo.read_lines":
            if "path" not in args:
                for alias in self._PATH_ALIASES:
                    if isinstance(args.get(alias), str):
                        args["path"] = args.pop(alias)
                        break
            if "start" not in args:
                for alias in ("start_line", "line", "from", "line_start"):
                    if alias in args:
                        args["start"] = args.pop(alias)
                        break
            end = None
            for alias in ("end", "end_line", "to", "line_end", "stop"):
                if alias in args:
                    end = args.pop(alias)
                    break
            if "count" not in args:
                for alias in ("limit", "lines", "num_lines", "n"):
                    if alias in args:
                        args["count"] = args.pop(alias)
                        break
            if end is not None and "count" not in args:
                start = args.get("start", 1)
                if (
                    isinstance(end, int)
                    and not isinstance(end, bool)
                    and isinstance(start, int)
                    and not isinstance(start, bool)
                    and end >= start >= 1
                ):
                    # Inclusive end line, as editors and sed ranges use it.
                    args["count"] = min(end - start + 1, 1_500)
                else:
                    args["end"] = end  # keep it so strict validation explains
            if "path" in args:
                args["path"] = self._repo_relative_alias(args["path"])
            return args
        if name == "repo.read_file":
            if "path" not in args:
                for alias in self._PATH_ALIASES:
                    if isinstance(args.get(alias), str):
                        args["path"] = args.pop(alias)
                        break
            # A range on a whole-file read: the whole file still answers it, so
            # the range is dropped instead of refusing the read.
            if "path" in args:
                for key in ("start", "count", "end", "limit", "offset", "start_line", "end_line"):
                    args.pop(key, None)
                args["path"] = self._repo_relative_alias(args["path"])
            return args
        if name == "repo.edit_file" and isinstance(args.get("edits"), list):
            edits = []
            for item in args["edits"]:
                if isinstance(item, dict) and (
                    ("old_string" in item and "old" not in item)
                    or ("new_string" in item and "new" not in item)
                ):
                    item = dict(item)
                    if "old_string" in item and "old" not in item:
                        item["old"] = item.pop("old_string")
                    if "new_string" in item and "new" not in item:
                        item["new"] = item.pop("new_string")
                edits.append(item)
            args["edits"] = edits
            return args
        return args

    @staticmethod
    def _validate_arguments(
        definition: ToolDefinition, arguments: Dict[str, Any]
    ) -> None:
        """Enforce the one JSON-Schema subset every Core tool declares.

        Built-in tools and registered external tools (MCP, browser, desktop)
        are validated by the same code on purpose. The desktop surface shipped
        integer coordinates that the older built-in validator did not know, so
        every click, scroll and window-id attach was refused before its handler
        ran. One validator cannot drift from the schemas it reads.
        """
        if not isinstance(arguments, dict):
            raise InvalidCommand("Core command arguments must be an object")
        CoreRuntime._validate_external_schema(definition.input_schema, "arguments")
        CoreRuntime._validate_external_value(
            arguments, definition.input_schema, "arguments", definition.name
        )

    @staticmethod
    def _schema_type_names(schema: Dict[str, Any], label: str) -> List[str]:
        """The declared type names, validated; empty when no type is declared."""

        if "type" not in schema:
            return []
        declared = schema["type"]
        if isinstance(declared, str):
            names = [declared]
        elif (
            isinstance(declared, list)
            and declared
            and all(isinstance(item, str) for item in declared)
            and len(set(declared)) == len(declared)
        ):
            names = declared
        else:
            raise InvalidCommand(f"{label} schema type is malformed")
        supported = {
            "null", "boolean", "integer", "number", "string", "array", "object"
        }
        unsupported = set(names).difference(supported)
        if unsupported:
            raise InvalidCommand(
                f"{label} schema uses unsupported types: {sorted(unsupported)}"
            )
        return names

    @staticmethod
    def _validate_external_schema(schema: Any, label: str) -> None:
        """Check the declared subset, not just the call, before trusting it."""

        if not isinstance(schema, dict):
            raise InvalidCommand(f"{label} schema must be an object")
        CoreRuntime._schema_type_names(schema, label)
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
            CoreRuntime._validate_external_schema(schema["items"], f"{label}.items")
        if "additionalProperties" in schema:
            additional = schema["additionalProperties"]
            if not isinstance(additional, bool):
                CoreRuntime._validate_external_schema(
                    additional, f"{label}.additionalProperties"
                )
        if "enum" in schema:
            options = schema["enum"]
            if not isinstance(options, list) or not options:
                raise InvalidCommand(
                    f"{label} schema enum must be a non-empty array"
                )
        for key in ("minimum", "maximum"):
            if key in schema:
                bound = schema[key]
                if (
                    isinstance(bound, bool)
                    or not isinstance(bound, (int, float))
                    or not math.isfinite(float(bound))
                ):
                    raise InvalidCommand(
                        f"{label} schema {key} must be a finite number"
                    )
        for key in ("minLength", "maxLength", "minItems", "maxItems"):
            if key in schema:
                limit = schema[key]
                if (
                    isinstance(limit, bool)
                    or not isinstance(limit, int)
                    or limit < 0
                ):
                    raise InvalidCommand(
                        f"{label} schema {key} must be a non-negative integer"
                    )
        if "pattern" in schema and not isinstance(schema["pattern"], str):
            raise InvalidCommand(f"{label} schema pattern must be a string")

    @staticmethod
    def _json_equal(left: Any, right: Any) -> bool:
        """JSON equality: ``1`` equals ``1.0`` but never equals ``true``."""

        if isinstance(left, bool) or isinstance(right, bool):
            return (
                isinstance(left, bool)
                and isinstance(right, bool)
                and left == right
            )
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            return left == right
        if isinstance(left, dict) and isinstance(right, dict):
            return set(left) == set(right) and all(
                CoreRuntime._json_equal(left[key], right[key]) for key in left
            )
        if isinstance(left, list) and isinstance(right, list):
            return len(left) == len(right) and all(
                CoreRuntime._json_equal(a, b) for a, b in zip(left, right)
            )
        return type(left) is type(right) and left == right

    @staticmethod
    def _validate_constraints(value: Any, schema: Dict[str, Any], label: str) -> None:
        """Enforce the bounds the schema declares, on the value it describes."""

        options = schema.get("enum")
        if isinstance(options, list) and options:
            if not any(CoreRuntime._json_equal(value, option) for option in options):
                raise InvalidCommand(f"{label} must be one of {options}")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            minimum = schema.get("minimum")
            if (
                isinstance(minimum, (int, float))
                and not isinstance(minimum, bool)
                and value < minimum
            ):
                raise InvalidCommand(f"{label} must be at least {minimum}")
            maximum = schema.get("maximum")
            if (
                isinstance(maximum, (int, float))
                and not isinstance(maximum, bool)
                and value > maximum
            ):
                raise InvalidCommand(f"{label} must be at most {maximum}")
        if isinstance(value, str):
            min_length = schema.get("minLength")
            if (
                isinstance(min_length, int)
                and not isinstance(min_length, bool)
                and len(value) < min_length
            ):
                raise InvalidCommand(
                    f"{label} must be at least {min_length} characters"
                )
            max_length = schema.get("maxLength")
            if (
                isinstance(max_length, int)
                and not isinstance(max_length, bool)
                and len(value) > max_length
            ):
                raise InvalidCommand(
                    f"{label} must be at most {max_length} characters"
                )
            pattern = schema.get("pattern")
            if isinstance(pattern, str):
                try:
                    compiled = re.compile(pattern)
                except re.error:
                    # A remote schema may use ECMA-262 syntax Python rejects.
                    # The keyword is advisory there; the type and length checks
                    # still hold, so the call is not refused for a dialect gap.
                    compiled = None
                if compiled is not None and compiled.search(value) is None:
                    raise InvalidCommand(f"{label} does not match {pattern!r}")
        if isinstance(value, list):
            min_items = schema.get("minItems")
            if (
                isinstance(min_items, int)
                and not isinstance(min_items, bool)
                and len(value) < min_items
            ):
                raise InvalidCommand(f"{label} must contain at least {min_items} items")
            max_items = schema.get("maxItems")
            if (
                isinstance(max_items, int)
                and not isinstance(max_items, bool)
                and len(value) > max_items
            ):
                raise InvalidCommand(f"{label} must contain at most {max_items} items")

    @staticmethod
    def _validate_external_value(
        value: Any, schema: Any, label: str, tool_name: str = ""
    ) -> None:
        """Enforce the safe JSON-Schema subset understood by Core.

        Unknown keywords are intentionally ignored so a valid remote schema is
        not rejected merely because Core does not implement every draft
        feature. Declared constraints are enforced: a schema that promises an
        enum or a bound and does not hold it is documentation that lies.
        """

        if not isinstance(schema, dict):
            raise InvalidCommand(f"{label} schema must be an object")
        type_names = CoreRuntime._schema_type_names(schema, label)
        if type_names:
            if not any(
                CoreRuntime._external_type_matches(value, item) for item in type_names
            ):
                expected = " or ".join(type_names)
                raise InvalidCommand(f"{label} must be {expected}")
        CoreRuntime._validate_constraints(value, schema, label)
        if isinstance(value, dict):
            properties = schema.get("properties")
            properties = properties if isinstance(properties, dict) else {}
            required = schema.get("required")
            if isinstance(required, list) and all(
                isinstance(item, str) for item in required
            ):
                missing = set(required).difference(value)
                if missing:
                    raise InvalidCommand(f"missing arguments: {sorted(missing)}")
            if schema.get("additionalProperties") is False:
                unknown = set(value).difference(properties)
                if unknown:
                    # Name the accepted arguments so the model can repair the
                    # call in one step instead of concluding the tool is
                    # unusable (the race logged exactly that failure mode).
                    hint = (
                        f"; {tool_name} accepts: {sorted(properties)}"
                        if tool_name
                        else ""
                    )
                    raise InvalidCommand(
                        f"unknown arguments: {sorted(unknown)}{hint}"
                    )
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
                    CoreRuntime._validate_external_value(
                        item, items, f"{label}[{index}]"
                    )

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
                "argv": result.data.get("argv", []),
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
        relative, encoded, path, mode = self._prepare_write(arguments)
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
        atomic_write(path, encoded, previous_mode)
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
        self, arguments: Dict[str, Any]
    ) -> tuple[str, bytes, Path, str]:
        relative = self._required(arguments, "path", str)
        content = self._required(arguments, "content", str)
        encoded = content.encode("utf-8")
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
