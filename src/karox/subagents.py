"""General-purpose karo-core workers for delegated research, coding and checks.

A worker is a real engine run, not a second agent loop.  ``WorkerEngine``
subclasses the product's :class:`karox.core_engine.CoreEngine`, so every worker
uses the vendored karo-core ``DefaultAgent`` loop, its models, its
environments and its packaged protocol templates.  What this module adds is
everything the parent needs to delegate safely:

* an isolated model context -- one fresh agent instance and history per
  worker, never shared and never visible to the parent's context;
* explicit tools and capabilities -- every shell command passes a per-kind
  command filter and a :class:`karox.policy.CapabilityPolicy` decision for the
  worker's own ``subagent`` origin, whose grants are the intersection of the
  kind's profile with what the parent origin holds;
* workspace isolation -- a scratch directory, a bounded copy of named inputs,
  or the repository itself for trusted coding workers;
* hard budgets -- steps, wall-clock seconds and estimated tokens, all enforced
  by the engine's own exit protocol;
* cancellation -- cooperative, checked before every model call and every
  command, with the reason recorded in the result;
* structured evidence -- per-worker NDJSON event streams, a saved trajectory,
  command records and evidence the parent reconciles into one verdict.

The narrow integration surface is :class:`WorkerTask` (what to run),
:class:`SubagentManager` (runs them, concurrently, cancellably) and
:func:`reconcile` (turns results into one answerable outcome).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Sequence, Tuple

from .command_guard import DeveloperCommandBlocked, validate_developer_command_argv
from .core_engine import CoreEngine, CoreEngineConfig
from .engine import __version__ as engine_version
from .engine.agents.default import DefaultAgent
from .engine.exceptions import LimitsExceeded
from .models import Capability, EvidenceRecord, Origin, OriginKind
from .policy import CapabilityPolicy, PolicyDenied
from .paths import runtime_dir

ModelFactory = Callable[[CoreEngineConfig], Any]

_WORKER_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
#: Command separators that end one command and start another.  The scanner is
#: quote-aware because the engine protocol routinely sends
#: ``python -c "import a; import b"`` -- a blind split on ``;`` would cut
#: inside a quoted argument and deny ordinary commands.  It is still a policy
#: gate in front of the capability check and the per-command timeout, not a
#: sandbox: quoted text is preserved, not interpreted.
_SEPARATORS = frozenset({";", "|", "&", "\n"})

WORKSPACE_MODES = ("scratch", "copy", "repository")

#: Directories a workspace walk never descends into, and never reports.
IGNORED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".karox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "target",
        "venv",
    }
)

#: Read-only shell heads a research worker may use.  ``git`` is additionally
#: restricted to its read subcommands.
_READ_ONLY_HEADS = frozenset(
    {
        "cat",
        "cd",
        "diff",
        "echo",
        "file",
        "find",
        "git",
        "grep",
        "head",
        "ls",
        "rg",
        "sort",
        "tail",
        "uniq",
        "wc",
    }
)
_GIT_READ_SUBCOMMANDS = frozenset(
    {
        "blame",
        "branch",
        "cat-file",
        "describe",
        "diff",
        "for-each-ref",
        "grep",
        "log",
        "ls-files",
        "ls-tree",
        "merge-base",
        "name-rev",
        "rev-list",
        "rev-parse",
        "shortlog",
        "show",
        "show-ref",
        "status",
    }
)
#: Check-running heads a verification worker may use.
_CHECKS_HEADS = frozenset(
    {
        "cat",
        "cd",
        "echo",
        "git",
        "ls",
        "py",
        "python",
        "python3",
        "pytest",
    }
)
#: Heads no worker may use, whatever its kind.
_ALWAYS_BLOCKED_HEADS = frozenset({"sudo", "doas", "su"})

class WorkerKind(str, Enum):
    RESEARCH = "research"
    CODING = "coding"
    VERIFICATION = "verification"
    GENERAL = "general"


#: Capabilities each worker kind requests for its ``subagent`` origin.  The
#: manager intersects these with the parent origin's effective grants, so a
#: worker can never hold more than its parent.  ``CapabilityPolicy.decide``
#: still intersects with the session profile at check time.
_KIND_CAPABILITIES: Dict["WorkerKind", FrozenSet[Capability]] = {
    WorkerKind.RESEARCH: frozenset({Capability.REPO_READ, Capability.GIT_READ}),
    WorkerKind.VERIFICATION: frozenset(
        {Capability.REPO_READ, Capability.CHECKS_RUN, Capability.GIT_READ}
    ),
    WorkerKind.CODING: frozenset(
        {
            Capability.REPO_READ,
            Capability.REPO_WRITE,
            Capability.PROCESS_RUN,
            Capability.CHECKS_RUN,
            Capability.GIT_READ,
        }
    ),
    WorkerKind.GENERAL: frozenset(
        {Capability.REPO_READ, Capability.PROCESS_RUN, Capability.CHECKS_RUN, Capability.GIT_READ}
    ),
}
_KIND_COMMAND_CAPABILITY: Dict["WorkerKind", Capability] = {
    WorkerKind.RESEARCH: Capability.REPO_READ,
    WorkerKind.VERIFICATION: Capability.CHECKS_RUN,
    WorkerKind.CODING: Capability.PROCESS_RUN,
    WorkerKind.GENERAL: Capability.PROCESS_RUN,
}


class WorkerCancelled(RuntimeError):
    """Raised when a worker refuses to continue after cancellation."""


class CancellationToken:
    """A cooperative cancel flag with the reason the run stopped."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._reason = "requested"
        self._lock = threading.Lock()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    def cancel(self, reason: str = "requested") -> None:
        with self._lock:
            self._reason = str(reason)[:500] or "requested"
        self._event.set()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled:
            raise WorkerCancelled(self.reason)


