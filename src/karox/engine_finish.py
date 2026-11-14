"""Turn finish, visual/coverage attestation and browser/layout evidence."""

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
from typing import Any, Dict, List, Mapping, Optional
from . import completion_gate
from . import goal_ledger, requirement_coverage
from .engine.exceptions import Submitted
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
from .models import CoreCommand


class EngineFinishMixin:
    """Methods composed into CoreToolEnvironment; see engine_tool_env.py."""

    def _visual_still_proven(self) -> bool:
        """Whether a clean visual verdict covers the current bytes; fail-closed."""

        try:
            record = self._core.sessions.load(self._session_id)
            return bool(completion_gate.visual_proven(self._core, record))
        except Exception:  # noqa: BLE001 - unknown means not proven
            return False

    def _attestation_owed(self) -> bool:
        """A pass covers the current bytes and finish owes coverage; fail-closed.

        With an advisory attestation (limit 0) the window still leaves room for
        the one finish that quotes results the run already has.
        """

        if COVERAGE_REFUSAL_LIMIT and self._coverage_refusals >= COVERAGE_REFUSAL_LIMIT:
            return False
        try:
            record = self._core.sessions.load(self._session_id)
            return bool(
                completion_gate.coverage_required(self._core, record)
                and completion_gate.visual_proven(self._core, record)
            )
        except Exception:  # noqa: BLE001 - unknown means nothing is owed
            return False

    def _self_review_owed(self) -> bool:
        """Once per run, when the run changed code (not only docs); fail-open."""

        if getattr(self, "_self_review_done", True):
            return False
        try:
            record = self._core.sessions.load(self._session_id)
            return any(not completion_gate.is_doc(p) for p in record.changed_files or ())
        except Exception:  # noqa: BLE001 - unknown means no extra turn
            return False

    def _finish(self, action: Mapping[str, Any]) -> Dict[str, Any]:
        arguments = action.get("arguments")
        answer = ""
        if isinstance(arguments, dict):
            answer = str(arguments.get("answer") or "")
        attached, attach_problem = self._answer_files(
            arguments.get(ANSWER_FILES_FIELD) if isinstance(arguments, dict) else None
        )
        if attach_problem:
            return self._refusal(action, "malformed_arguments", attach_problem)
        if not answer.strip() and not attached:
            return self._refusal(
                action,
                "malformed_arguments",
                'finish requires {"answer": "<final answer text>"}',
            )
        if attached:
            # The files are copied from disk, not regenerated: a reply that must
            # contain the deliverable gets its exact bytes at no output cost.
            answer = f"{answer.rstrip()}\n\n{attached}" if answer.strip() else attached
        # In the closing call the run is already stopped unverified; a refusal
        # could only cost the user the answer.
        goal_gap = [] if self._closing_turn() else self._goal_problems(arguments)
        if goal_gap:
            self._goal_refusals = int(getattr(self, "_goal_refusals", 0) or 0) + 1
            keep_open = getattr(self._kernel, "note_coverage_refusal", None)
            if callable(keep_open):
                keep_open()
            return self._refusal(action, "goal_evidence", goal_ledger.refusal(goal_gap))
        problems = [] if self._closing_turn() else self._coverage_problems(arguments)
        if problems:
            self._coverage_refusals += 1
            keep_open = getattr(self._kernel, "note_coverage_refusal", None)
            if callable(keep_open):
                keep_open()
            message = requirement_coverage.refusal_message(
                problems, getattr(self, "_coverage_hints", None)
            )
            reopened = self._browser_reopened_note()
            if reopened:
                message = f"{message}\n\n{reopened}"
            return self._refusal(action, "requirement_coverage", message)
        regressions = [] if self._closing_turn() else self._layout_regressions()
        if regressions:
            self._layout_refusals += 1
            keep_open = getattr(self._kernel, "note_coverage_refusal", None)
            if callable(keep_open):
                keep_open()
            return self._refusal(action, "layout_regression", layout_refusal_message(regressions))
        raise Submitted(
            {
                "role": "exit",
                "content": answer,
                "extra": {
                    "exit_status": "Submitted",
                    "exit_reason": "finish",
                    "submission": answer,
                },
            }
        )

    def _closing_turn(self) -> bool:
        active = getattr(self._kernel, "closing_turn_active", None)
        return bool(active()) if callable(active) else False

    def _answer_files(self, value: Any) -> tuple[str, str]:
        """Render ``answer_files`` as fenced blocks; returns (text, problem).

        A task that says "reply with the complete file in one code block" used
        to cost a second generation of the whole deliverable, and a run that
        ended without that turn shipped an empty reply (aquarium a1: a 41 KB
        page on disk, nothing in the answer). The runtime copies the bytes
        instead: they are exactly the file that was checked.
        """

        if value is None or value == []:
            return "", ""
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            return "", f"{ANSWER_FILES_FIELD} must be a list of repository-relative file paths"
        if len(value) > ANSWER_FILES_LIMIT:
            return "", f"{ANSWER_FILES_FIELD} takes at most {ANSWER_FILES_LIMIT} files"
        blocks: List[str] = []
        total = 0
        for relative in value:
            try:
                path = self._core.safe_path(relative)
            except Exception as exc:  # noqa: BLE001 - named back to the model
                return "", f"{ANSWER_FILES_FIELD}: {relative}: {exc}"
            if not path.is_file():
                return "", f"{ANSWER_FILES_FIELD}: {relative} is not a file in the repository"
            try:
                data = path.read_bytes()
            except OSError as exc:
                return "", f"{ANSWER_FILES_FIELD}: {relative} cannot be read: {exc}"
            total += len(data)
            if total > ANSWER_FILES_MAX_BYTES:
                return "", (
                    f"{ANSWER_FILES_FIELD}: the files total more than "
                    f"{ANSWER_FILES_MAX_BYTES // 1000} KB; attach fewer files"
                )
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                return "", f"{ANSWER_FILES_FIELD}: {relative} is not UTF-8 text"
            blocks.append(fenced_file(relative, text, label=len(value) > 1))
        return "\n\n".join(blocks), ""

    def _note_viewport(self, arguments: Mapping[str, Any], data: Mapping[str, Any]) -> None:
        """Remember which repository page was open at which size, and when."""

        action = str(arguments.get("action") or "")
        if action == "reload":
            if self._current_view is not None:
                self._viewports[self._current_view] = self._action_seq
            return
        if action != "open":
            return
        payload = arguments.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        page = payload.get("page")
        if not isinstance(page, str) or not page.strip():
            self._current_view = None
            return
        inner = data.get("data") if isinstance(data.get("data"), Mapping) else data
        viewport = inner.get("viewport") if isinstance(inner, Mapping) else None
        viewport = viewport if isinstance(viewport, Mapping) else {}
        try:
            width = int(viewport.get("width") or payload.get("width") or 0)
            height = int(viewport.get("height") or payload.get("height") or 0)
        except (TypeError, ValueError):
            return
        if width <= 0 or height <= 0:
            return
        key = (page.strip(), width, height)
        self._viewports[key] = self._action_seq
        self._current_view = key

    def _layout_regressions(self) -> List[str]:
        """Re-open pages checked before the last change; report sideways scroll.

        Only viewports the run itself opened are checked, so nothing here
        guesses at the task's sizes. Infrastructure trouble never blocks a
        finish: a failed open or read is skipped.
        """

        if int(getattr(self, "_layout_refusals", 0)) >= LAYOUT_REFUSAL_LIMIT:
            return []
        if "browser.command" not in getattr(self, "_definitions", {}):
            return []
        last_change = int(getattr(self, "_last_change_seq", 0))
        stale = [
            key
            for key, seq in sorted(
                getattr(self, "_viewports", {}).items(), key=lambda item: -item[1]
            )
            if seq < last_change
        ][:LAYOUT_RECHECK_VIEWPORTS]
        found: List[str] = []
        for page, width, height in stale:
            opened = self._internal_browser(
                {"action": "open", "payload": {"page": page, "width": width, "height": height}}
            )
            if opened is None:
                continue
            self._action_seq += 1
            self._viewports[(page, width, height)] = self._action_seq
            self._current_view = (page, width, height)
            measured = self._internal_browser(
                {"action": "evaluate", "payload": {"expression": LAYOUT_PROBE_JS}}
            )
            problem = layout_problem(page, width, height, measured)
            if problem:
                found.append(problem)
        return found

    def _internal_browser(self, arguments: Dict[str, Any]) -> Optional[Mapping[str, Any]]:
        """One browser action of KaroX's own check; None when it did not work."""

        try:
            remaining = max(0.1, float(self._kernel.remaining_seconds()))
            digest = hashlib.sha256(
                f"{self._session_id}\0layout\0{self._action_seq}\0{arguments.get('action')}".encode(
                    "utf-8"
                )
            ).hexdigest()
            command = CoreCommand(
                name="browser.command",
                arguments=arguments,
                session_id=self._session_id,
                origin=self._origin,
                correlation_id=f"agent-{digest[:32]}",
                idempotency_key=None,
                deadline_seconds=min(60.0, remaining),
            )
            result = self._core.execute(command, lease=None)
        except Exception:  # noqa: BLE001 - a failed check never blocks finish
            return None
        if not getattr(result, "ok", False) or not isinstance(result.data, Mapping):
            return None
        data = result.data
        return data.get("data") if isinstance(data.get("data"), Mapping) else data

    def _browser_reopened_note(self) -> str:
        """Say so when this refusal reopened a browser budget the model saw closed.

        Each refusal grants browser calls for the re-probe, but the model had
        been told the browser was "disabled for the rest of this run". A
        measured run (30/30 on the grader) never called the browser again: it
        wrote a harness page and spent its last visual checks running it.
        """

        refusals = int(getattr(self, "_coverage_refusals", 0))
        calls = int(getattr(self, "_browser_calls", 0))
        inputs = int(getattr(self, "_browser_inputs", 0))
        if (
            calls < BROWSER_CALLS_EXHAUSTED + COVERAGE_BROWSER_GRANT * (refusals - 1)
            and inputs < BROWSER_INPUT_LIMIT + COVERAGE_INPUT_GRANT * (refusals - 1)
        ):
            return ""
        calls_left = BROWSER_CALLS_EXHAUSTED + COVERAGE_BROWSER_GRANT * refusals - calls
        inputs_left = BROWSER_INPUT_LIMIT + COVERAGE_INPUT_GRANT * refusals - inputs
        return (
            f"browser_command is open again for this re-probe: {max(0, calls_left)} "
            f"more calls, {max(0, inputs_left)} more click/press/fill. Exercise the "
            "deliverable page itself and quote those results; a visual_check of a "
            "harness page spends the visual budget and proves nothing about the "
            "deliverable."
        )

    def _goal_problems(self, arguments: Any) -> List[str]:
        """Goals without a fresh output quote, once the review was asked (see goal_ledger)."""

        if getattr(self, "_review_hold_seq", None) is None:
            return []
        if int(getattr(self, "_goal_refusals", 0) or 0) >= goal_ledger.REFUSALS:
            return []
        try:
            record = self._core.sessions.load(self._session_id)
            if not any(not completion_gate.is_doc(p) for p in record.changed_files or ()):
                return []
            value = arguments.get(goal_ledger.FIELD) if isinstance(arguments, dict) else None
            return goal_ledger.problems(value, list(record.provider_history or ()))
        except Exception:  # noqa: BLE001 - the floor still judges the submission
            return []

    def _coverage_problems(self, arguments: Any) -> List[Any]:
        """Open requirements of this finish's attestation; [] lets it submit.

        Bounded: after ``COVERAGE_REFUSAL_LIMIT`` refusals the submission goes
        through and the completion floor reports what stayed open.
        """

        key_only = self._coverage_refusals >= COVERAGE_REFUSAL_LIMIT
        if key_only and self._coverage_refusals >= 1:
            return []
        try:
            record = self._core.sessions.load(self._session_id)
            if not completion_gate.coverage_required(self._core, record):
                return []
            attestation = (
                arguments.get(requirement_coverage.ATTESTATION_FIELD)
                if isinstance(arguments, dict)
                else None
            )
            diagnostic = completion_gate.diagnostic_paths(self._core, record)
            problems = requirement_coverage.judge(
                record,
                attestation,
                use_transcript=False,
                diagnostic=diagnostic,
            )
        except Exception:  # noqa: BLE001 - the floor still judges the submission
            return []
        if key_only:
            problems = [
                item for item in problems if requirement_coverage.is_key_problem(item[2])
            ]
            if not problems:
                self._coverage_hints = {}
                return []
        try:
            self._coverage_hints = requirement_coverage.closest_results(
                record, attestation, problems, diagnostic=diagnostic
            )
        except Exception:  # noqa: BLE001 - a hint is optional
            self._coverage_hints = {}
        return problems

    def _task_expects_change(self) -> bool:
        kernel = self._kernel
        if getattr(kernel, "maintenance_mode", False):
            return False
        if getattr(kernel, "mode", None) in {"plan", "ideate"}:
            return False
        if getattr(kernel, "require_change", False):
            return True
        try:
            task = self._core.sessions.load(self._session_id).task
        except Exception:  # noqa: BLE001 - a missing record means no nudge
            return False
        return bool(_CHANGE_INTENT.search(task or ""))
