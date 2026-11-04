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
import pathlib
import re
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

# The engine prints a startup banner and loads a global dotenv on import. A
# product import must be silent and must not adopt another tool's config file.
os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")

logger = logging.getLogger("karox.engine_kernel")


from .engine import __version__ as _engine_version  # noqa: E402
from .engine.agents.default import DefaultAgent  # noqa: E402
from .engine.exceptions import (  # noqa: E402
    FormatError,
    InterruptAgentFlow,
    UserInterruption,
)
from .engine_tool_env import CoreToolEnvironment, FINISH_BATCH_MARKER  # noqa: E402
from .engine_provider_bridge import (  # noqa: E402
    FINISH_TOOL_NAME,
    REWRITE_PRESSURE_TOKENS,
    EngineProviderModel,
    _response_from_engine_message,
)

from . import completion_gate  # noqa: E402
from . import check_report  # noqa: E402
from . import session_analysis  # noqa: E402
from .state_briefing import build_state_briefing  # noqa: E402
from .agent import (  # noqa: E402
    HARNESS_ORIGIN,
    REQUIRED_TOOLS,
    SYSTEM_PROMPT,
    AgentError,
    AgentEvent,
    AgentEventKind,
    AgentLimits,
    AgentReport,
    ContextBudget,
    _bounded_failure_summary,
    _is_small_talk_task,
    provider_alias,
)
from .agent_modes import normalize_mode  # noqa: E402
from .session_analysis import HARNESS_REQUESTED_BY  # noqa: E402
from .core import CoreRuntime, ToolDefinition  # noqa: E402
from .core_process import probe_forms_text  # noqa: E402
from .models import CoreResult, Origin  # noqa: E402
from .providers import (  # noqa: E402
    REASONING_EFFORTS,
    ImageAttachment,
    ModelResponse,
    ProviderError,
    ProviderErrorKind,
    ProviderTool,
)
from .security import redact, redact_content  # noqa: E402
from .sessions import MutationLease, SessionRecord, SessionStore  # noqa: E402
from .usage_analytics import merge_response_usage, usage_event_from_response  # noqa: E402



_MAX_SNAPSHOT_FILES = 50
_MAX_SNAPSHOT_BYTES = 2_000_000

#: Cap on one model request, in seconds. A single reply can legitimately be a
#: whole file: the aquarium run's first answer was 34k completion tokens, about
#: two minutes of streaming, and the old 120 s cap killed two runs mid-answer
#: with "deadline expired while reading stream". The cap still shrinks to the
#: time the run has left, so a nearly finished run never waits longer than it
#: has.
_MODEL_REQUEST_DEADLINE_SECONDS = 300.0


def _model_request_deadline(remaining_seconds: float) -> float:
    return min(_MODEL_REQUEST_DEADLINE_SECONDS, max(5.0, remaining_seconds))


#: The model's own wall clock is shortened by this much so the kernel can still
#: collect the proof a finished run owes: the approved check, the visual look
#: and the change review. Without it a run that fixed the artifact at minute 24
#: ended ``wall_time_limit`` with the grader passing and the report unverified.
#: Two 3D re-runs spent 1510 s each and stopped with "missing visual evidence".
FINISH_RESERVE_SECONDS = 120.0

#: Steps a refused finish keeps the visual grace window open: enough for a
#: probe page, a few browser reads and the attested finish the refusal asked
#: for. At 4, a measured 3D run (30/30 on the grader) re-probed for five steps
#: after its second refusal and was stopped one step before its finish.
_COVERAGE_GRACE_STEPS = 8


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


#: The engine execution contract appended to the product system prompt. It is
#: deliberately the last thing the model reads before deciding an action.
#:
#: Batching is part of the contract because it is the one lever the loop can
#: give the model without spending turns: every tool call in a response runs in
#: order and all observations return together, so two independent reads cost one
#: model call instead of two. The product prompt asks for the same thing; this
#: text used to say "prefer exactly one", which contradicted it and taught the
#: model to serialise work. Dependent calls stay serialised: a later argument
#: must not be guessed from a result the same response has not seen yet.
_ENGINE_CONTRACT = """

# KaroX engine execution contract

You act through typed tool calls; there is no shell.
- Batch independent actions: several tool calls in one response execute in
  order and their observations return together. Batch reads, and batch 2-4
  mutations whose arguments are already fully known. Never batch a call whose
  arguments depend on another call's result.
- Keep tool output small; observations are clipped.
- Read before you write. KaroX runs the approved checks itself after a change
  and returns failures for repair; call checks only when the result changes
  your next step. Run the narrowest test that covers your change first and
  the full suite once at the end; a passing check on unchanged files is not
  run again.
- An interactive page must behave as documented in every state a user or test
  can reach, including right after a test hook was called and after a reset:
  every documented key works, and behaviour that should happen on its own
  (spawning, movement, timers) starts within the first seconds of a run.
  Prove it the way a user would: press each named key with browser_command
  on the deliverable page, and let time pass from a fresh reset instead of
  forcing the behaviour.
- When the task is complete, call the finish tool with {"answer": "<final
  answer text>"}. For a web deliverable finish also carries "requirements":
  one {"id", "status", "observed"} entry per requirement ID KaroX lists, and a
  verified entry quotes a browser, check or visual result received after your
  last edit; a finish without it is refused with the list. After an accepted
  finish you cannot continue working. finish must be the
  only tool call in its response; if it is batched with other calls, it is
  refused and the other calls run, so call finish again after reviewing them.
- Never print tool markup as prose; a response without a tool call is a format
  error.
"""


#: Tool schemas whose payload is large and whose domain is narrow. They are
#: advertised only when the task names that domain. The tool itself, its
#: capability gate and its handler stay in place, so a model that already knows
#: the name can still call it; an unrelated task simply stops paying ~3.4 KB of
#: JSON on every request. Measured on the ledger fixture, the full catalog cost
#: about 1.1k prompt tokens per call of which none were ever called.
_DOMAIN_TOOL_KEYWORDS: Dict[str, tuple[str, ...]] = {
    "web.fetch": (
        "web",
        "website",
        "webpage",
        "http",
        "https",
        "url",
        "urls",
        "fetch",
        "documentation",
        "docs",
        "cdn",
        "unpkg",
        "jsdelivr",
        "github",
        "external library",
        "third-party",
        "npm package",
    ),
    "disk.scan": (
        "disk",
        "cleanup",
        "clean up",
        "free space",
        "storage",
        "cache",
        "prune",
        "temporary files",
    ),
    "disk.plan_cleanup": (
        "disk",
        "cleanup",
        "clean up",
        "free space",
        "storage",
        "cache",
        "prune",
        "temporary files",
    ),
    # The Safety section of the system prompt teaches the exact
    # `repo_command batch {op: delete, path}` form for deleting a scratch file,
    # so the name stays callable while it is not advertised. An argument error
    # on that rare call is cheaper than 1.3 KB of batch schema on every repair
    # request.
    "repo.command": (
        "batch",
        "atomic",
        "rename",
        "mkdir",
        "move file",
        "move the file",
        "delete file",
        "delete the file",
        "remove file",
        "remove the file",
    ),
}


def _tool_relevant_for_task(
    core_name: str, task: str, *, visual: bool = False, mode: Optional[str] = None
) -> bool:
    """Whether this tool's schema belongs on the request for this task."""

    keywords = _DOMAIN_TOOL_KEYWORDS.get(core_name)
    if keywords is None:
        return True
    if visual:
        # Image tasks commonly fetch a library or an asset.
        return True
    text = f"{task} {mode or ''}".lower()
    for word in keywords:
        # Word-ish boundaries: "asset" must not fire on "assets", and "url"
        # must not fire inside an unrelated identifier.
        if re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text):
            return True
    return False


