"""Provider-independent local action boundary for KaroX vNext."""

from __future__ import annotations

from .core_support import (  # noqa: F401
    WRITE_MODE_CREATE as WRITE_MODE_CREATE,
    WRITE_MODE_APPEND as WRITE_MODE_APPEND,
    VerificationRule as VerificationRule,
    _ripgrep_executable as _ripgrep_executable,
)

from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from .event_bus import EventBus
from .core_execute import CoreExecuteMixin
from .core_validation import CoreValidationMixin
from .core_files import CoreFilesMixin
from .core_git import GitToolsMixin
from .core_process import (
    ProcessToolsMixin,
    probe_interpreters,
)
from .core_search import SearchToolsMixin
from .core_types import (
    CoreError as CoreError,
    InvalidCommand as InvalidCommand,
    InvalidPath as InvalidPath,
    ToolDefinition as ToolDefinition,
    _path_label as _path_label,
)
from .models import Capability, CoreCommand as CoreCommand
from .policy import CapabilityPolicy
from .risk_engine import RiskEngine
from .sessions import SessionError as SessionError, SessionRecord, SessionStore















# How repo.write_file treats the file it names. ``create`` replaces the whole
# file; ``append`` adds to the end of a file that already exists, so a large new
# file can be written in pieces instead of being reproduced in one response.









class CoreRuntime(
    CoreExecuteMixin,
    CoreValidationMixin,
    CoreFilesMixin,
    GitToolsMixin,
    SearchToolsMixin,
    ProcessToolsMixin,
):
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
                    "properties": {
                        "staged": {"type": "boolean"},
                        "complete": {
                            "type": "boolean",
                            "description": (
                                "Read the whole change since the session's start "
                                "revision: staged and unstaged tracked edits plus "
                                "every new untracked file. Cannot be combined "
                                "with staged or with paths."
                            ),
                        },
                    },
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






    # Argument spellings models commonly use for the read/search tools. Measured
    # SWE runs (swer5-k5..swer8-k10) lost turns to refusals such as
    # "unknown arguments: ['end']" or "missing arguments: ['query']" where the
    # intent was unambiguous. Only unambiguous, read-shaped aliases are mapped;
    # anything else still reaches strict validation and is refused as before.
    _SEARCH_QUERY_ALIASES = ("text", "q", "search", "term", "string", "needle")
    _SEARCH_GLOB_ALIASES = ("glob", "include", "file_pattern", "files")
    _PATH_ALIASES = ("dir", "directory", "folder", "file", "file_path")
    _GLOB_CHARS = frozenset("*?[]{}")






















