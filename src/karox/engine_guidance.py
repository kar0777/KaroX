"""World-state guidance, refusal handling and observation notes."""

from __future__ import annotations

from .engine_tool_support import (  # noqa: F401
    _READABLE_TOOLS as _READABLE_TOOLS,
    _TEST_TOOLS as _TEST_TOOLS,
    _PROOF_TOOLS as _PROOF_TOOLS,
    _BOOKKEEPING_KEYS as _BOOKKEEPING_KEYS,
    FINISH_BATCH_MARKER as FINISH_BATCH_MARKER,
    ANSWER_FILES_FIELD as ANSWER_FILES_FIELD,
    ANSWER_FILES_LIMIT as ANSWER_FILES_LIMIT,
    ANSWER_FILES_MAX_BYTES as ANSWER_FILES_MAX_BYTES,
    AFFECTED_TESTS_TIMEOUT_SECONDS as AFFECTED_TESTS_TIMEOUT_SECONDS,
    AFFECTED_TEST_LIMIT as AFFECTED_TEST_LIMIT,
    _MUTATION_TOOLS as _MUTATION_TOOLS,
    REFUSAL_GUIDANCE_AFTER as REFUSAL_GUIDANCE_AFTER,
    REFUSAL_DISABLE_AFTER as REFUSAL_DISABLE_AFTER,
    TOOL_FAILURE_LIMITS as TOOL_FAILURE_LIMITS,
    _TOOL_ALTERNATIVE as _TOOL_ALTERNATIVE,
    BROWSER_CALLS_NOTICE as BROWSER_CALLS_NOTICE,
    BROWSER_CALLS_EXHAUSTED as BROWSER_CALLS_EXHAUSTED,
    BROWSER_INPUT_LIMIT as BROWSER_INPUT_LIMIT,
    BROWSER_PRESS_LIMIT as BROWSER_PRESS_LIMIT,
    COVERAGE_REFUSAL_LIMIT as COVERAGE_REFUSAL_LIMIT,
    LAYOUT_RECHECK_VIEWPORTS as LAYOUT_RECHECK_VIEWPORTS,
    LAYOUT_REFUSAL_LIMIT as LAYOUT_REFUSAL_LIMIT,
    LAYOUT_PROBE_JS as LAYOUT_PROBE_JS,
    COVERAGE_BROWSER_GRANT as COVERAGE_BROWSER_GRANT,
    COVERAGE_INPUT_GRANT as COVERAGE_INPUT_GRANT,
    _BROWSER_INPUT_ACTIONS as _BROWSER_INPUT_ACTIONS,
    TIME_LEFT_NOTICE_SECONDS as TIME_LEFT_NOTICE_SECONDS,
    _INTERACTION_TASK as _INTERACTION_TASK,
    RESET_IN_MOTION_HINT as RESET_IN_MOTION_HINT,
    RESEARCH_BUDGET_STEPS as RESEARCH_BUDGET_STEPS,
    RESEARCH_HARD_BUDGET_STEPS as RESEARCH_HARD_BUDGET_STEPS,
    _CHANGE_INTENT as _CHANGE_INTENT,
)

import hashlib
import json
import shlex
from typing import Any, Dict, Mapping
from .agent import AgentEventKind
from .core import ToolDefinition
from .engine.environments.local import clip_output
from .engine_tool_bodies import (  # noqa: E402,F401
    _FENCE_LANGUAGES as _FENCE_LANGUAGES,
    fenced_file as fenced_file,
    layout_problem as layout_problem,
    layout_refusal_message as layout_refusal_message,
    _counts_as_repository_change as _counts_as_repository_change,
    _REPO_DELETE_ACTIONS as _REPO_DELETE_ACTIONS,
    _patch_header_path as _patch_header_path,
    _pure_delete_targets as _pure_delete_targets,
    scratch_delete_paths as scratch_delete_paths,
    absent_delete_paths as absent_delete_paths,
    _requested_dry_run as _requested_dry_run,
    _EDIT_ALIASES as _EDIT_ALIASES,
    _normalize_edit_file as _normalize_edit_file,
    _normalize_repo_command as _normalize_repo_command,
    _batch_tail as _batch_tail,
    _stable_result as _stable_result,
    _progress_key as _progress_key,
    PYTEST_MISSING_HINT as PYTEST_MISSING_HINT,
    _PYTEST_MISSING as _PYTEST_MISSING,
    _pytest_missing as _pytest_missing,
    _read_lines_body as _read_lines_body,
    _visual_check_body as _visual_check_body,
    DIFF_ECHO_MAX_CHARS as DIFF_ECHO_MAX_CHARS,
    _HUNK as _HUNK,
    _compact_diff as _compact_diff,
    _mutation_body as _mutation_body,
    _checks_body as _checks_body,
    _failed_check_report as _failed_check_report,
)
from .security import redact


