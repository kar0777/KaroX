"""Guarded orchestrator-to-worker delegation for KaroX 5.

This module implements the missing half of the "choose one orchestrator and give
it a pool of workers" workflow without giving a model authority over KaroX
permissions or process execution.

The orchestrator may *propose* role/step assignments and KaroX Effort levels in
a small JSON contract. KaroX validates that proposal against the selected recipe
and Intelligence Pool, pins the user-selected orchestrator, then hands the
proposal back to the normal :class:`karox.orchestrator.Orchestrator`. The normal
verified router therefore remains the final authority for capabilities, quota,
high-risk quality floors, budgets and independent-review exclusions.

The proposal contract intentionally has no command, argv, environment,
credential, permission, budget, risk or verification fields.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from .effort import normalize_effort
from .intelligence_pool import IntelligenceEndpoint, IntelligencePool
from .orchestrator import OrchestrationPlan, OrchestrationPolicy, Orchestrator
from .risk_engine import RiskLevel
from .security import contains_credential

_MAX_RATIONALE_CHARS = 2_000
_ALLOWED_FIELDS = frozenset({"assignments", "effort_assignments", "rationale"})
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(?:authorization|api[_-]?key|token|secret|password|credential|cookie)\s*[:=]\s*\S+"
)


class DelegationProposalError(ValueError):
    """Raised when an orchestrator proposal is outside the guarded contract."""


@dataclasses.dataclass(frozen=True)
class DelegationProposal:
    assignments: Mapping[str, str] = dataclasses.field(default_factory=dict)
    effort_assignments: Mapping[str, str] = dataclasses.field(default_factory=dict)
    rationale: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "assignments", dict(self.assignments))
        object.__setattr__(self, "effort_assignments", dict(self.effort_assignments))
        if not isinstance(self.rationale, str):
            raise DelegationProposalError("delegation rationale must be text")
        rationale = self.rationale.strip()
        if len(rationale) > _MAX_RATIONALE_CHARS:
            raise DelegationProposalError(
                f"delegation rationale must be at most {_MAX_RATIONALE_CHARS} characters"
            )
        if rationale and (
            contains_credential(rationale) or _SECRET_ASSIGNMENT.search(rationale)
        ):
            raise DelegationProposalError("delegation rationale cannot contain credentials")
        object.__setattr__(self, "rationale", rationale)

    @classmethod
    def parse(cls, value: str | Mapping[str, Any]) -> "DelegationProposal":
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError as exc:
                raise DelegationProposalError(
                    f"orchestrator proposal must be strict JSON: {exc.msg}"
                ) from exc
        else:
            decoded = dict(value)
        if not isinstance(decoded, dict):
            raise DelegationProposalError("orchestrator proposal must be a JSON object")
        unknown = sorted(set(decoded) - _ALLOWED_FIELDS)
        if unknown:
            raise DelegationProposalError(
                "unsupported delegation proposal fields: " + ", ".join(unknown)
            )
        assignments = decoded.get("assignments", {})
        efforts = decoded.get("effort_assignments", {})
        if not isinstance(assignments, dict) or not all(
            isinstance(key, str) and isinstance(item, str)
            for key, item in assignments.items()
        ):
            raise DelegationProposalError("assignments must be an object of string values")
        if not isinstance(efforts, dict) or not all(
            isinstance(key, str) and isinstance(item, str)
            for key, item in efforts.items()
        ):
            raise DelegationProposalError(
                "effort_assignments must be an object of string values"
            )
        rationale = decoded.get("rationale", "")
        return cls(
            assignments=assignments,
            effort_assignments=efforts,
            rationale=rationale,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "assignments": dict(sorted(self.assignments.items())),
            "effort_assignments": dict(sorted(self.effort_assignments.items())),
            "rationale": self.rationale,
        }


@dataclasses.dataclass(frozen=True)
class GuidedPlanResult:
    plan: OrchestrationPlan
    proposal: DelegationProposal
    catalog_sha256: str
    proposal_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan.to_dict(),
            "proposal": self.proposal.to_dict(),
            "catalog_sha256": self.catalog_sha256,
            "proposal_sha256": self.proposal_sha256,
        }


class OrchestratorDelegationBroker:
    """Validate a model-authored delegation proposal and build a normal KaroX plan."""

    def __init__(
        self,
        *,
        orchestrator: Orchestrator,
        pool: IntelligencePool | None = None,
    ) -> None:
        self.orchestrator = orchestrator
        self.pool = pool or orchestrator.pool

    @staticmethod
    def _hash_json(value: Any) -> str:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _public_endpoint(endpoint: IntelligenceEndpoint) -> dict[str, Any]:
        quota = endpoint.quota
        return {
            "endpoint_id": endpoint.endpoint_id,
            "display_name": endpoint.display_name,
            "source_kind": endpoint.source_kind,
            "capabilities": list(endpoint.capabilities),
            "roles": list(endpoint.roles),
            "already_paid": endpoint.already_paid,
            "quota_known": quota.known,
            "remaining_fraction": quota.remaining_fraction,
        }

    def public_catalog(self) -> tuple[dict[str, Any], ...]:
        """Return the secret-free inventory an orchestrator is allowed to choose from."""

        rows = [
            self._public_endpoint(endpoint)
            for endpoint in self.pool.list(include_disabled=False)
        ]
        rows.sort(key=lambda item: str(item["endpoint_id"]))
        return tuple(rows)

    def proposal_prompt(
        self,
        *,
        objective: str,
        recipe_name: str,
        risk_level: RiskLevel,
        policy: OrchestrationPolicy,
        orchestrator_endpoint_id: str,
    ) -> str:
        """Build the bounded data-only prompt for the selected orchestrator model."""

        selected_recipe = self.orchestrator.recipe_registry.get(recipe_name)
        allowed_targets = sorted(
            {"orchestrator"}
            | {step.role for step in selected_recipe.steps}
            | {step.step_id for step in selected_recipe.steps}
        )
        payload = {
            "objective": objective.strip(),
            "recipe": selected_recipe.to_dict(),
            "risk_level": risk_level.value,
            "preset": policy.preset,
            "fixed_orchestrator_endpoint_id": orchestrator_endpoint_id,
            "allowed_assignment_targets": allowed_targets,
            "available_intelligence": list(self.public_catalog()),
        }
        return (
            "You are the selected KaroX orchestrator. Propose worker assignments only. "
            "Do not propose commands, tools, credentials, permissions, budgets, risk changes, "
            "or verification bypasses. KaroX will independently validate every choice.\n\n"
            "Return STRICT JSON with only these optional fields:\n"
            '{"assignments":{"ROLE_OR_STEP":"ENDPOINT_ID"},'
            '"effort_assignments":{"ROLE_OR_STEP":"EFFORT"},'
            '"rationale":"short secret-free explanation"}\n\n'
            "Planning input:\n"
            + json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        )

    def _endpoint(self, endpoint_id: str) -> IntelligenceEndpoint:
        try:
            endpoint = self.pool.get(endpoint_id)
        except Exception as exc:
            raise DelegationProposalError(
                f"delegation selected unknown endpoint: {endpoint_id}"
            ) from exc
        if not endpoint.enabled:
            raise DelegationProposalError(
                f"delegation selected disabled endpoint: {endpoint_id}"
            )
        return endpoint

    def apply(
        self,
        proposal: DelegationProposal | str | Mapping[str, Any],
        *,
        objective: str,
        recipe_name: str,
        risk_level: RiskLevel,
        policy: OrchestrationPolicy,
        orchestrator_endpoint_id: str,
        trusted_assignment_targets: Iterable[str] = (),
        task_id: str | None = None,
        run_id: str | None = None,
    ) -> GuidedPlanResult:
        """Validate the proposal, then let the normal verified router build the plan."""

        parsed = (
            proposal
            if isinstance(proposal, DelegationProposal)
            else DelegationProposal.parse(proposal)
        )
        selected_recipe = self.orchestrator.recipe_registry.get(recipe_name)
        allowed_targets = (
            {"orchestrator"}
            | {step.role for step in selected_recipe.steps}
            | {step.step_id for step in selected_recipe.steps}
        )

        unknown_assignments = sorted(set(parsed.assignments) - allowed_targets)
        if unknown_assignments:
            raise DelegationProposalError(
                "unknown delegation assignment target(s): "
                + ", ".join(unknown_assignments)
            )
        unknown_efforts = sorted(set(parsed.effort_assignments) - allowed_targets)
        if unknown_efforts:
            raise DelegationProposalError(
                "unknown delegation effort target(s): " + ", ".join(unknown_efforts)
            )

        fixed_orchestrator = self._endpoint(orchestrator_endpoint_id)
        proposed_orchestrator = parsed.assignments.get("orchestrator")
        if (
            proposed_orchestrator is not None
            and proposed_orchestrator != fixed_orchestrator.endpoint_id
        ):
            raise DelegationProposalError(
                "delegation proposal cannot replace the user-selected orchestrator"
            )

        assignments = dict(parsed.assignments)
        assignments["orchestrator"] = fixed_orchestrator.endpoint_id
        for endpoint_id in assignments.values():
            self._endpoint(endpoint_id)
        trusted_targets = frozenset(trusted_assignment_targets) | {"orchestrator"}
        unknown_trusted_targets = sorted(trusted_targets - set(assignments))
        if unknown_trusted_targets:
            raise DelegationProposalError(
                "trusted assignment target(s) are missing from the validated proposal: "
                + ", ".join(unknown_trusted_targets)
            )
        advisory_targets = frozenset(set(assignments) - trusted_targets)

        efforts: dict[str, str] = {}
        for target, effort in parsed.effort_assignments.items():
            try:
                efforts[target] = normalize_effort(effort)
            except ValueError as exc:
                raise DelegationProposalError(
                    f"invalid KaroX Effort for {target}: {effort}"
                ) from exc

        # Model-authored assignments stay advisory. Human overrides are passed in
        # ``trusted_assignment_targets`` and retain explicit-preference semantics;
        # all other proposal targets are only soft hints to the verified router.
        # This prevents a model proposal from granting itself high-risk first-use
        # authority or overriding stronger measured outcomes.
        plan = self.orchestrator.plan(
            objective=objective,
            recipe_name=recipe_name,
            risk_level=risk_level,
            policy=policy,
            orchestrator_endpoint_id=fixed_orchestrator.endpoint_id,
            role_assignments=assignments,
            advisory_assignment_targets=advisory_targets,
            effort_assignments=efforts,
            task_id=task_id,
            run_id=run_id,
        )
        catalog = self.public_catalog()
        return GuidedPlanResult(
            plan=plan,
            proposal=parsed,
            catalog_sha256=self._hash_json(catalog),
            proposal_sha256=self._hash_json(parsed.to_dict()),
        )


__all__ = [
    "DelegationProposal",
    "DelegationProposalError",
    "GuidedPlanResult",
    "OrchestratorDelegationBroker",
]
