"""Capability-first action decisions for KaroX.

This module deliberately sits *above* :mod:`karox.risk_engine` during the v5
migration.  Risk remains a fact classifier (how much damage is possible); this
module decides how KaroX should respond to that risk:

``AUTO``
    Execute normally.  Observation and bounded everyday development belongs
    here.
``GUARDED_AUTO``
    Execute without another user round-trip because the consequence is
    rebuildable/reversible or the current user request explicitly scoped it.
    The runtime still records the impact and keeps its ordinary atomicity,
    fingerprint, checkpoint or transaction guards.
``CONFIRM``
    A real irreversible boundary remains.  The human-facing layer must approve
    the exact action digest; the model cannot self-approve it.
``HARD_BLOCK``
    The requested effect crosses a boundary KaroX should not make available as
    an ordinary agent action (for example destructive OS paths or credential
    export).  This category is intentionally very small.

The important design rule is that *risk level is not an authorization level*.
A twelve-file commit may be ``HIGH`` by blast radius while still being locally
reversible and therefore ``GUARDED_AUTO``.  Conversely a one-file write to
``C:\\Windows`` is a hard boundary even though its file count is one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Mapping, Optional, Sequence

from .risk_engine import (
    RiskAction,
    RiskAssessment,
    RiskEngine,
    RiskLevel,
    SmartStopRequired,
    build_bulk_preview,
    looks_like_system_path,
)


class ActionDisposition(str, Enum):
    AUTO = "auto"
    GUARDED_AUTO = "guarded_auto"
    CONFIRM = "confirm"
    HARD_BLOCK = "hard_block"


class ConsequenceClass(str, Enum):
    OBSERVATION = "observation"
    REBUILDABLE = "rebuildable"
    WORKSPACE = "workspace"
    USER_DATA = "user_data"
    SYSTEM = "system"
    EXTERNAL = "external"
    UNKNOWN = "unknown"


# These names are outputs or caches by convention.  Deleting one is materially
# different from deleting source merely because both operations use unlink().
_REBUILDABLE_PARTS = frozenset(
    {
        ".cache",
        ".gradle",
        ".mypy_cache",
        ".next",
        ".nox",
        ".nuxt",
        ".output",
        ".pytest_cache",
        ".ruff_cache",
        ".svelte-kit",
        ".tox",
        ".turbo",
        ".venv",
        ".vite",
        "__pycache__",
        "build",
        "cache",
        "cacheddata",
        "code cache",
        "coverage",
        "dist",
        "gpucache",
        "logs",
        "node_modules",
        "out",
        "target",
        "temp",
        "tmp",
        "venv",
    }
)
_REBUILDABLE_SUFFIXES = (
    ".cache",
    ".log",
    ".pyc",
    ".tmp",
)

# External effects are not automatically hard-blocked: a user can genuinely
# ask KaroX to deploy or send something.  They simply remain a human boundary.
_EXTERNAL_CONFIRM_KINDS = frozenset(
    {
        "account.delete",
        "account.security_change",
        "billing.change",
        "browser.final_submit",
        "browser.grant_permission",
        "browser.send_message",
        "browser.upload",
        "deploy",
        "git.force_push",
        "git.history_rewrite",
        "git.push",
        "package.publish",
        "payment",
        "permission.change",
        "release.publish",
        "subscription.change",
    }
)

# These effects are not made available through an ordinary scoped approval.
# Keeping this set narrow is intentional: safety should protect capability, not
# replace it with blanket denial.
_HARD_BLOCK_KINDS = frozenset(
    {
        "credential.export",
        "repository.escape",
        "system.path_write",
    }
)

_EXTERNAL_ALWAYS_CONFIRM = frozenset(
    {
        # External effects are real commit points. Explicit task wording gives
        # intent context, but a hosted agent must still cross a machine-verifiable
        # one-shot user gate before the effect leaves the local workspace.
        "git.push",
        "git.force_push",
        "package.publish",
        "release.publish",
        "deploy",
        "browser.final_submit",
        "browser.send_message",
        "browser.upload",
        "account.delete",
        "account.security_change",
        "billing.change",
        "payment",
        "permission.change",
        "subscription.change",
    }
)

_DESTRUCTIVE_KINDS = frozenset(
    {
        "repo.delete",
        "git.clean",
        "git.reset_hard",
        "git.branch_delete",
        "git.tag_delete",
    }
)

_OBSERVATION_KINDS = frozenset(
    {
        "repo.read",
        "repo.search",
        "repo.list",
        "disk.read",
        "git.read",
        "status.read",
        "diagnostics.read",
        "browser.read",
        "browser.snapshot",
        "browser.screenshot",
    }
)

# Local task-intent recognition is deliberately lexical and conservative.  It
# is not a model call: no extra tokens, no second opinion loop, and no chance for
# a weaker helper model to reinterpret the user's authority.
_CLEANUP_RE = re.compile(
    r"(?:\bclean(?:up)?\b|\bclear\s+(?:the\s+)?cache\b|\bremove\s+junk\b|"
    r"очист\w*|почист\w*|\bмусор\w*\b|\bкэш\w*\b)",
    re.IGNORECASE,
)
_DELETE_RE = re.compile(
    r"(?:\bdelete\b|\bremove\b|\berase\b|\bpurge\b|\buninstall\b|"
    r"удал\w*|снес\w*|убер\w*)",
    re.IGNORECASE,
)
_PUSH_RE = re.compile(r"(?:\bpush\b|\bпуш\w*\b)", re.IGNORECASE)
_FORCE_PUSH_RE = re.compile(
    r"(?:\bforce[- ]?push\b|\bpush\b.{0,24}\bforce\b|\bforce\b.{0,24}\bpush\b|"
    r"форс\w*.{0,24}пуш\w*|пуш\w*.{0,24}форс\w*)",
    re.IGNORECASE,
)
_PUBLISH_RE = re.compile(
    r"(?:\bpublish\b|\brelease\b|опубликов\w*|релизн\w*)", re.IGNORECASE
)
_DEPLOY_RE = re.compile(r"(?:\bdeploy\b|задепло\w*|депло\w*)", re.IGNORECASE)
_SEND_RE = re.compile(r"(?:\bsend\b|отправ\w*)", re.IGNORECASE)
_UPLOAD_RE = re.compile(r"(?:\bupload\b|загруз\w*|аплоад\w*)", re.IGNORECASE)
_EXTERNAL_RE = re.compile(
    r"(?:\bpush\b|\bpublish\b|\brelease\b|\bdeploy\b|\bsend\b|\bupload\b|"
    r"пуш\w*|отправ\w*|опубликов\w*|релизн\w*|задепло\w*|депло\w*|аплоад\w*)",
    re.IGNORECASE,
)


def _norm_text(value: str) -> str:
    return str(value or "").replace("\\", "/").casefold()


def _path_is_rebuildable(value: str) -> bool:
    normalized = _norm_text(value).strip("/")
    if not normalized:
        return False
    parts = tuple(part for part in normalized.split("/") if part)
    if any(part in _REBUILDABLE_PARTS for part in parts):
        return True
    leaf = parts[-1] if parts else normalized
    return leaf.endswith(_REBUILDABLE_SUFFIXES)


def _path_is_system(value: str) -> bool:
    return bool(value) and looks_like_system_path(str(value))


@dataclass(frozen=True)
class IntentScope:
    """A compact interpretation of authority already present in the user task."""

    raw: str = ""
    cleanup_requested: bool = False
    deletion_requested: bool = False
    external_requested: bool = False
    push_requested: bool = False
    force_push_requested: bool = False
    publish_requested: bool = False
    deploy_requested: bool = False
    send_requested: bool = False
    upload_requested: bool = False

    @classmethod
    def compile(cls, text: str | None) -> "IntentScope":
        raw = str(text or "")
        cleanup = bool(_CLEANUP_RE.search(raw))
        deletion = cleanup or bool(_DELETE_RE.search(raw))
        external = bool(_EXTERNAL_RE.search(raw))
        return cls(
            raw=raw,
            cleanup_requested=cleanup,
            deletion_requested=deletion,
            external_requested=external,
            push_requested=bool(_PUSH_RE.search(raw)),
            force_push_requested=bool(_FORCE_PUSH_RE.search(raw)),
            publish_requested=bool(_PUBLISH_RE.search(raw)),
            deploy_requested=bool(_DEPLOY_RE.search(raw)),
            send_requested=bool(_SEND_RE.search(raw)),
            upload_requested=bool(_UPLOAD_RE.search(raw)),
        )

    def mentions(self, path: str) -> bool:
        """Whether the task names a target with enough specificity to matter."""

        task = _norm_text(self.raw)
        candidate = _norm_text(path).strip("/")
        if not task or not candidate:
            return False
        if candidate in task:
            return True
        parts = [item for item in candidate.split("/") if item]
        # A filename or two-component suffix is usually what people type in a
        # prompt; requiring the entire absolute path would reject normal intent.
        probes = []
        if parts:
            probes.append(parts[-1])
        if len(parts) >= 2:
            probes.append("/".join(parts[-2:]))
        return any(len(probe) >= 3 and probe in task for probe in probes)

    def authorizes_delete(self, action: RiskAction, consequence: ConsequenceClass) -> bool:
        if consequence is ConsequenceClass.REBUILDABLE:
            return self.cleanup_requested or self.deletion_requested
        if consequence is ConsequenceClass.WORKSPACE:
            # An explicit delete/remove request scopes destructive work to the
            # current workspace.  The repository boundary still prevents escape.
            return self.deletion_requested
        if consequence is ConsequenceClass.USER_DATA:
            # Personal data outside the repository needs a named target; a vague
            # "clean things up" must not silently expand to Documents/Desktop.
            return self.deletion_requested and bool(action.paths) and all(
                self.mentions(path) for path in action.paths
            )
        return False

    def authorizes_external(self, kind: str) -> bool:
        """Match an external effect to the authority the task actually names."""

        if kind == "git.push":
            return self.push_requested
        if kind == "git.force_push":
            return self.force_push_requested
        if kind in {"release.publish", "package.publish"}:
            return self.publish_requested
        if kind == "deploy":
            return self.deploy_requested
        if kind == "browser.send_message":
            return self.send_requested
        if kind == "browser.upload":
            return self.upload_requested
        if kind == "browser.final_submit":
            # Final submit is necessarily contextual; an explicit send/upload
            # request is enough, but an unrelated "push" elsewhere in the task
            # is not.
            return self.send_requested or self.upload_requested
        return False


@dataclass(frozen=True)
class ActionDecision:
    action_digest: str
    disposition: ActionDisposition
    consequence: ConsequenceClass
    assessment: RiskAssessment
    reasons: tuple[str, ...]
    intent_authorized: bool = False
    impact: Mapping[str, Any] = field(default_factory=dict)

    @property
    def requires_confirmation(self) -> bool:
        return self.disposition is ActionDisposition.CONFIRM

    @property
    def hard_blocked(self) -> bool:
        return self.disposition is ActionDisposition.HARD_BLOCK

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_digest": self.action_digest,
            "disposition": self.disposition.value,
            "consequence": self.consequence.value,
            "risk": self.assessment.level.value,
            "requires_confirmation": self.requires_confirmation,
            "reasons": list(self.reasons),
            "intent_authorized": self.intent_authorized,
            "impact": dict(self.impact),
        }


class ActionConfirmationRequired(SmartStopRequired):
    """Compatibility-preserving Smart Stop with the richer action decision."""

    def __init__(self, decision: ActionDecision) -> None:
        super().__init__(decision.assessment)
        self.decision = decision


class ActionHardBlocked(PermissionError):
    def __init__(self, decision: ActionDecision) -> None:
        super().__init__(decision.assessment.headline or decision.assessment.kind)
        self.decision = decision


def consequence_for(action: RiskAction) -> ConsequenceClass:
    if action.kind in _OBSERVATION_KINDS:
        return ConsequenceClass.OBSERVATION
    if action.kind in _HARD_BLOCK_KINDS:
        return ConsequenceClass.SYSTEM
    if action.kind in _EXTERNAL_CONFIRM_KINDS:
        return ConsequenceClass.EXTERNAL
    if any(_path_is_system(path) for path in action.paths) or _path_is_system(action.target):
        return ConsequenceClass.SYSTEM

    destructive = (
        action.kind in _DESTRUCTIVE_KINDS
        or action.delete_count > 0
        or bool(action.details.get("deletion_requested"))
    )
    if destructive:
        declared_delete_paths = action.details.get("deletion_paths")
        if isinstance(declared_delete_paths, Sequence) and not isinstance(
            declared_delete_paths, (str, bytes)
        ):
            delete_paths = tuple(
                item for item in declared_delete_paths if isinstance(item, str) and item
            )
        else:
            delete_paths = ()
        candidates = delete_paths or action.paths or (
            (action.target,) if action.target else ()
        )
        if not candidates:
            # A shell/interpreter program may clearly request deletion while its
            # actual targets remain opaque. Do not guess that such a command is
            # repository-scoped merely because the wrapper tool is.
            return ConsequenceClass.UNKNOWN
        # Workspace authority comes before filename heuristics. A personal
        # folder outside the selected workspace does not become disposable just
        # because somebody named it `cache` or `logs`. Conversely, when the
        # user explicitly selected a whole drive as the workspace, cache paths
        # on that drive remain inside scope and may be treated as rebuildable.
        if action.outside_repository:
            return ConsequenceClass.USER_DATA
        if all(_path_is_rebuildable(path) for path in candidates):
            return ConsequenceClass.REBUILDABLE
        return ConsequenceClass.WORKSPACE

    if action.kind in {
        "repo.write",
        "repo.create",
        "repo.move",
        "repo.bulk_write",
        "repo.wide_replace",
        "git.commit",
        "package.install",
        "migration.run",
        "process.run_dev",
        "process.run_unknown",
        "checks.run",
        "tests.run",
        "devserver.start",
        "browser.input",
        "mcp.call",
        "network.request",
    }:
        return ConsequenceClass.WORKSPACE
    if action.outside_repository:
        return ConsequenceClass.USER_DATA
    return ConsequenceClass.UNKNOWN


class ActionDecisionEngine:
    """Turn risk facts + current user intent into one execution disposition."""

    def __init__(self, risk: Optional[RiskEngine] = None) -> None:
        self.risk = risk or RiskEngine()

    def decide(
        self,
        action: RiskAction,
        *,
        user_intent: str = "",
        bypass_mode: bool = False,
    ) -> ActionDecision:
        assessment = self.risk.assess(action)
        intent = IntentScope.compile(user_intent)
        consequence = consequence_for(action)
        reasons = list(assessment.reasons)
        intent_authorized = False

        if action.beyond_user_request:
            disposition = ActionDisposition.CONFIRM
            reasons.append("outside_intent_scope")
        elif consequence is ConsequenceClass.SYSTEM or action.kind in _HARD_BLOCK_KINDS:
            disposition = ActionDisposition.HARD_BLOCK
            reasons.append("hard_boundary")
        elif consequence is ConsequenceClass.EXTERNAL:
            # User wording is useful intent context but is not itself proof that
            # a remote commit point was approved. Push/publish/deploy/send/upload
            # therefore keep an exact one-shot human gate even when the task asks
            # for them; the intent flag is retained for truthful UX/audit. Less
            # consequential external effects added in the future may still use
            # explicit task intent as their guarded authorization source.
            intent_authorized = intent.authorizes_external(action.kind)
            if action.kind in _EXTERNAL_ALWAYS_CONFIRM:
                disposition = ActionDisposition.CONFIRM
                if intent_authorized:
                    reasons.append("explicit_external_intent_context")
                reasons.append("external_side_effect")
            elif intent_authorized:
                disposition = ActionDisposition.GUARDED_AUTO
                reasons.append("explicit_external_intent")
            else:
                disposition = ActionDisposition.CONFIRM
                reasons.append("external_side_effect")
        elif consequence is ConsequenceClass.OBSERVATION:
            disposition = ActionDisposition.AUTO
        elif action.kind in {"git.reset_hard", "git.clean", "git.history_rewrite"}:
            # These can destroy work the repository cannot necessarily recreate,
            # especially untracked files.  An eventual checkpoint adapter may
            # lower this to guarded-auto when it proves complete rollback.
            disposition = ActionDisposition.CONFIRM
            reasons.append("destructive_git")
        elif consequence is ConsequenceClass.REBUILDABLE:
            disposition = ActionDisposition.GUARDED_AUTO
            intent_authorized = intent.authorizes_delete(action, consequence)
            reasons.append("rebuildable_effect")
        elif action.kind == "repo.delete" or action.delete_count or action.details.get(
            "deletion_requested"
        ):
            intent_authorized = intent.authorizes_delete(action, consequence)
            if bypass_mode and consequence is ConsequenceClass.WORKSPACE:
                # Bypass is the explicit opt-in for autonomous destructive work
                # inside the selected repository. Outside-repository/user-data,
                # system and external effects retain their normal boundaries.
                disposition = ActionDisposition.GUARDED_AUTO
                reasons.append("bypass_workspace_delete")
            else:
                # Ordinary coding mode should not turn every edit into a prompt,
                # but source deletion is intentionally the exception. A rollback
                # checkpoint or a vague cleanup request does not silently widen
                # this boundary; the user can approve it or opt into Bypass.
                disposition = ActionDisposition.CONFIRM
                reasons.append("destructive_delete_requires_user")
        elif action.kind == "git.commit":
            # A local commit is a rollback/checkpoint primitive, not an external
            # side effect.  File count alone must not turn it into a user prompt.
            disposition = ActionDisposition.GUARDED_AUTO
            reasons.append("local_reversible_commit")
        elif action.kind in {
            "repo.write",
            "repo.create",
            "repo.move",
            "repo.bulk_write",
            "repo.wide_replace",
            "checks.run",
            "tests.run",
            "package.install",
            "process.run_dev",
            "devserver.start",
            "browser.input",
            "network.request",
        }:
            disposition = ActionDisposition.AUTO
        elif action.kind in {"process.run_unknown", "mcp.call", "migration.run"}:
            # Full/elevated capability policy already decides whether the caller
            # may use these surfaces.  Keep them capable, but label them guarded
            # for audit/UX because the exact downstream side effect is opaque.
            disposition = ActionDisposition.GUARDED_AUTO
            reasons.append("opaque_but_capability_granted")
        elif assessment.level.rank <= RiskLevel.MEDIUM.rank:
            disposition = ActionDisposition.AUTO
        else:
            # Unknown future tools fail closed at *confirmation*, not at blanket
            # denial.  The user can still authorize a legitimate new capability.
            disposition = ActionDisposition.CONFIRM
            reasons.append("unknown_consequence")

        impact = dict(build_bulk_preview(action)) if (action.paths or action.effective_file_count) else {}
        impact.update(
            {
                "disposition": disposition.value,
                "consequence": consequence.value,
                "intent_authorized": intent_authorized,
            }
        )
        requires = disposition is ActionDisposition.CONFIRM
        # Keep the old assessment/event format useful while exposing the new
        # decision.  Risk severity itself remains untouched.
        compatible = replace(
            assessment,
            requires_confirmation=requires,
            preview=impact or assessment.preview,
        )
        return ActionDecision(
            action_digest=action.digest,
            disposition=disposition,
            consequence=consequence,
            assessment=compatible,
            reasons=tuple(dict.fromkeys(reasons)),
            intent_authorized=intent_authorized,
            impact=impact,
        )

    def authorize(
        self,
        action: RiskAction,
        *,
        user_intent: str = "",
        confirmation_token: Optional[str] = None,
        bypass_mode: bool = False,
    ) -> ActionDecision:
        decision = self.decide(
            action,
            user_intent=user_intent,
            bypass_mode=bypass_mode,
        )
        if decision.hard_blocked:
            raise ActionHardBlocked(decision)
        if not decision.requires_confirmation:
            return decision
        if confirmation_token is None:
            raise ActionConfirmationRequired(decision)
        # Confirmation remains exact-action, one-shot and opaque to the model.
        # The same mature ledger is reused instead of inventing a second token
        # system for the migration.
        self.risk.ledger.redeem(confirmation_token, decision.assessment)
        return decision


__all__ = [
    "ActionConfirmationRequired",
    "ActionDecision",
    "ActionDecisionEngine",
    "ActionDisposition",
    "ActionHardBlocked",
    "ConsequenceClass",
    "IntentScope",
    "consequence_for",
]