@dataclass(frozen=True)
class WorkerBudgets:
    """Hard per-worker limits; all are enforced, none are advisory."""

    max_steps: int = 8
    max_seconds: float = 120.0
    max_tokens: Optional[int] = None
    command_timeout: float = 60.0
    context_token_budget: int = 32_000

    def __post_init__(self) -> None:
        if isinstance(self.max_steps, bool) or not 1 <= self.max_steps <= 256:
            raise ValueError("max_steps must be between 1 and 256")
        if (
            isinstance(self.max_seconds, bool)
            or not isinstance(self.max_seconds, (int, float))
            or not 1.0 <= float(self.max_seconds) <= 3600.0
        ):
            raise ValueError("max_seconds must be between 1 and 3600")
        if self.max_tokens is not None and (
            isinstance(self.max_tokens, bool)
            or not isinstance(self.max_tokens, int)
            or self.max_tokens < 1
        ):
            raise ValueError("max_tokens must be a positive integer")
        if (
            isinstance(self.command_timeout, bool)
            or not isinstance(self.command_timeout, (int, float))
            or not 1.0 <= float(self.command_timeout) <= 600.0
        ):
            raise ValueError("command_timeout must be between 1 and 600 seconds")
        if (
            isinstance(self.context_token_budget, bool)
            or not isinstance(self.context_token_budget, int)
            or not 4_000 <= self.context_token_budget <= 1_000_000
        ):
            raise ValueError("context_token_budget must be between 4000 and 1000000")


@dataclass(frozen=True)
class WorkerTask:
    """One delegated job.  Everything the worker may do is stated here."""

    worker_id: str
    kind: WorkerKind
    task: str
    repository: Path
    workspace_mode: str = "scratch"
    input_files: Tuple[str, ...] = ()
    model_factory: Optional[ModelFactory] = None
    model: str = "deepseek-ai/DeepSeek-V4.1-Flash"
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    budgets: WorkerBudgets = WorkerBudgets()
    cancellation: Optional[CancellationToken] = None
    #: For a verification worker: the worker_id whose work it verifies.
    verifies: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.worker_id, str) or not _WORKER_ID.fullmatch(self.worker_id):
            raise ValueError(
                "worker_id must match [a-z0-9][a-z0-9-]{0,63}"
            )
        if not isinstance(self.task, str) or not 1 <= len(self.task.strip()) <= 20_000:
            raise ValueError("task must contain 1-20000 characters")
        if self.workspace_mode not in WORKSPACE_MODES:
            raise ValueError(
                "workspace_mode must be one of: " + ", ".join(WORKSPACE_MODES)
            )
        if self.verifies is not None and self.verifies == self.worker_id:
            raise ValueError("verifies must name another worker, not the worker itself")


def _split_segments(command: str) -> List[str]:
    """Split a shell line at separators that sit outside quotes."""

    segments: List[str] = []
    current: List[str] = []
    quote: Optional[str] = None
    index = 0
    while index < len(command):
        char = command[index]
        if quote is not None:
            if (
                char == "\\"
                and quote == '"'
                and index + 1 < len(command)
                and command[index + 1] in {'"', "\\"}
            ):
                current.append(char)
                current.append(command[index + 1])
                index += 2
                continue
            if char == quote:
                quote = None
            current.append(char)
        elif char in {'"', "'"}:
            quote = char
            current.append(char)
        elif char in _SEPARATORS:
            segments.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1
    segments.append("".join(current))
    return [segment for segment in (item.strip() for item in segments) if segment]


def _confined(root: Path, target: Path) -> bool:
    try:
        common = os.path.commonpath(
            [os.path.normcase(str(root)), os.path.normcase(str(target))]
        )
    except ValueError:
        return False
    return common == os.path.normcase(str(root))


def _exit_message(status: str, submission: str = "", **extra: Any) -> LimitsExceeded:
    payload: Dict[str, Any] = {
        "role": "exit",
        "content": status,
        "extra": {"exit_status": status, "submission": submission, **extra},
    }
    return LimitsExceeded(payload)