class _EngineBudgetExceeded(RuntimeError):
    """A provider response crossed a configured budget; tool calls are blocked."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _EngineProviderFailure(RuntimeError):
    """A provider call failed after its own retries; the run must end."""

    def __init__(self, kind: ProviderErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind






#: The identity the engine-backed report stamps in its ``engine`` block: the
#: in-band proof for stream consumers (the race harness reads exactly this).
#: The engine's trajectory artifact under the session's runtime dir is the
#: durable proof; both name the same implementation.
_ENGINE_IDENTITY: Dict[str, str] = {
    "name": "karo-core",
    "version": _engine_version,
    "implementation": "karox.engine_kernel.EngineAgent",
}


_STALE_READ_TOOLS = {"repo_read_file", "repo_read_lines"}
_STALE_EDIT_TOOLS = {"repo_edit_file"}
_STALE_READ_MARKER = (
    "[KaroX: this read predates a later edit of the same file; "
    "re-read exact lines if needed]"
)
_STALE_READ_CHARS = 1_200


class EngineAgent(DefaultAgent):
    """``DefaultAgent`` with resume, cancellation and product event hooks.

    The decision loop -- query, context management, limits, action execution,
    finish contract -- is the engine's own; only the run/step/query seams are
    extended so the kernel can persist durable checkpoints after every step
    and a caller can cancel between steps.
    """

    def __init__(
        self,
        model: Any,
        env: Any,
        *,
        kernel: "EngineKernel",
        resume_step_calls: int = 0,
        **kwargs: Any,
    ) -> None:
        super().__init__(model, env, **kwargs)
        self._kernel = kernel
        self.n_calls = int(resume_step_calls)
        # The task message the durable snapshot is appended to, and its text
        # before the first append.
        self._briefing_target: Optional[int] = None
        self._briefing_base: str = ""

    def add_messages(self, *messages: Dict[str, Any]) -> List[Dict[str, Any]]:
        added = super().add_messages(*messages)
        self._kernel._on_engine_messages(added)
        return added

    def step(self) -> List[Dict[str, Any]]:
        if self._kernel._cancel_requested():
            raise UserInterruption(
                {
                    "role": "exit",
                    "content": (
                    "visual check budget is exhausted; stopping"
                    if self._kernel._visual_budget_hit
                    else "cancelled by the user"
                ),
                    "extra": {"exit_status": "UserInterruption", "submission": ""},
                }
            )
        self._kernel._on_step_started(self.n_calls + 1)
        try:
            result = super().step()
        finally:
            self._kernel._on_step_finished(self.n_calls)
        return result

    def execute_actions(self, message: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Run a batch, refusing a finish that is not alone.

        The engine executes calls in order and raises ``Submitted`` from the
        finish tool. In a batch that aborts the loop before the later calls
        run and before any observation is added, so a model that wrote
        ``[finish("created a and b"), write b]`` could be reported verified
        with only ``a`` on disk. Rewriting that finish to a refusal marker
        keeps the batch intact: every call runs, every result is visible, and
        the model must submit again with finish alone.
        """

        actions = (message.get("extra") or {}).get("actions") or []
        if len(actions) > 1 and any(
            isinstance(action, Mapping) and action.get("name") == FINISH_TOOL_NAME
            for action in actions
        ):
            rewritten: List[Any] = []
            for action in actions:
                if isinstance(action, Mapping) and action.get("name") == FINISH_TOOL_NAME:
                    rewritten.append(
                        {**action, "name": FINISH_BATCH_MARKER, "arguments": {}}
                    )
                else:
                    rewritten.append(action)
            message = {
                **message,
                "extra": {**(message.get("extra") or {}), "actions": rewritten},
            }
        return super().execute_actions(message)

    def query(self) -> Dict[str, Any]:
        if hasattr(self.model, "step"):
            self.model.step = self.n_calls + 1  # type: ignore[union-attr]
        message = super().query()
        budget = (message.get("extra") or {}).get("budget_exceeded")
        if budget:
            reason = str((message.get("extra") or {}).get("budget_reason") or "budget")
            raise _EngineBudgetExceeded(reason)
        return message

    def _supersede_stale_reads(self) -> None:
        """Shrink a read once the same file has been edited after it.

        A repair run reads a file, edits it, then re-sends the full original
        read on every later request. The edit result carries the diff and the
        read is no longer the current file, so the body is clipped to a marker
        with both ends: enough context to keep working, and an explicit note
        that exact lines need a fresh read. The durable session record keeps the
        full result; only the in-memory transcript shrinks.
        """

        edits: Dict[str, int] = {}
        reads: List[tuple[int, str, str]] = []
        for index, message in enumerate(self.messages):
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            for call in message.get("tool_calls") or ():
                if not isinstance(call, dict):
                    continue
                name = str(call.get("name") or "")
                if name not in _STALE_READ_TOOLS and name not in _STALE_EDIT_TOOLS:
                    continue
                try:
                    arguments = json.loads(str(call.get("raw_arguments") or ""))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if not isinstance(arguments, dict):
                    continue
                path = arguments.get("path")
                if not isinstance(path, str) or not path:
                    continue
                if name in _STALE_EDIT_TOOLS:
                    edits[path] = index
                else:
                    reads.append((index, path, str(call.get("call_id") or "")))
        if not edits:
            return
        tool_messages = {
            str(message.get("tool_call_id") or ""): message
            for message in self.messages
            if isinstance(message, dict) and message.get("role") == "tool"
        }
        stale: List[tuple[Dict[str, Any], str]] = []
        for read_index, path, call_id in reads:
            edit_index = edits.get(path)
            if edit_index is None or edit_index <= read_index:
                continue
            tool_message = tool_messages.get(call_id)
            if tool_message is None:
                continue
            content = tool_message.get("content")
            if (
                not isinstance(content, str)
                or len(content) <= 2_000
                or _STALE_READ_MARKER in content
            ):
                continue
            stale.append((tool_message, content))
        if not stale:
            return
        # Clipping rewrites a message the provider may hold in its prompt
        # cache; on a caching provider that re-bills the whole suffix after the
        # read, so the model layer decides when the shrink is worth it.
        allowed = getattr(self.model, "history_rewrite_allowed", None)
        reclaimable = sum(len(content) - _STALE_READ_CHARS for _, content in stale)
        if callable(allowed) and not allowed(reclaimable):
            return
        for tool_message, content in stale:
            tool_message["content"] = self._clip_text(
                content, _STALE_READ_CHARS, _STALE_READ_MARKER
            )

    def manage_context(self) -> None:
        self._supersede_stale_reads()
        events_before = self.n_context_events
        estimate_before = self._estimate_tokens()
        super().manage_context()
        if self.n_context_events > events_before:
            self._kernel._on_compacted(
                estimate_before,
                self._estimate_tokens(),
                self.n_context_events,
                overflow=int(getattr(self, "context_overflow_tokens", 0) or 0),
            )
            self._refresh_state_briefing()
        overflow = int(getattr(self, "context_overflow_tokens", 0) or 0)
        if overflow and self.n_context_events == events_before:
            if not getattr(self, "_overflow_reported", False):
                self._overflow_reported = True
                self._kernel._emit(
                    AgentEventKind.WARNING,
                    reason="context_floor_exceeded",
                    detail=(
                        f"history is ~{overflow} tokens over the context budget and "
                        "cannot be compacted further; the next request may exceed "
                        "the model window"
                    ),
                )
        elif not overflow:
            self._overflow_reported = False

    def _refresh_state_briefing(self) -> None:
        """Append the durable state to the task message after a compaction.

        The task message is outside the compactor's middle slice, so the block
        cannot be clipped or dropped and survives every later compaction. The
        base text is captured once, so a rebuild replaces the block instead of
        stacking a second copy.
        """

        briefing = self._kernel.state_briefing()
        if not briefing:
            return
        if self._briefing_target is None:
            for index, message in enumerate(self.messages):
                if message.get("role") == "user":
                    self._briefing_target = index
                    self._briefing_base = str(message.get("content") or "")
                    break
        if self._briefing_target is None or self._briefing_target >= len(self.messages):
            return
        self.messages[self._briefing_target]["content"] = (
            f"{self._briefing_base}\n\n{briefing}"
        )

    def _check_no_progress(self, message: Dict[str, Any], outputs: List[Dict[str, Any]]) -> None:
        """Same contract as the engine's port 4, with a typed-action key.

        The engine's stock key reads ``action["command"]``, which every typed
        action would blank to "". Keying on the action identity (tool name or
        command plus arguments) restores the original meaning: identical work
        with an identical result, N times in a row, is a finished model.
        """
        threshold = self.config.no_progress_streak
        if threshold <= 0:
            return
        actions = message.get("extra", {}).get("actions", [])
        identities: List[Any] = []
        for action in actions:
            if isinstance(action, dict) and action.get("command") is not None:
                identities.append(("command", str(action.get("command"))))
            else:
                identities.append(
                    (
                        "tool",
                        str(action.get("name")),
                        json.dumps(
                            action.get("arguments"), ensure_ascii=False, sort_keys=True
                        ),
                    )
                )
        # A rendered core result carries pids, timings and evidence ids that
        # differ on every run; its progress_key is the result without them.
        results = [
            (o.get("returncode"), str(o.get("progress_key") or o.get("output", "")))
            for o in outputs
        ]
        self._track_progress(repr((identities, results)), message)

    # -- run control -------------------------------------------------------

    def run(
        self,
        task: str = "",
        *,
        resume_messages: Optional[List[Dict[str, Any]]] = None,
        system_prompt: str = "",
        instance_contract: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        self.extra_template_vars |= {
            "task": task,
            "system_prompt": system_prompt,
            "instance_contract": instance_contract,
            **kwargs,
        }
        if resume_messages:
            self.messages = list(resume_messages)
        else:
            self.messages = []
            self.add_messages(
                self.model.format_message(
                    role="system",
                    content=self._render_template("{{ system_prompt }}"),
                ),
                self.model.format_message(
                    role="user",
                    content=self._render_template(self.config.instance_template),
                ),
            )
        return self._loop()

    def continue_run(self) -> Dict[str, Any]:
        """Resume the step loop after the caller appended repair messages."""
        if self.messages and self.messages[-1].get("role") == "exit":
            self.messages = self.messages[:-1]
        # A repair turn is a fresh chance: format errors spent before the
        # submission must not make its first malformed response the last.
        self.n_consecutive_format_errors = 0
        return self._loop()

    def _loop(self) -> Dict[str, Any]:
        # The engine's own run loop, unchanged in shape: a step either lands
        # cleanly, fails its format contract, or flows through an interrupt
        # (submission, limits, user stop) whose exit message ends the loop.
        while True:
            try:
                self.step()
                self.n_consecutive_format_errors = 0
            except FormatError as error:
                self.cost += float(
                    error.messages[0].get("extra", {}).get("cost") or 0.0
                )
                self.n_consecutive_format_errors += 1
                if (
                    0
                    < self.config.max_consecutive_format_errors
                    <= self.n_consecutive_format_errors
                ):
                    self.add_messages(*error.messages)
                    final_text = (
                        self._final_answer_text(error)
                        if self.config.submit_on_final_answer
                        else ""
                    )
                    if final_text:
                        self.add_messages(
                            {
                                "role": "exit",
                                "content": final_text,
                                "extra": {
                                    "exit_status": "Submitted",
                                    "exit_reason": "prose-final",
                                    "submission": final_text,
                                },
                            }
                        )
                    else:
                        self.add_messages(
                            {
                                "role": "exit",
                                "content": "RepeatedFormatError",
                                "extra": {
                                    "exit_status": "RepeatedFormatError",
                                    "submission": "",
                                },
                            }
                        )
                else:
                    self.add_messages(*error.messages)
            except _EngineBudgetExceeded:
                raise
            except ProviderError as exc:
                if exc.kind is ProviderErrorKind.BUDGET_EXCEEDED:
                    # A budget refusal is a controlled stop, not a provider
                    # failure: the session can be resumed with a wider budget.
                    raise _EngineBudgetExceeded("provider_budget") from exc
                raise _EngineProviderFailure(exc.kind, str(exc)) from exc
            except InterruptAgentFlow as interrupt:
                self.add_messages(*interrupt.messages)
            except Exception as exc:  # noqa: BLE001 - the engine's own behavior
                self.handle_uncaught_exception(exc)
                raise
            finally:
                self.save(self.config.output_path)
            if self.messages[-1].get("role") == "exit":
                break
        return self.messages[-1].get("extra", {})


class EngineKernel:
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
        self._step = 0
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

    # -- public surface ----------------------------------------------------

    def cancel(self) -> None:
        """Request cancellation; the loop stops at the next step boundary."""
        self._cancelled = True
        if self._cancel_event is not None:
            self._cancel_event.set()

    def note_visual_budget_exhausted(self) -> None:
        """End the run honestly once no further visual verdict can arrive.

        The checker's budget is per task. After it, every ``visual_check`` call
        returns ``method: limit`` and the completion gate can never see a
        passing vision verdict, so more edits cannot make the run verified.
        The aquarium run spent 26 steps editing after the first limit verdict
        and another 20 in a later run, then ended on the step limit with a
        broken resize. The report now says why it stopped.
        """

        self._visual_budget_hit = True
        self.cancel()

    def note_visual_limit(self, *, proven: bool) -> None:
        """React to a ``method: limit`` visual verdict.

        Without a pass on the current bytes no later verdict can make the run
        verified, so it ends (``note_visual_budget_exhausted``). With one, the
        limit changes nothing: the run can still finish verified without an
        edit, so it gets the same short grace as after the last real verdict
        (never a longer one). A measured run asked for one more look after its
        last pass and was stopped with a correct, still-proven game.
        """

        if not proven:
            self.note_visual_budget_exhausted()
            return
        if self._visual_grace_until is None:
            self._visual_grace_until = self._step + 1

    def note_visual_last_check(self, *, attest: bool = False) -> None:
        """Allow two more model calls after the last real visual verdict.

        The closing look can judge an edit made after the budget, but the model
        could also keep editing to the step limit (80 steps and 4.1M tokens in
        the karo-13 run). The grace window is the edit turn and the closing
        check; the next call stops the run honestly.

        ``attest``: the verdict is a pass that still covers the bytes of a
        deliverable whose finish owes a requirement attestation. Such a run is
        provable without another edit, but its finish needs fresh probes and
        the attestation, so it gets the window a refused finish gets. A
        measured 3D run (30/30 on the grader) got its last pass with no checks
        left, spent the two calls on the change review the pass asked for and
        was stopped before its first finish. The window is still bounded, and
        an edit inside it leaves the run unprovable: no further verdict can
        arrive except the closing look.
        """

        self._visual_grace_until = self._step + (
            _COVERAGE_GRACE_STEPS if attest else 1
        )

    def note_coverage_refusal(self) -> None:
        """Keep the visual grace window open for the attestation finish owes.

        A web deliverable's finish is refused until every requirement is
        attested against fresh results. If that refusal lands inside the grace
        window after the last visual verdict, the stop would come before the
        model could probe and attest, ending a correct run unverified.
        """

        if self._visual_grace_until is not None:
            self._visual_grace_until = max(
                self._visual_grace_until, self._step + _COVERAGE_GRACE_STEPS
            )

    def run(self, session_id: str) -> AgentReport:
        self._cancel_event = threading.Event()
        started = self.monotonic()
        self._deadline = started + float(self.limits.max_seconds)
        self._engine_submission = ""
        self._compaction = None
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
        try:
            record = self.sessions.load(session_id)
            self.sessions.validate_repository(record, self.core.repository)
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
                # Half the compaction budget, capped: the history is shrunk in
                # place well before compaction has to drop anything.
                rewrite_pressure_tokens=min(
                    REWRITE_PRESSURE_TOKENS, max(8_000, self._engine_context_budget() // 2)
                ),
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
                instance_template="{{ instance_contract }}\n\n{{ task }}",
            )
            try:
                exit_extra = agent.run(
                    record.task,
                    resume_messages=resume_messages,
                    system_prompt=self.system_prompt,
                    instance_contract="Solve the task in the repository above.",
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
                    return self._finish(
                        session_id,
                        lease,
                        "verified",
                        "verification",
                        reason,
                        provider_message,
                    )
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
                while True:
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
                    if completion_gate.only_unproducible(
                        completion_gate.floor(self.core, record)
                    ):
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
            reason = self._terminal_reason(session_id, exit_status)
            current = self.sessions.load(session_id)
            phase = "verification" if current.changed_files else "execution"
            return self._finish(session_id, lease, "stopped", phase, reason, provider_message)
        finally:
            self.sessions.release(lease)
            self._environment = None

    def remaining_seconds(self) -> float:
        return max(0.1, self._deadline - self.monotonic())

    # -- construction helpers ----------------------------------------------

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

    def _seed_history(
        self, session_id: str, lease: MutationLease, record: SessionRecord
    ) -> Tuple[Optional[List[Dict[str, Any]]], int]:
        """Initialize or reconstruct the engine transcript from the session."""
        if not record.provider_history:

            def update(current: SessionRecord) -> None:
                current.status = "active"
                current.phase = "execution"

            self._update_session(session_id, lease, update)
            return None, 0
        messages: List[Dict[str, Any]] = []
        assistant_steps = 0
        for entry in record.provider_history:
            role = entry.get("role")
            if role not in {"system", "user", "assistant", "tool"}:
                continue
            message: Dict[str, Any] = {
                "role": str(role),
                "content": entry.get("content"),
            }
            if role == "assistant":
                raw_calls = entry.get("tool_calls")
                if isinstance(raw_calls, list) and raw_calls:
                    message["tool_calls"] = [
                        {
                            "call_id": str(item.get("call_id", "")),
                            "name": str(item.get("name", "")),
                            "raw_arguments": str(item.get("raw_arguments", "")),
                        }
                        for item in raw_calls
                        if isinstance(item, Mapping)
                    ]
                blocks = entry.get("reasoning_blocks")
                if isinstance(blocks, list) and blocks:
                    message["reasoning_blocks"] = blocks
                assistant_steps += 1
            if role == "tool":
                call_id = str(entry.get("tool_call_id", ""))
                if not call_id:
                    continue
                message["tool_call_id"] = call_id
                self._persisted_call_ids.add(call_id)
            messages.append(message)
        if not self._task_covered(record):
            current_task = str(record.task)
            self._append_message(
                session_id,
                lease,
                {
                    "role": "user",
                    "content": current_task,
                    "kind": "task",
                    "origin": self.origin.key,
                },
                phase="execution",
            )
            # Insert before the first assistant, not at the end: a user message
            # between an assistant tool call and its result is rejected by
            # transports that pair them (OpenAI-compatible, Anthropic).
            insert_at = next(
                (
                    index
                    for index, message in enumerate(messages)
                    if message.get("role") == "assistant"
                ),
                len(messages),
            )
            messages.insert(insert_at, {"role": "user", "content": current_task})
        if not messages:
            return None, 0
        answered: Set[str] = {
            str(item.get("tool_call_id", ""))
            for item in messages
            if item.get("role") == "tool"
        }
        # A run interrupted between the model's call and its execution leaves
        # a tool_use without a tool_result; a transport that pairs them would
        # refuse the whole resumed request. Insert the synthetic result
        # immediately after its assistant message: appending all of them at
        # the end left assistant/user/tool messages in between. Synthesized in
        # memory only; the durable record still says the call never ran.
        rebuilt: List[Dict[str, Any]] = []
        for message in messages:
            rebuilt.append(message)
            if message.get("role") != "assistant":
                continue
            for call in message.get("tool_calls") or ():
                call_id = str(call.get("call_id", ""))
                if not call_id or call_id in answered:
                    continue
                answered.add(call_id)
                rebuilt.append(
                    {
                        "role": "tool",
                        "content": (
                            "<returncode>-1</returncode>\n<output>\nthe run "
                            "was interrupted before this call executed; it did "
                            "not run and changed nothing\n</output>"
                        ),
                        "tool_call_id": call_id,
                    }
                )
        return rebuilt, assistant_steps

    @staticmethod
    def _task_covered(record: SessionRecord) -> bool:
        task = str(record.task)
        if not task:
            return True
        for entry in record.provider_history:
            if entry.get("kind") == "task" and str(entry.get("content", "")).endswith(task):
                return True
        # The first user turn of an engine run is the rendered instance
        # template, which ends with the task. Requiring exact equality made
        # every engine-written session look uncovered and injected a second
        # task message at the end of a resumed transcript.
        return any(
            entry.get("role") == "user"
            and str(entry.get("content", "")).endswith(task)
            for entry in record.provider_history
        )

    # -- engine hooks ------------------------------------------------------

    def _cancel_requested(self) -> bool:
        if self._cancelled:
            return True
        if (
            self._visual_grace_until is not None
            and self._step > self._visual_grace_until
        ):
            self._visual_budget_hit = True
            return True
        return self._cancel_event is not None and self._cancel_event.is_set()

    def _model_event(self, kind: str, **fields: Any) -> None:
        try:
            event_kind = AgentEventKind(kind)
        except ValueError:
            return
        self._emit(event_kind, **fields)

    def _on_step_started(self, step: int) -> None:
        self._step = step
        self._heartbeat()
        self._emit(AgentEventKind.STEP_STARTED)

    def _on_step_finished(self, step: int) -> None:
        self._step = step
        self._emit(AgentEventKind.STEP_FINISHED)

    def _on_compacted(
        self, tokens_before: int, tokens_after: int, count: int, overflow: int = 0
    ) -> None:
        self._compaction = {
            "engine": True,
            "tokens_before": int(tokens_before),
            "tokens_after": int(tokens_after),
            "count": int(count),
            "over_budget_tokens": max(0, int(overflow)),
        }
        summary = f"engine history compacted to ~{tokens_after} tokens (event {count})"
        if overflow > 0:
            summary += f"; still ~{overflow} tokens over budget"
        self._emit(AgentEventKind.COMPACTED, summary=summary)

    def _on_engine_messages(self, messages: Sequence[Mapping[str, Any]]) -> None:
        """Persist new engine messages as durable session checkpoints.

        Tool results carry their own persistence path (the environment writes
        the full CoreResult); here only messages the environment did not
        already persist are written, deduplicated by tool call id.
        """
        session_id = self._session_id
        lease = self._lease
        if session_id is None or lease is None:
            return
        for message in messages:
            role = message.get("role")
            extra = message.get("extra") or {}
            if role == "exit":
                # Only a real submission is an answer: the engine's limit and
                # interruption exits carry their status as content, not prose.
                if str(extra.get("exit_status") or "") == "Submitted":
                    submission = str(extra.get("submission") or message.get("content") or "")
                    if submission:
                        self._engine_submission = submission
                        if str(extra.get("exit_reason") or "") == "prose-final":
                            # The prose answer was never added as an assistant
                            # message (the bridge raised FormatError for it),
                            # so the durable transcript gets it here.
                            self._append_message(
                                self._session_id,
                                self._lease,
                                {
                                    "role": "assistant",
                                    "origin": self.origin.key,
                                    "content": str(redact(submission)),
                                    "tool_calls": [],
                                    "provider": self.provider.provider_name,
                                    "model": self.model,
                                    "finish_reason": "stop",
                                    "usage": {},
                                },
                            )
                continue
            if role == "assistant":
                self._persist_engine_assistant(session_id, lease, message)
                continue
            if role == "tool":
                call_id = str(message.get("tool_call_id", ""))
                if not call_id or call_id in self._persisted_call_ids:
                    continue
                self._persisted_call_ids.add(call_id)
                self._append_message(
                    session_id,
                    lease,
                    {
                        "role": "tool",
                        "origin": self.origin.key,
                        "content": str(message.get("content") or ""),
                        "tool_call_id": call_id,
                    },
                )
                continue
            content = message.get("content")
            if self._format_error_response(message) is not None:
                self._persist_format_error_nudge(session_id, lease, message)
                continue
            history = self.sessions.load(session_id).provider_history
            # Keep the caller's kind: a repair prompt is engine_repair, not an
            # anonymous engine_note. Only a message without one gets the
            # first-turn/observation default.
            kind = str(message.get("kind") or "")
            if not kind:
                kind = "task" if not history and role == "user" else "engine_note"
            self._append_message(
                session_id,
                lease,
                {
                    "role": str(role),
                    "content": str(content) if content is not None else "",
                    "kind": kind,
                    "origin": self.origin.key,
                },
                phase="execution",
            )

    # -- persistence -------------------------------------------------------

    def _format_error_response(self, message: Mapping[str, Any]) -> Optional[ModelResponse]:
        """Rebuild the billed response behind a format-error nudge, if any.

        The nudge's ``extra`` carries the wire facts the bridge persisted when
        the model's answer failed the action contract. Tokens for that answer
        were spent, so the session ledger must count them exactly as the
        legacy loop counted a response whose tool calls failed validation.
        """
        extra = message.get("extra") or {}
        if not isinstance(extra, Mapping) or str(extra.get("interrupt_type") or "") != "FormatError":
            return None
        usage = extra.get("usage")
        if not isinstance(usage, dict) or not usage:
            return None
        response = extra.get("response")
        choices = response.get("choices") if isinstance(response, Mapping) else None
        choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], Mapping) else {}
        wire = choice.get("message") if isinstance(choice, Mapping) else None
        content = wire.get("content") if isinstance(wire, Mapping) else None
        return ModelResponse(
            content=content if isinstance(content, str) else None,
            tool_calls=(),
            finish_reason=choice.get("finish_reason") if isinstance(choice.get("finish_reason"), str) else None,
            usage={str(key): value for key, value in usage.items()},
            transport_attempts=1,
            selected_provider=extra.get("provider") if isinstance(extra.get("provider"), str) else None,
            selected_model=extra.get("model") if isinstance(extra.get("model"), str) else None,
        )

    def _persist_format_error_nudge(
        self, session_id: str, lease: MutationLease, message: Mapping[str, Any]
    ) -> None:
        response = self._format_error_response(message)
        if response is None:
            return
        content = message.get("content")
        entry = {
            "role": "user",
            "content": str(content) if content is not None else "",
            "kind": "engine_note",
            "origin": self.origin.key,
        }
        usage_event = usage_event_from_response(
            session_id=session_id,
            step=max(1, self._step),
            response=response,
        )
        usage_event["kind"] = "format_error"

        def update(record: SessionRecord) -> None:
            record.provider_history.append(entry)
            record.usage = merge_response_usage(
                record.usage,
                response,
                event=usage_event,
                model_fallback=self.model,
            )

        self._update_session(session_id, lease, update)

    def _update_session(
        self,
        session_id: str,
        lease: MutationLease,
        update: Callable[[SessionRecord], None],
    ) -> SessionRecord:
        record = self.sessions.load(session_id)
        update(record)
        saved = self.sessions.save(record, record.revision, lease)
        self._known_revision = saved.revision
        return saved

    def _append_message(
        self,
        session_id: str,
        lease: MutationLease,
        entry: Dict[str, Any],
        *,
        phase: Optional[str] = None,
    ) -> None:
        def update(record: SessionRecord) -> None:
            record.provider_history.append(entry)
            if phase:
                record.phase = phase

        self._update_session(session_id, lease, update)

    def _heartbeat(self) -> None:
        lease = self._lease
        if lease is None:
            return
        try:
            self.sessions.heartbeat(
                lease,
                ttl_seconds=min(3600.0, max(5.0, float(self.limits.max_seconds) + 5.0)),
                session_revision=self._known_revision,
            )
        except Exception:
            pass

    def _persist_engine_tool_result(
        self,
        *,
        call_id: str,
        alias: str,
        core_name: str,
        result: CoreResult,
        output: Mapping[str, Any],
        requested_by: str,
    ) -> None:
        session_id = self._session_id
        lease = self._lease
        if session_id is None or lease is None:
            return
        if call_id:
            self._persisted_call_ids.add(call_id)
        harness_call = requested_by == HARNESS_REQUESTED_BY
        if harness_call:
            for item in result.evidence:
                item.metadata["requested_by"] = HARNESS_REQUESTED_BY
        result_value = result.to_dict()
        if harness_call:
            result_value["requested_by"] = HARNESS_REQUESTED_BY
        content = str(output.get("observation") or output.get("output") or "")
        entry: Dict[str, Any]
        if call_id:
            entry = {
                "role": "tool",
                "origin": HARNESS_ORIGIN if harness_call else self.origin.key,
                "content": redact_content(content),
                "tool_call_id": call_id,
                "tool_name": alias,
                "core_name": core_name,
                "result": redact_content(result_value),
            }
        else:
            # A fenced command without a tool call id: the engine itself feeds
            # this back as a user observation, and the durable record matches.
            entry = {
                "role": "user",
                "origin": self.origin.key,
                "content": redact_content(content),
                "kind": "engine_observation",
            }

        def update(record: SessionRecord) -> None:
            record.provider_history.append(entry)
            stamped = {item.evidence_id: item.to_dict() for item in result.evidence}
            record.evidence = [
                stamped.get(item.get("evidence_id"), item) for item in record.evidence
            ]
            if not result.mutation:
                known = {
                    item.get("evidence_id")
                    for item in record.evidence
                    if isinstance(item, dict)
                }
                for item in result.evidence:
                    if item.evidence_id not in known:
                        record.evidence.append(item.to_dict())
            if core_name in {"git.status", "git.diff"}:
                key = "status" if core_name == "git.status" else "diff"
                data = result.data if isinstance(result.data, dict) else {}
                record.git_state[key] = {
                    "correlation_id": result.correlation_id,
                    "ok": result.ok,
                    "exit_code": data.get("exit_code"),
                    "timed_out": data.get("timed_out", False),
                    "sha256": data.get("sha256"),
                    "stdout": data.get("stdout", ""),
                }

        self._update_session(session_id, lease, update)

    def _persist_engine_tool_error(
        self,
        call_id: str,
        alias: str,
        core_name: Optional[str],
        error_type: str,
        message: str,
    ) -> None:
        session_id = self._session_id
        lease = self._lease
        if session_id is None or lease is None:
            return
        payload = {
            "ok": False,
            "error": {"type": error_type, "message": str(redact(message))[:4000]},
        }
        entry: Dict[str, Any]
        if call_id:
            entry = {
                "role": "tool",
                "origin": self.origin.key,
                "content": json.dumps(payload, ensure_ascii=False, sort_keys=True),
                "tool_call_id": call_id,
                "tool_name": alias,
                "core_name": core_name,
                "error": payload["error"],
            }
        else:
            entry = {
                "role": "user",
                "origin": self.origin.key,
                "content": json.dumps(payload, ensure_ascii=False, sort_keys=True),
                "kind": "engine_observation",
            }
        self._append_message(session_id, lease, entry)

    def _persist_engine_assistant(
        self, session_id: str, lease: MutationLease, message: Mapping[str, Any]
    ) -> None:
        response = _response_from_engine_message(message)
        tool_calls = []
        for item in response.tool_calls:
            safe_arguments, recoverable = session_analysis.safe_arguments(
                item.raw_arguments
            )
            tool_calls.append(
                {
                    "call_id": item.call_id,
                    "name": item.name,
                    "raw_arguments": safe_arguments,
                    "arguments_sha256": hashlib.sha256(
                        item.raw_arguments.encode("utf-8")
                    ).hexdigest(),
                    "recoverable": recoverable,
                }
            )
        entry: Dict[str, Any] = {
            "role": "assistant",
            "origin": self.origin.key,
            "content": redact(response.content) if response.content is not None else None,
            "reasoning_summary": (
                redact(response.reasoning_summary)
                if response.reasoning_summary is not None
                else None
            ),
            "tool_calls": tool_calls,
            "provider": response.selected_provider or self.provider.provider_name,
            "model": response.selected_model or self.model,
            "finish_reason": response.finish_reason,
            "response_id": response.response_id,
            "transport_attempts": response.transport_attempts,
            "usage": dict(response.usage),
        }
        telemetry = (message.get("extra") or {}).get("prompt_cache")
        if isinstance(telemetry, Mapping):
            # Digest-derived counts only: why this request did or did not hit
            # the provider's prefix cache.
            entry["prompt_cache"] = dict(telemetry)
        stored = dict(redact(entry))
        blocks = message.get("reasoning_blocks")
        if isinstance(blocks, list) and blocks:
            # Transport form, never display-redacted: a signed block that no
            # longer matches its signature is rejected by the provider.
            stored["reasoning_blocks"] = blocks
        usage_event = usage_event_from_response(
            session_id=session_id,
            step=max(1, self._step),
            response=response,
        )

        def update(record: SessionRecord) -> None:
            record.provider_history.append(stored)
            record.usage = merge_response_usage(
                record.usage,
                response,
                event=usage_event,
                model_fallback=self.model,
            )
            if isinstance(telemetry, Mapping) and telemetry.get("full_miss") is True:
                misses = record.usage.get("prompt_cache_misses")
                counts = dict(misses) if isinstance(misses, dict) else {}
                cause = str(telemetry.get("cause") or "unknown")
                counts[cause] = int(counts.get(cause) or 0) + 1
                record.usage["prompt_cache_misses"] = counts

        self._update_session(session_id, lease, update)

    # -- events ------------------------------------------------------------

    def _emit(self, kind: AgentEventKind, **fields: Any) -> None:
        observer = self._on_event
        if observer is None:
            return
        try:
            observer(AgentEvent(kind, self._step, **fields))
        except Exception:
            # A display observer must never end the run, and one malformed
            # event must not permanently mute the stream: drop this event,
            # keep the observer for the next one.
            logger.debug("dropping one unrenderable kernel event", exc_info=True)

    # -- harness finishing -------------------------------------------------

    def _harness_final_calls(self, record: SessionRecord) -> Tuple[Tuple[str, Dict[str, Any]], ...]:
        """The calls the kernel owes a finishing run, in order.

        Same discipline as the legacy kernel: only what is actually missing,
        never a check the model still owes a repair for, and the Git reads
        last so they describe the bytes the evidence chain will attest.
        """
        if self._verification(record):
            return ()
        if not record.changed_files:
            return ()
        missing = completion_gate.floor(self.core, record)
        if any(reason.startswith("active failed check:") for reason in missing):
            return ()
        if completion_gate.only_unproducible(missing):
            # No approved command can produce this proof; running the same
            # passing check again only pays for a duplicate and cannot change
            # the report. The gap stays visible in ``unmet``.
            return ()
        calls: List[Tuple[str, Dict[str, Any]]] = []
        seen: Set[Tuple[str, ...]] = set()
        for command in self.core.verification_command_order:
            joined = " ".join(command)
            if (
                f"missing current check: {joined}" in missing
                or f"unresolved failed check: {joined}" in missing
            ):
                calls.append(("checks.run", {"argv": list(command)}))
                seen.add(tuple(command))
        for argv in session_analysis.unresolved_model_checks(record, missing):
            if len(calls) >= 4:
                break
            if tuple(argv) not in seen:
                seen.add(tuple(argv))
                calls.append(("checks.run", {"argv": list(argv)}))
        # The fallback exists to produce proof when the floor names an
        # obligation but not a command (for example "missing behavioral
        # evidence" after a run that never checked anything). It must not fire
        # for an obligation a check cannot satisfy: a run whose only missing
        # proof is the final git.diff review has already run its checks, and
        # re-running the full suite there is a whole duplicate test run paid
        # for nothing. Mainline runs do verify themselves when the task asks
        # them to, so this case is the common one, not the corner one.
        check_owed = any(
            reason.startswith(
                (
                    "missing current check:",
                    "unresolved failed check:",
                    "missing current structural evidence:",
                    "missing behavioral evidence",
                    "missing runtime smoke evidence",
                    "missing browser runtime evidence",
                )
            )
            for reason in missing
        )
        # The floor can call a check current while the evidence chain has none:
        # its identity leaves scratch artifacts and harness pages out, and the
        # chain restarts at every changing write. A probe written after the
        # last check (fresh quotes for an attested finish) left a run that the
        # floor had cleared unverified, with only the Git reads run here. One
        # check on the final bytes closes the chain; Git reads alone never can.
        check_owed = check_owed or session_analysis.chain_owes_check(record)
        if not calls and check_owed:
            commands = [list(item) for item in self.core.verification_command_order] or [
                list(item) for item in sorted(self.core.verification_commands or ())
            ]
            if not commands:
                return ()
            argv = [item for item in commands[0] if item != "*"]
            if not argv:
                return ()
            calls.append(("checks.run", {"argv": argv}))
        visual_calls: Tuple[Tuple[str, Dict[str, Any]], ...] = ()
        if "missing visual evidence" in missing and "visual.check" in self._definitions:
            target = self._render_target(record)
            if target is not None:
                visual_calls = (("visual.check", {"path": target.name}),)
        return (*calls, *visual_calls, ("git.status", {}), ("git.diff", {}))

    def _render_target(self, record: SessionRecord) -> Optional[pathlib.Path]:
        del record
        try:
            from .web_check import default_pages

            pages = default_pages(self.core.repository)
        except Exception:
            return None
        for page in pages or ():
            candidate = self.core.repository / page
            if candidate.is_file():
                return candidate
        return None

    def _run_harness_final_calls(
        self, session_id: str, lease: MutationLease, deadline: float
    ) -> None:
        record = self.sessions.load(session_id)
        calls = self._harness_final_calls(record)
        if not calls:
            return
        self._emit(
            AgentEventKind.WARNING,
            reason="harness_final_checks",
            detail=", ".join(name for name, _arguments in calls),
        )
        environment = self._environment
        assert environment is not None
        for index, (core_name, arguments) in enumerate(calls, start=1):
            if self.monotonic() >= deadline:
                return
            history = self.sessions.load(session_id).provider_history
            digest = hashlib.sha256(
                f"{session_id}\0{len(history)}\0{index}\0{core_name}".encode("utf-8")
            ).hexdigest()
            action = {
                "name": provider_alias(core_name),
                "arguments": arguments,
                "tool_call_id": f"harness-{len(history)}-{index}-{digest[:8]}",
            }
            environment._execute(action, requested_by=HARNESS_REQUESTED_BY)
            self._heartbeat()
            current = self.sessions.load(session_id)
            if any(
                reason.startswith("active failed check:")
                for reason in completion_gate.floor(self.core, current)
            ):
                break

    def _append_repair_prompt(self, agent: EngineAgent, failure: str) -> None:
        """Give the model one bounded repair turn with the failure in front."""
        last_assistant: Optional[Dict[str, Any]] = None
        for message in reversed(agent.messages):
            if message.get("role") == "assistant":
                last_assistant = message
                break
        prompt = (
            "Verification failed after your finish. Repair the work and call "
            "finish again.\n\nFailure summary:\n"
            f"{failure}\n\n"
            "Fix the cause, keep changes minimal, and only then finish."
        )
        delivered = False
        if last_assistant is not None:
            for call in last_assistant.get("tool_calls") or ():
                call_id = str(call.get("call_id", ""))
                if not call_id:
                    continue
                answered = any(
                    message.get("role") == "tool" and message.get("tool_call_id") == call_id
                    for message in agent.messages
                )
                if not answered:
                    # Every unanswered call gets its result: a transport that
                    # pairs tool_use with tool_result (Anthropic, Gemini)
                    # refuses the request otherwise, and the repair turn would
                    # die before the model ever saw the failure. The first one
                    # -- the finish call -- carries the repair itself: a new
                    # user turn makes a thinking model's chat template drop the
                    # reasoning of every earlier turn, which re-bills the whole
                    # history on a caching provider (0.18M prompt tokens over
                    # two repairs in the W&B run) and loses the plan the model
                    # was following.
                    agent.add_messages(
                        {
                            "role": "tool",
                            "content": prompt
                            if not delivered
                            else (
                                "the task was submitted, but KaroX verification "
                                "failed; continue working"
                            ),
                            "tool_call_id": call_id,
                        }
                    )
                    delivered = True
        if not delivered:
            agent.add_messages({"role": "user", "content": prompt, "kind": "engine_repair"})

    # -- verification ------------------------------------------------------

    def _verification(self, record: SessionRecord) -> bool:
        return session_analysis.evidence_chain(record) and not completion_gate.floor(
            self.core, record
        )

    def _answer_complete(self, record: SessionRecord) -> bool:
        if self.require_change or record.changed_files:
            return False
        if session_analysis.attempted_mutation(record, self._mutating_mcp_tools()):
            return False
        # The finish tool's answer is the model's final message; the
        # transcript only carries it in the call arguments, so a terse model
        # that never wrote prose would otherwise leave a browser answer
        # unverifiable even after a successful inspection.
        if not (
            self._engine_submission
            or session_analysis.last_provider_message(record)
        ):
            return False
        if _is_small_talk_task(record.task):
            return True
        return bool(session_analysis.answer_basis(record))

    def _mutating_mcp_tools(self) -> frozenset[str]:
        """The bound MCP tools not classified read-only: they may change a server."""

        tools = getattr(self.core, "tools", None)
        if not callable(tools):
            return frozenset()
        return frozenset(
            definition.name
            for definition in tools()
            if definition.mutates and str(definition.name).startswith("mcp.")
        )

    def _completed(self, record: SessionRecord) -> bool:
        visual_ok = (
            not completion_gate.visual_required(record)
            or completion_gate.visual_proven(self.core, record)
        )
        return (self._verification(record) and visual_ok) or self._answer_complete(record)

    def _failure_summary(self, record: SessionRecord) -> str:
        """What the repair turn is told, in the order a fix needs it.

        The floor reasons say which obligation is unmet; the failure map says
        where. Parsed failures survive the log clipping, name the exact file
        and line, and make "nothing changed since the last repair" visible
        instead of silently repeating a turn.
        """

        pieces = list(completion_gate.floor(self.core, record))
        failed: List[Dict[str, Any]] = []
        # A map from an older state is worse than none: "missing current check"
        # means no check ran on these bytes, and showing a failure the last
        # edit may already have fixed sends the repair at the wrong file.
        failed_owed = any(
            reason.startswith(("active failed check:", "unresolved failed check:"))
            for reason in pieces
        )
        entries = record.provider_history if failed_owed else ()
        for entry in entries:
            if entry.get("role") != "tool" or entry.get("core_name") != "checks.run":
                continue
            result = entry.get("result")
            if not isinstance(result, dict):
                continue
            data = result.get("data")
            if not isinstance(data, dict) or data.get("verification_eligible") is not True:
                continue
            if (
                result.get("ok") is True
                and data.get("exit_code") == 0
                and not data.get("timed_out")
            ):
                continue
            failed.append(data)
        detail = ""
        seen_sets: List[tuple[str, ...]] = []
        last_report: Optional[Dict[str, Any]] = None
        for data in failed:
            report = data.get("failure_map")
            identity = check_report.fingerprints(report) if isinstance(report, dict) else ()
            seen_sets.append(identity)
            rendered = check_report.render_failure_map(report)
            if isinstance(report, dict) and report.get("failures"):
                # Only the latest failing state feeds the rerun hint: an older
                # failure may describe a file the last edit already fixed.
                last_report = report
            if not rendered:
                output = "\n".join(
                    str(data.get(field) or "") for field in ("stdout", "stderr")
                ).strip()
                rendered = _bounded_failure_summary(output) if output else ""
            # The latest failed check wins even when it has no rendered detail:
            # keeping an older map would present a stale failure as current.
            detail = rendered
        if seen_sets and seen_sets[-1]:
            repeats = sum(1 for item in seen_sets[:-1] if item == seen_sets[-1])
            if repeats:
                detail += (
                    f"\nThe same failure set is still present after {repeats} "
                    "earlier run(s): the last change did not alter it."
                )
        hint = check_report.render_targets(check_report.rerun_targets(last_report, 4))
        return "\n\n".join(part for part in ("; ".join(p for p in pieces if p), detail, hint) if part)

    @staticmethod
    def _last_failed_check_map(record: SessionRecord) -> Optional[Dict[str, Any]]:
        """The parsed failure map of the latest approved check, if it failed."""

        for entry in reversed(record.provider_history):
            if entry.get("role") != "tool" or entry.get("core_name") != "checks.run":
                continue
            result = entry.get("result")
            if not isinstance(result, dict):
                continue
            data = result.get("data")
            if not isinstance(data, dict) or data.get("verification_eligible") is not True:
                continue
            passed = (
                result.get("ok") is True
                and data.get("exit_code") == 0
                and not data.get("timed_out")
            )
            if passed:
                return None
            report = data.get("failure_map")
            return dict(report) if isinstance(report, dict) else None
        return None

    def state_briefing(self) -> str:
        """The deterministic session snapshot the agent appends after compaction.

        Built from the record, never from a model summary: requirements, the
        model's own plan, changed files, the latest approved check and its
        parsed failures all survive a compaction that drops the messages that
        first carried them.
        """

        if self._session_id is None:
            return ""
        try:
            record = self.sessions.load(self._session_id)
        except Exception:  # noqa: BLE001 - a missing snapshot must not break a turn
            return ""
        return build_state_briefing(
            task=record.task,
            requirements=record.requirements,
            plan_items=record.plan_items,
            changed_files=record.changed_files,
            history=record.provider_history,
        )

    def _terminal_reason(self, session_id: str, exit_status: str) -> str:
        if exit_status == "LimitsExceeded":
            return "step_limit"
        if exit_status == "TimeExceeded":
            return "wall_time_limit"
        if exit_status == "UserInterruption":
            return "visual_budget_exhausted" if self._visual_budget_hit else "cancelled"
        if exit_status == "RepeatedFormatError":
            return "format_errors_exhausted"
        record = self.sessions.load(session_id)
        if self.require_change and not record.changed_files:
            return "no_changes"
        missing = completion_gate.floor(self.core, record)
        if completion_gate.only_unproducible(missing):
            return "unproducible_requirement"
        if self._failure_summary(record):
            return "verification_failed"
        return "unverified_changes" if record.changed_files else "no_changes"

    def _provider_message(self) -> Optional[str]:
        if self._engine_submission:
            return str(redact(self._engine_submission))
        return None

    # -- snapshot / restore ------------------------------------------------

    def _snapshot_verified_state(
        self, session_id: str, lease: MutationLease, record: SessionRecord
    ) -> None:
        if not self._restore_on_regression or not record.changed_files:
            return
        files = list(record.changed_files)[:_MAX_SNAPSHOT_FILES]
        directory = self.sessions.session_dir(session_id) / "verified"
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        digests: Dict[str, str] = {}
        for relative in files:
            try:
                source = self.core.safe_path(relative)
            except Exception:
                continue
            if not source.is_file():
                continue
            try:
                payload = source.read_bytes()
            except OSError:
                continue
            if len(payload) > _MAX_SNAPSHOT_BYTES:
                continue
            target = directory / pathlib.PurePosixPath(str(relative))
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
            except OSError:
                continue
            digests[str(relative)] = hashlib.sha256(payload).hexdigest()
        if not digests:
            return
        snapshot = {
            "step": session_analysis.step_count(record.provider_history),
            "files": digests,
            "checked_at": time.time(),
        }

        def update(current: SessionRecord) -> None:
            current.verified_snapshot = dict(snapshot)

        try:
            self._update_session(session_id, lease, update)
        except Exception:
            # A failed snapshot write must not fail a run that succeeded; the
            # record still says verified.
            return

    def _restore_verified_state(
        self, session_id: str, record: SessionRecord
    ) -> Optional[str]:
        if not self._restore_on_regression:
            return None
        stored = dict(record.verified_snapshot) if record.verified_snapshot else {}
        files = stored.get("files")
        if not isinstance(files, dict) or not files:
            return None
        directory = self.sessions.session_dir(session_id) / "verified"
        restored: List[str] = []
        for relative, digest in files.items():
            source = directory / pathlib.PurePosixPath(str(relative))
            if not source.is_file():
                continue
            try:
                payload = source.read_bytes()
            except OSError:
                continue
            if hashlib.sha256(payload).hexdigest() != str(digest):
                continue
            try:
                target = self.core.safe_path(str(relative))
            except Exception:
                continue
            try:
                if target.is_file() and target.read_bytes() == payload:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                restored.append(str(relative))
            except OSError:
                continue
        if not restored:
            return None
        return (
            f"restored the verified state from step {stored.get('step', '?')} "
            f"({', '.join(restored[:5])})"
        )

    # -- finishing ---------------------------------------------------------

    def _record_provider_failure(
        self, session_id: str, lease: MutationLease, failure: _EngineProviderFailure
    ) -> AgentReport:
        def update(record: SessionRecord) -> None:
            failures = record.usage.get("provider_failures")
            if not isinstance(failures, list):
                failures = []
            failures.append(
                {
                    "at_step": self._step,
                    "kind": failure.kind.value,
                    "error": str(failure)[:400],
                }
            )
            record.usage["provider_failures"] = failures[-100:]

        self._update_session(session_id, lease, update)
        self._emit(
            AgentEventKind.ERROR,
            reason=f"provider_error:{failure.kind.value}",
            detail=str(failure)[:400],
        )
        return self._finish(
            session_id,
            lease,
            "failed",
            "provider",
            f"provider_error:{failure.kind.value}",
        )

    def _finish(
        self,
        session_id: str,
        lease: MutationLease,
        status: str,
        phase: str,
        reason: str,
        provider_message: Optional[str] = None,
    ) -> AgentReport:
        if status == "verified" and completion_gate.floor(
            self.core, self.sessions.load(session_id)
        ):
            status, phase, reason = "stopped", "verification", "completion_unverified"
        if status == "verified":
            current = self.sessions.load(session_id)
            if completion_gate.visual_required(current) and not completion_gate.visual_proven(
                self.core, current
            ):
                status, phase, reason = "stopped", "stopped", "render_unverified"
        restore_note: Optional[str] = None
        if status != "verified":
            current = self.sessions.load(session_id)
            if self._completed(current):
                # It is verified after all: this path is the one that ends as
                # an answer or a stop after the chain already held.
                restore_note = None
            else:
                restore_note = self._restore_verified_state(session_id, current)
        if restore_note is not None:
            self._emit(
                AgentEventKind.WARNING,
                reason="regression_restored",
                detail=restore_note,
            )

        def update(record: SessionRecord) -> None:
            record.status = status
            record.phase = phase
            if status != "verified":
                record.summary = f"Native agent stopped: {reason}."
                if restore_note is not None:
                    record.summary += " " + restore_note + "."
            elif reason == "answer":
                record.summary = "Native agent answered from recorded local evidence."
            else:
                record.summary = "Native agent completed with required local evidence."

        record = self._update_session(session_id, lease, update)
        verified = status == "verified" and self._completed(record)
        if status == "verified" and not verified:
            raise AgentError("session verification changed before final report")
        return self._report(
            record, reason=reason, provider_message=provider_message, restored=restore_note
        )

    def _report(
        self,
        record: SessionRecord,
        *,
        reason: str,
        provider_message: Optional[str] = None,
        restored: Optional[str] = None,
    ) -> AgentReport:
        verified = record.status == "verified" and self._completed(record)
        unmet: tuple[str, ...] = ()
        failure_map: Optional[Dict[str, Any]] = None
        if not verified:
            # A stopped run says why, and where the last approved check failed.
            # Those two facts are the difference between "not verified" and a
            # report a user can act on without opening the session directory.
            unmet = tuple(completion_gate.floor(self.core, record))
            failure_map = self._last_failed_check_map(record)
        self._emit(
            AgentEventKind.FINISHED,
            reason=reason,
            status=record.status,
            ok=verified,
            usage=dict(record.usage) if isinstance(record.usage, dict) else {},
        )
        return AgentReport(
            session_id=record.session_id,
            status=record.status,
            phase=record.phase,
            verified=verified,
            reason=reason,
            steps=session_analysis.step_count(record.provider_history),
            changed_files=tuple(record.changed_files),
            checks=tuple(record.checks),
            git_state=dict(record.git_state),
            evidence=tuple(record.evidence),
            usage=dict(record.usage),
            provider_message=provider_message
            or session_analysis.last_provider_message(record),
            answer_basis=session_analysis.answer_basis(record) if reason == "answer" else (),
            project_context=dict(self.project_context),
            compaction=dict(self._compaction) if self._compaction else None,
            restored=restored,
            unmet=unmet,
            failure_map=failure_map,
            mode=self.mode,
            engine=dict(_ENGINE_IDENTITY),
        )


