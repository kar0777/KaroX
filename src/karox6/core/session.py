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
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace
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
from karox6.core.loopguard import LoopGuard
from karox6.core.messages import Message, Price, ToolCall, Usage
from karox6.core.prompt import build_system_prompt
from karox6.core.snapshot import write_snapshot
from karox6.core.store import SessionStore, default_home, new_session_id
from karox6.providers.base import (
    Delta,
    ModelRequest,
    ModelResponse,
    Provider,
    ProviderError,
    RetryPolicy,
    complete_with_retry,
)
from karox6.tools.base import Approver, Tool, ToolArgsError, ToolContext, deny_all, parse_arguments
from karox6.tools.files import FileTracker
from karox6.tools.jobs import JobManager
from karox6.tools.registry import build_tools, tool_specs
from karox6.tools.shell import ShellInfo, detect_shell

GENERIC_TOOL_CAP = 60_000
TRUNCATION_NOTICE = (
    "Your previous response was cut off at the output token limit before it was complete. "
    "Continue from where it stopped. Keep each response shorter: split large file contents "
    "into several smaller write or edit calls."
)


@dataclass(slots=True)
class SessionConfig:
    model: str
    provider_name: str = "openai-chat"
    workspace: Path = field(default_factory=Path.cwd)
    home: Path = field(default_factory=default_home)
    max_steps: int | None = None
    max_seconds: float | None = None
    max_cost_usd: float | None = None
    max_output_tokens: int | None = None
    context_window: int = 128_000
    compact_at_tokens: int | None = None  # default: derived from the context window
    temperature: float | None = None
    reasoning_effort: str | None = None
    price: Price | None = None
    apply_patch: bool = False
    replay_reasoning: str = "turn"  # all | turn | none
    loop_hard_stop: int = 20
    snapshot_on_finish: bool = True  # copy changed files to submissions/NN (grading)
    max_parallel: int = 10
    stream_deltas: bool = False
    extra_body: dict[str, Any] = field(default_factory=dict)
    system_prompt: str | None = None  # replaces the base prompt (environment is still added)
    retry: RetryPolicy = field(default_factory=RetryPolicy)

    def compaction_threshold(self) -> int:
        if self.compact_at_tokens:
            return self.compact_at_tokens
        reserve = (self.max_output_tokens or 32_000) + 16_000
        return max(int(min(self.context_window * 0.8, self.context_window - reserve)), 8_000)