class WorkerCommandPolicy:
    """The per-kind gate every shell command passes before it runs.

    Modes: ``read_only`` (research) allows observation commands only and pins
    git to read subcommands; ``checks`` (verification) allows check runners;
    ``workspace_write`` (coding/general) allows ordinary development commands
    inside the worker workspace.  All modes keep the global developer-command
    guard (no shells-as-heads trickery, no ssh/scp, no git push) and refuse a
    ``cd`` that would leave the workspace.
    """

    def __init__(self, kind: WorkerKind) -> None:
        if not isinstance(kind, WorkerKind):
            raise ValueError("kind must be a WorkerKind")
        self.kind = kind

    @property
    def mode(self) -> str:
        if self.kind is WorkerKind.RESEARCH:
            return "read_only"
        if self.kind is WorkerKind.VERIFICATION:
            return "checks"
        return "workspace_write"

    def allow(self, command: str, workspace: Path) -> Tuple[bool, str]:
        if not isinstance(command, str) or not command.strip():
            return False, "empty command"
        for segment in _split_segments(command):
            allowed, reason = self._allow_segment(segment, workspace)
            if not allowed:
                return False, reason
        return True, "allowed"

    def _allow_segment(self, segment: str, workspace: Path) -> Tuple[bool, str]:
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError as exc:
            return False, f"unparsable command segment: {exc}"
        tokens = [item for item in tokens if item]
        if not tokens:
            return True, "empty"
        try:
            validate_developer_command_argv(tokens)
        except DeveloperCommandBlocked as exc:
            return False, f"blocked by the developer command guard: {exc}"
        words = list(tokens)
        while words and _ENV_ASSIGNMENT.match(words[0]):
            words.pop(0)
        if not words:
            return True, "assignments only"
        head = os.path.basename(words[0].replace("\\", "/")).casefold()
        for suffix in (".exe", ".cmd", ".bat", ".com"):
            if head.endswith(suffix):
                head = head[: -len(suffix)]
                break
        if head in _ALWAYS_BLOCKED_HEADS:
            return False, f"{head} is not available to workers"
        if head == "cd":
            target = words[1] if len(words) > 1 else "."
            resolved = Path(target)
            if not resolved.is_absolute():
                resolved = workspace / target
            # normpath-level resolution: a plain commonpath does not fold
            # ``..``, so ``cd ..`` would look confined to the workspace.
            resolved = Path(os.path.normpath(str(resolved)))
            if not _confined(workspace, resolved):
                return False, "cd would leave the worker workspace"
            return True, "confined cd"
        if self.mode == "read_only":
            if head not in _READ_ONLY_HEADS:
                return False, f"{head} is not a read-only command"
            if head == "git" and not self._git_is_read_only(words):
                return False, "only git read subcommands are available to research workers"
            return True, "read-only command"
        if self.mode == "checks":
            if head not in _CHECKS_HEADS:
                return False, f"{head} is not a check command"
            if head == "git" and not self._git_is_read_only(words):
                return False, "only git read subcommands are available to verification workers"
            return True, "check command"
        return True, "workspace command"

    @staticmethod
    def _git_is_read_only(words: Sequence[str]) -> bool:
        for word in words[1:]:
            if word.startswith("-"):
                continue
            return word in _GIT_READ_SUBCOMMANDS
        return False


class WorkerEventSink:
    """Per-worker NDJSON events: one line per observable moment."""

    def __init__(self, path: Path, worker_id: str) -> None:
        self.path = path
        self.worker_id = worker_id
        self.events: List[Dict[str, Any]] = []
        self._sequence = 0

    def emit(self, event_type: str, **fields: Any) -> Dict[str, Any]:
        self._sequence += 1
        record = {
            "ts": round(time.time(), 3),
            "seq": self._sequence,
            "type": event_type,
            "worker_id": self.worker_id,
        }
        record.update({key: value for key, value in fields.items() if value is not None})
        self.events.append(record)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
        return record


