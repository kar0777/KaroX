"""TUI task launch, stream handling, finish, actions and chrome."""

from __future__ import annotations

import time
from typing import Any
from rich.text import Text
from textual.widgets import Static
from .backend import AgentRequest
from .i18n import plural, reason_text, tr
from .protocol import Event, Outcome, SessionState, tool_category
from .tooltext import duration_text, plan_rows, plan_step, short_tokens, working_text
from .views import (
    token_text,
)
from .widgets import (
    AssistantMessage,
    Notice,
    StatusLine,
    TextBlock,
    ToolCard,
    UserMessage,
    status_segments,
)

from .app_support import (  # noqa: F401
    EFFORT_LEVELS as EFFORT_LEVELS,
    MODES as MODES,
    LANGUAGES as LANGUAGES,
    _REFUSED_WHILE_RUNNING as _REFUSED_WHILE_RUNNING,
    RunState as RunState,
    _display_path as _display_path,
    _cost_text as _cost_text,
    _save_preference as _save_preference,
    _git_branch as _git_branch,
    _detect_verification as _detect_verification,
)


class AppRunMixin:
    """Methods composed into KaroxApp; see app.py."""

    def _start_task(self, task: str) -> None:
        if self._agent_running:
            # Typed while the agent works: it runs next, in order, the way a
            # shell keeps type-ahead. The working line shows the queue.
            self._queue.append(task)
            self._refresh_working()
            return
        if self._model is None:
            try:
                self._model = self.backend.selected_model()
            except Exception:  # noqa: BLE001 - reported as "no model" below
                self._model = None
            self._model_loaded = True
        if self._model is None:
            self._notice(tr(self.language, "notice.no_model"), "warning")
            return
        if not self._verification:
            self._ask_for_check(task)
            return
        self._show_shortcuts(False)
        self.chat.add(UserMessage(task))
        self._reset_turn()
        self._agent_running = True
        self._stopping = False
        self.run_state = RunState(state="running", started_at=time.monotonic())
        self._refresh_status()
        # Let the user line finish mounting before the first event can arrive:
        # a fast run could otherwise paint into widgets that are still queued.
        self.call_after_refresh(self._launch_run, task)

    def _reset_turn(self) -> None:
        self._segment = None
        self._segments = []
        self._cards.clear()
        self._running_tools.clear()
        self._phase_key = "working.thinking"
        self._plan_rows = []
        self._retry_note = ""
        self._last_reasoning = ""

    def _launch_run(self, task: str) -> None:
        self.run_worker(
            lambda: self._stream(task),
            thread=True,
            group="agent",
            name="agent-run",
            exit_on_error=False,
        )

    def _stream(self, task: str) -> None:
        request = AgentRequest(
            repository=self.repository,
            task=task,
            session_id=self.session_id,
            continue_task=self._continue,
            verification=self._verification,
            effort=None if self.effort == "auto" else self.effort,
            mode=None if self.mode == "build" else self.mode,
            mcp_servers=tuple(sorted(self._pending_mcp.items())),
        )

        def on_event(event: Event) -> None:
            self._from_thread(self._apply_event, event)

        try:
            result = self.backend.stream_agent(request, on_event)
        except Exception as exc:  # noqa: BLE001 - a UI must survive any bridge failure
            self._from_thread(self._finish, None, -1, str(exc), False)
            return
        self._from_thread(
            self._finish, result.outcome, result.exit_code, result.stderr, result.stopped
        )

    def _open_segment(self) -> AssistantMessage:
        if self._segment is None:
            self._segment = self.chat.add(AssistantMessage(self.language))
            self._segments.append(self._segment)
        return self._segment

    def _close_segment(self) -> None:
        segment, self._segment = self._segment, None
        if segment is not None:
            segment.finish()

    def _apply_event(self, event: Event) -> None:
        """Fold one typed stream event into the transcript and the working line.

        The transcript gets the durable lines -- answer text, one line per tool
        call with its result, notices that matter later. Everything that is
        only true right now (which call is running, a provider retry, what
        the plan says is next) goes to the working line and disappears with
        the run.
        """

        state = self.run_state
        kind = event.kind
        lang = self.language
        if kind == "text_delta":
            if event.text:
                self._open_segment().append_text(event.text)
                self._phase_key = "working.writing"
        elif kind in {"reasoning_delta", "reasoning_summary_delta"}:
            text = event.reasoning if kind == "reasoning_delta" else event.reasoning_summary
            if text:
                self._open_segment().append_reasoning(
                    text, summary=kind == "reasoning_summary_delta"
                )
                self._phase_key = "working.reasoning"
        elif kind == "step_started":
            # A new model turn starts a new stretch of text: without the break
            # "working on it" and the next step's "done" render as one word.
            self._close_segment()
            state.step = max(state.step, event.step)
            self._phase_key = "working.thinking"
            self._retry_note = ""
        elif kind == "step_finished":
            self._absorb_usage(event.usage)
        elif kind == "tool_started":
            self._close_segment()
            card = ToolCard(
                event.tool,
                event.call_id,
                tool_category(event.tool),
                language=lang,
                detail=event.detail,
                expanded=self._verbose,
            )
            self.chat.add(card)
            key = event.call_id or f"anonymous-{id(card)}"
            self._cards[key] = card
            self._running_tools[key] = (event.tool, event.detail)
            if event.tool == "plan.update":
                rows = plan_rows(event.detail)
                if rows:
                    self._plan_rows = rows
            self._retry_note = ""
        elif kind == "tool_finished":
            self._close_segment()
            category = tool_category(event.tool)
            key = event.call_id or next(
                (
                    name
                    for name, (tool, _detail) in reversed(list(self._running_tools.items()))
                    if tool == event.tool and name.startswith("anonymous-")
                ),
                "",
            )
            finished = self._cards.get(key) if key else None
            if finished is None:
                finished = ToolCard(
                    event.tool, event.call_id, category, language=lang, expanded=self._verbose
                )
                self.chat.add(finished)
                if event.call_id:
                    self._cards[event.call_id] = finished
            finished.finish(event.ok, event.summary, event.duration_seconds, preview=event.detail)
            self._running_tools.pop(key, None)
            self._count_tool(category)
            self._phase_key = "working.thinking"
        elif kind == "phase_changed":
            state.phase = event.phase
        elif kind == "compacted":
            self._close_segment()
            self._notice(tr(lang, "notice.compacted", summary=event.summary or "…"), "info")
        elif kind == "warning":
            warned = self._cards.get(event.call_id) if event.call_id else None
            if event.reason == "provider_retry":
                self._retry_note = event.detail or event.reason
            elif warned is not None:
                warned.note_error(event.detail or event.reason)
            else:
                self._close_segment()
                self._notice(self._warning_text(event.reason, event.detail), "warning")
        elif kind == "error":
            failed = self._cards.get(event.call_id) if event.call_id else None
            if failed is not None:
                # A refused or failed call: the reason belongs on its own line.
                failed.note_error(event.detail or event.reason)
            else:
                self._close_segment()
                self._notice(f"{event.reason}: {event.detail}".strip(": "), "error")
        elif kind == "file_edited":
            if event.path and event.path not in state.files:
                state.files.append(event.path)
        elif kind == "test_finished":
            if event.ok:
                state.tests_ok += 1
            else:
                state.tests_failed += 1
        if event.usage:
            if kind == "finished":
                self._absorb_usage(event.usage, cumulative=True)
            elif kind != "step_finished":
                self._absorb_usage(event.usage)
        self._refresh_working()

    def _warning_text(self, reason: str, detail: str) -> str:
        """A kernel warning in the reader's words; unknown reasons stay raw."""

        key = f"warn.{reason}"
        text = tr(self.language, key, detail=detail)
        if text == key:
            return f"{reason}: {detail}".strip(": ")
        return text

    def _count_tool(self, category: str) -> None:
        state = self.run_state
        if category == "read":
            state.reads += 1
        elif category == "edit":
            state.edits += 1
        elif category == "mcp":
            state.mcp_calls += 1
        elif category == "skill":
            state.skill_calls += 1
        elif category == "inspect":
            state.inspections += 1

    def _absorb_usage(self, usage: dict[str, int], *, cumulative: bool = False) -> None:
        if not usage:
            return
        state = self.run_state
        total = usage.get("total_tokens")
        if isinstance(total, int) and total > 0:
            if cumulative:
                self._tokens = total
                state.tokens = total
            else:
                self._tokens += total
                state.tokens += total
        prompt = usage.get("prompt_tokens") or usage.get("input_tokens")
        if isinstance(prompt, int) and prompt > 0:
            state.prompt_tokens = prompt
            window = 0
            if isinstance(self._model, dict):
                raw = self._model.get("context_window")
                if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
                    window = raw
            if window:
                state.context_fraction = prompt / float(window)
        self._refresh_chrome()

    def _finish(
        self,
        outcome: Outcome | None,
        exit_code: int,
        stderr: str,
        stopped: bool,
    ) -> None:
        self._agent_running = False
        self._stopping = False
        elapsed = self.run_state.elapsed()
        self._close_segment()
        # A call the run never reported back on (a stop in the middle of a
        # command) must not keep blinking as if it were still running.
        for key in list(self._running_tools):
            card = self._cards.get(key)
            if card is not None and card.ok is None:
                card.interrupt()
        self._running_tools.clear()
        self._retry_note = ""
        answered = any(segment.answer_text().strip() for segment in self._segments)
        if not answered and not stopped and outcome is not None and outcome.provider_message.strip():
            # The report's final message is shown only when nothing streamed;
            # otherwise it would repeat the answer the reader already has.
            self._open_segment().append_text(outcome.provider_message)
            self._close_segment()
        for segment in list(self._segments):
            if segment.is_blank():
                self._segments.remove(segment)
                try:
                    segment.remove()
                except Exception:  # noqa: BLE001 - an unmounted segment has nothing to remove
                    pass
        self._last_reasoning = "\n\n".join(
            segment.reasoning_text() for segment in self._segments if segment.reasoning_text()
        )
        if stopped:
            self.run_state.state = "stopped"
            self._result_notice(tr(self.language, "notice.stopped"), "warning")
            self._refresh_checkpoint()
        elif outcome is not None:
            if outcome.session_id:
                self.session_id = outcome.session_id
            self._tokens = outcome.token_total or self._tokens
            self.run_state.state = "failed" if outcome.status in {"failed", "error"} else "done"
            self._result_notice(self._result_text(outcome, elapsed), self._result_level(outcome))
            self._continue = True
            # The run created the session and stored the servers it was
            # asked to attach; from here on /mcp edits the session directly.
            self._pending_mcp.clear()
            self._refresh_checkpoint()
        else:
            self.run_state.state = "failed"
            lines = [line for line in stderr.strip().splitlines() if line.strip()]
            detail = lines[-1] if lines else f"exit code {exit_code}"
            self._result_notice(tr(self.language, "notice.backend_error", error=detail), "error")
        self._refresh_status()
        if self._queue:
            if outcome is not None and not stopped:
                self.call_after_refresh(self._start_task, self._queue.pop(0))
            else:
                # Stopped or broken: hand the type-ahead back instead of
                # running it on top of a turn that did not finish.
                self.composer.set_text("\n".join(self._queue))
                self._queue.clear()
                self._refresh_working()
        if self.composer.display:
            self.composer.focus()

    def _refresh_checkpoint(self) -> None:
        """Re-read the durable record so the saved-state fact is never guessed."""

        session_id = self.session_id

        def done(record: dict[str, Any] | None) -> None:
            if session_id != self.session_id:
                return
            self.checkpoint = SessionState.from_record(record) if record is not None else None
            self._refresh_status()

        self._background(lambda: self.backend.session(session_id), done, failed=lambda _: None)

    def _result_text(self, outcome: Outcome, elapsed: float) -> str:
        lang = self.language
        if outcome.verified:
            head = tr(lang, "result.verified")
        elif outcome.reason == "answer":
            head = tr(lang, "result.answer")
        else:
            head = tr(lang, "result.failed")
        parts = [head]
        if outcome.steps:
            parts.append(plural(lang, outcome.steps, "step"))
        if outcome.changed_files:
            parts.append(plural(lang, len(outcome.changed_files), "file"))
        passed, total = outcome.check_tally
        if total:
            parts.append(tr(lang, "result.checks", passed=passed, total=total))
        if outcome.token_total:
            parts.append(tr(lang, "result.tokens", count=token_text(outcome.token_total)))
        if outcome.costs:
            parts.append(tr(lang, "result.cost", cost=_cost_text(outcome.costs)))
        if elapsed:
            parts.append(tr(lang, "result.duration", seconds=elapsed))
        lines = [" · ".join(parts)]
        if not outcome.verified and outcome.reason and outcome.reason != "answer":
            lines.append(tr(lang, "result.reason", reason=reason_text(lang, outcome.reason)))
        unmet = outcome.raw.get("unmet")
        if isinstance(unmet, list) and unmet:
            shown = "; ".join(str(item) for item in unmet[:3])
            if len(unmet) > 3:
                shown += f" (+{len(unmet) - 3})"
            lines.append(tr(lang, "result.unmet", items=shown))
        failure_map = outcome.raw.get("failure_map")
        if isinstance(failure_map, dict):
            from ..check_report import render_failure_map

            rendered = render_failure_map(failure_map)
            if rendered:
                lines.extend(rendered.splitlines()[:5])
        if outcome.changed_files:
            shown = ", ".join(outcome.changed_files[:6])
            if len(outcome.changed_files) > 6:
                shown += f" (+{len(outcome.changed_files) - 6})"
            lines.append(tr(lang, "result.changed", files=shown))
        return "\n".join(lines)

    @staticmethod
    def _result_level(outcome: Outcome) -> str:
        if outcome.verified or outcome.reason == "answer":
            return "success"
        if outcome.status in {"failed", "error"}:
            return "error"
        return "warning"

    def action_new_session(self) -> None:
        if not self._idle():
            return
        self.session_id = self._new_session_id()
        self._continue = False
        self._tokens = 0
        self.checkpoint = None
        self.run_state = RunState()
        self._reset_turn()
        self.chat.clear_messages()
        self._add_welcome()
        self._notice(tr(self.language, "notice.new_session", id=self.session_id), "info")
        self._refresh_chrome()
        self._refresh_status()

    def action_clear(self) -> None:
        if not self._idle():
            return
        self._reset_turn()
        self._welcome_card = None
        self.chat.clear_messages()
        self._notice(tr(self.language, "notice.clear"), "info")

    def action_show_reasoning(self) -> None:
        self._cmd_reasoning("")

    def action_stop(self) -> None:
        if self.menu.active:
            self.menu.cancel()
            return
        if self.query_one("#shortcuts", Static).display:
            self._show_shortcuts(False)
            return
        if not self._agent_running or self._stopping:
            return
        self._stopping = True
        self.run_state.state = "stopping"
        self._refresh_status()
        self.run_worker(self.backend.stop, thread=True, group="stop", name="agent-stop")

    def action_toggle_verbose(self) -> None:
        """Ctrl+O: every tool line, past and future, shows all it has."""

        self._verbose = not self._verbose
        for card in self.chat.query(ToolCard):
            card.set_expanded(self._verbose)
        self._refresh_chrome()

    def action_interrupt(self) -> None:
        """Ctrl+C: close what is open, else stop the run, else clear, else quit."""

        if len(self.screen_stack) > 1:
            self.pop_screen()
            return
        if self._prompt_open:
            self._cancel_prompt()
            return
        if self._agent_running:
            self.action_stop()
            return
        if self.menu.active:
            self.menu.cancel()
            return
        if str(self.composer.text):
            self.composer.set_text("")
            return
        self.exit(0)

    def _model_label(self) -> str:
        if self._model is None:
            return ""
        provider = self._model.get("provider_id")
        model = self._model.get("model_id")
        if not isinstance(provider, str) or not isinstance(model, str):
            return ""
        return f"{provider}/{model}"

    def _refresh_chrome(self) -> None:
        """The footer: a hint on the left, the window's facts on the right."""

        lang = self.language
        width = self.size.width or 100
        if self.mode != "build":
            hint, level = tr(lang, "footer.mode", mode=self.mode), "mode"
        else:
            hint, level = tr(lang, "footer.hint"), ""
        if self._verbose:
            hint += " · " + tr(lang, "footer.verbose")
        model = self._model_label()
        if not model and not self._model_loaded:
            model = tr(lang, "status.loading")
        status = self.query_one(StatusLine)
        status.set_hint(hint, level)
        status.set_facts(
            status_segments(
                model=model,
                repository="",
                tokens=self._tokens,
                context_fraction=self.run_state.context_fraction,
                effort=self.effort,
                # The mode is the hint's business; stating it twice is noise.
                mode="build",
                width=max(24, width - 8 - len(hint)),
                language=lang,
            )
        )

    def _refresh_status(self) -> None:
        self._refresh_working()
        self._refresh_chrome()

    def _refresh_working(self) -> None:
        """KaroX's dots and one line saying what is happening right now."""

        line = self.working
        lang = self.language
        if not self._agent_running:
            line.set_working(self._lookup_text)
            return
        state = self.run_state
        if self._stopping:
            text = tr(lang, "working.stopping")
        elif self._running_tools:
            tool, detail = list(self._running_tools.values())[-1]
            text = working_text(tool, detail, lang)
        else:
            text = tr(lang, self._phase_key)
        elapsed = duration_text(state.elapsed(), lang)
        if state.tokens:
            facts = tr(lang, "working.facts", elapsed=elapsed, tokens=short_tokens(state.tokens))
        else:
            facts = tr(lang, "working.facts_quiet", elapsed=elapsed)
        below: list[str] = []
        if self._retry_note:
            below.append(tr(lang, "working.retry", detail=self._retry_note))
        position, total, upcoming = plan_step(self._plan_rows)
        if upcoming:
            below.append(tr(lang, "working.next", item=upcoming, index=position, total=total))
        if self._queue:
            first = " ".join(self._queue[0].split())
            if len(first) > 60:
                first = first[:59] + "…"
            if len(self._queue) > 1:
                below.append(
                    tr(lang, "working.queued_more", text=first, count=len(self._queue) - 1)
                )
            else:
                below.append(tr(lang, "working.queued", text=first))
        line.set_working(
            text,
            facts,
            "\n".join(below),
            "warning" if self._stopping or self._retry_note else "",
        )

    def _idle(self) -> bool:
        """Refuse a command that would move the session under a live run."""

        if self._agent_running:
            self._notice(tr(self.language, "notice.already_running"), "warning")
            return False
        return True

    def _notice(self, text: str, level: str = "info") -> None:
        if text:
            self.chat.add(Notice(text, level))

    def _result_notice(self, text: str, level: str) -> None:
        # The end of a turn stands one row apart from the last tool line.
        if text:
            notice = Notice(text, level)
            notice.add_class("run-result")
            self.chat.add(notice)

    def _print_block(self, title: str, body: Text | str) -> None:
        # Not ``_print``: Textual's App._print(text, stderr) receives every
        # print() made while the app runs, so shadowing it crashed the TUI.
        self.chat.add(TextBlock(title, body))
