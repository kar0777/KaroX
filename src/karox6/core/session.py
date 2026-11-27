"""The agent loop. Every interface drives the agent through ``Session.run()``.

Loop: ask the model; if it called no tools, the run is complete (the model decides
when it is done — there are no post-finish passes); otherwise execute the calls
(read-only calls concurrently), append the results and ask again.

The harness speaks up only on measurable failures: limits (steps, time, cost),
repeated identical calls (3/5/8, hard stop at 20), malformed calls (unknown tool,
invalid arguments, output cut off at the token limit). No hidden checks run here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import time
import uuid
from collections.abc import AsyncGenerator, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from karox6 import __version__
from karox6.context.compaction import (
    COMPACTION_INSTRUCTION,
    checkpoint_message,
    estimate_tokens,
    prune_tool_results,
    split_for_summary,
)
from karox6.context.spill import limit_output
from karox6.core import events as ev
from karox6.core.config import SessionConfig as SessionConfig  # noqa: PLC0414
from karox6.core.loopguard import (
    FINISH_GATE_CAP_NOTICE,
    FINISH_GATE_MAX_STEPS,
    FINISH_GATE_MIN_SECONDS,
    FINISH_GATE_MIN_STEPS,
    LoopGuard,
    ProgressGuard,
    WorkspaceMonitor,
    finish_gate_message,
)
from karox6.core.messages import (
    IMAGE_TOKENS,
    IMAGE_TYPES,
    Message,
    ToolCall,
    Usage,
    image_note,
)
from karox6.core.prompt import build_system_prompt
from karox6.core.review import (
    REVIEW_APPEND,
    REVIEW_MAX_SECONDS,
    REVIEW_MAX_STEPS,
    REVIEW_STEP_NOTICES,
    Finding,
    FindingTool,
    Guard,
    git_diff,
    review_prompt,
    verified,
)
from karox6.core.review import notice as review_notice
from karox6.core.snapshot import write_snapshot
from karox6.core.store import (
    CANCELLED_RESULT,
    SessionStore,
    new_session_id,
)
from karox6.models import ModelInfo, model_info, rejects_images, remember
from karox6.providers.base import (
    UNAVAILABLE_STATUSES,
    Delta,
    ModelRequest,
    ModelResponse,
    Provider,
    ProviderError,
    complete_with_retry,
    is_empty,
)
from karox6.tools.base import (
    Approver,
    Tool,
    ToolArgsError,
    ToolContext,
    ToolResult,
    deny_all,
    parse_arguments,
)
from karox6.tools.files import FileTracker, _git_dirty
from karox6.tools.jobs import JobManager
from karox6.tools.registry import build_tools, tool_specs
from karox6.tools.shell import ShellInfo, detect_shell

GENERIC_TOOL_CAP = 60_000
SUBAGENT_MAX_STEPS = 40
SUBAGENT_MAX_SECONDS = 900.0
TRUNCATION_NOTICE = (
    "Your previous response was cut off at the output token limit before it was complete. "
    "Continue from where it stopped. Keep each response shorter: split large file contents "
    "into several smaller write or edit calls."
)
# The whole output limit went to reasoning: nothing was said and no tool was called. The
# model cannot know that (it only sees its own thoughts end), and "write shorter" misleads
# it: tell it what happened and that the analysis is to be used, not started again. In
# gtrio2 (replies capped at 8192 tokens) such steps held half of KaroX's reasoning tokens.
THINKING_CUT_NOTICE = (
    "Your previous reply was cut off at the output token limit while you were still "
    "thinking: no answer and no tool call reached me. Do not start the analysis over. Use "
    "what you have already worked out, decide on the next single action, and take it now "
    "with a tool call (or give your answer). Keep your thinking for this step brief."
)
# Asked for after a reply that was all reasoning: one step lower than the configured effort.
EASIER_EFFORT = {"max": "high", "xhigh": "high", "high": "medium", "medium": "low"}
# An empty reply (reasoning only, no text and no tool call) repeats when the same request
# is sent again: some providers cache identical requests and replay the empty answer
# (gtrio2: 11 of 24 KaroX runs died this way on Dahl while the retries came back in ~1 s
# with the same tokens). The notice changes the request and tells the model what is missing.
EMPTY_REPLY_NOTICE = (
    "Your previous reply was empty: no text and no tool call reached me. Continue the task "
    "now: call the next tool you need, or give your final answer if you are done."
)
MAX_EMPTY_REPLIES = 3  # per run; after that the run ends with the provider's error
MAX_SERVER_SHRINKS = 2  # per run: requests cut down after a server error on every retry
FINAL_CALL_STEPS = 30  # after the wrap-up notice, ask for the final answer
WRAP_UP_NOTICE = (
    "This run has taken {steps} steps, and every further step re-sends the whole "
    "conversation. Wrap up: keep the changes that work, run the most relevant tests once, "
    "and reply with your final summary. Continue past that only for a concrete fix you "
    "can name."
)
DEADLINE_NOTICE = (
    "Time check: about {left} minutes remain of this run's {total}-minute time limit. This "
    "is not a signal to stop or to summarize: keep working on the task. Only if an output "
    "file the task requires does not exist yet, write your best current result to it now "
    "(the run is cut off at the limit, and a partial result beats none)."
)
FINAL_NOTICE = (
    "This run has taken {steps} steps. Stop now: reply with your final summary (what you "
    "changed, what is verified, what remains) without calling more tools."
)


class Session:
    def __init__(
        self,
        config: SessionConfig,
        provider: Provider,
        *,
        approver: Approver = deny_all,
        shell: ShellInfo | None = None,
        tools: list[Tool] | None = None,
        extra_tools: list[Tool] | None = None,
        session_id: str | None = None,
        messages: list[Message] | None = None,
        usage: Usage | None = None,
        cost_usd: float = 0.0,
    ) -> None:
        self.config = config
        self.provider = provider
        self.workspace = config.workspace.resolve()
        self.shell = shell or detect_shell()
        self.tools = [
            *(
                tools
                if tools is not None
                else build_tools(self.shell, apply_patch=config.apply_patch)
            ),
            *(extra_tools or []),
        ]
        if config.subagents and tools is None:
            from karox6.tools.agent import AgentTool

            self.tools.append(AgentTool(self._spawn_subagent))
        if config.code_mode and tools is None:
            from karox6.tools.code import RunCodeTool

            self.tools.append(RunCodeTool(lambda: self._tool_map))
        self._subagents = 0
        self._tool_map = {t.name: t for t in self.tools}
        self.session_id = session_id or new_session_id()
        self.store = SessionStore(config.home, self.session_id)
        self.messages: list[Message] = list(messages or [])
        self.resumed = bool(messages)
        self.usage = usage or Usage()
        self.cost_usd = cost_usd
        self.files = FileTracker(self.workspace, ignore=_inside(config.home, self.workspace))
        self.jobs = JobManager()
        self.guard = LoopGuard(hard_stop=config.loop_hard_stop)
        self.approver = approver
        append = config.system_append
        if "run_code" in self._tool_map:
            from karox6.tools.code import CODE_MODE_APPEND

            append = "\n\n".join(p for p in (append, CODE_MODE_APPEND) if p)
        self.system_prompt = build_system_prompt(
            self.workspace, self.shell, base=config.system_prompt, append=append
        )
        self._last_input_tokens = 0
        self._last_cached_tokens = 0  # of the last request that measured the context
        self._chars_since_measure = 0
        self._effort: str | None = None  # eased reasoning effort after an all-thinking cut
        self._deadline: float | None = None  # monotonic end of the current run
        self._learned_window: int | None = None  # from a context-window error
        self._model_facts: tuple[str, ModelInfo] | None = None  # (model, what is known)
        if not self.resumed:
            self.store.append(
                "meta",
                session_id=self.session_id,
                version=__version__,
                model=config.model,
                provider=config.provider_name,
                workspace=str(self.workspace),
                shell=self.shell.flavor,
            )

    # -- public -------------------------------------------------------------------

    async def run(
        self, prompt: str, images: list[dict[str, str]] | None = None
    ) -> AsyncGenerator[ev.Event, None]:
        """Run one user turn; yields events until (and including) ``RunFinished``.
        ``images`` are pictures attached to the prompt (``core.messages.Message.images``).

        Each ``run()`` call is one user task (including in the REPL): when ``finish_gate``
        is enabled, it fires at most once per ``run()`` call and never re-arms during that
        run (even after compaction)."""
        queue: asyncio.Queue[ev.Event | None] = asyncio.Queue()

        def emit(event: ev.Event) -> None:
            queue.put_nowait(event)

        async def produce() -> None:
            try:
                await self._drive(prompt, emit, images or [])
            finally:
                queue.put_nowait(None)

        task = asyncio.create_task(produce())
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield event
            await task
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    # -- loop ---------------------------------------------------------------------

    async def _drive(
        self, prompt: str, emit: Callable[[ev.Event], None], images: list[dict[str, str]]
    ) -> None:
        started = time.monotonic()
        self._deadline = (
            started + self.config.max_seconds if self.config.max_seconds is not None else None
        )
        ctx = ToolContext(
            workspace=self.workspace,
            session_dir=self.store.dir,
            files=self.files,
            jobs=self.jobs,
            shell=self.shell,
            approve=self._make_approver(emit),
            emit=emit,
            deadline=self._deadline,
            copy_notes=self.config.copy_notes,
            command_wait_cap_s=self.config.command_wait_cap_s,
        )
        emit(
            ev.RunStarted(
                session_id=self.session_id,
                model=self.config.model,
                provider=self.config.provider_name,
                workspace=str(self.workspace),
                version=__version__,
                resumed=self.resumed,
            )
        )
        self.guard.reset()
        self._close_dangling_calls()
        self._append(Message("user", prompt, images=list(images)))
        steps = 0
        truncations = 0
        empties = 0  # empty replies answered with EMPTY_REPLY_NOTICE
        shrinks = 0  # requests made smaller after a server error that kept coming back
        reminded = 0  # wrap-up notices sent (a truncated step may skip the exact count)
        warned = False  # deadline notice sent
        reviewed = False  # the independent review ran (once per run)
        # Finish gate fires at most once per run() call (per user task) and never re-arms
        # within a run, even if compaction summarizes away the notice.
        gate_fired = False
        gate_cap: int | None = None  # max step after finish_gate fires
        gate_cap_notified = False
        gate_text = ""  # pre-gate answer preserved across every exit path
        start_output_tokens = self.usage.output_tokens
        start_usage = self.usage.to_dict()
        start_cost = self.cost_usd
        start_files: dict[str, tuple[int, int] | None] = {}
        with contextlib.suppress(Exception):
            start_files = await asyncio.to_thread(self._file_stamps, None)
        need_monitor = self.config.progress_guard or (
            self.config.finish_gate and not self.config.review and not self._is_read_only()
        )
        monitor: WorkspaceMonitor | None = None
        progress_guard: ProgressGuard | None = None
        if need_monitor:
            monitor = WorkspaceMonitor(self.workspace, self.files.ignore, self.files.changed_files)
            start_diff_hash = await asyncio.to_thread(monitor.initialize)
            if self.config.progress_guard and not monitor.disabled:
                progress_guard = ProgressGuard()
                progress_guard.reset(start_diff_hash, self.files.read_count, 0)
        status, reason, final_text = "completed", "", ""
        try:
            while True:
                limit = self._limit_reached(steps, started)
                if limit:
                    status, reason = "limit", limit
                    break
                if gate_cap is not None and steps >= gate_cap:
                    reason = "finish_gate cap"
                    break
                await self._maybe_compact(emit)
                cap_final = gate_cap is not None and steps == gate_cap - 1
                if cap_final and not gate_cap_notified:
                    self._notice("finish_gate_cap", FINISH_GATE_CAP_NOTICE, emit)
                    gate_cap_notified = True
                steps += 1
                try:
                    response = await self._call_model(
                        steps, started, emit, allow_tools=not cap_final
                    )
                except ProviderError as exc:
                    if exc.code == "empty_response" and empties < MAX_EMPTY_REPLIES:
                        empties += 1
                        self._notice("empty", EMPTY_REPLY_NOTICE, emit)
                        continue
                    # The conversation outgrew what the provider accepts (our estimate was
                    # low, or the route's real window is smaller than configured): shrink
                    # it once and retry instead of losing the run.
                    if self._images_refused(exc, emit):
                        recovered = True  # the same request again, pictures as notes
                    elif exc.code == "context_window":
                        recovered = await self._recover_overflow(emit)
                    else:
                        recovered = shrinks < MAX_SERVER_SHRINKS and self._shrink_for_server(
                            exc, emit
                        )
                        shrinks += recovered
                    if not recovered:
                        raise
                    response = await self._call_model(
                        steps, started, emit, allow_tools=not cap_final
                    )
                if response is None:
                    status, reason = "limit", f"time limit {self.config.max_seconds:.0f}s"
                    break
                message = response.message
                if not (response.finish_reason == "content_filter" and is_empty(message)):
                    # An empty refused reply is not kept: replaying an assistant turn with
                    # no content is itself rejected by the API on the next request.
                    self._append(message)
                emit(
                    ev.AssistantMessage(
                        step=steps,
                        text=message.content,
                        tool_calls=[c.to_dict() for c in message.tool_calls],
                        finish_reason=response.finish_reason,
                    )
                )
                if cap_final:
                    if response.finish_reason == "content_filter":
                        status, reason = "error", "the provider stopped the reply (content_filter)"
                    else:
                        final_text = message.content if not message.tool_calls else ""
                        reason = "finish_gate cap"
                    break
                if response.finish_reason == "length":
                    truncations += 1
                    if truncations > 3:
                        status, reason = "error", "output repeatedly cut off at the token limit"
                        break
                    if message.tool_calls:
                        # Calls cut mid-arguments are answered as errors below; tell the
                        # model why so it does not resend the same oversized call.
                        await self._run_calls(message.tool_calls, ctx, emit)
                    if not message.content.strip() and not message.tool_calls:
                        self._ease_effort(emit)
                        self._notice("truncated", THINKING_CUT_NOTICE, emit)
                    else:
                        self._notice("truncated", TRUNCATION_NOTICE, emit)
                    continue
                truncations = 0  # the limit is on consecutive cut-off replies
                self._effort = None  # a reply came through: back to the configured effort
                if not message.tool_calls:
                    final_text = message.content
                    if response.finish_reason == "content_filter":
                        # The provider cut the reply; it is not the model's final answer.
                        status, reason = "error", "the provider stopped the reply (content_filter)"
                        break
                    if self.config.review and not reviewed:
                        reviewed = True
                        found = await self._review(started, ctx, emit)
                        if found:
                            self._notice("review", review_notice(found), emit)
                            final_text = ""
                            continue
                    if (
                        self.config.finish_gate
                        and not self.config.review
                        and not gate_fired
                        and gate_cap is None
                        and monitor is not None
                        and not self._is_read_only()
                        and self._has_finish_gate_budget(steps, started)
                    ):
                        changes = await asyncio.to_thread(monitor.changes_since_start)
                        if changes is not None:
                            gate_fired = True
                            gate_text = message.content
                            gate_cap = steps + FINISH_GATE_MAX_STEPS
                            self.store.append(
                                "finish_gate",
                                fired=True,
                                step=steps,
                                files=changes.changed,
                            )
                            self._notice(
                                "finish_gate",
                                finish_gate_message(changes.changed, changes.modified_tests),
                                emit,
                            )
                            final_text = ""
                            continue
                    break
                stop = await self._run_calls(message.tool_calls, ctx, emit)
                if stop:
                    status, reason = "loop", stop
                    break
                if progress_guard is not None and monitor is not None and gate_cap is None:
                    diff_hash = await asyncio.to_thread(monitor.diff_hash)
                    if monitor.disabled:
                        progress_guard = None
                    else:
                        from karox6.tools.shell import ShellTool

                        for call in message.tool_calls:
                            if isinstance(self._lookup(call.name), ShellTool):
                                command = _command_text(call.arguments)
                                if command:
                                    progress_guard.note_command(command)
                        progress_notice = progress_guard.observe(
                            steps,
                            diff_hash,
                            self.files.read_count,
                            self.usage.output_tokens - start_output_tokens,
                        )
                        if progress_notice is not None:
                            self._notice("progress_guard", progress_notice, emit)
                if steps in self.config.step_notices:
                    self._notice("step", self.config.step_notices[steps], emit)
                if not warned and self._deadline_near(started):
                    self._notice("deadline", self._deadline_text(started), emit)
                    warned = True
                wrap = self.config.wrap_up_steps
                if wrap and reminded == 0 and steps >= wrap:
                    self._notice("wrap_up", WRAP_UP_NOTICE.format(steps=steps), emit)
                    reminded = 1
                elif wrap and reminded == 1 and steps >= wrap + FINAL_CALL_STEPS:
                    self._notice("wrap_up", FINAL_NOTICE.format(steps=steps), emit)
                    reminded = 2
        except ProviderError as exc:
            status, reason = "error", str(exc)
            emit(ev.ErrorEvent(code=exc.code, message=str(exc)))
        except asyncio.CancelledError:
            status, reason = "cancelled", "cancelled"
            raise
        except Exception as exc:  # a bug in KaroX must still end the run cleanly
            status, reason = "error", f"internal error: {type(exc).__name__}: {exc}"
            emit(ev.ErrorEvent(code="internal", message=reason))
        finally:
            with contextlib.suppress(Exception):
                await self.jobs.shutdown()
            files_changed: list[str] = []
            with contextlib.suppress(Exception):
                files_changed = await asyncio.to_thread(self.files.changed_files)
            if self.config.snapshot_on_finish:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(
                        write_snapshot,
                        self.store.dir,
                        self.workspace,
                        files_changed,
                        step=steps,
                        status=status,
                    )
            final_text = final_text or gate_text
            run_files: list[str] | None = None
            with contextlib.suppress(Exception):
                now_files = await asyncio.to_thread(self._file_stamps, files_changed)
                run_files = [
                    p for p, stamp in now_files.items() if start_files.get(p, ...) != stamp
                ]
            now_usage = self.usage.to_dict()
            finished = ev.RunFinished(
                status=status,
                reason=reason,
                final_text=final_text,
                steps=steps,
                elapsed_s=round(time.monotonic() - started, 2),
                usage=self.usage.to_dict(),
                cost_usd=round(self.cost_usd, 6) if self.config.price else None,
                files_changed=files_changed,
                session_id=self.session_id,
                run_usage={k: v - start_usage.get(k, 0) for k, v in now_usage.items()},
                run_cost_usd=(round(self.cost_usd - start_cost, 6) if self.config.price else None),
                run_files=run_files,
            )
            with contextlib.suppress(Exception):
                self.store.append("event", event=finished.to_dict())
            emit(finished)

    def _file_stamps(self, paths: list[str] | None) -> dict[str, tuple[int, int] | None]:
        """(mtime, size) of each changed file (``None``: gone), to tell which of them
        this run changed: the session's list grows over every run."""
        if paths is None:
            paths = self.files.changed_files()
        out: dict[str, tuple[int, int] | None] = {}
        for raw in paths:
            path = Path(raw) if Path(raw).is_absolute() else self.workspace / raw
            try:
                st = path.stat()
                out[raw] = (st.st_mtime_ns, st.st_size)
            except OSError:
                out[raw] = None
        return out

    def _is_read_only(self) -> bool:
        return bool(self.tools) and all(t.read_only for t in self.tools)

    def _has_finish_gate_budget(self, steps: int, started: float) -> bool:
        """True when at least 2 steps and 90 s remain for the post-finish check."""
        cfg = self.config
        if cfg.max_steps is not None and cfg.max_steps - steps < FINISH_GATE_MIN_STEPS:
            return False
        return not (
            cfg.max_seconds is not None
            and cfg.max_seconds - (time.monotonic() - started) < FINISH_GATE_MIN_SECONDS
        )

    def _deadline_near(self, started: float) -> bool:
        cfg = self.config
        if not cfg.deadline_notice_s or cfg.max_seconds is None:
            return False
        return cfg.max_seconds - (time.monotonic() - started) <= cfg.deadline_notice_s

    def _deadline_text(self, started: float) -> str:
        total = self.config.max_seconds or 0.0
        left = max(total - (time.monotonic() - started), 0.0)
        return DEADLINE_NOTICE.format(left=max(round(left / 60), 1), total=round(total / 60))

    def _limit_reached(self, steps: int, started: float) -> str | None:
        cfg = self.config
        if cfg.max_steps is not None and steps >= cfg.max_steps:
            return f"step limit {cfg.max_steps}"
        if cfg.max_seconds is not None and time.monotonic() - started >= cfg.max_seconds:
            return f"time limit {cfg.max_seconds:.0f}s"
        if cfg.max_cost_usd is not None and cfg.price and self.cost_usd >= cfg.max_cost_usd:
            return f"cost limit ${cfg.max_cost_usd:.2f}"
        return None

    async def _call_model(
        self,
        step: int,
        started: float,
        emit: Callable[[ev.Event], None],
        *,
        allow_tools: bool = True,
    ) -> ModelResponse | None:
        request = self._request(allow_tools=allow_tools)
        timing = _CallTiming()

        def on_delta(delta: Delta) -> None:
            timing.delta()
            if self.config.stream_deltas:
                emit(
                    ev.TextDelta(delta.text)
                    if delta.kind == "text"
                    else ev.ReasoningDelta(delta.text)
                )

        def on_retry(attempt: int, delay: float, exc: ProviderError) -> None:
            timing.retry(delay)
            emit(ev.Retry(attempt=attempt, delay_s=round(delay, 2), error=str(exc)))

        call = complete_with_retry(
            self.provider, request, on_delta, self.config.retry, on_retry=on_retry
        )
        if self.config.max_seconds is None:
            response = await call
        else:
            remaining = self.config.max_seconds - (time.monotonic() - started)
            try:
                response = await asyncio.wait_for(call, max(remaining, 1.0))
            except TimeoutError:
                return None
        self._account(step, response, emit, timing=timing)
        return response

    def _account(
        self,
        step: int,
        response: ModelResponse,
        emit: Callable[[ev.Event], None],
        *,
        measures_context: bool = True,
        timing: _CallTiming | None = None,
    ) -> None:
        usage = response.usage
        times = timing.done() if timing is not None else {}
        usage.requests = max(usage.requests, 1)
        self.usage.add(usage)
        cost = self.config.price.cost(usage) if self.config.price else None
        if cost is not None:
            self.cost_usd += cost
        if measures_context:
            # A summary call sends only part of the history: its input size is not the
            # conversation's. The reply itself is counted when it is appended.
            self._last_input_tokens = usage.input_tokens
            self._last_cached_tokens = usage.cached_tokens
            self._chars_since_measure = 0
        self.store.append(
            "usage",
            step=step,
            usage=usage.to_dict(),
            cost_usd=cost,
            finish_reason=response.finish_reason,
            **times,
        )
        emit(
            ev.UsageUpdate(
                step=step,
                usage=usage.to_dict(),
                total=self.usage.to_dict(),
                cost_usd=round(cost, 6) if cost is not None else None,
                total_cost_usd=round(self.cost_usd, 6) if self.config.price else None,
                finish_reason=response.finish_reason or "",
                **times,
            )
        )

    def _request(self, *, allow_tools: bool = True) -> ModelRequest:
        messages = self._replay_view(self.messages)
        vision = self.model().vision
        if vision is False:
            messages = _without_images(messages)
        # Pictures from tool results go to every model not known to refuse them: a
        # provider that refuses is remembered (_images_refused) and the request goes
        # again without them, while withholding them blinds a model that sees.
        messages = _tool_pictures(messages, show=vision is not False)
        return ModelRequest(
            model=self.config.model,
            system=self.system_prompt,
            messages=messages,
            tools=tool_specs(self.tools) if allow_tools else [],
            max_output_tokens=self.config.max_output_tokens,
            temperature=self.config.temperature,
            reasoning_effort=self._effort or self.config.reasoning_effort,
            extra_body=dict(self.config.extra_body),
        )

    def _ease_effort(self, emit: Callable[[ev.Event], None]) -> None:
        """After a reply that spent the whole output limit on reasoning: ask for one step
        less effort until a reply comes through. Only when an effort is configured (the
        provider then accepts the field); a gateway that pins the effort ignores it."""
        easier = EASIER_EFFORT.get(self._effort or self.config.reasoning_effort or "")
        if easier is not None:
            self._effort = easier
            emit(ev.Notice(kind="effort", text=f"reasoning effort {easier} until a reply fits"))

    def _replay_view(self, messages: list[Message]) -> list[Message]:
        """Drop reasoning the provider should not see again (``replay_reasoning`` policy)."""
        mode = self.config.replay_reasoning
        if mode == "all":
            return messages
        boundary = len(messages) if mode == "none" else 0
        if mode == "turn":
            for i in range(len(messages) - 1, -1, -1):
                m = messages[i]
                if m.role == "user" and not m.meta.get("kind"):
                    boundary = i
                    break
        return [
            replace(m, reasoning=None)
            if m.role == "assistant" and m.reasoning and i < boundary
            else m
            for i, m in enumerate(messages)
        ]

    # -- tools --------------------------------------------------------------------

    async def _run_calls(
        self, calls: list[ToolCall], ctx: ToolContext, emit: Callable[[ev.Event], None]
    ) -> str | None:
        """Execute one assistant message's calls; append results. Returns a stop reason or None."""
        results: list[Message | None] = [None] * len(calls)
        index = 0
        sem = asyncio.Semaphore(max(self.config.max_parallel, 1))

        async def one(i: int) -> None:
            async with sem:
                results[i] = await self._run_one(calls[i], ctx, emit)

        try:
            while index < len(calls):
                tool = self._lookup(calls[index].name)
                if tool is not None and tool.read_only:
                    batch = [index]
                    while index + len(batch) < len(calls):
                        nxt = self._lookup(calls[index + len(batch)].name)
                        if nxt is None or not nxt.read_only:
                            break
                        batch.append(index + len(batch))
                    tasks = [asyncio.create_task(one(i)) for i in batch]
                    try:
                        await asyncio.gather(*tasks)
                    except BaseException:
                        # When one tool aborts an approval, gather does not cancel its
                        # siblings itself. Wait for all tool cleanup before returning.
                        for task in tasks:
                            if not task.done() and not task.cancelling():
                                task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        raise
                    index += len(batch)
                else:
                    await one(index)
                    index += 1
        except BaseException:
            # Cancelled (or a bug) mid-batch: keep the results of the calls that finished,
            # so the history - and a resumed session - shows what was already done.
            for result in results:
                if result is not None:
                    self._append(result)
            raise

        stop: str | None = None
        for call, result in zip(calls, results, strict=True):
            assert result is not None
            if self.guard.observe(call.name, call.arguments) and stop is None:
                stop = (
                    f"the same {call.name} call was repeated {self.config.loop_hard_stop} times "
                    "in a row"
                )
        notes = self.jobs.drain_finished()
        last = results[-1]
        if notes and last is not None:
            results[-1] = replace(last, content=last.content + "\n\n" + "\n".join(notes))
        for result in results:
            assert result is not None
            self._append(result)
        for text in self.guard.take_reminders():
            self._notice("loop_guard", text, emit)
        return stop

    def _lookup(self, name: str) -> Tool | None:
        tool = self._tool_map.get(name)
        if tool is not None:
            return tool
        norm = name.strip().lower().removeprefix("functions.")
        for key, value in self._tool_map.items():
            if key.lower() == norm:
                return value
        return None

    async def _run_one(
        self, call: ToolCall, ctx: ToolContext, emit: Callable[[ev.Event], None]
    ) -> Message:
        emit(ev.ToolStarted(call_id=call.id, name=call.name, arguments=call.arguments))
        started = time.monotonic()
        tool = self._lookup(call.name)
        meta: dict[str, Any] = {}
        pictures: list[Path] = []
        if tool is None:
            names = ", ".join(self._tool_map)
            content, is_error = (
                f"Error: unknown tool {call.name!r}. Available tools: {names}.",
                True,
            )
        else:
            try:
                args = parse_arguments(call.arguments, tool.parameters)
            except ToolArgsError as exc:
                content, is_error = (
                    f"Error: {exc}. Send the call again with corrected arguments.",
                    True,
                )
            else:
                try:
                    result = await tool.run(args, ctx)
                    content, is_error, meta = result.content, result.is_error, result.meta
                    pictures = list(result.images)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    content, is_error = (
                        f"Error: {tool.name} failed: {type(exc).__name__}: {exc}",
                        True,
                    )
                if tool.name != "read":
                    content, spill = limit_output(
                        content, spill_dir=ctx.spill_dir, name=tool.name, max_chars=GENERIC_TOOL_CAP
                    )
                    if spill is not None:
                        meta = {**meta, "spill_path": str(spill)}
        emit(
            ev.ToolFinished(
                call_id=call.id,
                name=call.name,
                is_error=is_error,
                output=content,
                duration_ms=int((time.monotonic() - started) * 1000),
                meta=meta,
            )
        )
        return Message(
            "tool",
            content,
            tool_call_id=call.id,
            name=tool.name if tool is not None else call.name,
            is_error=is_error,
            images=self._keep_pictures(pictures, meta),
        )

    def _keep_pictures(self, pictures: list[Path], meta: dict[str, Any]) -> list[dict[str, str]]:
        """Copy a tool result's pictures into the session (later requests send the same
        bytes whatever happens to the originals); a screenshot arrives as PNG bytes."""
        shot = meta.pop("image_png", None)
        if not pictures and not isinstance(shot, bytes | bytearray):
            return []
        folder = self.store.dir / "images"
        kept: list[dict[str, str]] = []
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if isinstance(shot, bytes | bytearray):
                target = folder / f"tool-{uuid.uuid4().hex[:12]}.png"
                target.write_bytes(bytes(shot))
                kept.append({"path": str(target), "media_type": "image/png"})
            for source in pictures[:MAX_TOOL_PICTURES]:
                suffix = source.suffix.lower()
                if suffix not in IMAGE_TYPES:
                    continue
                target = folder / f"tool-{uuid.uuid4().hex[:12]}{suffix}"
                shutil.copyfile(source, target)
                kept.append({"path": str(target), "media_type": IMAGE_TYPES[suffix]})
        except OSError:
            pass  # the text result still says what the picture was
        return kept

    def _make_approver(self, emit: Callable[[ev.Event], None]) -> Approver:
        counter = 0

        async def approve(request: Any) -> bool:
            nonlocal counter
            counter += 1
            request_id = f"approval-{counter}"
            emit(
                ev.ApprovalRequested(
                    request_id=request_id,
                    tool=request.tool,
                    summary=request.summary,
                    rule=request.rule,
                    reason=request.reason,
                )
            )
            approved = await self.approver(request)
            emit(ev.ApprovalResolved(request_id=request_id, approved=approved, by="approver"))
            return approved

        return approve

    # -- context ------------------------------------------------------------------

    async def _spawn_subagent(self, prompt: str, label: str, ctx: ToolContext) -> ToolResult:
        """Run a child session on ``prompt``; return its final report as a tool result."""
        from karox6.tools.agent import SUBAGENT_APPEND
        from karox6.tools.fs import ReadTool
        from karox6.tools.search import GlobTool, GrepTool
        from karox6.tools.shell import ShellTool

        self._subagents += 1
        number = self._subagents
        cfg = self.config
        config = replace(
            cfg,
            subagents=False,
            finish_gate=False,
            progress_guard=False,
            snapshot_on_finish=False,
            stream_deltas=False,
            max_steps=min(cfg.max_steps or SUBAGENT_MAX_STEPS, SUBAGENT_MAX_STEPS),
            max_seconds=self._subagent_seconds(),
            max_cost_usd=None,
            system_append="\n\n".join(p for p in (cfg.system_append, SUBAGENT_APPEND) if p),
        )
        child = Session(
            config,
            self.provider,
            shell=self.shell,
            tools=[ShellTool(self.shell), ReadTool(), GrepTool(), GlobTool()],
            session_id=f"{self.session_id}.agent-{number}",
        )
        tag = f"agent-{number}"
        final, status, reason, steps = "", "error", "", 0
        async for event in child.run(prompt):
            if isinstance(event, ev.ToolStarted | ev.ToolFinished):
                ctx.emit(
                    replace(event, call_id=f"{tag}:{event.call_id}", name=f"{tag}/{event.name}")
                )
            elif isinstance(event, ev.Retry):
                ctx.emit(event)
            elif isinstance(event, ev.RunFinished):
                final, status, reason, steps = (
                    event.final_text,
                    event.status,
                    event.reason,
                    event.steps,
                )
        self.usage.add(child.usage)
        self.cost_usd += child.cost_usd
        with contextlib.suppress(Exception):
            self.store.append(
                "usage", step=0, usage=child.usage.to_dict(), cost_usd=child.cost_usd, source=tag
            )
        if not final:
            final = next(
                (
                    m.content
                    for m in reversed(child.messages)
                    if m.role == "assistant" and m.content
                ),
                "",
            )
        head = "" if status == "completed" else f"[subagent stopped: {reason or status}]\n"
        meta = {"subagent": child.session_id, "label": label, "status": status, "steps": steps}
        return ToolResult(
            head + (final or "(the subagent produced no report)"),
            is_error=not final,
            meta=meta,
        )

    async def _review(
        self, started: float, ctx: ToolContext, emit: Callable[[ev.Event], None]
    ) -> list[Finding]:
        """Run the independent review (core/review.py); the reproduced findings."""
        from karox6.tools.fs import ReadTool
        from karox6.tools.search import GlobTool, GrepTool
        from karox6.tools.shell import ShellTool

        files = await asyncio.to_thread(self.files.changed_files)
        seconds = REVIEW_MAX_SECONDS
        if self.config.max_seconds is not None:
            left = self.config.max_seconds - (time.monotonic() - started)
            if left < 300:
                return []  # no time to review and then fix
            seconds = min(seconds, left * 0.4)
        if not files:
            return []
        dirty = await asyncio.to_thread(_git_dirty, self.workspace)
        guard = Guard(self.workspace)
        await asyncio.to_thread(guard.save, files, dirty)
        diff = await asyncio.to_thread(git_diff, self.workspace, files)
        prompt = review_prompt([m.content for m in _user_turns(self.messages)], diff, files)
        reporter = FindingTool()
        cfg = self.config
        config = replace(
            cfg,
            subagents=False,
            review=False,
            finish_gate=False,
            progress_guard=False,
            snapshot_on_finish=False,
            stream_deltas=False,
            max_steps=REVIEW_MAX_STEPS,
            step_notices=REVIEW_STEP_NOTICES,
            max_seconds=seconds,
            max_cost_usd=None,
            system_append="\n\n".join(p for p in (cfg.system_append, REVIEW_APPEND) if p),
        )
        child = Session(
            config,
            self.provider,
            shell=self.shell,
            tools=[ShellTool(self.shell), ReadTool(), GrepTool(), GlobTool(), reporter],
            session_id=f"{self.session_id}.review",
        )
        emit(ev.Notice(kind="review", text=f"independent review of {len(files)} changed files"))
        try:
            async for event in child.run(prompt):
                if isinstance(event, ev.ToolStarted | ev.ToolFinished):
                    tagged = f"review:{event.call_id}"
                    emit(replace(event, call_id=tagged, name=f"review/{event.name}"))
                elif isinstance(event, ev.Retry):
                    emit(event)
        finally:
            restored = await asyncio.to_thread(
                guard.restore, await asyncio.to_thread(_git_dirty, self.workspace)
            )
            self.usage.add(child.usage)
            self.cost_usd += child.cost_usd
            with contextlib.suppress(Exception):
                self.store.append(
                    "usage",
                    step=0,
                    usage=child.usage.to_dict(),
                    cost_usd=child.cost_usd,
                    source="review",
                )
        found = verified(reporter.findings, _shell_runs(child.messages))
        note = f"review: {len(reporter.findings)} reported, {len(found)} reproduced"
        if restored:
            note += f"; put back {len(restored)} file(s) the review touched"
        emit(ev.Notice(kind="review", text=note))
        return found

    def take_history(
        self, messages: list[Message], usage: Usage, cost_usd: float, source: str
    ) -> None:
        """Start this (new, empty) session as a copy of another conversation: ``/fork`` and
        ``--fork``. The copy is written to this session's own log, so it resumes on its
        own; the source session stays as it was."""
        self.messages = [Message.from_dict(m.to_dict()) for m in messages]
        self.resumed = bool(self.messages)
        self.usage = Usage.from_dict(usage.to_dict())
        self.cost_usd = cost_usd
        self.store.append("fork", source=source)
        self.store.reset(self.messages)
        self.store.append(
            "usage", step=0, usage=self.usage.to_dict(), cost_usd=cost_usd, source="fork"
        )
        self._last_input_tokens = self._last_cached_tokens = 0
        self._chars_since_measure = 0

    def note_for_model(self, kind: str, text: str) -> None:
        """Add a harness note to the conversation between turns (the model reads it with
        the next request; it is not a user turn)."""
        self._append(Message("user", text, meta={"kind": kind}))

    def rewind(self, count: int) -> list[Message]:
        """Drop the conversation after its first ``count`` messages (``/undo``); the
        dropped messages are returned so that a redo can put them back."""
        count = max(min(count, len(self.messages)), 0)
        dropped = self.messages[count:]
        if dropped:
            self.messages = self.messages[:count]
            self.store.reset(self.messages)
            self._last_input_tokens = self._last_cached_tokens = 0
            self._chars_since_measure = 0
        return dropped

    def extend(self, messages: list[Message]) -> None:
        """Put messages back at the end of the conversation (the redo of ``rewind``)."""
        if messages:
            self.messages = [*self.messages, *messages]
            self.store.reset(self.messages)
            self._last_input_tokens = self._last_cached_tokens = 0
            self._chars_since_measure = 0

    def _images_refused(self, exc: ProviderError, emit: Callable[[ev.Event], None]) -> bool:
        """The provider said the model takes no pictures: remember it (for this model,
        in the KaroX home) and send without them. Once: a second refusal is an error."""
        if self.model().vision is False or not rejects_images(exc.code, str(exc.args[0])):
            return False
        if not any(m.images for m in self.messages):
            return False
        remember(self.config.home, self.config.model, vision=False)
        self._model_facts = (
            self.config.model,
            replace(self.model(), vision=False, source="learned"),
        )
        emit(
            ev.Notice(
                kind="no_images",
                text=f"{self.config.model} does not take pictures: sent without them "
                "(remembered; models.json in the KaroX home changes it)",
            )
        )
        return True

    def reread_model_facts(self) -> None:
        """models.json changed (a provider's model list was read): look the model up again."""
        self._model_facts = None

    def forget_model_state(self, *, provider_changed: bool) -> None:
        """After /model or /setup: what the session learned about the old model (its
        smaller context window, the size of the last request) does not apply to the new
        one; after a provider switch, provider-native data in the history (thinking
        signatures, encrypted reasoning items, the reasoning field's name) is dropped,
        since another API rejects or misreads it. The conversation itself stays."""
        self._learned_window = None
        self._model_facts = None
        self._last_input_tokens = 0
        self._last_cached_tokens = 0
        self._effort = None
        if provider_changed:
            for message in self.messages:
                message.extra = {}
                for call in message.tool_calls:
                    call.extra = {}

    def _subagent_seconds(self) -> float:
        """A subagent's time limit: its own cap, and never past the parent's deadline."""
        limit = float(min(self.config.max_seconds or SUBAGENT_MAX_SECONDS, SUBAGENT_MAX_SECONDS))
        if self._deadline is not None:
            limit = min(limit, max(self._deadline - time.monotonic(), 1.0))
        return limit

    def _close_dangling_calls(self) -> None:
        """Answer the tool calls a cancelled turn left open, so the history stays valid."""
        last = max((i for i, m in enumerate(self.messages) if m.role == "assistant"), default=-1)
        if last < 0 or not self.messages[last].tool_calls:
            return
        answered = {m.tool_call_id for m in self.messages[last + 1 :] if m.role == "tool"}
        for call in self.messages[last].tool_calls:
            if call.id not in answered:
                self._append(
                    Message(
                        "tool",
                        CANCELLED_RESULT,
                        tool_call_id=call.id,
                        name=call.name,
                        is_error=True,
                    )
                )

    def _append(self, message: Message) -> None:
        self.messages.append(message)
        self._chars_since_measure += (
            len(message.content)
            + len(message.reasoning or "")
            + sum(len(c.arguments) for c in message.tool_calls)
            + int(IMAGE_TOKENS * 3.5) * len(message.images)
        )
        self.store.message(message)

    def _notice(self, kind: str, text: str, emit: Callable[[ev.Event], None]) -> None:
        self._append(Message("user", text, meta={"kind": kind}))
        emit(ev.Notice(kind=kind, text=text))

    async def compact(self, emit: Callable[[ev.Event], None] | None = None) -> list[ev.Event]:
        """Compact the conversation history on demand (the same steps as the automatic
        compaction: a model-written checkpoint, old tool output cut, the head dropped)."""
        events: list[ev.Event] = []

        def record(event: ev.Event) -> None:
            events.append(event)
            if emit is not None:
                emit(event)

        if len(self.messages) < 3:
            return events
        self._close_dangling_calls()
        before = self.context_tokens()
        cached = self._cache_active()
        if not cached:
            before = self._prune_for_compaction(before, record)
        pruned = bool(events)
        msg_tokens = estimate_tokens(self.messages)
        retain = min(int(self._threshold() * 0.2), max(msg_tokens // 4, 1))
        # An explicit compaction has its own time budget after the previous turn ended.
        previous_deadline = self._deadline
        self._deadline = (
            time.monotonic() + self.config.max_seconds
            if self.config.max_seconds is not None
            else None
        )
        try:
            summarized = await self._summarize(before, record, retain_tokens=retain)
            out_of_time = self._deadline is not None and time.monotonic() >= self._deadline
            if not summarized and cached:
                pruned = self._prune_for_compaction(before, record) < before
            if not summarized and not pruned and not out_of_time:
                await self._drop_head(before, record, retain_tokens=retain)
        finally:
            self._deadline = previous_deadline
        return events

    def context_tokens(self) -> int:
        if self._last_input_tokens:
            return self._last_input_tokens + int(self._chars_since_measure / 3.5)
        return estimate_tokens(self.messages, self.system_prompt)

    def _cache_active(self) -> bool:
        """The provider served most of the last request from its prompt cache."""
        return bool(self._last_input_tokens) and (
            self._last_cached_tokens * 2 >= self._last_input_tokens
        )

    async def _maybe_compact(self, emit: Callable[[ev.Event], None]) -> None:
        threshold = self._threshold()
        before = self.context_tokens()
        if before < threshold:
            return
        # Cutting the middle out of old tool results costs no model call, but it rewrites
        # early messages, so a provider with a prompt cache reads the whole conversation
        # again at the full price (measured: 0-6k of 250-300k tokens cached on the call
        # after a prune) and, in 11 of 16 recorded runs, the summary followed a few steps
        # later anyway. With a live cache the checkpoint comes first: its request reuses
        # the cached conversation, and the context restarts small. Without one (local
        # models) pruning stays the free first step.
        cached = self._cache_active()
        if not cached:
            before = self._prune_for_compaction(before, emit)
            if before < threshold:
                return
        try:
            summarized = await self._summarize(before, emit)
        except ProviderError as exc:
            # Even the summary request is too big for the model: shrink without it.
            if exc.code != "context_window":
                raise
            await self._recover_overflow(emit)
            return
        out_of_time = self._deadline is not None and time.monotonic() >= self._deadline
        if not summarized and cached and not out_of_time:
            before = self._prune_for_compaction(before, emit)
            if before < threshold:
                return
        if not summarized and not out_of_time:
            # No usable checkpoint (e.g. the model spent its output on reasoning twice):
            # shrink without the model, or every next step would pay for two more tries.
            await self._drop_head(before, emit)

    def _shrink_for_server(self, exc: ProviderError, emit: Callable[[ev.Event], None]) -> bool:
        """A server error that came back on every retry while the server is not down (a
        plain 5xx, not "unavailable", which is waited out): it fails on this request, and
        the same request cannot do better. In devt2b one request got HTTP 500 thirty times
        in 150 s while every other request on the key went through, and the run ended
        after two steps. Cut the middle out of every big tool result, the newest included
        (their spill files keep the whole text), so the caller can ask once more with a
        different, smaller request. False when there is nothing to cut."""
        if exc.code != "server" or exc.status in UNAVAILABLE_STATUSES:
            return False
        before = self.context_tokens()
        removed = prune_tool_results(self.messages, keep_last=0)
        if not removed:
            return False
        after = max(before - int(removed / 3.5), 0)
        self._last_input_tokens, self._chars_since_measure = after, 0
        self.store.reset(self.messages)
        emit(ev.Compacted(mode="prune", tokens_before=before, tokens_after=after))
        return True

    def _prune_for_compaction(self, before: int, emit: Callable[[ev.Event], None]) -> int:
        removed = prune_tool_results(self.messages)
        if not removed:
            return before
        after = max(before - int(removed / 3.5), 0)
        self._last_input_tokens, self._chars_since_measure = after, 0
        self.store.reset(self.messages)
        emit(ev.Compacted(mode="prune", tokens_before=before, tokens_after=after))
        return after

    def model(self) -> ModelInfo:
        """What is known about the configured model (re-read after /model)."""
        name = self.config.model
        if self._model_facts is None or self._model_facts[0] != name:
            server = getattr(self.provider, "base_url", None)
            server = server if isinstance(server, str) else None
            self._model_facts = (name, model_info(name, self.config.home, server))
        return self._model_facts[1]

    def window_hint(self) -> int | None:
        """The window this session must stay under besides the configured one: what a
        context-window error taught it, or the model's known window."""
        known = [w for w in (self._learned_window, self.model().context_window) if w]
        return min(known) if known else None

    def _threshold(self) -> int:
        return self.config.compaction_threshold(self.window_hint())

    async def _recover_overflow(self, emit: Callable[[ev.Event], None]) -> bool:
        """After a context-window error: learn the smaller window, prune every big tool
        result, then summarize; if no summary can be made, keep the task, a checkpoint of
        the changed files and the recent tail. True when the conversation got smaller."""
        before = estimate_tokens(self.messages, self.system_prompt)
        sent = max(self.context_tokens(), before)
        # The provider refused about ``sent`` tokens: for this session stay well below
        # that (the configured window is left as it is).
        learned = max(int(sent * 0.85), 2_000)
        if self._learned_window is None or learned < self._learned_window:
            self._learned_window = learned
        removed = prune_tool_results(self.messages, keep_last=0)
        if removed:
            self.store.reset(self.messages)
            emit(
                ev.Compacted(
                    mode="prune", tokens_before=before, tokens_after=before - int(removed / 3.5)
                )
            )
        summarized = False
        try:
            # The head itself must fit in one request: summarize at most half of it.
            summarized = await self._summarize(
                estimate_tokens(self.messages, self.system_prompt),
                emit,
                head_budget=min(self.config.effective_window(self.window_hint()), sent) // 2,
            )
        except ProviderError as exc:
            if exc.code != "context_window":
                raise
        dropped = False if summarized else await self._drop_head(before, emit)
        after = estimate_tokens(self.messages, self.system_prompt)
        self._last_input_tokens, self._chars_since_measure = after, 0
        # Retry when anything was cut; the retry itself shows whether it now fits.
        return bool(removed or summarized or dropped)

    async def _drop_head(
        self, before: int, emit: Callable[[ev.Event], None], *, retain_tokens: int | None = None
    ) -> bool:
        """Model-free fallback: keep the task message, a checkpoint and the recent tail."""
        retain = int(self._threshold() * 0.2) if retain_tokens is None else retain_tokens
        first_user = next((m for m in self.messages if m.role == "user"), None)
        head, tail = split_for_summary(self.messages, retain)
        if not head:
            return False
        files = await asyncio.to_thread(self.files.changed_files)
        note = (
            "(No summary: the earlier part of the conversation no longer fit in the model's "
            "context window and was dropped. Re-read any file you need.)"
        )
        rebuilt = [m for m in _user_turns(head) if m not in tail]
        if first_user is not None and first_user not in tail and first_user not in rebuilt:
            rebuilt.insert(0, first_user)
        self.messages = [*rebuilt, checkpoint_message(note, files), *tail]
        self.store.reset(self.messages)
        after = estimate_tokens(self.messages, self.system_prompt)
        self._last_input_tokens, self._chars_since_measure = after, 0
        emit(ev.Compacted(mode="drop", tokens_before=before, tokens_after=after))
        return True

    async def _summary_call(
        self, request: ModelRequest, emit: Callable[[ev.Event], None]
    ) -> str | None:
        """The checkpoint text ("" when the model gave none); None when out of time."""
        timing = _CallTiming()
        call = complete_with_retry(
            self.provider,
            request,
            lambda _delta: timing.delta(),
            self.config.retry,
            on_retry=lambda _attempt, delay, _exc: timing.retry(delay),
        )
        if self._deadline is None:
            response = await call
        else:
            # The run's time limit bounds compaction too (its retries included); on
            # timeout the history stays as it is and the main loop reports the limit.
            try:
                remaining = max(self._deadline - time.monotonic(), 1.0)
                response = await asyncio.wait_for(call, remaining)
            except TimeoutError:
                return None
        self._account(0, response, emit, measures_context=False, timing=timing)
        if response.message.tool_calls or response.finish_reason in ("length", "content_filter"):
            # A preamble before a tool call ("let me check the file") or a summary cut at
            # the token limit is not a checkpoint; replacing the history with it would
            # lose the work it was meant to keep.
            return ""
        return response.message.content.strip()

    async def _summarize(
        self,
        before: int,
        emit: Callable[[ev.Event], None],
        head_budget: int | None = None,
        retain_tokens: int | None = None,
    ) -> bool:
        """Replace the older history with a model-written checkpoint; True on success."""
        began = time.monotonic()
        retain = int(self._threshold() * 0.2) if retain_tokens is None else retain_tokens
        first_user = next((m for m in self.messages if m.role == "user"), None)
        head, tail = split_for_summary(self.messages, retain)
        if not head:
            return False
        # The user's own messages in the head are kept verbatim beside the checkpoint: a
        # later requirement ("keep the public API") must not depend on the summary.
        cut = [m for m in _user_turns(head) if m is not first_user]
        if head_budget is not None:
            # Keep the newest part of the head that fits; older tool work is lost, but the
            # user's own messages are kept as they are and the checkpoint lists the changed
            # files.
            full = head
            while len(head) > 1 and estimate_tokens(head, self.system_prompt) > head_budget:
                head = head[max(len(head) // 4, 1) :]
                while head and head[0].role == "tool":
                    head = head[1:]
            if not head:
                return False
            kept = {id(m) for m in head}
            cut = [m for m in _user_turns(full) if id(m) not in kept and m is not first_user]
            cut += [m for m in _user_turns(head) if m is not first_user]
        request = self._request()
        # The instruction is marked as a harness message, not a new user turn: reasoning
        # is then replayed exactly as in the step before, so the request is the cached
        # conversation plus one message (as a "turn" it dropped all reasoning, the prefix
        # changed and the whole head was billed at the full price).
        ask = Message("user", COMPACTION_INSTRUCTION, meta={"kind": "compaction_request"})
        request.messages = self._replay_view([*head, ask])
        # Tool schemas stay in the first try so the cached prefix is reused; a model that
        # answers with a tool call instead of text gets one retry without tools.
        summary = await self._summary_call(request, emit)
        if summary is None:
            return False
        if not summary:
            summary = await self._summary_call(replace(request, tools=[]), emit)
            if not summary:
                return False
        files = await asyncio.to_thread(self.files.changed_files)
        rebuilt: list[Message] = []
        if first_user is not None and first_user not in tail:
            rebuilt.append(first_user)
        rebuilt.extend(cut)
        rebuilt.append(checkpoint_message(summary, files))
        rebuilt.extend(tail)
        self.messages = rebuilt
        self.store.reset(self.messages)
        after = estimate_tokens(self.messages, self.system_prompt)
        self._last_input_tokens, self._chars_since_measure = after, 0
        emit(
            ev.Compacted(
                mode="summary",
                tokens_before=before,
                tokens_after=after,
                duration_s=round(time.monotonic() - began, 2),
            )
        )
        return True


class _CallTiming:
    """Timing of one model call across its retries (monotonic clock)."""

    __slots__ = ("attempt_start", "first_delta", "retries", "start")

    def __init__(self) -> None:
        self.start = self.attempt_start = time.monotonic()
        self.first_delta: float | None = None
        self.retries = 0

    def delta(self) -> None:
        if self.first_delta is None:
            self.first_delta = time.monotonic()

    def retry(self, delay: float) -> None:
        # The next attempt starts after the wait; deltas of the failed one do not count.
        self.retries += 1
        self.attempt_start = time.monotonic() + delay
        self.first_delta = None

    def done(self) -> dict[str, Any]:
        end = time.monotonic()
        return {
            "latency_s": round(end - self.attempt_start, 2),
            "ttft_s": (
                round(self.first_delta - self.attempt_start, 2)
                if self.first_delta is not None
                else None
            ),
            "total_s": round(end - self.start, 2),
            "retries": self.retries,
        }


def _shell_runs(messages: list[Message]) -> list[tuple[str, str]]:
    """(command, output) of every shell call in ``messages``."""
    results = {m.tool_call_id: m.content for m in messages if m.role == "tool"}
    out = []
    for m in messages:
        for call in m.tool_calls if m.role == "assistant" else []:
            if call.name not in ("bash", "pwsh"):
                continue
            try:
                command = str(json.loads(call.arguments or "{}").get("command") or "")
            except (ValueError, AttributeError):
                continue
            if command and call.id in results:
                out.append((command, results[call.id]))
    return out


def _user_turns(messages: list[Message]) -> list[Message]:
    """The user's own messages (not harness notices or checkpoints)."""
    return [m for m in messages if m.role == "user" and not m.meta.get("kind")]


def _inside(path: Path, workspace: Path) -> tuple[str, ...]:
    """``path`` relative to the workspace when it lies inside it (else nothing)."""
    try:
        return (path.resolve().relative_to(workspace).as_posix(),)
    except ValueError:
        return ()


def _command_text(arguments: str) -> str:
    """A shell call's command (with its workdir); empty when the arguments are invalid."""
    try:
        data = json.loads(arguments or "{}")
    except (ValueError, TypeError):
        return ""
    if not isinstance(data, dict) or not isinstance(data.get("command"), str):
        return ""
    command: str = data["command"]
    workdir = data.get("workdir")
    return f"cd {workdir}; {command}" if isinstance(workdir, str) and workdir else command


def _without_images(messages: list[Message]) -> list[Message]:
    """Copies of ``messages`` with each picture replaced by a line of text: for a model
    that takes none (the stored conversation keeps them)."""
    out: list[Message] = []
    for m in messages:
        if not m.images:
            out.append(m)
            continue
        notes = " ".join(
            image_note(image).replace(
                "is no longer available", "not shown: this model takes no pictures"
            )
            for image in m.images
        )
        out.append(replace(m, images=[], content=f"{m.content}\n{notes}".strip()))
    return out


MAX_TOOL_PICTURES = 4  # per tool result


def _tool_pictures(messages: list[Message], *, show: bool) -> list[Message]:
    """Tool messages carry text only on the wire (most APIs take no pictures there): the
    pictures of a run of results follow it as one user message, or are left out."""
    if not any(m.role == "tool" and m.images for m in messages):
        return messages
    out: list[Message] = []
    waiting: list[tuple[str, dict[str, str]]] = []

    def flush() -> None:
        if waiting:
            names = ", ".join(dict.fromkeys(name for name, _ in waiting))
            text = f"[pictures from the tool results above: {names}]"
            out.append(
                Message(
                    "user", text, images=[i for _, i in waiting], meta={"kind": "tool_pictures"}
                )
            )
            waiting.clear()

    for m in messages:
        if m.role == "tool" and m.images:
            out.append(replace(m, images=[]))
            if show:
                waiting.extend((m.name or "tool", image) for image in m.images)
            continue
        if m.role != "tool":
            flush()
        out.append(m)
    flush()
    return out