class PolicyCommandRunner:
    """The engine's command runner with worker gates bolted in front.

    Checks, in order: cancellation, the per-kind command filter, the worker
    origin's capability, then the real execution with its own timeout.  Every
    denial and every executed command is recorded for the result and the
    event stream, so the parent can see exactly what the worker was allowed
    to do.
    """

    def __init__(
        self,
        *,
        policy: CapabilityPolicy,
        origin: Origin,
        kind: WorkerKind,
        workspace: Path,
        cancellation: CancellationToken,
        events: Optional[WorkerEventSink] = None,
    ) -> None:
        self.policy = policy
        self.origin = origin
        self.filter = WorkerCommandPolicy(kind)
        self.workspace = workspace
        self.cancellation = cancellation
        self.events = events
        self.denials: List[Dict[str, Any]] = []
        self.commands: List[Dict[str, Any]] = []

    def __call__(self, command: str, cwd: Path, timeout: int) -> Dict[str, Any]:
        if self.cancellation.is_cancelled:
            return _observation(
                f"worker cancelled: {self.cancellation.reason}", 125
            )
        allowed, reason = self.filter.allow(command, self.workspace)
        if not allowed:
            self._record_denial(command, "filter", reason)
            return _observation(f"command denied by worker policy: {reason}", 126)
        try:
            self.policy.require(
                self.origin, _KIND_COMMAND_CAPABILITY[self.filter.kind]
            )
        except PolicyDenied as exc:
            self._record_denial(command, "capability", str(exc))
            return _observation(f"command denied by worker policy: {exc}", 126)
        started = time.perf_counter()
        output, exit_code, timed_out = _execute(command, str(cwd), float(timeout))
        duration = round(time.perf_counter() - started, 3)
        record = {
            "command": command[:500],
            "exit_code": exit_code,
            "denied": False,
            "timed_out": timed_out,
            "duration_seconds": duration,
        }
        self.commands.append(record)
        if self.events is not None:
            self.events.emit(
                "worker.command",
                command=command[:500],
                exit_code=exit_code,
                timed_out=timed_out,
                duration_seconds=duration,
            )
        return {
            "output": output,
            "returncode": exit_code,
            "exception_info": "command timed out" if timed_out else "",
        }

    def _record_denial(self, command: str, stage: str, reason: str) -> None:
        record = {
            "command": command[:500],
            "stage": stage,
            "reason": reason,
            "timestamp": round(time.time(), 3),
        }
        self.denials.append(record)
        self.commands.append(
            {
                "command": command[:500],
                "exit_code": 126,
                "denied": True,
                "timed_out": False,
                "duration_seconds": 0.0,
            }
        )
        if self.events is not None:
            self.events.emit(
                "worker.command_denied", command=command[:500], stage=stage, reason=reason
            )


def _observation(message: str, returncode: int) -> Dict[str, Any]:
    return {"output": message, "returncode": returncode, "exception_info": ""}


def _execute(command: str, cwd: str, timeout: float) -> Tuple[str, int, bool]:
    process = subprocess.Popen(
        command,
        shell=True,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=os.name == "posix",
    )
    try:
        stdout, _ = process.communicate(timeout=timeout)
        return str(stdout or ""), int(process.returncode or 0), False
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(process.pid, 9)
        else:
            process.kill()
        stdout, _ = process.communicate()
        return str(stdout or ""), int(process.returncode or -1), True


class WorkerAgent(DefaultAgent):
    """The engine loop with cancellation and token accounting at query time.

    The overrides are deliberately tiny: everything else -- context economy,
    no-progress submission, finish handling, budgets -- is inherited unchanged
    from the vendored engine, so a worker behaves exactly like the product
    ``agent run`` loop it shares.
    """

    def __init__(
        self,
        model: Any,
        env: Any,
        *,
        cancellation: Optional[CancellationToken] = None,
        events: Optional[WorkerEventSink] = None,
        token_budget: int = 0,
        worker_id: str = "",
        **kwargs: Any,
    ) -> None:
        super().__init__(model, env, **kwargs)
        self.worker_cancellation = cancellation if cancellation is not None else CancellationToken()
        self.worker_events = events
        self.worker_token_budget = int(token_budget or 0)
        self.worker_id = worker_id
        self.tokens_used = 0

    def query(self) -> Dict[str, Any]:
        if self.worker_cancellation.is_cancelled:
            raise _exit_message(
                "Cancelled",
                cancellation_reason=self.worker_cancellation.reason,
            )
        request_estimate = self._estimate_tokens()
        message = super().query()
        self.tokens_used += self._tokens_for(message, request_estimate)
        if self.worker_events is not None:
            self.worker_events.emit(
                "worker.model_call",
                step=self.n_calls,
                tokens=self.tokens_used,
                cost=self.cost,
            )
        if 0 < self.worker_token_budget <= self.tokens_used:
            raise _exit_message(
                "TokenBudgetExceeded",
                tokens_used=self.tokens_used,
                token_budget=self.worker_token_budget,
            )
        return message

    @staticmethod
    def _tokens_for(message: Dict[str, Any], request_estimate: int) -> int:
        """Reported usage when the model provides it, chars//4 when not."""

        usage = (message.get("extra") or {}).get("usage")
        if isinstance(usage, dict):
            total = usage.get("total_tokens")
            if (
                isinstance(total, (int, float))
                and not isinstance(total, bool)
                and total >= 0
            ):
                return int(total)
            input_tokens = usage.get("input_tokens")
            output_tokens = usage.get("output_tokens")
            if (
                isinstance(input_tokens, (int, float))
                and not isinstance(input_tokens, bool)
                and isinstance(output_tokens, (int, float))
                and not isinstance(output_tokens, bool)
            ):
                return int(input_tokens) + int(output_tokens)
        response_chars = len(str(message.get("content") or ""))
        return request_estimate + response_chars // 4