class Session:
    def __init__(
        self,
        config: SessionConfig,
        provider: Provider,
        *,
        approver: Approver = deny_all,
        shell: ShellInfo | None = None,
        tools: list[Tool] | None = None,
        session_id: str | None = None,
        messages: list[Message] | None = None,
        usage: Usage | None = None,
        cost_usd: float = 0.0,
    ) -> None:
        self.config = config
        self.provider = provider
        self.workspace = config.workspace.resolve()
        self.shell = shell or detect_shell()
        self.tools = (
            tools if tools is not None else build_tools(self.shell, apply_patch=config.apply_patch)
        )
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
        self.system_prompt = build_system_prompt(
            self.workspace, self.shell, base=config.system_prompt
        )
        self._last_input_tokens = 0
        self._chars_since_measure = 0
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

    async def run(self, prompt: str) -> AsyncIterator[ev.Event]:
        """Run one user turn; yields events until (and including) ``RunFinished``."""
        queue: asyncio.Queue[ev.Event | None] = asyncio.Queue()

        def emit(event: ev.Event) -> None:
            queue.put_nowait(event)

        async def produce() -> None:
            try:
                await self._drive(prompt, emit)
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

    async def _drive(self, prompt: str, emit: Callable[[ev.Event], None]) -> None:
        started = time.monotonic()
        ctx = ToolContext(
            workspace=self.workspace,
            session_dir=self.store.dir,
            files=self.files,
            jobs=self.jobs,
            shell=self.shell,
            approve=self._make_approver(emit),
            emit=emit,
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
        self._append(Message("user", prompt))
        steps = 0
        truncations = 0
        status, reason, final_text = "completed", "", ""
        try:
            while True:
                limit = self._limit_reached(steps, started)
                if limit:
                    status, reason = "limit", limit
                    break
                await self._maybe_compact(emit)
                steps += 1
                response = await self._call_model(steps, started, emit)
                if response is None:
                    status, reason = "limit", f"time limit {self.config.max_seconds:.0f}s"
                    break
                message = response.message
                self._append(message)
                emit(
                    ev.AssistantMessage(
                        step=steps,
                        text=message.content,
                        tool_calls=[c.to_dict() for c in message.tool_calls],
                        finish_reason=response.finish_reason,
                    )
                )
                if response.finish_reason == "length":
                    truncations += 1
                    if truncations > 3:
                        status, reason = "error", "output repeatedly cut off at the token limit"
                        break
                    if message.tool_calls:
                        # Calls cut mid-arguments are answered as errors below; tell the
                        # model why so it does not resend the same oversized call.
                        await self._run_calls(message.tool_calls, ctx, emit)
                    self._notice("truncated", TRUNCATION_NOTICE, emit)
                    continue
                if not message.tool_calls:
                    final_text = message.content
                    break
                stop = await self._run_calls(message.tool_calls, ctx, emit)
                if stop:
                    status, reason = "loop", stop
                    break
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
            )
            with contextlib.suppress(Exception):
                self.store.append("event", event=finished.to_dict())
            emit(finished)

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
        self, step: int, started: float, emit: Callable[[ev.Event], None]
    ) -> ModelResponse | None:
        request = self._request()

        def on_delta(delta: Delta) -> None:
            if self.config.stream_deltas:
                emit(
                    ev.TextDelta(delta.text)
                    if delta.kind == "text"
                    else ev.ReasoningDelta(delta.text)
                )

        def on_retry(attempt: int, delay: float, exc: ProviderError) -> None:
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
        self._account(step, response, emit)
        return response

    def _account(
        self, step: int, response: ModelResponse, emit: Callable[[ev.Event], None]
    ) -> None:
        usage = response.usage
        usage.requests = max(usage.requests, 1)
        self.usage.add(usage)
        cost = self.config.price.cost(usage) if self.config.price else None
        if cost is not None:
            self.cost_usd += cost
        self._last_input_tokens = usage.input_tokens
        self._chars_since_measure = len(response.message.content) + sum(
            len(c.arguments) for c in response.message.tool_calls
        )
        self.store.append("usage", step=step, usage=usage.to_dict(), cost_usd=cost)
        emit(
            ev.UsageUpdate(
                step=step,
                usage=usage.to_dict(),
                total=self.usage.to_dict(),
                cost_usd=round(cost, 6) if cost is not None else None,
                total_cost_usd=round(self.cost_usd, 6) if self.config.price else None,
            )
        )

    def _request(self) -> ModelRequest:
        messages = self._replay_view(self.messages)
        return ModelRequest(
            model=self.config.model,
            system=self.system_prompt,
            messages=messages,
            tools=tool_specs(self.tools),
            max_output_tokens=self.config.max_output_tokens,
            temperature=self.config.temperature,
            reasoning_effort=self.config.reasoning_effort,
            extra_body=dict(self.config.extra_body),
        )

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

        while index < len(calls):
            tool = self._lookup(calls[index].name)
            if tool is not None and tool.read_only:
                batch = [index]
                while index + len(batch) < len(calls):
                    nxt = self._lookup(calls[index + len(batch)].name)
                    if nxt is None or not nxt.read_only:
                        break
                    batch.append(index + len(batch))
                await asyncio.gather(*(one(i) for i in batch))
                index += len(batch)
            else:
                await one(index)
                index += 1

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
        )

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

    def _append(self, message: Message) -> None:
        self.messages.append(message)
        self._chars_since_measure += len(message.content) + len(message.reasoning or "")
        self.store.message(message)

    def _notice(self, kind: str, text: str, emit: Callable[[ev.Event], None]) -> None:
        self._append(Message("user", text, meta={"kind": kind}))
        emit(ev.Notice(kind=kind, text=text))

    def context_tokens(self) -> int:
        if self._last_input_tokens:
            return self._last_input_tokens + int(self._chars_since_measure / 3.5)
        return estimate_tokens(self.messages, self.system_prompt)

    async def _maybe_compact(self, emit: Callable[[ev.Event], None]) -> None:
        threshold = self.config.compaction_threshold()
        before = self.context_tokens()
        if before < threshold:
            return
        removed = prune_tool_results(self.messages)
        if removed:
            after = max(before - int(removed / 3.5), 0)
            self._last_input_tokens, self._chars_since_measure = after, 0
            self.store.reset(self.messages)
            emit(ev.Compacted(mode="prune", tokens_before=before, tokens_after=after))
            if after < threshold:
                return
            before = after
        await self._summarize(before, emit)

    async def _summarize(self, before: int, emit: Callable[[ev.Event], None]) -> None:
        retain = int(self.config.compaction_threshold() * 0.2)
        first_user = next((m for m in self.messages if m.role == "user"), None)
        head, tail = split_for_summary(self.messages, retain)
        if not head:
            return
        request = self._request()
        request.messages = self._replay_view([*head, Message("user", COMPACTION_INSTRUCTION)])
        response = await complete_with_retry(self.provider, request, None, self.config.retry)
        self._account(0, response, emit)
        summary = response.message.content.strip()
        if not summary:
            return
        files = await asyncio.to_thread(self.files.changed_files)
        rebuilt: list[Message] = []
        if first_user is not None and first_user not in tail:
            rebuilt.append(first_user)
        rebuilt.append(checkpoint_message(summary, files))
        rebuilt.extend(tail)
        self.messages = rebuilt
        self.store.reset(self.messages)
        after = estimate_tokens(self.messages, self.system_prompt)
        self._last_input_tokens, self._chars_since_measure = after, 0
        emit(ev.Compacted(mode="summary", tokens_before=before, tokens_after=after))


def _inside(path: Path, workspace: Path) -> tuple[str, ...]:
    """``path`` relative to the workspace when it lies inside it (else nothing)."""
    try:
        return (path.resolve().relative_to(workspace).as_posix(),)
    except ValueError:
        return ()