BATCHING_NOTICE_STREAK = 4
BATCHING_NOTICES = 3
#: Responses without an edit after a passing test run before the settle nudge.
SETTLE_NOTICE_STEPS = 4
SETTLE_NOTICES = 2
#: Commands run since the last code change at which the probe-streak notice fires.
PROBE_STREAK_AT = (12, 24)
#: Counts of one exact call at which the repeat notice names the loop.
REPEAT_NOTICE_AT = (3, 5, 8)


class EngineGuidanceMixin:
    """Methods composed into CoreToolEnvironment; see engine_tool_env.py."""

    def _fenced_command(self, action: Mapping[str, Any]) -> Dict[str, Any]:
        """A salvaged fenced command, matched fail-closed against approvals.

        karo-core port 5 kept runs alive by executing the command the model
        wrote in text. KaroX has no shell, so the only fenced text that can
        run is one that exactly matches a user-approved verification command;
        everything else is refused with the typed-tool guidance.
        """
        command = str(action.get("command", "")).strip()
        try:
            argv = shlex.split(command, posix=True)
        except ValueError:
            argv = []
        if argv:
            approved = self._core.verification_command_order or ()
            for rule in approved:
                expected = tuple(item for item in rule if item != "*")
                if expected and tuple(argv) == expected:
                    call_id = (
                        f"fenced-{hashlib.sha256(command.encode('utf-8')).hexdigest()[:16]}"
                    )
                    return self._execute(
                        {
                            "name": "checks_run",
                            "arguments": {"argv": list(argv)},
                            "tool_call_id": call_id,
                        },
                        requested_by="model",
                    )
        self._kernel._emit(
            AgentEventKind.WARNING,
            reason="fenced_command_refused",
            detail="KaroX executes typed tools, not shell text",
        )
        return self._refusal(
            action,
            "fenced_command_refused",
            "KaroX has no shell: reissue the action as a typed tool call "
            "(repo_write_file, checks_run, ...). Fenced shell text only runs "
            "when it exactly matches an approved verification command.",
        )

    def _research_notice(self, definition: ToolDefinition) -> str:
        """A direct "stop reading, change something" line after a long survey."""

        if definition.mutates or not self._research_applies:
            return ""
        elapsed = int(self._kernel._step) - int(self._last_mutation_step)
        for milestone in (RESEARCH_BUDGET_STEPS, RESEARCH_HARD_BUDGET_STEPS):
            if elapsed < milestone or milestone in self._research_notices:
                continue
            self._research_notices.add(milestone)
            if milestone == RESEARCH_BUDGET_STEPS:
                return (
                    f"Research budget: {milestone} model steps have passed without a "
                    "repository change. Stop collecting references: name the part of the "
                    "task the current code does not meet yet, measured the way the task "
                    "states it, make the smallest edit for it, then run the covering "
                    "check. More reading will not produce a different answer."
                )
            return (
                f"Research budget exhausted: {milestone} steps without a change. Make "
                "the edit now. Finish without one only if a probe that measures the "
                "task's goal the way the task states it shows it already holds. "
                "Further reading is not the task."
            )
        return ""

    def _interaction_hint(self, core_name: str) -> str:
        if core_name not in {"browser.command", "visual.check"}:
            return ""
        if (
            not self._interaction_task
            or self._browser_inputs
            or self._browser_presses
            or self._interaction_hint_sent
        ):
            return ""
        self._interaction_hint_sent = True
        return (
            "This task names a restart or hotspot. Frames cannot prove it: use "
            "browser.command click or press on the control, then read the state. "
            "The run cannot be verified without that input. " + RESET_IN_MOTION_HINT
        )

    def _browser_budget_exhausted(self, core_name: str, action: str = "") -> str:
        if core_name != "browser.command":
            return ""
        if action == "press":
            limit = BROWSER_PRESS_LIMIT + COVERAGE_INPUT_GRANT * self._coverage_refusals
            if self._browser_presses < limit:
                return ""
            return (
                f"browser key presses are disabled for the rest of this run after "
                f"{self._browser_presses} presses. Stop pressing, run the final "
                "check and visual review, and finish."
            )
        if action in _BROWSER_INPUT_ACTIONS:
            limit = BROWSER_INPUT_LIMIT + COVERAGE_INPUT_GRANT * self._coverage_refusals
            if self._browser_inputs < limit:
                return ""
            return (
                f"browser input is disabled for the rest of this run after "
                f"{self._browser_inputs} click/fill actions. Stop "
                "interacting, run the final check and visual review, and finish."
            )
        limit = BROWSER_CALLS_EXHAUSTED + COVERAGE_BROWSER_GRANT * self._coverage_refusals
        if self._browser_calls < limit:
            return ""
        return (
            f"browser observations are disabled for the rest of this run after "
            f"{self._browser_calls} calls. Stop opening, waiting and taking "
            "screenshots; click or press the named control if you still owe an "
            "interaction, then finish. visual.check remains available."
        )

    def _browser_notice(self) -> str:
        if self._browser_calls < BROWSER_CALLS_NOTICE or self._browser_notice_sent:
            return ""
        self._browser_notice_sent = True
        return (
            f"Browser budget: {self._browser_calls} calls used. Stop exploring: "
            "exercise only the interaction the task names, fix what the last "
            "probe showed, then run the visual check and finish. Browser calls "
            f"stop at {BROWSER_CALLS_EXHAUSTED}."
        )

    def _batching_notice(self, definition: ToolDefinition, action: Mapping[str, Any]) -> str:
        """A nudge after several responses that each made one read.

        Benchmark 2026-11-13 (click-3239 v39): 25 consecutive responses each
        ran one git or read call; every response re-sent ~60k tokens of
        context. Reads the model already knows it needs belong in one response.
        """

        step = int(getattr(self._kernel, "_step", 0) or 0)
        lone = not definition.mutates and int(action.get("batch_size") or 1) <= 1
        if not lone:
            self._lone_read_steps = []
            return ""
        steps = [n for n in getattr(self, "_lone_read_steps", []) if n != step] + [step]
        consecutive = all(b - a == 1 for a, b in zip(steps, steps[1:]))
        self._lone_read_steps = steps if consecutive else [step]
        sent = int(getattr(self, "_batching_notices", 0) or 0)
        if len(self._lone_read_steps) < BATCHING_NOTICE_STREAK or sent >= BATCHING_NOTICES:
            return ""
        self._batching_notices = sent + 1
        self._lone_read_steps = []
        return (
            f"Your last {BATCHING_NOTICE_STREAK} responses each made a single read, and "
            "every response re-sends the whole conversation. Put the reads, "
            "searches and probes you already know you need into one response."
        )

    def _settle_notice(self, core_name: str, arguments: Mapping[str, Any], data: Mapping[str, Any]) -> str:
        """A nudge to finish once tests pass and responses stop changing code.

        Benchmark 2026-11-13 (rebench26 sqlglot-7457, sqlfluff-7617, click-3239):
        after the covering tests passed, 9-14 more responses re-probed, re-read
        and re-linted without an edit -- a third of each run's output -- and the
        finish review then asked for the same checks again.
        """

        step = int(getattr(self._kernel, "_step", 0) or 0)
        seq = int(getattr(self, "_action_seq", 0) or 0)
        # exstruct-113 v52: a green baseline before any edit was read as "the
        # task is done" and the run finished with an unchanged tree.
        last_change = int(getattr(self, "_last_change_seq", 0) or 0)
        if not last_change or getattr(self, "_review_hold_seq", None) == last_change:
            return ""
        if seq and seq == int(getattr(self, "_last_change_seq", 0) or 0):
            self._green_step = None
            return ""
        argv = arguments.get("argv")
        command = " ".join(str(item) for item in argv) if isinstance(argv, list) else ""
        is_test = core_name in ("tests.run", "checks.run_affected") or (
            core_name == "checks.run" and ("pytest" in command or " test" in command)
        )
        exit_code = data.get("exit_code", data.get("returncode"))
        if is_test and exit_code == 0 and getattr(self, "_green_step", None) is None:
            self._green_step = step
            return ""
        green = getattr(self, "_green_step", None)
        sent = int(getattr(self, "_settle_notices", 0) or 0)
        if green is None or sent >= SETTLE_NOTICES or step - green < SETTLE_NOTICE_STEPS:
            return ""
        self._settle_notices = sent + 1
        self._green_step = step
        return (
            f"The tests passed {SETTLE_NOTICE_STEPS}+ responses ago and nothing has been "
            "edited since. Unless a behaviour the task states, a variation real "
            "use would bring (several items, other values), or a doubt you stated "
            "about the result (its type, shape or default use) is still unresolved, "
            "finish now: the finish review will name anything missing."
        )

    def _probe_streak_notice(self, core_name: str) -> str:
        """Name a long run of commands after an edit that changed nothing.

        TB 4.1 risk-scorer (v48 159 requests, v50 149 and the wall-time limit):
        after the fix, 100+ scratch scripts in /tmp re-derived what earlier
        ones had shown. The settle notice never fired: no pytest went green.
        """

        if core_name not in ("checks.run", "tests.run", "checks.run_affected"):
            return ""
        changed = int(getattr(self, "_last_change_seq", 0) or 0)
        if not changed:
            return ""
        if getattr(self, "_streak_base", None) != changed:
            self._streak_base, self._streak_runs = changed, 0
        self._streak_runs = int(getattr(self, "_streak_runs", 0) or 0) + 1
        if self._streak_runs not in PROBE_STREAK_AT:
            return ""
        return (
            f"You have run {self._streak_runs} commands since your last code change. "
            "Decide from what they already showed: edit the code if a probe found a "
            "defect or a doubt you stated is still open, otherwise finish -- the "
            "finish review names anything missing."
        )

    def _repeat_notice(self, core_name: str, arguments: Mapping[str, Any]) -> str:
        """Name a loop once the exact same call comes back 3, 5 and 8 times.

        DeepSeek Harness (repeat-tool-reminder) breaks Flash's loops this way:
        the call still runs, the model is only told it has seen this result.
        """

        # The same call after an edit is a new observation (scim2 duel1: a
        # re-run of an edited probe was told "its result will not change").
        changed = int(getattr(self, "_last_change_seq", 0) or 0)
        try:
            key = f"{changed}\x00{core_name}\x00" + json.dumps(arguments, sort_keys=True, default=str)
        except (TypeError, ValueError):
            return ""
        counts = getattr(self, "_repeat_counts", None)
        if counts is None:
            counts = self._repeat_counts = {}
        counts[key] = counts.get(key, 0) + 1
        if counts[key] not in REPEAT_NOTICE_AT:
            return ""
        return (
            f"You have made this exact {core_name} call {counts[key]} times. Its result "
            "will not change unless something else changes first. Use what you already "
            "have, or try a different approach."
        )

    def _time_left_notice(self) -> str:
        if self._time_notice_sent:
            return ""
        try:
            remaining = float(self._kernel.remaining_seconds())
        except Exception:  # noqa: BLE001 - a test double may not expose it
            return ""
        if remaining > TIME_LEFT_NOTICE_SECONDS:
            return ""
        self._time_notice_sent = True
        return (
            f"Only {int(remaining)} seconds remain. Stop exploring and editing "
            "broadly: fix a named defect if one is open, run the final check and "
            "visual review, and finish."
        )

    def _note_refusal(self, core_name: str, error_name: str) -> int:
        del error_name
        count = self._refusals.get(core_name, 0) + 1
        self._refusals[core_name] = count
        return count

    def _disabled_after_refusals(self, core_name: str) -> str:
        count = self._refusals.get(core_name, 0)
        limit = TOOL_FAILURE_LIMITS.get(core_name, REFUSAL_DISABLE_AFTER)
        if count < limit:
            return ""
        alternative = _TOOL_ALTERNATIVE.get(
            core_name, "stop retrying it and finish with the work already verified."
        )
        return (
            f"{core_name} is disabled for the rest of this run after {count} "
            f"consecutive refusals; further calls will not execute. {alternative}"
        )

    @staticmethod
    def _refusal_guidance(error_name: str, count: int) -> str:
        prefix = f"This is refusal {count} in a row for this tool. "
        if error_name == "ActionConfirmationRequired":
            body = (
                "The gate needs an explicit user decision and will keep refusing; "
                "no argument can approve it. Do not send confirmation fields. Leave "
                "the path as it is, use a non-destructive edit, or finish with the "
                "work already verified."
            )
        elif error_name == "InvalidCommand":
            body = (
                "The arguments were rejected before any change. Do not repeat this "
                "shape or invent fields: re-read the tool schema, send only its "
                "documented fields, and use another tool if this one cannot express "
                "the action."
            )
        else:
            body = (
                "The call was refused before any change. Do not repeat it unchanged; "
                "change the approach or finish with the work already verified."
            )
        return prefix + body

    def _refusal(self, action: Mapping[str, Any], reason: str, message: str) -> Dict[str, Any]:
        call_id = str(action.get("tool_call_id", "") or "")
        alias = str(action.get("name", "") or "")
        core_name = self._aliases.get(alias)
        if core_name is None and alias == "checks_run":
            core_name = "checks.run"
        self._kernel._persist_engine_tool_error(call_id, alias, core_name, reason, message)
        self._kernel._emit(
            AgentEventKind.WARNING,
            tool=core_name or alias or None,
            call_id=call_id or None,
            reason=reason,
            detail=str(redact(message))[:400],
        )
        return self._observation(message, 1, reason)

    def _observation(self, text: str, returncode: int, reason: str) -> Dict[str, Any]:
        clipped = clip_output(text, int(self.config.output_char_limit))
        return {
            "output": clipped,
            "observation": clipped,
            "returncode": returncode,
            "exception_info": "" if returncode == 0 else reason,
        }