class WorkerEngine(CoreEngine):
    """CoreEngine that builds a WorkerAgent and keeps it reachable.

    ``run()`` is inherited verbatim; the agent the run built is exposed as
    :attr:`worker_agent` so the manager can report the real exit status,
    token usage and cancellation reason instead of a flattened status string.
    """

    def __init__(
        self,
        config: CoreEngineConfig,
        *,
        cancellation: Optional[CancellationToken] = None,
        events: Optional[WorkerEventSink] = None,
        token_budget: int = 0,
        worker_id: str = "",
    ) -> None:
        super().__init__(config)
        self.worker_cancellation = cancellation
        self.worker_events = events
        self.worker_token_budget = token_budget
        self.worker_id = worker_id
        self.worker_agent: Optional[WorkerAgent] = None

    def _agent(self, model: Any, env: Any) -> WorkerAgent:
        options = dict(self._base.get("agent") or {})
        options.update(
            {
                "step_limit": self.config.max_steps,
                "wall_time_limit_seconds": self.config.max_seconds,
                "cost_limit": 0.0,
                "context_token_budget": self.config.context_token_budget,
                "output_path": self.config.trajectory_path,
            }
        )
        agent = WorkerAgent(
            model,
            env,
            cancellation=self.worker_cancellation,
            events=self.worker_events,
            token_budget=self.worker_token_budget,
            worker_id=self.worker_id,
            **options,
        )
        self.worker_agent = agent
        return agent


@dataclass
class WorkerResult:
    """The answerable outcome of one worker run."""

    worker_id: str
    kind: str
    status: str
    submission: str
    steps: int
    tokens: int
    cost: float
    wall_seconds: float
    workspace: str
    events_path: str
    trajectory_path: Optional[str]
    artifacts: List[Dict[str, Any]] = field(default_factory=list)
    commands: List[Dict[str, Any]] = field(default_factory=list)
    denials: List[Dict[str, Any]] = field(default_factory=list)
    evidence: List[EvidenceRecord] = field(default_factory=list)
    cancellation_reason: Optional[str] = None
    verifies: Optional[str] = None
    engine_version: str = engine_version
    exit_status: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "kind": self.kind,
            "status": self.status,
            "submission": self.submission,
            "steps": self.steps,
            "tokens": self.tokens,
            "cost": round(self.cost, 6),
            "wall_seconds": round(self.wall_seconds, 3),
            "workspace": self.workspace,
            "events_path": self.events_path,
            "trajectory_path": self.trajectory_path,
            "artifacts": list(self.artifacts),
            "commands": list(self.commands),
            "denials": list(self.denials),
            "evidence": [item.to_dict() for item in self.evidence],
            "cancellation_reason": self.cancellation_reason,
            "verifies": self.verifies,
            "engine_version": self.engine_version,
            "exit_status": self.exit_status,
        }


#: The engine's own exit labels, mapped onto what the parent reports.
_STATUS_BY_EXIT = {
    "Submitted": "submitted",
    "LimitsExceeded": "steps_exceeded",
    "TimeExceeded": "wall_time_exceeded",
    "RepeatedFormatError": "format_errors_exceeded",
    "Cancelled": "cancelled",
    "TokenBudgetExceeded": "token_budget_exceeded",
}


def _confined_repo_path(repository: Path, relative: str) -> Path:
    """Join a declared input path and refuse anything that escapes the repo."""

    target = repository.joinpath(*PurePosixPath(relative).parts)
    resolved = target.resolve(strict=True)
    if not _confined(repository, resolved):
        raise ValueError(f"input_files must stay inside the repository: {relative}")
    return resolved


def _scan_artifacts(
    workspace: Path, started: float, *, limit: int = 100
) -> List[Dict[str, Any]]:
    """Files the worker created or modified, with content digests.

    Only files whose mtime is at or after the run started are reported; files
    that merely existed in a copied workspace are inputs, not results.  The
    walk prunes dependency and metadata directories and is hard-capped so a
    large repository cannot turn the result into a filesystem dump.
    """

    artifacts: List[Dict[str, Any]] = []
    if not workspace.is_dir():
        return artifacts
    for root, directories, files in os.walk(workspace):
        directories[:] = sorted(
            item for item in directories if item not in IGNORED_DIRECTORY_NAMES
        )
        for name in sorted(files):
            path = Path(root) / name
            try:
                info = path.stat()
            except OSError:
                continue
            if info.st_mtime < started - 0.001:
                continue
            digest: Optional[str] = None
            if info.st_size <= 2 * 1024 * 1024:
                try:
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                except OSError:
                    digest = None
            artifacts.append(
                {
                    "path": path.relative_to(workspace).as_posix(),
                    "sha256": digest,
                    "bytes": int(info.st_size),
                }
            )
            if len(artifacts) >= limit:
                return artifacts
    return artifacts