def _engine_provider_tool(alias: str, definition: ToolDefinition) -> ProviderTool:
    return ProviderTool(alias, definition.description, dict(definition.input_schema))


def _finish_provider_tool() -> ProviderTool:
    return ProviderTool(
        FINISH_TOOL_NAME,
        (
            "Submit the final answer and end the task. Arguments: "
            '{"answer": "<final answer text>"}. A web deliverable also needs '
            '"requirements": one entry per requirement ID KaroX lists (it lists '
            "them when finish arrives without them). After an accepted finish "
            "you cannot continue working."
        ),
        {
            "type": "object",
            "properties": {
                "answer": {
                    "type": "string",
                    "description": "The final answer to report to the user.",
                },
                "requirements": {
                    "type": "array",
                    "description": (
                        "Requirement attestation for a web deliverable, one entry "
                        "per listed requirement ID. status verified: observed is "
                        "text copied exactly from a browser_command, checks_run or "
                        "visual_check result received after your last edit. "
                        "not_applicable: observed says why the line asks nothing "
                        "of the deliverable. unmet: the requirement is not met."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["verified", "not_applicable", "unmet"],
                            },
                            "observed": {"type": "string"},
                        },
                        "required": ["id", "status", "observed"],
                    },
                },
            },
            "required": ["answer"],
            "additionalProperties": False,
        },
    )
