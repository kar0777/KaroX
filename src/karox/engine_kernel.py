"""The vendored karo-core engine as KaroX's default agent kernel.

B1 wiring: ``karox.agent.AgentKernel`` delegates here by default, so
``karox agent run`` (and the TUI backend that spawns it) executes through
``src/karox/engine`` -- the decision loop measured at 8/11 in the harness race
(stage A v4) -- instead of the legacy v5 model/tool loop.

Three adapters keep the product's guarantees while the engine drives:

- :class:`EngineProviderModel` implements the engine's ``Model`` protocol on
  top of KaroX's provider layer, so OpenAI-compatible Chat, OpenAI Responses,
  Anthropic and Gemini all serve the engine's action-oriented protocol. It
  keeps the engine's ports for empty/truncated responses (format-error budget,
  the length-specific nudge, fenced-command salvage and the prose final answer
  contract) by raising the engine's own ``FormatError`` with the response
  shape those ports read.
- :class:`CoreToolEnvironment` implements the engine's ``Environment``
  protocol on top of ``CoreRuntime``: every action is a typed Core tool call
  that passes ``policy.py`` and ``risk_engine.py`` exactly as before, with
  mutation leases, idempotency keys, evidence persistence and clipped
  observations.
- :class:`EngineKernel` owns the session lease, the durable per-step
  checkpoints, cancellation, the harness finishing calls and the same
  evidence-chain verification the retired v5 loop used -- nothing here may turn
  a model's prose into a verified claim. The proof-state readers it shares
  with that loop live in :mod:`karox.session_analysis`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, Mapping, Optional, Set, Tuple

# The engine prints a startup banner and loads a global dotenv on import. A
# product import must be silent and must not adopt another tool's config file.
os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")

logger = logging.getLogger("karox.engine_kernel")


from .engine.agents.default import DefaultAgent  # noqa: E402
from .engine_tool_env import (  # noqa: E402
    CoreToolEnvironment,
)
from .engine_tool_catalog import (  # noqa: E402
    _engine_provider_tool,
    _finish_provider_tool,
    _tool_relevant_for_task,
)
from .engine_kernel_control import EngineControlMixin
from .engine_kernel_io import EngineIoMixin
from .engine_kernel_finish import EngineFinishMixin
from .engine_kernel_harness import EngineHarnessMixin
from .prompts import self_review_text
from . import check_baseline
from .engine_prompts import (  # noqa: E402
    _ENGINE_CONTRACT,
)
from .engine_agent import (  # noqa: E402
    _EngineAgentMixin,
    _EngineBudgetExceeded,
    _EngineProviderFailure,
)
from .engine_provider_bridge import (  # noqa: E402
    FINISH_TOOL_NAME,
    REWRITE_PRESSURE_TOKENS,
    EngineProviderModel,
)

from . import completion_gate  # noqa: E402
from . import session_analysis  # noqa: E402
from .agent import (  # noqa: E402
    REQUIRED_TOOLS,
    SYSTEM_PROMPT,
    AgentError,
    AgentEvent,
    AgentEventKind,
    AgentLimits,
    AgentReport,
    ContextBudget,
    provider_alias,
)
from .agent_modes import normalize_mode  # noqa: E402
from .core import CoreRuntime, ToolDefinition  # noqa: E402
from .core_process import probe_forms_text  # noqa: E402
from .models import Origin  # noqa: E402
from .providers import (  # noqa: E402
    REASONING_EFFORTS,
    ImageAttachment,
    ProviderTool,
)
from .sessions import MutationLease, SessionStore  # noqa: E402



#: Cap on one model request, in seconds. A single reply can legitimately be a
#: whole file: the aquarium run's first answer was 34k completion tokens, about
#: two minutes of streaming, and the old 120 s cap killed two runs mid-answer
#: with "deadline expired while reading stream". The cap still shrinks to the
#: time the run has left, so a nearly finished run never waits longer than it
#: has.
_MODEL_REQUEST_DEADLINE_SECONDS = 300.0


def _with_review_note(review: str, note: str) -> str:
    """Put the review's extra item before its closing "finish now" line.

    Appended after it, the model read "finish now" before "read each now".
    """

    if not note:
        return review
    head, sep, closing = review.rstrip().rpartition("\n")
    if not sep:
        return review + note
    return head + note + "\n" + closing


def _model_request_deadline(remaining_seconds: float) -> float:
    return min(_MODEL_REQUEST_DEADLINE_SECONDS, max(5.0, remaining_seconds))


#: The model's own wall clock is shortened by this much so the kernel can still
#: collect the proof a finished run owes: the approved check, the visual look
#: and the change review. Without it a run that fixed the artifact at minute 24
#: ended ``wall_time_limit`` with the grader passing and the report unverified.
#: Two 3D re-runs spent 1510 s each and stopped with "missing visual evidence".
FINISH_RESERVE_SECONDS = 120.0




def _model_wall_limit(max_seconds: float) -> tuple[int, float]:
    """Return ``(engine wall limit, reserved seconds)`` for one run.

    A short run keeps its whole budget: the reserve exists to buy proof time,
    not to make a 2-minute run stop at 0. A run needs at least the reserve plus
    two minutes of model time before the split is worth it.
    """

    total = int(float(max_seconds))
    if total <= 0:
        return 0, 0.0
    if total < int(FINISH_RESERVE_SECONDS) + 120:
        return max(1, total), 0.0
    reserve = FINISH_RESERVE_SECONDS
    return max(1, total - int(reserve)), reserve








def _any_language_server() -> bool:
    """Whether lsp.diagnostics could reach any allowlisted language server."""
    try:
        from .lsp_diagnostics import available_language_servers

        return any(row.get("available") for row in available_language_servers())
    except Exception:  # noqa: BLE001 - advertise rather than hide on doubt
        return True


#: The identity the engine-backed report stamps in its ``engine`` block: the
#: in-band proof for stream consumers (the race harness reads exactly this).
#: The engine's trajectory artifact under the session's runtime dir is the
#: durable proof; both name the same implementation. Defined in the leaf module
#: so the finish mixin can read it without importing this facade back.
from .engine_identity import ENGINE_IDENTITY as _ENGINE_IDENTITY  # noqa: E402,F401


class EngineAgent(_EngineAgentMixin, DefaultAgent):
    """``DefaultAgent`` with resume, cancellation and product event hooks.

    The decision loop -- query, context management, limits, action execution,
    finish contract -- is the engine's own; only the run/step/query seams are
    extended so the kernel can persist durable checkpoints after every step
    and a caller can cancel between steps.
    """


class EngineKernel(EngineHarnessMixin, EngineFinishMixin, EngineIoMixin, EngineControlMixin):
    """Runs one engine-backed native-agent session.

    Public surface mirrors the historical ``AgentKernel``: construct with the
    provider/model/core/session plumbing and an ``on_event`` observer, call
    ``run(session_id)``, receive an :class:`AgentReport``. The engine's native
    context/output ceilings and recovery behavior govern this path.
    """

    def __init__(
        self,
        *,
        provider: Any,
        model: str,
        core: CoreRuntime,
        sessions: SessionStore,
        origin: Origin,
        limits: AgentLimits = AgentLimits(),
        system_prompt: str = SYSTEM_PROMPT,
        context: ContextBudget = ContextBudget(),
        project_context: Optional[Mapping[str, Any]] = None,
        require_change: bool = False,
        max_output_tokens: Optional[int] = None,
        reasoning_effort: Optional[str] = None,
        restore_on_regression: bool = True,
        mode: Optional[str] = None,
        maintenance_mode: bool = False,
        self_review: bool = False,
        request_images: Tuple[ImageAttachment, ...] = (),
        pricing_registry: Optional[Any] = None,
        on_event: Optional[Callable[[AgentEvent], None]] = None,
        output_char_limit: int = 50_000,
        command_timeout: int = 300,
        engine_keep_recent_messages: int = 24,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("agent model must be a non-empty string")
        if not isinstance(system_prompt, str) or not system_prompt.strip():
            raise ValueError("agent system prompt must be a non-empty string")
        if reasoning_effort is not None and reasoning_effort not in REASONING_EFFORTS:
            raise ValueError(
                "reasoning effort must be one of " + ", ".join(sorted(REASONING_EFFORTS))
            )
        self.provider = provider
        self.model = model
        self.core = core
        self.sessions = sessions
        self.origin = origin
        self.limits = limits
        self.context = context
        self.project_context: Dict[str, Any] = dict(project_context or {})
        self.require_change = bool(require_change)
        self.maintenance_mode = bool(maintenance_mode)
        self.self_review = bool(self_review)  # one maintainer review before finish
        if self.maintenance_mode and self.require_change:
            raise ValueError("maintenance mode plans cleanup but never mutates directly")
        self.mode = None if mode is None else normalize_mode(mode)
        if not isinstance(request_images, tuple) or len(request_images) > 8:
            raise ValueError("agent request_images must be a tuple of at most 8 images")
        if not all(isinstance(item, ImageAttachment) for item in request_images):
            raise ValueError("agent request_images contains an invalid attachment")
        self.request_images = request_images
        if max_output_tokens is not None and (
            not isinstance(max_output_tokens, int)
            or isinstance(max_output_tokens, bool)
            or max_output_tokens <= 0
        ):
            raise ValueError("agent output ceiling must be a positive integer")
        self.max_output_tokens = max_output_tokens
        self.reasoning_effort = reasoning_effort
        self.pricing_registry = pricing_registry
        self._on_event = on_event
        self._output_char_limit = int(output_char_limit)
        self._command_timeout = int(command_timeout)
        self._engine_keep_recent_messages = int(engine_keep_recent_messages)
        self._restore_on_regression = bool(restore_on_regression)
        self.monotonic = monotonic
        self._cancel_event: Optional[threading.Event] = None
        self._cancelled = False
        #: Set when the model asks for a visual verdict after the checker budget
        #: is gone. No further check can pass, so the run cannot become
        #: verified; stopping here is honest and avoids a step-limit burn.
        self._visual_budget_hit = False
        #: After the last real visual verdict, the model gets two more calls to
        #: apply the fix and ask for the closing look. Without a bound, it kept
        #: editing to the step limit (80 steps, 4.1M tokens in one run).
        self._visual_grace_until: Optional[int] = None
        #: The one model call a visual-budget stop leaves for the final answer
        #: (``_closing_answer_turn``): the step after which the run stops.
        self._closing_turn_until: Optional[int] = None
        self._closing_turn_used = False
        self._step = 0
        self._last_step_usage: Dict[str, int] = {}
        self._known_revision: Optional[int] = None
        self._compaction: Optional[Dict[str, Any]] = None
        self._engine_submission = ""
        self._deadline = 0.0
        #: Seconds held back from the model's wall clock for final proof.
        self._finish_reserve_seconds = 0.0
        self._session_id: Optional[str] = None
        self._lease: Optional[MutationLease] = None
        self._environment: Optional[CoreToolEnvironment] = None
        self._persisted_call_ids: Set[str] = set()
        approved_checks = core.verification_commands
        if not approved_checks and not self.maintenance_mode:
            raise AgentError(
                "native agents require at least one user-approved verification command"
            )
        self.system_prompt = system_prompt + _ENGINE_CONTRACT
        probes = tuple(getattr(core, "probe_interpreters", ()) or ())
        if approved_checks:
            rendered = json.dumps(
                [list(item) for item in sorted(approved_checks)], ensure_ascii=False
            )
            if probes:
                self.system_prompt += (
                    "\nOnly these user-approved checks count as verification "
                    f"evidence: {rendered}"
                    "\nProbes: checks.run also runs "
                    + probe_forms_text(probes)
                    + ". Reproduce a reported bug with a probe before editing and "
                    "rerun it after the fix; a probe is never verification, so "
                    "finish on an approved check."
                )
            else:
                self.system_prompt += (
                    "\nOnly these user-approved checks may be executed and used as "
                    f"verification evidence: {rendered}"
                )
            if any(item and item[-1] == "*" for item in approved_checks):
                self.system_prompt += (
                    '\nAn entry ending in "*" is a prefix: repeat the arguments '
                    "before it exactly, then put your own arguments where the "
                    '"*" is, and never send the "*" itself.'
                )
            if any("karox.web_check" in item for item in approved_checks):
                # The aquarium k2 run guessed the render check "probably finds
                # index.html", copied its page there three times, paid for two
                # renders per check and left the copy in the user's tree.
                self.system_prompt += (
                    "\nkarox.web_check renders every .html page and .svg picture "
                    "at the repository root by itself, index.html first: it needs "
                    "no copy, rename or extra index.html of the deliverable."
                )
        definitions = {item.name: item for item in core.tools()}
        missing = REQUIRED_TOOLS.difference(definitions)
        if missing:
            raise AgentError(f"Core is missing required tools: {sorted(missing)}")
        self._definitions = definitions
        aliases: Dict[str, str] = {}
        for core_name in sorted(definitions):
            alias = provider_alias(core_name)
            current = aliases.get(alias)
            if current is not None and current != core_name:
                raise AgentError(
                    f"provider tool alias collision: {alias} maps to {current} and {core_name}"
                )
            aliases[alias] = core_name
        if len(set(aliases.values())) != len(aliases):
            raise AgentError("multiple provider aliases map to the same Core tool")
        self.engine_tool_aliases: Dict[str, str] = aliases
        self._permitted_tool_pairs = tuple(
            (alias, core_name)
            for alias, core_name in aliases.items()
            if self._may_call(definitions[core_name])
        )
        # lsp.diagnostics without any installed language server can only
        # refuse: a sandbox pytest run spent two steps learning that pyright
        # was missing. The name stays callable; only its schema is withheld.
        self._language_server_available = _any_language_server()

    # -- public surface ----------------------------------------------------







    def run(self, session_id: str) -> AgentReport:
        self._cancel_event = threading.Event()
        started = self.monotonic()
        self._deadline = started + float(self.limits.max_seconds)
        self._engine_submission = ""
        self._compaction = None
        self._closing_turn_until = None
        self._closing_turn_used = False
        self._step = 0
        self._persisted_call_ids = set()
        lease_ttl = min(3600.0, max(5.0, float(self.limits.max_seconds) + 5.0))
        lease = self.sessions.acquire(
            session_id,
            f"native-agent:{self.origin.identity}",
            ttl_seconds=lease_ttl,
        )
        self._session_id = session_id
        self._lease = lease
        # The visual checker calls the same provider from inside a tool; its
        # tokens belong in this session's usage like every other request. They
        # were dropped before (record_usage was never set), so web runs
        # reported fewer tokens and less cost than they spent.
        checker = getattr(self.core, "_visual_checker", None)
        if checker is not None:
            existing = getattr(checker, "record_usage", "")
            if existing is None or getattr(existing, "__func__", None) is type(self)._record_side_usage:
                checker.record_usage = self._record_side_usage
        try:
            record = self.sessions.load(session_id)
            self.sessions.validate_repository(record, self.core.repository)
            # Tests the task names are its targets: one that failed before the
            # edits and still fails is unfinished work, not pre-existing noise.
            self.core._task_test_names = check_baseline.task_test_names(record.task)
            if record.revoked:
                raise AgentError("session access has been revoked")
            if record.status == "verified" and self._completed(record):
                return self._report(
                    record,
                    reason="already_verified",
                    provider_message=session_analysis.last_provider_message(record),
                )
            resume_messages, resume_calls = self._seed_history(session_id, lease, record)
            model = EngineProviderModel(
                provider=self.provider,
                model_id=self.model,
                tools=self._advertised_tools(record.task or ""),
                known_names=set(self.engine_tool_aliases) | {FINISH_TOOL_NAME},
                max_output_tokens=self.max_output_tokens,
                reasoning_effort=self.reasoning_effort,
                event=self._model_event,
                deadline_seconds=lambda: _model_request_deadline(self.remaining_seconds()),
                images=self.request_images,
                rewrite_pressure_tokens=self._rewrite_pressure_tokens(),
                # One key per session: every step shares the prefix, and a
                # resumed session keeps routing to the cache its prefix built.
                cache_key="karox-"
                + hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24],
            )
            environment = CoreToolEnvironment(
                core=self.core,
                session_id=session_id,
                origin=self.origin,
                lease=lease,
                kernel=self,
                output_char_limit=self._output_char_limit,
                command_timeout=self._command_timeout,
            )
            self._environment = environment
            wall_limit, finish_reserve = _model_wall_limit(self.limits.max_seconds)
            self._finish_reserve_seconds = finish_reserve
            agent = EngineAgent(
                model,
                environment,
                kernel=self,
                resume_step_calls=resume_calls,
                step_limit=int(self.limits.max_steps) + resume_calls,
                wall_time_limit_seconds=wall_limit,
                cost_limit=0.0,
                context_token_budget=self._engine_context_budget(),
                output_path=self.sessions.session_dir(session_id)
                / "engine"
                / "trajectory.json",
                submit_on_final_answer=True,
                no_progress_streak=4,
                keep_recent_messages=self._engine_keep_recent_messages,
                system_template="{{ system_prompt }}",
                instance_template="{{ instance_contract }}\n\n{{ task }}{{ finish_checklist | default("") }}",
            )
            try:
                exit_extra = agent.run(
                    record.task,
                    resume_messages=resume_messages,
                    system_prompt=self.system_prompt,
                    instance_contract="Solve the task in the repository above.",
                    finish_checklist=""
                    if resume_messages
                    else self._task_dossier(record) + self._finish_checklist(record),
                )
            except _EngineBudgetExceeded as exc:
                return self._finish(
                    session_id,
                    lease,
                    "stopped",
                    "budget",
                    f"budget_exceeded:{exc.reason}",
                    self._provider_message(),
                )
            except _EngineProviderFailure as exc:
                return self._record_provider_failure(session_id, lease, exc)
            except KeyboardInterrupt:
                self.cancel()
                return self._finish(
                    session_id,
                    lease,
                    "stopped",
                    "execution",
                    "cancelled",
                    self._provider_message(),
                )
            except Exception as exc:  # noqa: BLE001 - report, never crash the caller
                return self._finish(
                    session_id,
                    lease,
                    "failed",
                    "execution",
                    f"engine_error:{type(exc).__name__}",
                    self._provider_message(),
                )
            exit_status = str(exit_extra.get("exit_status") or "")
            provider_message = self._provider_message()
            if exit_status == "UserInterruption" and self._visual_budget_hit:
                # The grace stop lands on the edit that followed the last real
                # visual verdict: the page has new bytes and no look. The
                # checker keeps one closing-look slot for exactly this case, so
                # collect it before reporting a budget stop. In the vision-test
                # run the model saw a pass, edited one CSS rule, and the run
                # stopped unverified although the closing look would have
                # passed the new bytes.
                self._run_harness_final_calls(session_id, lease, self._deadline)
                record = self.sessions.load(session_id)
                if self._completed(record):
                    reason = "answer" if self._answer_complete(record) else "submitted"
                    self._snapshot_verified_state(session_id, lease, record)
                    # Verified, but the stop came between two tool calls: the
                    # aquarium k1 run (2026-11-05) ended verified with "\n\n"
                    # as its whole reply. One closing call collects the answer.
                    if self._closing_answer_turn(agent, verified=True):
                        provider_message = self._provider_message()
                    return self._finish(
                        session_id,
                        lease,
                        "verified",
                        "verification",
                        reason,
                        provider_message,
                    )
            if exit_status == "UserInterruption" and self._visual_budget_hit:
                # Still unprovable: the stop stays a stop, but the user gets
                # the model's own final answer instead of an empty reply.
                if self._closing_answer_turn(agent):
                    provider_message = self._provider_message()
            # The model used its shortened wall clock; the rest is reserved for proof.
            # It gets the same final calls as a submission, and only a clean
            # completion gate turns that into `verified`.
            if (
                exit_status == "TimeExceeded"
                and self._finish_reserve_seconds > 0
                and self.remaining_seconds() > 5
            ):
                self._run_harness_final_calls(session_id, lease, self._deadline)
                record = self.sessions.load(session_id)
                if self._completed(record):
                    reason = "answer" if self._answer_complete(record) else "submitted"
                    self._snapshot_verified_state(session_id, lease, record)
                    if self._closing_answer_turn(agent, verified=True):
                        provider_message = self._provider_message()
                    return self._finish(
                        session_id,
                        lease,
                        "verified",
                        "verification",
                        reason,
                        provider_message,
                    )
            # Only a submission, or the reserved proof window above, may be
            # verified. A run that ended on a step or wall-clock limit, a
            # format-error budget or a user stop was not completed by the model,
            # and finishing evidence collected after an interrupted run would
            # launder an unfinished workspace into a verified one.
            if exit_status == "Submitted":
                # The engine submitted. KaroX -- never the model's prose --
                # decides what counts as verified, collecting the finishing
                # evidence itself and giving the model bounded repair rounds
                # when a check fails.
                repair_attempts = 0
                # One maintainer review before KaroX verifies a code change; prose
                # submissions skip finish, so it lives here, not only in finish.
                review_due = self._review_turn_due()
                while True:
                    if review_due:
                        review_due = False
                        self._append_repair_prompt(
                            agent, "", prompt=_with_review_note(self_review_text(), self._unopened_files_note(agent, session_id))
                        )
                    else:
                        self._run_harness_final_calls(session_id, lease, self._deadline)
                        record = self.sessions.load(session_id)
                        if self._completed(record):
                            reason = "answer" if self._answer_complete(record) else "submitted"
                            self._snapshot_verified_state(session_id, lease, record)
                            return self._finish(
                                session_id, lease, "verified", "verification", reason, provider_message
                            )
                        if exit_status != "Submitted":
                            break
                        failure = self._failure_summary(record)
                        floor_now = completion_gate.floor(self.core, record)
                        if completion_gate.only_attestation(floor_now) and self._attestation_spent():
                            self._emit(
                                AgentEventKind.WARNING,
                                reason="attestation_open",
                                detail="requirements stayed unattested after the bounded refusals",
                            )
                            break
                        if completion_gate.only_unproducible(floor_now):
                            # A repair turn cannot produce this proof: no approved
                            # command exists for it. Asking the model to try again
                            # spends turns and tokens on an impossible obligation.
                            self._emit(
                                AgentEventKind.WARNING,
                                reason="unproducible_requirement",
                                detail=(
                                    "the remaining proof cannot be produced by an "
                                    "approved command"
                                ),
                            )
                            break
                        remaining = self._deadline - self.monotonic()
                        if (
                            not failure
                            or repair_attempts >= self.limits.max_repair_prompts
                            or remaining < 1.0
                        ):
                            break
                        repair_attempts += 1
                        self._emit(
                            AgentEventKind.WARNING,
                            reason="engine_repair_prompt",
                            detail="verification failed; one bounded repair turn follows",
                        )
                        self._append_repair_prompt(agent, failure)
                    try:
                        exit_extra = agent.continue_run()
                    except _EngineBudgetExceeded as exc:
                        return self._finish(
                            session_id,
                            lease,
                            "stopped",
                            "budget",
                            f"budget_exceeded:{exc.reason}",
                            self._provider_message(),
                        )
                    except _EngineProviderFailure as exc:
                        return self._record_provider_failure(session_id, lease, exc)
                    except KeyboardInterrupt:
                        self.cancel()
                        return self._finish(
                            session_id,
                            lease,
                            "stopped",
                            "execution",
                            "cancelled",
                            self._provider_message(),
                        )
                    except Exception as exc:  # noqa: BLE001 - report, never crash
                        return self._finish(
                            session_id,
                            lease,
                            "failed",
                            "execution",
                            f"engine_error:{type(exc).__name__}",
                            self._provider_message(),
                        )
                    exit_status = str(exit_extra.get("exit_status") or "")
                    provider_message = self._provider_message()
                    if exit_status != "Submitted":
                        # A repair or review turn that ended on a cancel, a
                        # limit or a format-error budget was not submitted:
                        # running the final checks now could still return
                        # "verified" after the user said stop.
                        break
            reason = self._terminal_reason(session_id, exit_status)
            current = self.sessions.load(session_id)
            phase = "verification" if current.changed_files else "execution"
            return self._finish(session_id, lease, "stopped", phase, reason, provider_message)
        finally:
            self.sessions.release(lease)
            self._environment = None


    # -- construction helpers ----------------------------------------------

    def _task_dossier(self, record: Any) -> str:
        """Candidate files, the functions they hold and their tests, found before step one.

        Sits in the cached first message. ``KAROX_TASK_DOSSIER=0`` turns it off
        for an ablation run.
        """

        if os.environ.get("KAROX_TASK_DOSSIER", "1") == "0":
            return ""
        try:
            from . import task_dossier

            text = task_dossier.build(self.core, str(getattr(record, "task", "") or ""))["text"]
        except Exception:  # noqa: BLE001 - optional context never stops a run
            logger.debug("task dossier failed", exc_info=True)
            return ""
        return f"\n\n<karox_dossier>\n{text}\n</karox_dossier>" if text else ""

    def _may_call(self, definition: ToolDefinition) -> bool:
        required = (definition.capability, *definition.additional_capabilities)
        return all(
            self.core.policy.decide(self.origin, capability).allowed
            for capability in required
        )

    def _advertised_tools(self, task: str) -> Tuple[ProviderTool, ...]:
        """The schemas this task pays for, plus finish.

        Capability filtering removes tools this origin may not use at all; this
        pass additionally removes large, domain-specific schemas whose trigger
        words the task does not contain. Hidden names stay in ``known_names``,
        so an explicit call still reaches Core and is audited; the saving is
        only in what every unrelated request has to carry.
        """

        return tuple(
            _engine_provider_tool(alias, self._definitions[core_name])
            for alias, core_name in self._permitted_tool_pairs
            if _tool_relevant_for_task(
                core_name,
                task,
                visual=bool(self.request_images),
                mode=self.mode,
            )
            and (core_name != "lsp.diagnostics" or self._language_server_available)
        ) + (_finish_provider_tool(),)

    def _engine_context_budget(self) -> int:
        """The engine's compaction budget in the engine's own token units.

        The engine estimates tokens as chars//4 while the product budgets
        against chars/3, so a naive pass of the product ceiling would let the
        engine compact ~33% later than the native ceiling allows. Scaling by
        3/4 aligns the engine's estimate with the product's ceiling.
        """
        ceiling = max(1_000, int(self.context.token_ceiling))
        # Completion is part of the same window. A large output ceiling and a
        # high utilization could otherwise ask the provider for a prompt that
        # already fills the window, leaving no room for the answer.
        reserve = int(self.max_output_tokens or 0)
        if reserve > 0:
            ceiling = max(1_000, ceiling - reserve)
        return max(1_000, int(ceiling * 0.75))

    def _rewrite_pressure_tokens(self) -> int:
        """Token threshold at which a caching run rewrites stale history in one batch.

        For unknown or small (<=128K) context windows, ``REWRITE_PRESSURE_TOKENS``
        (96K) remains the ceiling so a request never approaches a 120K-128K limit
        before shrinking. For larger advertised windows (e.g. 256K or 1M), the
        threshold is derived from the usable input budget after reserving room
        for completion output tokens, system prompt + tool schemas, and new
        messages before ``_engine_context_budget()`` triggers hard compaction.
        """
        budget = self._engine_context_budget()
        window = self.context.max_input_tokens
        if not self.context.window_known or window is None or window <= 128_000:
            return min(REWRITE_PRESSURE_TOKENS, max(8_000, budget // 2))
        ceiling = max(1_000, int(self.context.token_ceiling))
        output_reserve = (
            int(self.max_output_tokens)
            if self.max_output_tokens is not None and self.max_output_tokens > 0
            else int(window * 0.20)
        )
        usable_ceiling = max(1_000, ceiling - output_reserve)
        usable_budget = min(budget, max(1_000, int(usable_ceiling * 0.75)))
        headroom = max(24_000, usable_budget // 4)
        return max(8_000, usable_budget - headroom)



    # -- engine hooks ------------------------------------------------------








    # -- persistence -------------------------------------------------------











    # -- events ------------------------------------------------------------


    # -- harness finishing -------------------------------------------------







    # -- verification ------------------------------------------------------










    # -- snapshot / restore ------------------------------------------------



    # -- finishing ---------------------------------------------------------