def _worker_evidence(
    worker_id: str, kind: str, status: str, commands: Sequence[Dict[str, Any]]
) -> List[EvidenceRecord]:
    records: List[EvidenceRecord] = []
    for record in commands:
        if record.get("denied"):
            continue
        records.append(
            EvidenceRecord(
                kind="subagent_command",
                summary=(
                    f"worker {worker_id} command exited "
                    f"{record.get('exit_code')}"
                ),
                command=[str(record.get("command", ""))],
                exit_code=int(record.get("exit_code", -1)),
                metadata={
                    "worker_id": worker_id,
                    "kind": kind,
                    "timed_out": bool(record.get("timed_out")),
                    "duration_seconds": record.get("duration_seconds"),
                },
            )
        )
        if len(records) >= 64:
            break
    records.append(
        EvidenceRecord(
            kind="subagent_run",
            summary=f"worker {worker_id} ({kind}) finished as {status}",
            metadata={
                "worker_id": worker_id,
                "kind": kind,
                "status": status,
                "commands": len(commands),
            },
        )
    )
    return records


class SubagentManager:
    """Runs workers concurrently with per-worker isolation and cancellation.

    The policy is shared session state and is written before any worker
    thread starts (origin grants at submit time); workers only read it
    afterwards.  Workspaces, event streams, trajectories and tokens are
    strictly per worker, so results never mix.
    """

    MAX_WORKERS = 16
    MAX_INPUT_FILES = 200
    MAX_INPUT_FILE_BYTES = 2 * 1024 * 1024
    MAX_INPUT_TOTAL_BYTES = 20 * 1024 * 1024

    def __init__(
        self,
        policy: CapabilityPolicy,
        *,
        session_id: str = "default",
        root: Optional[Path] = None,
        parent_origin: Optional[Origin] = None,
        max_workers: int = 4,
        output_char_limit: int = 50_000,
    ) -> None:
        if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
            raise ValueError(
                "session_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}"
            )
        if not isinstance(max_workers, int) or not 1 <= max_workers <= self.MAX_WORKERS:
            raise ValueError(f"max_workers must be between 1 and {self.MAX_WORKERS}")
        self.policy = policy
        self.session_id = session_id
        self.root = Path(root) if root is not None else runtime_dir() / "vnext" / "subagents" / session_id
        self.parent_origin = parent_origin
        self.max_workers = max_workers
        self.output_char_limit = output_char_limit
        self._tokens: Dict[str, CancellationToken] = {}
        self._lock = threading.Lock()

    # --------------------------------------------------------------- running

    def run(self, tasks: Sequence[WorkerTask]) -> List[WorkerResult]:
        if not isinstance(tasks, (list, tuple)) or not tasks:
            raise ValueError("run requires a non-empty list of WorkerTask")
        if not all(isinstance(item, WorkerTask) for item in tasks):
            raise ValueError("run requires WorkerTask items")
        identifiers = [task.worker_id for task in tasks]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("worker ids must be unique within one run")
        for task in tasks:
            self._validate_task(task)
        prepared = [self._prepare(task) for task in tasks]
        with ThreadPoolExecutor(
            max_workers=min(self.max_workers, len(prepared))
        ) as pool:
            results = list(pool.map(self._run_one, prepared))
        self._write_manifest(results)
        return results

    def run_sync(self, task: WorkerTask) -> WorkerResult:
        return self.run([task])[0]

    def cancel(self, worker_id: str, reason: str = "requested") -> None:
        with self._lock:
            token = self._tokens.get(worker_id)
        if token is not None:
            token.cancel(reason)

    def cancel_all(self, reason: str = "requested") -> None:
        with self._lock:
            tokens = list(self._tokens.values())
        for token in tokens:
            token.cancel(reason)

    def reconcile(self, results: Sequence[WorkerResult]) -> "ReconciledReport":
        return reconcile(results)

    # ------------------------------------------------------------- internals

    def _validate_task(self, task: WorkerTask) -> None:
        if task.workspace_mode == "scratch" and task.input_files:
            raise ValueError(
                "input_files are only meaningful with workspace_mode copy"
            )
        for item in task.input_files:
            if (
                not isinstance(item, str)
                or not item
                or "\\" in item
                or PurePosixPath(item).is_absolute()
                or any(part in {"", ".", ".."} for part in PurePosixPath(item).parts)
            ):
                raise ValueError(
                    "input_files must be relative paths inside the repository"
                )
        if task.workspace_mode == "copy" and not task.input_files:
            raise ValueError("workspace_mode copy requires input_files to copy")

    def _prepare(self, task: WorkerTask) -> Dict[str, Any]:
        worker_dir = self.root / task.worker_id
        if worker_dir.exists():
            raise ValueError(
                f"worker directory already exists: {worker_dir}"
            )
        repository = Path(task.repository).expanduser().resolve(strict=True)
        if not repository.is_dir():
            raise ValueError(f"repository is not a directory: {repository}")
        if task.workspace_mode == "repository":
            workspace = repository
        else:
            workspace = worker_dir / "workspace"
            workspace.mkdir(parents=True, exist_ok=True)
            if task.workspace_mode == "copy":
                self._copy_inputs(repository, workspace, task.input_files)
        token = task.cancellation if task.cancellation is not None else CancellationToken()
        with self._lock:
            self._tokens[task.worker_id] = token
        origin = Origin(
            OriginKind.SUBAGENT,
            task.worker_id,
            parent=self.parent_origin.key if self.parent_origin is not None else None,
        )
        grants = set(_KIND_CAPABILITIES[task.kind])
        if self.parent_origin is not None:
            parent_grants = self.policy.origin_grants.get(self.parent_origin.key)
            if parent_grants is not None:
                grants = grants.intersection(parent_grants)
        self.policy.set_grants(origin, grants)
        events = WorkerEventSink(worker_dir / "events.ndjson", task.worker_id)
        return {
            "task": task,
            "worker_dir": worker_dir,
            "workspace": workspace,
            "origin": origin,
            "token": token,
            "events": events,
        }

    def _copy_inputs(
        self, repository: Path, workspace: Path, input_files: Sequence[str]
    ) -> None:
        if len(input_files) > self.MAX_INPUT_FILES:
            raise ValueError(
                f"input_files supports at most {self.MAX_INPUT_FILES} entries"
            )
        total = 0
        for item in input_files:
            source = _confined_repo_path(repository, item)
            info = source.stat()
            if info.st_size > self.MAX_INPUT_FILE_BYTES:
                raise ValueError(
                    f"input file exceeds the {self.MAX_INPUT_FILE_BYTES}-byte limit: {item}"
                )
            total += info.st_size
            if total > self.MAX_INPUT_TOTAL_BYTES:
                raise ValueError(
                    "input_files exceed the "
                    f"{self.MAX_INPUT_TOTAL_BYTES}-byte total limit"
                )
            target = workspace / Path(*PurePosixPath(item).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)

    def _run_one(self, prepared: Dict[str, Any]) -> WorkerResult:
        task: WorkerTask = prepared["task"]
        workspace: Path = prepared["workspace"]
        worker_dir: Path = prepared["worker_dir"]
        token: CancellationToken = prepared["token"]
        events: WorkerEventSink = prepared["events"]
        origin: Origin = prepared["origin"]
        started = time.time()
        events.emit(
            "worker.started",
            kind=task.kind.value,
            task=task.task[:500],
            workspace=str(workspace),
        )
        runner = PolicyCommandRunner(
            policy=self.policy,
            origin=origin,
            kind=task.kind,
            workspace=workspace,
            cancellation=token,
            events=events,
        )
        config = CoreEngineConfig(
            repository=workspace,
            task=task.task,
            model=task.model,
            base_url=task.base_url,
            api_key=task.api_key,
            max_steps=task.budgets.max_steps,
            max_seconds=int(task.budgets.max_seconds),
            output_char_limit=self.output_char_limit,
            context_token_budget=task.budgets.context_token_budget,
            command_timeout=int(task.budgets.command_timeout),
            trajectory_path=worker_dir / "trajectory.json",
            command_runner=runner,
            model_factory=task.model_factory,
        )
        engine = WorkerEngine(
            config,
            cancellation=token,
            events=events,
            token_budget=task.budgets.max_tokens or 0,
            worker_id=task.worker_id,
        )
        report = engine.run()
        agent = engine.worker_agent
        exit_status = ""
        if agent is not None and agent.messages:
            exit_status = str(
                (agent.messages[-1].get("extra") or {}).get("exit_status") or ""
            )
        status = _STATUS_BY_EXIT.get(exit_status, report.status)
        cancellation_reason = None
        if status == "cancelled":
            cancellation_reason = token.reason
        artifacts = (
            _scan_artifacts(workspace, started)
            if workspace.is_dir()
            else []
        )
        result = WorkerResult(
            worker_id=task.worker_id,
            kind=task.kind.value,
            status=status,
            submission=str(report.submission or ""),
            steps=int(agent.n_calls) if agent is not None else 0,
            tokens=int(agent.tokens_used) if agent is not None else 0,
            cost=float(agent.cost) if agent is not None else 0.0,
            wall_seconds=time.time() - started,
            workspace=str(workspace),
            events_path=str(events.path),
            trajectory_path=(
                str(config.trajectory_path)
                if config.trajectory_path is not None and config.trajectory_path.exists()
                else None
            ),
            artifacts=artifacts,
            commands=list(runner.commands),
            denials=list(runner.denials),
            evidence=_worker_evidence(task.worker_id, task.kind.value, status, runner.commands),
            cancellation_reason=cancellation_reason,
            verifies=task.verifies,
            engine_version=engine_version,
            exit_status=exit_status or report.status,
        )
        events.emit(
            "worker.exit",
            status=result.status,
            steps=result.steps,
            tokens=result.tokens,
            wall_seconds=round(result.wall_seconds, 3),
            cancellation_reason=result.cancellation_reason,
        )
        return result

    def _write_manifest(self, results: Sequence[WorkerResult]) -> None:
        payload = {
            "session_id": self.session_id,
            "root": str(self.root),
            "finished_at": round(time.time(), 3),
            "workers": [
                {
                    "worker_id": result.worker_id,
                    "kind": result.kind,
                    "status": result.status,
                    "steps": result.steps,
                    "tokens": result.tokens,
                    "wall_seconds": round(result.wall_seconds, 3),
                    "workspace": result.workspace,
                    "events_path": result.events_path,
                    "trajectory_path": result.trajectory_path,
                    "verifies": result.verifies,
                }
                for result in results
            ],
        }
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "manifest.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )


@dataclass(frozen=True)
class ReconciledWorker:
    worker_id: str
    kind: str
    status: str
    verdict: Optional[str]
    submission: str
    artifacts: Tuple[Dict[str, Any], ...] = ()
    denials: Tuple[Dict[str, Any], ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "kind": self.kind,
            "status": self.status,
            "verdict": self.verdict,
            "submission": self.submission,
            "artifacts": list(self.artifacts),
            "denials": list(self.denials),
        }


@dataclass(frozen=True)
class ReconciledReport:
    overall: str
    workers: Tuple[ReconciledWorker, ...]
    conflicts: Tuple[Dict[str, Any], ...]
    evidence: Tuple[EvidenceRecord, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "overall": self.overall,
            "workers": [item.to_dict() for item in self.workers],
            "conflicts": [dict(item) for item in self.conflicts],
            "evidence": [item.to_dict() for item in self.evidence],
        }


def reconcile(results: Sequence[WorkerResult]) -> ReconciledReport:
    """Turn worker results into one parent-facing, answerable outcome.

    A verification worker's verdict comes from the commands it actually ran:
    pass when every executed command exited zero, fail when any exited
    non-zero, inconclusive when it ran nothing.  A failed verification linked
    to a worker that reported success is a conflict, not a footnote: the
    parent must see that its coding worker's claim did not survive checking.
    """

    if not isinstance(results, (list, tuple)) or not results:
        raise ValueError("reconcile requires a non-empty list of WorkerResult")
    by_id = {result.worker_id: result for result in results}
    verdicts: Dict[str, Optional[str]] = {}
    workers: List[ReconciledWorker] = []
    evidence: List[EvidenceRecord] = []
    for result in results:
        verdict: Optional[str] = None
        if result.kind == WorkerKind.VERIFICATION.value:
            executed = [item for item in result.commands if not item.get("denied")]
            if executed and all(item.get("exit_code") == 0 for item in executed):
                verdict = "pass"
            elif executed:
                verdict = "fail"
            else:
                verdict = "inconclusive"
        verdicts[result.worker_id] = verdict
        workers.append(
            ReconciledWorker(
                worker_id=result.worker_id,
                kind=result.kind,
                status=result.status,
                verdict=verdict,
                submission=result.submission,
                artifacts=tuple(result.artifacts),
                denials=tuple(result.denials),
            )
        )
        evidence.extend(result.evidence)
    conflicts: List[Dict[str, Any]] = []
    for result in results:
        if result.kind != WorkerKind.VERIFICATION.value or not result.verifies:
            continue
        if verdicts.get(result.worker_id) != "fail":
            continue
        target = by_id.get(result.verifies)
        conflicts.append(
            {
                "verifier": result.worker_id,
                "worker_id": result.verifies,
                "reason": (
                    "verification commands failed while the linked worker "
                    f"reported {target.status if target else 'missing'}"
                ),
            }
        )
    verifications = [
        result for result in results if result.kind == WorkerKind.VERIFICATION.value
    ]
    if any(verdicts.get(item.worker_id) == "fail" for item in verifications):
        overall = "failed"
    elif verifications:
        linked_ok = all(
            by_id[item.verifies].status == "submitted"
            for item in verifications
            if item.verifies and item.verifies in by_id
        )
        all_workers_ok = all(
            result.status == "submitted"
            for result in results
            if result.kind != WorkerKind.VERIFICATION.value
        )
        overall = (
            "verified"
            if linked_ok and all_workers_ok
            else "mixed"
        )
    else:
        overall = "incomplete"
    return ReconciledReport(
        overall=overall,
        workers=tuple(workers),
        conflicts=tuple(conflicts),
        evidence=tuple(evidence),
    )


__all__ = [
    "CancellationToken",
    "ReconciledReport",
    "ReconciledWorker",
    "SubagentManager",
    "WorkerAgent",
    "WorkerBudgets",
    "WorkerCancelled",
    "WorkerCommandPolicy",
    "WorkerEngine",
    "WorkerEventSink",
    "WorkerKind",
    "WorkerResult",
    "WorkerTask",
    "reconcile",
]
