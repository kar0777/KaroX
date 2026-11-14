"""Engine kernel harness mixin: final proof calls, closing turn and repair."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .engine_kernel import EngineAgent

import logging
import os

os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")

logger = logging.getLogger("karox.engine_kernel")

import hashlib
import pathlib
import time
from typing import Any, Dict, List, Optional, Set, Tuple
from .engine_tool_env import (  # noqa: E402
    COVERAGE_REFUSAL_LIMIT,
)
from .engine_prompts import (  # noqa: E402
    _CLOSING_TURN_PROMPT,
    _CLOSING_TURN_VERIFIED_PROMPT,
)
from . import completion_gate
from . import check_report
from . import session_analysis
from .agent import (  # noqa: E402
    AgentEventKind,
    _bounded_failure_summary,
    provider_alias,
)
from .session_analysis import HARNESS_REQUESTED_BY
from .sessions import MutationLease, SessionRecord


#: The closing answer call needs a provider round trip; with less time left the
#: stop reports without it.
_CLOSING_TURN_MIN_SECONDS = 20.0


class EngineHarnessMixin:
    """Methods composed into EngineKernel; see engine_kernel.py."""

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
            # A wildcard grant is not a command: stripping "*" ran a whole large
            # suite (10 min on HARD12). Re-run the model's last passing check instead.
            argv = _last_passing_check(record) or [item for item in commands[0] if item != "*"]
            if not argv:
                return ()
            calls.append(("checks.run", {"argv": argv}))
        visual_calls: Tuple[Tuple[str, Dict[str, Any]], ...] = ()
        if (
            "visual.check" in self._definitions
            and (
                "missing visual evidence" in missing
                or "missing browser runtime evidence" in missing
            )
        ):
            target = self._render_target(record)
            if target is not None and completion_gate._approved_visual_producer_for_change(
                self.core,
                record,
                [target.name],
                completion_gate.diagnostic_paths(self.core, record),
            ):
                # Only a real, approved visual tool can produce browser evidence;
                # a tool failure or a verdict on unrelated/old bytes still fails
                # the unchanged completion gate.
                visual_calls = (("visual.check", {"path": target.name}),)
        return (
            *calls,
            *visual_calls,
            ("git.status", {}),
            ("git.diff", {"complete": True}),
        )

    def _render_target(self, record: SessionRecord) -> Optional[pathlib.Path]:
        """The page the harness's closing look judges: the run's own first.

        ``default_pages`` puts ``index.html`` first. The aquarium k2 run
        (2026-11-06) copied its deliverable to ``index.html`` with a probe; the
        closing look judged that copy, its pass named a page the run never
        changed, the gate rightly did not count it, and the run ended
        unverified although nobody had judged its own final bytes. A page (or
        picture) the run changed, or one that loads a changed asset, is what
        the user opens; only without one does the conventional entry page
        stand in.
        """

        try:
            from .web_check import default_pages, default_pictures

            root = self.core.repository
            pages = [root / page for page in default_pages(root)]
            pictures = [root / page for page in default_pictures(root)]
        except Exception:
            return None
        pages = [item for item in pages if item.is_file()]
        pictures = [item for item in pictures if item.is_file()]
        changed = list(getattr(record, "changed_files", None) or ())
        try:
            diagnostic = completion_gate.diagnostic_paths(self.core, record)
        except Exception:  # noqa: BLE001 - nothing is diagnostic then
            diagnostic = frozenset()
        for candidate in (*pages, *pictures):
            try:
                if completion_gate._page_loads_the_change(
                    self.core, candidate.name, changed, diagnostic
                ):
                    return candidate
            except Exception:  # noqa: BLE001 - try the next page
                continue
        return pages[0] if pages else None

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

    def _closing_answer_turn(self, agent: EngineAgent, *, verified: bool = False) -> bool:
        """One model call for the final answer after a visual-budget stop.

        The stop is honest -- no further verdict can make the page verified --
        but it used to end the run between two tool calls, so the user got no
        reply at all: the aquarium a1 run (2026-11-05) left a 41 KB page on
        disk and an empty answer where the task demanded the HTML. The model
        now gets exactly one call in which only finish runs; the run still
        ends ``stopped`` with the same reason, and an edit is refused because
        nothing could judge it. Returns whether an answer arrived.
        """

        if self._closing_turn_used or self._engine_submission:
            return False
        if self._deadline - self.monotonic() < _CLOSING_TURN_MIN_SECONDS:
            return False
        self._closing_turn_used = True
        self._cancelled = False
        if self._cancel_event is not None:
            self._cancel_event.clear()
        self._visual_grace_until = None
        self._closing_turn_until = self._step + 1
        config = agent.config
        if 0 < int(config.step_limit) <= int(agent.n_calls):
            config.step_limit = int(agent.n_calls) + 1
        # After a wall-clock stop the engine's own clock is spent too; the
        # closing call may use what is left of KaroX's deadline, nothing more.
        wall_limit = int(getattr(config, "wall_time_limit_seconds", 0) or 0)
        started = getattr(agent, "_start_time", None)
        if wall_limit > 0 and isinstance(started, (int, float)):
            elapsed = int(time.time() - started)
            if wall_limit <= elapsed + 1:
                config.wall_time_limit_seconds = elapsed + max(
                    2, int(self._deadline - self.monotonic())
                )
        # A prose reply in this call is the answer, not a format error to retry.
        format_budget = config.max_consecutive_format_errors
        config.max_consecutive_format_errors = 1
        self._emit(
            AgentEventKind.WARNING,
            reason="closing_answer_turn",
            detail=(
                "the work phase ended before the model's answer; one call for it"
                if verified
                else "visual check budget is exhausted; one call for the final answer"
            ),
        )
        agent.add_messages(
            {
                "role": "user",
                "kind": "engine_closing",
                "content": _CLOSING_TURN_VERIFIED_PROMPT if verified else _CLOSING_TURN_PROMPT,
            }
        )
        try:
            agent.continue_run()
        except KeyboardInterrupt:
            self.cancel()
        except Exception:  # noqa: BLE001 - the stop is already decided
            pass
        finally:
            config.max_consecutive_format_errors = format_budget
            if wall_limit > 0:
                config.wall_time_limit_seconds = wall_limit
            self._closing_turn_until = None
        return bool(self._engine_submission)

    def _attestation_spent(self) -> bool:
        """Whether the finish gate already used its bounded coverage refusals."""

        environment = self._environment
        refusals = int(getattr(environment, "_coverage_refusals", 0) or 0)
        return refusals >= COVERAGE_REFUSAL_LIMIT

    def _append_repair_prompt(self, agent: EngineAgent, failure: str, prompt: str = "") -> None:
        """Give the model one bounded repair turn with the failure in front."""
        last_assistant: Optional[Dict[str, Any]] = None
        for message in reversed(agent.messages):
            if message.get("role") == "assistant":
                last_assistant = message
                break
        prompt = prompt or (
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

    def _unopened_files_note(self, agent: Any, session_id: str) -> str:
        """Point the review at files the task names but the run never opened."""

        try:
            from . import task_files

            record = self.sessions.load(session_id)
            missed = task_files.unopened(
                record.task,
                pathlib.Path(self.core.repository),
                agent.messages,
                record.changed_files or (),
            )
            return task_files.review_note(missed)
        except Exception:  # noqa: BLE001 - the review stands without the note
            return ""

    def _error_path_note(self, session_id: str) -> str:
        """Name the old raises in the functions this run edited (see review_anchors)."""

        try:
            from . import git_baseline, review_anchors

            record = self.sessions.load(session_id)
            baseline = git_baseline.normalize(getattr(record, "git_baseline", None))
            if not baseline or baseline.get("kind") != git_baseline.KIND_COMMIT:
                return ""
            return review_anchors.review_note(pathlib.Path(self.core.repository), str(baseline["commit"]))
        except Exception:  # noqa: BLE001 - the review stands without the note
            return ""

    def _review_turn_due(self) -> bool:
        """The run owes its one maintainer review (code changed, time left)."""
        environment = self._environment
        owed = getattr(environment, "_self_review_owed", None)
        if not callable(owed) or not owed() or self.remaining_seconds() < 60:
            return False
        environment._self_review_done = True  # type: ignore[union-attr]
        # wtforms-892 duel1: a settle notice right after the review request
        # said "finish now" and the review's findings were never acted on.
        environment._review_hold_seq = int(getattr(environment, "_last_change_seq", 0) or 0)  # type: ignore[union-attr]
        environment._review_hold_step = int(getattr(self, "_step", 0) or 0)  # type: ignore[union-attr]
        return True

    def _failure_summary(self, record: SessionRecord) -> str:
        """What the repair turn is told, in the order a fix needs it.

        The floor reasons say which obligation is unmet; the failure map says
        where. Parsed failures survive the log clipping, name the exact file
        and line, and make "nothing changed since the last repair" visible
        instead of silently repeating a turn.
        """

        pieces = list(completion_gate.floor(self.core, record))
        # A review nothing can produce (no Git) is not the model's to fix:
        # naming it sent the TB 4.1 mvcc run off to hand-craft a diff.
        unproducible = completion_gate.NO_PRODUCER + "current change review"
        if unproducible in pieces:
            pieces = [p for p in pieces if p not in (unproducible, "missing current change review")]
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
            ) or data.get("preexisting_only") is True:
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
            ) or data.get("preexisting_only") is True
            if passed:
                return None
            report = data.get("failure_map")
            return dict(report) if isinstance(report, dict) else None
        return None


def _last_passing_check(record: SessionRecord) -> List[str]:
    """argv of the most recent passing, verification-eligible, non-inline check."""
    for entry in reversed(record.provider_history):
        result = entry.get("result") if entry.get("core_name") == "checks.run" else None
        data = result.get("data") if isinstance(result, dict) else None
        argv = data.get("argv") if isinstance(data, dict) else None
        if (
            isinstance(argv, list) and len(argv) > 1 and argv[1] != "-c"
            and data.get("verification_eligible") is True and result.get("ok") is True
        ):
            return [str(a) for a in argv]
    return []
