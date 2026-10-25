"""Per-session skill runtime: auditable activation and scoped execution.

The catalogue (:mod:`karox.skills`) discovers untrusted recipes and loads
their text.  This module is the session half that sits between the catalogue
and the Core runtime:

* activation is explicit (the model asked by name) or automatic (declared
  triggers matched the task), and every activation is audited;
* a recipe may declare executable helpers under ``scripts/``.  Running one is
  a mutation: it is refused unless the calling origin holds ``process.run``,
  the manifest is still the one discovery read, the path is one the recipe
  declared, and it stays inside the skill's own directory.  Every invocation,
  denial and failure is an audit row;
* skill text never widens authority.  Activation changes no policy grants --
  the only path from a recipe to capabilities stays
  :func:`karox.skills.configure_skill_policy`, which the operator drives.

The narrow integration surface is :class:`SkillSession` (drives the session)
and :class:`SkillSessionRuntime` (a ``CapabilityCoreRuntime`` that exposes the
``skill.run`` tool through the normal policy / risk / lease / idempotency
machinery of :meth:`karox.core.CoreRuntime.execute`).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .action_execution import CapabilityCoreRuntime
from .core import InvalidCommand, ToolDefinition
from .models import Capability, EvidenceRecord, Origin, OriginKind
from .policy import CapabilityPolicy, PolicyDenied
from .paths import runtime_dir
from .skills import (
    SkillCatalog,
    SkillContent,
    SkillError,
    SkillMetadata,
    SkillPermission,
    _validate_reference,
    configure_skill_policy,
    skill_system_prompt,
)


class SkillScriptDenied(SkillError):
    """Raised when script execution is refused before the process starts."""


#: Session identifiers become directory names under the runtime root.
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_WORD = re.compile(r"[A-Za-z0-9_]")
_SCRIPT_INTERPRETERS: Dict[str, Tuple[str, ...]] = {}


@dataclass(frozen=True)
class SkillAuditRecord:
    """One answerable row: what happened, who asked, and why."""

    event: str
    name: str
    identity: str
    mode: Optional[str] = None
    invoker: Optional[str] = None
    reason: Optional[str] = None
    script: Optional[str] = None
    exit_code: Optional[int] = None
    timed_out: Optional[bool] = None
    duration_seconds: Optional[float] = None
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        value: Dict[str, Any] = {
            "event": self.event,
            "name": self.name,
            "identity": self.identity,
            "timestamp": self.timestamp,
        }
        for key in (
            "mode",
            "invoker",
            "reason",
            "script",
            "exit_code",
            "timed_out",
            "duration_seconds",
        ):
            item = getattr(self, key)
            if item is not None:
                value[key] = item
        return value


@dataclass(frozen=True)
class SkillActivation:
    """A recipe that became active this session, with its prompt block."""

    content: SkillContent
    prompt: str
    mode: str

    @property
    def name(self) -> str:
        return self.content.metadata.name

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "identity": self.content.metadata.identity,
            "mode": self.mode,
            "content_sha256": self.content.metadata.content_sha256,
            "characters": len(self.prompt),
        }


@dataclass(frozen=True)
class SkillCandidate:
    """An automatic-selection proposal with the evidence that produced it."""

    metadata: SkillMetadata
    matched_triggers: Tuple[str, ...]


@dataclass(frozen=True)
class SkillScriptResult:
    """The outcome of one declared script invocation."""

    ok: bool
    name: str
    script: str
    command: Tuple[str, ...]
    exit_code: int
    output: str
    timed_out: bool
    duration_seconds: float
    script_sha256: str
    scratch: str
    session_id: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "name": self.name,
            "script": self.script,
            "command": list(self.command),
            "exit_code": self.exit_code,
            "output": self.output,
            "timed_out": self.timed_out,
            "duration_seconds": round(self.duration_seconds, 3),
            "script_sha256": self.script_sha256,
            "scratch": self.scratch,
            "session_id": self.session_id,
        }

    def evidence(self) -> EvidenceRecord:
        return EvidenceRecord(
            kind="skill_script",
            summary=(
                f"Skill {self.name} script {self.script} "
                + ("timed out" if self.timed_out else f"exited {self.exit_code}")
            ),
            command=list(self.command),
            exit_code=self.exit_code,
            artifact_sha256=self.script_sha256,
            metadata={
                "skill": self.name,
                "script": self.script,
                "session_id": self.session_id,
                "timed_out": self.timed_out,
                "duration_seconds": round(self.duration_seconds, 3),
            },
        )


def _clip(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    head, tail = int(limit * 0.6), int(limit * 0.4)
    dropped = len(text) - head - tail
    return (
        f"{text[:head]}\n"
        f"... [karox skill runtime: dropped {dropped} of {len(text)} output chars] ...\n"
        f"{text[-tail:]}"
    )


def _interpreter(suffix: str) -> Tuple[str, ...]:
    if suffix == ".py":
        return (sys.executable,)
    if suffix == ".sh":
        found = _SCRIPT_INTERPRETERS.get("sh")
        if found is None:
            located = shutil.which("bash") or shutil.which("sh")
            found = (located,) if located else ()
            _SCRIPT_INTERPRETERS["sh"] = found
        if not found:
            raise SkillScriptDenied(
                "no shell interpreter is available for .sh skill scripts"
            )
        return found
    raise SkillScriptDenied(
        f"Skill script has no gated interpreter: {suffix!r}"
    )


def _validate_task_text(value: Any) -> str:
    if not isinstance(value, str):
        raise SkillError("task text must be a string")
    text = value.strip()
    if not text or len(text) > 20_000:
        raise SkillError("task text must contain 1-20000 characters")
    return text


class SkillSession:
    """One session's view of the catalogue: what is active and what ran.

    The session never widens authority.  Activating a recipe loads text; it
    does not touch :class:`CapabilityPolicy`.  The only capability path for a
    recipe is still the operator-driven selection stored by
    :func:`karox.skills.skill_selection`, applied through
    :func:`karox.skills.configure_skill_policy`.
    """

    MAX_ACTIVE_SKILLS = 8
    MAX_SCRIPT_ARGS = 32
    MAX_SCRIPT_ARG_CHARS = 2000
    MAX_SCRIPT_OUTPUT_CHARS = 50_000
    MAX_SCRIPT_TIMEOUT_SECONDS = 600.0
    DEFAULT_SCRIPT_TIMEOUT_SECONDS = 120.0

    def __init__(
        self,
        catalog: SkillCatalog,
        session_id: str,
        *,
        policy: Optional[CapabilityPolicy] = None,
        invoker_origin: Optional[Origin] = None,
        disabled: Iterable[str] = (),
        scratch_root: Optional[Path] = None,
        audit_path: Optional[Path] = None,
        max_auto_activated: int = 3,
        script_timeout_seconds: float = DEFAULT_SCRIPT_TIMEOUT_SECONDS,
    ) -> None:
        if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
            raise SkillError(
                "skill session id must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}"
            )
        if not isinstance(max_auto_activated, int) or not 1 <= max_auto_activated <= 8:
            raise SkillError("max_auto_activated must be between 1 and 8")
        if (
            isinstance(script_timeout_seconds, bool)
            or not isinstance(script_timeout_seconds, (int, float))
            or not 0.1 <= float(script_timeout_seconds) <= self.MAX_SCRIPT_TIMEOUT_SECONDS
        ):
            raise SkillError(
                "script timeout must be between 0.1 and "
                f"{int(self.MAX_SCRIPT_TIMEOUT_SECONDS)} seconds"
            )
        self.catalog = catalog
        self.session_id = session_id
        self.policy = policy
        self.invoker_origin = invoker_origin
        self.disabled: frozenset[str] = frozenset(disabled)
        self.max_auto_activated = max_auto_activated
        self.script_timeout_seconds = float(script_timeout_seconds)
        self.scratch_root = (
            Path(scratch_root)
            if scratch_root is not None
            else runtime_dir() / "vnext" / "skill-sessions"
        )
        self.audit_path = Path(audit_path) if audit_path is not None else None
        self._active: Dict[str, SkillContent] = {}
        self._audit: List[SkillAuditRecord] = []
        self._audit_flushed = 0

    # ------------------------------------------------------------ activation

    @property
    def disabled_skills(self) -> Tuple[str, ...]:
        return tuple(sorted(self.disabled))

    @property
    def active_skills(self) -> Tuple[str, ...]:
        return tuple(sorted(self._active))

    def disable(self, name: str) -> None:
        self.disabled = self.disabled | {name}

    def enable(self, name: str) -> None:
        self.disabled = self.disabled - {name}

    def activate(self, name: str, *, mode: str, reason: str) -> SkillActivation:
        """Load one recipe into the session and audit the activation."""

        if mode not in {"explicit", "automatic"}:
            raise SkillError("activation mode must be explicit or automatic")
        metadata = self.catalog.get(name)
        if metadata.name in self.disabled:
            self._record(
                "activation_denied",
                metadata,
                mode=mode,
                invoker=self._invoker_key(),
                reason="skill is disabled for this session",
            )
            raise SkillError(
                f"Skill is disabled for this session: {metadata.name}"
            )
        if metadata.name in self._active:
            raise SkillError(
                f"Skill is already active this session: {metadata.name}"
            )
        if len(self._active) >= self.MAX_ACTIVE_SKILLS:
            raise SkillError(
                f"at most {self.MAX_ACTIVE_SKILLS} skills may be active per session"
            )
        content = self.catalog.load(name)
        self._active[metadata.name] = content
        self._record(
            "activated",
            metadata,
            mode=mode,
            invoker=self._invoker_key(),
            reason=str(reason)[:500],
        )
        return SkillActivation(content=content, prompt=skill_system_prompt(content), mode=mode)

    def automatic_candidates(self, task_text: str) -> Tuple[SkillCandidate, ...]:
        """Deterministic trigger matching: evidence, never a silent guess.

        A recipe is a candidate when one of its declared ``triggers`` (or its
        name) appears as a word in the task text.  ``when_to_use`` is prose for
        the model, not matching input, so it never selects a recipe here.
        Disabled and already-active recipes are not candidates.
        """

        text = _validate_task_text(task_text)
        candidates: List[SkillCandidate] = []
        for metadata in self.catalog.discover():
            if metadata.name in self.disabled or metadata.name in self._active:
                continue
            matched: List[str] = []
            for trigger in (*metadata.triggers, metadata.name):
                pattern = r"\b" + re.escape(trigger) + r"\b"
                if re.search(pattern, text, flags=re.IGNORECASE):
                    matched.append(trigger)
            if matched:
                candidates.append(
                    SkillCandidate(metadata=metadata, matched_triggers=tuple(matched))
                )
        candidates.sort(key=lambda item: (-len(item.matched_triggers), item.metadata.name))
        return tuple(candidates)

    def activate_automatic(self, task_text: str) -> Tuple[SkillActivation, ...]:
        """Activate the strongest trigger matches, audited as automatic."""

        candidates = self.automatic_candidates(task_text)[: self.max_auto_activated]
        activations: List[SkillActivation] = []
        for candidate in candidates:
            reason = "automatic: matched " + ", ".join(candidate.matched_triggers)
            activations.append(
                self.activate(candidate.metadata.name, mode="automatic", reason=reason)
            )
        return tuple(activations)

    def active_prompt(self) -> str:
        """The system-prompt block for every active recipe, bounded and ordered."""

        return "\n\n".join(
            skill_system_prompt(self._active[name]) for name in sorted(self._active)
        )

    # ------------------------------------------------------------- execution

    def validate_script(self, name: str, script: str) -> SkillMetadata:
        """The non-executing half of ``run_script``: declared and well-formed.

        Used as the Core preflight so a deterministic failure (undeclared or
        unsafe path) is refused before an idempotency intent is reserved.
        """

        metadata = self.catalog.get(name)
        SkillCatalog._assert_unchanged(metadata)
        if metadata.name in self.disabled:
            raise SkillError(
                f"Skill is disabled for this session: {metadata.name}"
            )
        reference = _validate_reference(script)
        if reference not in metadata.scripts:
            raise SkillError(
                f"Skill {metadata.name} does not declare the script {reference}"
            )
        return metadata

    def run_script(
        self,
        name: str,
        script: str,
        args: Sequence[str] = (),
        *,
        origin: Optional[Origin] = None,
        deadline_seconds: Optional[float] = None,
    ) -> SkillScriptResult:
        """Run one declared skill script, gated and audited end to end.

        The script executes with the session scratch directory as its working
        directory -- never the skill's own directory and never the repository
        -- with ``KAROX_SKILL_DIR`` pointing at the recipe so it can read its
        bundled resources.  The interpreter is chosen by the gated suffix
        (``.py`` -> this Python, ``.sh`` -> the discovered shell) and the
        script must still be the regular, non-link, confined file the recipe
        declared.
        """

        caller = origin or self.invoker_origin
        if self.policy is None:
            raise SkillScriptDenied(
                "skill script execution requires a capability policy"
            )
        if caller is None:
            raise SkillScriptDenied(
                "skill script execution requires the session invoker origin"
            )
        metadata = self.validate_script(name, script)
        reference = _validate_reference(script)
        if not isinstance(args, (list, tuple)) or not all(
            isinstance(item, str) for item in args
        ):
            raise SkillError("Skill script arguments must be a list of strings")
        if len(args) > self.MAX_SCRIPT_ARGS:
            raise SkillError(
                f"Skill script arguments support at most {self.MAX_SCRIPT_ARGS} entries"
            )
        for item in args:
            if (
                not item
                or len(item) > self.MAX_SCRIPT_ARG_CHARS
                or any(ord(character) < 32 for character in item)
            ):
                raise SkillError(
                    "Skill script arguments must contain 1-"
                    f"{self.MAX_SCRIPT_ARG_CHARS} printable characters"
                )

        skill_origin = Origin(
            OriginKind.SKILL, metadata.identity, parent=caller.key
        )
        if Capability.PROCESS_RUN in self.policy.origin_denies.get(
            skill_origin.key, set()
        ):
            self._record(
                "script_denied",
                metadata,
                invoker=caller.key,
                script=reference,
                reason="skill origin denied process.run",
            )
            raise SkillScriptDenied(
                f"Skill {metadata.name} is denied process.run for this session"
            )
        try:
            self.policy.require(caller, Capability.PROCESS_RUN)
        except PolicyDenied as exc:
            self._record(
                "script_denied",
                metadata,
                invoker=caller.key,
                script=reference,
                reason=f"capability denied: {exc}",
            )
            raise

        target = SkillCatalog._confined_reference(metadata.directory, reference)
        if not target.is_file():
            raise SkillError(f"Skill script is not a regular file: {reference}")
        script_bytes = target.read_bytes()
        if len(script_bytes) > SkillCatalog.MAX_REFERENCE_BYTES:
            raise SkillError(
                f"Skill script exceeds the {SkillCatalog.MAX_REFERENCE_BYTES}-byte limit"
            )
        digest = hashlib.sha256(script_bytes).hexdigest()
        argv = [*_interpreter(target.suffix), str(target), *args]

        scratch = self.scratch_root / self.session_id / metadata.name
        scratch.mkdir(parents=True, exist_ok=True)

        timeout = self.script_timeout_seconds
        if deadline_seconds is not None:
            timeout = min(timeout, max(0.1, float(deadline_seconds)))

        self._record(
            "script_started",
            metadata,
            invoker=caller.key,
            script=reference,
            reason=f"sha256 {digest}",
        )
        started = time.perf_counter()
        try:
            process = subprocess.Popen(
                argv,
                cwd=str(scratch),
                env={**os.environ, "KAROX_SKILL_DIR": str(metadata.directory)},
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=os.name == "posix",
            )
        except OSError as exc:
            self._record(
                "script_failed",
                metadata,
                invoker=caller.key,
                script=reference,
                reason=f"cannot start script: {exc}",
            )
            raise SkillError(f"cannot start Skill script {reference}: {exc}") from exc
        timed_out = False
        try:
            output, _ = process.communicate(timeout=timeout)
            exit_code = (
                process.returncode if process.returncode is not None else -1
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            if os.name == "posix":
                os.killpg(process.pid, 15)
            else:
                process.terminate()
            try:
                output, _ = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    os.killpg(process.pid, 9)
                else:
                    process.kill()
                output, _ = process.communicate()
            exit_code = process.returncode if process.returncode is not None else -1
        duration = time.perf_counter() - started
        text = _clip(str(output or ""), self.MAX_SCRIPT_OUTPUT_CHARS)
        self._record(
            "script_finished" if not timed_out else "script_failed",
            metadata,
            invoker=caller.key,
            script=reference,
            exit_code=exit_code,
            timed_out=timed_out,
            duration_seconds=round(duration, 3),
        )
        return SkillScriptResult(
            ok=(not timed_out) and exit_code == 0,
            name=metadata.name,
            script=reference,
            command=tuple(argv),
            exit_code=exit_code,
            output=text,
            timed_out=timed_out,
            duration_seconds=duration,
            script_sha256=digest,
            scratch=str(scratch),
            session_id=self.session_id,
        )

    # ------------------------------------------------------------------ audit

    def audit_log(self) -> Tuple[SkillAuditRecord, ...]:
        return tuple(self._audit)

    def flush(self) -> None:
        """Append audit rows that are not on disk yet, as JSON lines."""

        if self.audit_path is None:
            self._audit_flushed = len(self._audit)
            return
        pending = self._audit[self._audit_flushed :]
        if not pending:
            return
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as handle:
            for record in pending:
                handle.write(json.dumps(record.to_dict(), sort_keys=True) + "\n")
        self._audit_flushed = len(self._audit)

    def _invoker_key(self) -> Optional[str]:
        return self.invoker_origin.key if self.invoker_origin is not None else None

    def _record(self, event: str, metadata: SkillMetadata, **fields: Any) -> None:
        self._audit.append(
            SkillAuditRecord(event=event, name=metadata.name, identity=metadata.identity, **fields)
        )
        self.flush()


class SkillSessionRuntime(CapabilityCoreRuntime):
    """A Core runtime whose toolset includes the audited ``skill.run`` tool.

    This is the wiring the kernel adapter uses verbatim: construct the session
    next to the catalogue the runtime already receives, and every script run
    flows through :meth:`CoreRuntime.execute`, which applies the capability
    decision, the mutation lease, the idempotency reservation and the audit
    trail exactly as it does for every other mutating tool.
    """

    def __init__(self, *args: Any, skill_session: SkillSession, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.skill_session = skill_session
        handlers = dict(self._handlers)
        handlers["skill.run"] = self._skill_run
        self._handlers = handlers
        definitions = dict(self._definitions)
        definitions["skill.run"] = ToolDefinition(
            "skill.run",
            "Run one script a Skill declared under scripts/. The script runs in "
            "an isolated scratch directory with KAROX_SKILL_DIR pointing at the "
            "recipe; pass args as a list of strings.",
            Capability.PROCESS_RUN,
            True,
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "script": {
                        "type": "string",
                        "description": "Declared script path, e.g. scripts/check.py",
                    },
                    "args": {
                        "type": "array",
                        "maxItems": SkillSession.MAX_SCRIPT_ARGS,
                        "items": {"type": "string"},
                        "description": "Command line arguments for the script",
                    },
                },
                "required": ["name", "script"],
                "additionalProperties": False,
            },
        )
        self._definitions = definitions

    def _skill_run(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        name = self._required(arguments, "name", str)
        script = self._required(arguments, "script", str)
        raw_args = arguments.get("args", [])
        if not isinstance(raw_args, list) or not all(
            isinstance(item, str) for item in raw_args
        ):
            raise InvalidCommand("skill.run args must be a list of strings")
        result = self.skill_session.run_script(
            name,
            script,
            raw_args,
            origin=self.skill_session.invoker_origin,
            deadline_seconds=deadline_seconds,
        )
        data = result.to_dict()
        data["run_id"] = uuid.uuid4().hex
        data["_evidence"] = [result.evidence()]
        return data

    def _preflight_mutation(
        self,
        command_name: str,
        arguments: Dict[str, Any],
        deadline_seconds: float,
    ) -> None:
        if command_name == "skill.run":
            self.skill_session.validate_script(
                self._required(arguments, "name", str),
                self._required(arguments, "script", str),
            )
            return
        super()._preflight_mutation(command_name, arguments, deadline_seconds)


__all__ = [
    "SkillActivation",
    "SkillAuditRecord",
    "SkillCandidate",
    "SkillScriptDenied",
    "SkillScriptResult",
    "SkillSession",
    "SkillSessionRuntime",
    "SkillPermission",
    "configure_skill_policy",
]
