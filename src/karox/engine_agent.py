"""The engine-agent mixin: the product's DefaultAgent extensions.

The concrete :class:`karox.engine_kernel.EngineAgent` class stays in
``engine_kernel.py`` and composes this mixin. That is deliberate: the vendored
engine serializes ``self.__class__.__module__`` into its trajectory, and the
product report and tests pin ``karox.engine_kernel.EngineAgent``. Moving the
concrete class would silently change a durable artifact.

This module owns only the agent-side seam: resume, cancellation between steps,
budget/provider-failure signalling, context management, stale-read superseding,
state-briefing refresh and the no-progress guard. The kernel (session lease,
durable IO, verification, report) lives in ``engine_kernel`` and its mixins.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Tuple

# The vendored engine prints a startup banner and loads a global dotenv on
# import. Set this before the first ``.engine`` import, exactly as the kernel
# facade does.
os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")

if TYPE_CHECKING:
    from .engine_kernel import EngineKernel

from .engine.exceptions import (  # noqa: E402
    FormatError,
    InterruptAgentFlow,
    UserInterruption,
)
from .engine_tool_env import (  # noqa: E402
    FINISH_BATCH_MARKER,
)
from .engine_provider_bridge import (  # noqa: E402
    FINISH_TOOL_NAME,
    _ELIDABLE_ARGUMENT_TOOLS,
    _ELIDED_ARGUMENT_CHARS,
    _conservative_text_chars,
    _elided_argument_marker,
)
from .state_briefing import (  # noqa: E402
    HEADER as STATE_BRIEFING_HEADER,
)
from .agent import (  # noqa: E402
    AgentEventKind,
)
from .providers import (  # noqa: E402
    ProviderError,
    ProviderErrorKind,
)
from .unified_patch import codex_patch_files  # noqa: E402

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

_STALE_READ_TOOLS = {"repo_read_file", "repo_read_lines"}
_STALE_EDIT_TOOLS = {"repo_edit_file", "repo_write_file", "repo_apply_patch"}
_STALE_READ_MARKER = (
    "[KaroX: this read predates a later edit of the same file; "
    "re-read exact lines if needed]"
)
_STALE_READ_CHARS = 1_200


def _touched_edit_paths(name: str, arguments: Mapping[str, Any]) -> Tuple[str, ...]:
    """Relative repository paths modified by a write, edit, or patch tool call."""

    paths: List[str] = []
    path = arguments.get("path")
    if isinstance(path, str) and path:
        paths.append(path)
    if name == "repo_apply_patch":
        patch_text = arguments.get("patch")
        if isinstance(patch_text, str):
            for line in patch_text.splitlines():
                if line.startswith(("+++ b/", "--- a/")):
                    candidate = line[6:].strip()
                    if candidate and candidate != "/dev/null" and candidate not in paths:
                        paths.append(candidate)
            for _kind, candidate in codex_patch_files(patch_text):
                if candidate not in paths:
                    paths.append(candidate)
    return tuple(paths)


_BRIEFING_BASE_KEY = "_karox_briefing_base"


class _EngineAgentMixin:
    """Methods composed into the concrete EngineAgent in engine_kernel."""

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

    def _message_wire_chars(self, message: Mapping[str, Any]) -> int:
        elided = getattr(self.model, "_elided_calls", None)
        elided_set = elided if isinstance(elided, set) else set()
        total = _conservative_text_chars(str(message.get("content") or ""))
        for call in message.get("tool_calls") or ():
            if not isinstance(call, Mapping):
                continue
            call_id = str(call.get("call_id") or "")
            name = str(call.get("name") or "")
            raw = str(call.get("raw_arguments") or "")
            if (
                call_id in elided_set
                and name in _ELIDABLE_ARGUMENT_TOOLS
                and len(raw) > _ELIDED_ARGUMENT_CHARS
            ):
                raw = _elided_argument_marker(name)
            total += _conservative_text_chars(raw) + len(name) + 16
        for block in message.get("reasoning_blocks") or ():
            if isinstance(block, Mapping):
                total += (
                    _conservative_text_chars(str(block.get("text") or ""))
                    + len(str(block.get("data") or ""))
                    + len(str(block.get("signature") or ""))
                )
        total += _conservative_text_chars(str(message.get("reasoning_content") or ""))
        return total

    def _estimate_tokens(self) -> int:
        """Conservative local token estimate with UTF-8, JSON-escape, tool-schema and elision accounting."""

        extra_tools = int(getattr(self.model, "extra_tool_schema_tokens", 0) or 0)
        chars = sum(
            self._message_wire_chars(m) for m in self.messages if isinstance(m, Mapping)
        )
        return (chars // 4) + extra_tools

    def _effective_prompt_tokens(self) -> int:
        """Combine local conservative estimate with provider-reported usage + post-turn delta."""

        local_tokens = self._estimate_tokens()
        last_prompt = int(getattr(self.model, "last_prompt_tokens", 0) or 0)
        last_local = int(getattr(self.model, "last_prompt_local_tokens", 0) or 0)
        if last_prompt <= 0:
            return local_tokens
        if last_local <= 0:
            return max(local_tokens, last_prompt)
        delta = local_tokens - last_local
        if delta >= 0:
            return max(local_tokens, last_prompt + delta)
        return max(local_tokens, int(last_prompt * (local_tokens / max(1, last_local))))

    def _supersede_stale_reads(self, *, force: bool = False) -> None:
        """Coordinated in-place history shrink for superseded reads and old writes.

        A repair run reads a file, edits or overwrites it, then re-sends the
        full original read on every later request. Meanwhile, older completed
        whole-file writes and patches carry arguments that are already on disk.
        Coordinating both into a single batch decision prevents two separate
        prefix-cache invalidations a few turns apart, and keeps ``self.messages``
        and ``_elided_calls`` synchronized before ``super().manage_context()``
        decides whether middle messages must be dropped.
        """

        tool_messages: Dict[str, Tuple[int, Dict[str, Any]]] = {}
        call_message_index: Dict[str, int] = {}
        edits: Dict[str, int] = {}
        reads: List[tuple[int, str, str]] = []
        for index, message in enumerate(self.messages):
            if not isinstance(message, dict):
                continue
            if message.get("role") == "tool":
                cid = str(message.get("tool_call_id") or "")
                if cid:
                    tool_messages[cid] = (index, message)
        for index, message in enumerate(self.messages):
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            for call in message.get("tool_calls") or ():
                if not isinstance(call, dict):
                    continue
                call_id = str(call.get("call_id") or "")
                if call_id:
                    call_message_index[call_id] = index
                name = str(call.get("name") or "")
                if name not in _STALE_READ_TOOLS and name not in _STALE_EDIT_TOOLS:
                    continue
                try:
                    arguments = json.loads(str(call.get("raw_arguments") or ""))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if not isinstance(arguments, dict):
                    continue
                if name in _STALE_EDIT_TOOLS:
                    pair = tool_messages.get(call_id)
                    tool_text = str(pair[1].get("content") or "") if pair else ""
                    if (
                        "<returncode>" in tool_text
                        and "<returncode>0</returncode>" not in tool_text
                    ):
                        continue
                    for path in _touched_edit_paths(name, arguments):
                        edits[path] = index
                else:
                    read_path = arguments.get("path")
                    if isinstance(read_path, str) and read_path:
                        reads.append((index, read_path, call_id))
        stale: List[tuple[int, Dict[str, Any], str]] = []
        for read_index, path, call_id in reads:
            edit_index = edits.get(path)
            if edit_index is None or edit_index <= read_index:
                continue
            pair = tool_messages.get(call_id)
            if pair is None:
                continue
            tool_idx, tool_message = pair
            content = tool_message.get("content")
            if (
                not isinstance(content, str)
                or len(content) <= 2_000
                or _STALE_READ_MARKER in content
            ):
                continue
            stale.append((tool_idx, tool_message, content))
        pending_fn = getattr(self.model, "pending_elidable_writes", None)
        pending_writes: List[tuple[str, str, int]] = (
            list(pending_fn(self.messages)) if callable(pending_fn) else []
        )
        reclaimable_reads = sum(len(content) - _STALE_READ_CHARS for _, _, content in stale)
        reclaimable_writes = sum(size for _, _, size in pending_writes)
        total_reclaimable = reclaimable_reads + reclaimable_writes
        if total_reclaimable <= 0:
            return
        if not force:
            allowed = getattr(self.model, "history_rewrite_allowed", None)
            if callable(allowed):
                mutated_indices = [idx for idx, _, _ in stale] + [
                    call_message_index[cid]
                    for cid, _, _ in pending_writes
                    if cid in call_message_index
                ]
                first_mutated = min(mutated_indices) if mutated_indices else 2
                suffix_chars = max(
                    0,
                    sum(
                        self._message_wire_chars(m)
                        for m in self.messages[first_mutated:]
                        if isinstance(m, Mapping)
                    )
                    - total_reclaimable,
                )
                try:
                    permitted = allowed(
                        total_reclaimable,
                        estimated_tokens=self._effective_prompt_tokens(),
                        invalidated_suffix_chars=suffix_chars,
                    )
                except TypeError:
                    permitted = allowed(total_reclaimable)
                if not permitted:
                    return
        for _, tool_message, content in stale:
            tool_message["content"] = self._clip_text(
                content, _STALE_READ_CHARS, _STALE_READ_MARKER
            )
        if pending_writes:
            elide_fn = getattr(self.model, "elide_calls", None)
            if callable(elide_fn):
                elide_fn(call_id for call_id, _, _ in pending_writes)
        note_fn = getattr(self.model, "note_history_rewritten", None)
        if callable(note_fn):
            note_fn(total_reclaimable)

    def _strip_existing_briefing(self) -> Optional[Tuple[int, str, str]]:
        """Remove an attached state briefing from messages[2:], returning (index, base, full)."""

        header_block = f"\n\n{STATE_BRIEFING_HEADER}\n"
        for index in range(len(self.messages) - 1, 1, -1):
            message = self.messages[index]
            if not isinstance(message, dict):
                continue
            if _BRIEFING_BASE_KEY in message:
                full = str(message.get("content") or "")
                base = str(message.pop(_BRIEFING_BASE_KEY) or "")
                message["content"] = base
                return index, base, full
            content = str(message.get("content") or "")
            end_tag = content.rfind("</output>")
            tail_start = end_tag + len("</output>") if end_tag >= 0 else 0
            pos = content.find(header_block, tail_start)
            if pos >= tail_start and content[pos + 2 :].startswith(f"{STATE_BRIEFING_HEADER}\nTask:"):
                base = content[:pos]
                message["content"] = base
                return index, base, content
        return None

    def manage_context(self) -> None:
        budget = self.config.context_token_budget
        over_budget = budget > 0 and self._effective_prompt_tokens() > budget
        self._supersede_stale_reads(force=over_budget)
        if budget <= 0 or self._effective_prompt_tokens() <= budget or len(self.messages) <= 2:
            self.context_overflow_tokens = 0
            self._overflow_reported = False
            return
        events_before = self.n_context_events
        estimate_before = self._estimate_tokens()
        previous_briefing = self._strip_existing_briefing()
        before_snapshots = [
            (
                m.get("content"),
                m.get("reasoning_content"),
                tuple(
                    c.get("raw_arguments")
                    for c in (m.get("tool_calls") or ())
                    if isinstance(c, Mapping)
                ),
            )
            for m in self.messages
        ]
        briefing = self._kernel.state_briefing()
        briefing_tokens = (_conservative_text_chars(briefing) // 4) + 4 if briefing else 0
        local_now = self._estimate_tokens()
        effective_now = self._effective_prompt_tokens()
        target_budget = budget
        if effective_now > budget and effective_now > local_now and local_now > 0:
            target_budget = min(budget, max(1, int(local_now * (budget / effective_now))))
        if briefing_tokens > 0 and target_budget > briefing_tokens * 2:
            target_budget = max(1, target_budget - briefing_tokens)
        if local_now <= target_budget:
            target_budget = max(1, local_now - 1)
        self.config.context_token_budget = target_budget
        try:
            super().manage_context()
        finally:
            self.config.context_token_budget = budget
        if self.n_context_events > events_before:
            guard = max(2, int(self.config.keep_recent_messages))
            cutoff = max(2, len(self.messages) - guard - 1)
            changed_indices = [
                idx
                for idx in range(2, len(self.messages))
                if (
                    self.messages[idx].get("content"),
                    self.messages[idx].get("reasoning_content"),
                    tuple(
                        c.get("raw_arguments")
                        for c in (self.messages[idx].get("tool_calls") or ())
                        if isinstance(c, Mapping)
                    ),
                )
                != before_snapshots[idx]
            ]
            middle_changed = [idx for idx in changed_indices if idx <= cutoff]
            boundary = max(middle_changed) if middle_changed else cutoff
            if previous_briefing is not None:
                boundary = max(boundary, min(previous_briefing[0], cutoff))
            self._refresh_state_briefing(boundary_index=boundary, briefing=briefing)
            tokens_after = self._estimate_tokens()
            self.context_overflow_tokens = max(0, tokens_after - budget)
            self._kernel._on_compacted(
                estimate_before,
                tokens_after,
                self.n_context_events,
                overflow=int(self.context_overflow_tokens),
            )
        elif previous_briefing is not None:
            prev_idx, prev_base, prev_full = previous_briefing
            if 2 <= prev_idx < len(self.messages):
                self.messages[prev_idx][_BRIEFING_BASE_KEY] = prev_base
                self.messages[prev_idx]["content"] = prev_full
            self.context_overflow_tokens = max(0, self._estimate_tokens() - budget)
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

    def _refresh_state_briefing(
        self,
        *,
        boundary_index: Optional[int] = None,
        briefing: Optional[str] = None,
    ) -> None:
        """Attach the durable state at the compaction boundary (index >= 2).

        ``self.messages[0]`` (system) and ``self.messages[1]`` (user task) are
        never modified, keeping the static prefix cache-safe across compactions.
        """

        if len(self.messages) <= 2:
            return
        text = briefing if briefing is not None else self._kernel.state_briefing()
        if not text:
            return
        prev = self._strip_existing_briefing()
        bound = boundary_index
        if bound is None:
            bound = prev[0] if prev is not None else min(len(self.messages) - 1, 2)
        bound = max(2, min(int(bound), len(self.messages) - 1))
        target_idx: Optional[int] = None
        for idx in range(bound, 1, -1):
            if self.messages[idx].get("role") in {"tool", "user"}:
                target_idx = idx
                break
        if target_idx is None:
            if bound + 1 < len(self.messages) and self.messages[bound + 1].get("role") in {"tool", "user"}:
                target_idx = bound + 1
            else:
                target_idx = bound
        target = self.messages[target_idx]
        raw_base = str(target.get("content") or "")
        target[_BRIEFING_BASE_KEY] = raw_base
        target["content"] = f"{raw_base}\n\n{text}" if raw_base else text

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
        # Nor may a repeat streak from before it: the same read the repair
        # needs first would otherwise count as the fourth identical step and
        # submit the unrepaired work again at once.
        self._same_step_streak = 0
        self._last_action_key = ""
        self._recent_step_keys = []
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
                # Deliberation that ran out of output budget is not a format failure: up to 8 cuts are retried (HARD12 tb1).
                self._length_cuts = getattr(self, "_length_cuts", 0) + ("output token limit" in str(error.messages[0].get("content", "")))
                self.n_consecutive_format_errors += 0 if "output token limit" in str(error.messages[0].get("content", "")) and self._length_cuts <= 8 else 1
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
