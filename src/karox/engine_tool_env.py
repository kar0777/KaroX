"""The engine's Environment protocol on KaroX's typed Core tools.

Every action the vendored kernel takes becomes one audited Core command:
capability policy and the risk engine gate it, mutations take the session lease
and an idempotency key, results are clipped to the engine's output ceiling and
persisted with their evidence. ``finish`` is the engine's submission marker; a
bare fenced command only ever runs when it exactly matches an approved
verification command, and is otherwise refused with guidance.

This module holds the action translation only. The session lease, durable
checkpoints, cancellation and the evidence-chain verification stay in
:mod:`karox.engine_kernel`.
"""

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

from .engine_finish import EngineFinishMixin
from .engine_guidance import EngineGuidanceMixin
from .engine_attach import EngineAttachMixin

import hashlib
import os
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional

# The engine prints a startup banner and loads a global dotenv on import. This
# module can be imported before the kernel that normally sets the guard, so it
# carries the same promise: a product import is silent and adopts no config.
os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")

from . import completion_gate  # noqa: E402
from .agent import AgentEventKind, _derived_tool_events  # noqa: E402
from .check_report import select_test_targets  # noqa: E402
from .core import CoreRuntime, ToolDefinition  # noqa: E402
from .engine.environments.local import clip_output  # noqa: E402
from .engine_provider_bridge import FINISH_TOOL_NAME  # noqa: E402
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
from .models import CoreCommand, EvidenceRecord, Origin  # noqa: E402
from .security import redact  # noqa: E402
from .tool_display import call_detail, result_preview, result_summary  # noqa: E402
from .sessions import MutationLease  # noqa: E402

if TYPE_CHECKING:
    from .engine_kernel import EngineKernel


# Every check and test run executes. There is no cache of earlier verdicts.
#
# A ``checks.run`` command -- an approved literal argv, a wildcard-admitted
# inline snippet or a reproduction probe -- and every ``tests.run`` execute code
# the repository controls: a pytest run imports its ``conftest.py``, a probe
# runs any snippet, a project test writes whatever it likes. That code can write
# files no ``changed_files`` entry and no ``completion_gate.identity`` covers: a
# fixture, a lock or state file, an ignored dependency. The identity compares
# only the files KaroX knows about, so equal identities are not evidence that an
# earlier run left the tree alone, and a verdict produced before that run's own
# writes cannot stand in for a later execution.
#
# A cached verdict is therefore unsound on every one of those surfaces. The
# price of dropping the cache is measured in
# ``docs/audits/DSH_REUSE_INTEGRITY_20261106.md``: repeated check and test calls
# run again (wall clock only), and no provider request is added.















#: Action names a model sends when it means "delete these files". The prompt
#: names the batch form, but repo.command is usually not advertised, so the
#: model writes it from memory: a sandbox pytest run (2026-11-04) sent {},
#: then action=delete, then a JSON-string payload -- three refused steps for
#: one scratch-file cleanup.












#: Spellings of one replacement a model uses at the top level because the
#: ``edits=[{old, new}]`` form names them so. Measured runs (2026-11-05) sent
#: {"old", "new"} and {"old", "new_string"} three times; each was refused as
#: unknown arguments and cost a step.








class CoreToolEnvironment(EngineFinishMixin, EngineGuidanceMixin, EngineAttachMixin):
    """The engine's Environment protocol on CoreRuntime's typed tools.

    Every engine action becomes one audited Core command: capability policy
    and the risk engine gate it, mutations take the session lease and an
    idempotency key, results are clipped to the engine's output ceiling and
    persisted with their evidence. ``finish`` is the submission marker; a bare
    fenced command only ever runs when it exactly matches an approved
    verification command, and is otherwise refused with guidance.
    """

    #: Finish submissions refused for missing requirement attestations. The
    #: class default keeps environments built without ``__init__`` (tests,
    #: adapters) on the unextended browser budget.
    _coverage_refusals: int = 0
    _browser_presses: int = 0

    def __init__(
        self,
        *,
        core: CoreRuntime,
        session_id: str,
        origin: Origin,
        lease: MutationLease,
        kernel: "EngineKernel",
        output_char_limit: int,
        command_timeout: int,
    ) -> None:
        self._core = core
        self._session_id = session_id
        self._origin = origin
        self._lease = lease
        self._kernel = kernel
        self.config = SimpleNamespace(
            cwd=str(core.repository),
            timeout=command_timeout,
            output_char_limit=output_char_limit,
            env={},
        )
        self._aliases: Dict[str, str] = kernel.engine_tool_aliases
        self._definitions: Dict[str, ToolDefinition] = {
            definition.name: definition for definition in core.tools()
        }
        # The last (content digest, targets) pair already auto-run after an
        # edit, so a retried call does not pay for the same test run twice.
        self._affected_key: Optional[tuple[str, tuple[str, ...]]] = None
        #: Consecutive refusals per core tool. A success from the same tool resets
        #: it; other tools do not, because the pathological loop interleaves
        #: reads and edits between refusals. Alternating error types still counts:
        #: the model is guessing, not making progress.
        self._refusals: Dict[str, int] = {}
        #: Step of the last repository-changing tool result, and the research
        #: milestones already delivered in this run.
        self._last_mutation_step = 0
        self._research_notices: set[int] = set()
        #: A code task gets the research budget; a question or a plan does not.
        self._research_applies = self._task_expects_change()
        #: The task names a control whose state a screenshot cannot prove.
        try:
            self._interaction_task = bool(
                _INTERACTION_TASK.search(
                    self._core.sessions.load(self._session_id).task or ""
                )
            )
        except Exception:  # noqa: BLE001 - a missing record means no hint
            self._interaction_task = False
        self._interaction_hint_sent = False
        #: Browser calls are counted across the run; a page can be probed
        #: forever without this. The notice and the hard stop are separate so
        #: the model gets one clear chance to finish before the tool closes.
        self._browser_calls = 0
        self._browser_inputs = 0
        self._browser_notice_sent = False
        self._coverage_refusals = 0
        self._time_notice_sent = False
        #: Order of tool results, the last repository change, and when each
        #: (page, width, height) was last opened: a viewport opened before the
        #: last change is re-checked before finish.
        self._action_seq = 0
        self._last_change_seq = 0
        self._viewports: Dict[tuple, int] = {}
        self._current_view: Optional[tuple] = None
        self._layout_refusals = 0
        self._self_review_done = not getattr(kernel, "self_review", False)

    # -- Environment protocol ----------------------------------------------

    def execute(
        self, action: Dict[str, Any], cwd: str = "", *, timeout: Optional[int] = None
    ) -> Dict[str, Any]:
        del cwd, timeout
        return self._execute(action, requested_by="model")

    def get_template_vars(self, **kwargs: Any) -> Dict[str, Any]:
        return {
            "cwd": self.config.cwd,
            "timeout": self.config.timeout,
            "output_char_limit": self.config.output_char_limit,
            **kwargs,
        }

    def serialize(self) -> Dict[str, Any]:
        return {
            "info": {
                "config": {
                    "environment": {
                        "environment_type": f"{type(self).__module__}.{type(self).__name__}",
                        "cwd": self.config.cwd,
                        "output_char_limit": self.config.output_char_limit,
                    }
                }
            }
        }

    # -- internals ---------------------------------------------------------

    def _execute(self, action: Mapping[str, Any], *, requested_by: str) -> Dict[str, Any]:
        # A call the bridge could not parse rides in a batch as an observation,
        # so the calls beside it still run and the model sees the exact error
        # against the call id that caused it.
        parse_error = action.get("parse_error")
        if parse_error:
            return self._refusal(action, "malformed_arguments", str(parse_error))
        name = action.get("name")
        if name == FINISH_BATCH_MARKER:
            return self._refusal(
                action,
                "finish_not_alone",
                "finish must be the only tool call in a response. The other "
                "calls in this batch ran; review their results, then call "
                "finish again. A submission batched with work would have "
                "dropped that work and reported a change that never happened.",
            )
        if name == FINISH_TOOL_NAME:
            return self._finish(action)
        if requested_by == "model" and self._closing_turn():
            return self._refusal(
                action,
                "closing_turn",
                "The work phase is over: nothing can judge another edit or check "
                "now. Call finish alone with your final answer.",
            )
        if name is None:
            return self._fenced_command(action)
        core_name = self._aliases.get(str(name))
        if core_name is None:
            return self._refusal(action, "unknown_tool", f"tool '{name}' is not available")
        arguments = action.get("arguments")
        if not isinstance(arguments, dict):
            return self._refusal(action, "malformed_arguments", "tool arguments must be an object")
        definition = self._definitions.get(core_name)
        if definition is None:
            return self._refusal(action, "unknown_tool", f"tool '{name}' is not available")
        if core_name == "repo.apply_patch" and "repo.command" in self._definitions:
            try:
                created = self._core.sessions.load(self._session_id).created_files
            except Exception:  # noqa: BLE001 - no record means no rewrite
                created = ()
            doomed = scratch_delete_paths(arguments, created)
            if doomed:
                core_name = "repo.command"
                definition = self._definitions[core_name]
                batch: Dict[str, Any] = {
                    "operations": [{"op": "delete", "path": path} for path in doomed]
                }
                # A preview stays a preview: dropping the flag here once
                # deleted a probe the model meant only to check, and its
                # real delete a step later was refused as a stranger's file.
                if _requested_dry_run(arguments):
                    batch["dry_run"] = True
                arguments = {"action": "batch", "payload": batch}
            else:
                gone = absent_delete_paths(arguments, getattr(self._core, "repository", None))
                if gone:
                    return self._observation(
                        "nothing to delete: "
                        + ", ".join(gone)
                        + " is not in the working tree (an earlier delete already removed it)",
                        0,
                        "",
                    )
        disabled = self._disabled_after_refusals(core_name)
        if disabled:
            return self._refusal(action, "tool_disabled_after_refusals", disabled)
        browser_disabled = self._browser_budget_exhausted(
            core_name, str(arguments.get("action") or "")
        )
        if browser_disabled:
            return self._refusal(action, "browser_budget_exhausted", browser_disabled)
        # The session already knows which files it changed, so a focused run
        # can name its own targets instead of refusing the call and spending a
        # whole model turn on "which test file is this?". The derived list is
        # ordinary targets: the worker validates every path again.
        derived_targets: List[str] = []
        if core_name == "tests.run" and not arguments.get("targets"):
            suite = arguments.get("suite", "focused")
            if suite == "focused":
                record = self._core.sessions.load(self._session_id)
                derived_targets = select_test_targets(
                    self._core.repository, record.changed_files
                )
                if derived_targets:
                    arguments = dict(arguments)
                    arguments["targets"] = derived_targets
        if core_name == "repo.command":
            arguments = _normalize_repo_command(arguments)
        if core_name == "repo.edit_file":
            arguments = _normalize_edit_file(arguments)
        display = core_name
        call_id = str(action.get("tool_call_id", "") or "")
        started = self._kernel.monotonic()
        self._kernel._emit(
            AgentEventKind.TOOL_STARTED,
            tool=display,
            call_id=call_id or None,
            detail=call_detail(core_name, arguments) or None,
        )
        if core_name in _TEST_TOOLS:
            self._kernel._emit(AgentEventKind.TEST_STARTED, tool=display, call_id=call_id or None)
        remaining = max(0.1, self._kernel.remaining_seconds())
        identity = f"{self._session_id}\0{call_id}\0{core_name}".encode("utf-8")
        digest = hashlib.sha256(identity).hexdigest()
        command = CoreCommand(
            name=core_name,
            arguments=dict(arguments),
            session_id=self._session_id,
            origin=self._origin,
            correlation_id=f"agent-{digest[:32]}",
            idempotency_key=f"agent-{digest}" if definition.mutates else None,
            deadline_seconds=min(3600.0, remaining),
        )
        proof_before = (
            completion_gate.identity(self._core, self._core.sessions.load(self._session_id))
            if core_name in _PROOF_TOOLS
            else None
        )
        try:
            result = self._core.execute(
                command, lease=self._lease if definition.mutates else None
            )
        except Exception as exc:  # noqa: BLE001 - a refusal is an observation
            message = f"{type(exc).__name__}: {redact(str(exc))}"
            refusals = self._note_refusal(core_name, type(exc).__name__)
            if refusals >= REFUSAL_GUIDANCE_AFTER:
                message = f"{message}\n\n{self._refusal_guidance(type(exc).__name__, refusals)}"
            self._kernel._persist_engine_tool_error(
                call_id, str(name), core_name, type(exc).__name__, message
            )
            self._kernel._emit(
                AgentEventKind.ERROR,
                tool=display,
                call_id=call_id or None,
                reason=type(exc).__name__,
                detail=str(redact(str(exc)))[:400],
            )
            self._kernel._emit(
                AgentEventKind.TOOL_FINISHED,
                tool=display,
                call_id=call_id or None,
                ok=False,
                summary=type(exc).__name__,
                duration_seconds=round(max(0.0, self._kernel.monotonic() - started), 3),
            )
            return self._observation(message, 1, "refused")
        self._refusals.pop(core_name, None)
        if core_name == "browser.command":
            browser_action = str(arguments.get("action") or "")
            if browser_action == "press":
                self._browser_presses += 1
            else:
                self._browser_calls += 1
                if browser_action in _BROWSER_INPUT_ACTIONS:
                    self._browser_inputs += 1
        data = result.data if isinstance(result.data, dict) else {}
        self._action_seq = int(getattr(self, "_action_seq", 0)) + 1
        if core_name == "browser.command" and result.ok:
            self._note_viewport(arguments, data)
        if _counts_as_repository_change(core_name, data, result.ok):
            self._last_mutation_step = self._kernel._step
            self._last_change_seq = self._action_seq
        if core_name == "tests.run" and isinstance(result.data, dict):
            # Say where the target list came from: a reader of the session must
            # be able to tell a model's choice from the harness's derivation.
            result.data["targets_source"] = (
                "session_changes"
                if derived_targets
                else ("request" if arguments.get("targets") else "none")
            )
        if proof_before is not None:
            proof_after = completion_gate.identity(
                self._core, self._core.sessions.load(self._session_id)
            )
            proof = proof_before if proof_before == proof_after else {}
            result.data["completion_identity"] = proof
            result.data["completion_target"] = arguments.get("path", "")
            if not result.evidence:
                result.evidence.append(
                    EvidenceRecord(kind="completion_observation", summary=core_name)
                )
            for item in result.evidence:
                item.metadata["completion_identity"] = proof
        self._attach_quick_checks(result)
        self._attach_affected_tests(result, action)
        output = self._render_output(core_name, result)
        notices = (
            self._research_notice(definition),
            self._browser_notice(),
            self._time_left_notice(),
            self._interaction_hint(core_name),
        )
        notice = "\n\n".join(item for item in notices if item)
        if notice:
            combined = f"{notice}\n\n{output['output']}"
            output["output"] = clip_output(combined, int(self.config.output_char_limit))
            output["observation"] = output["output"]
        self._kernel._persist_engine_tool_result(
            call_id=call_id,
            alias=str(name),
            core_name=core_name,
            result=result,
            output=output,
            requested_by=requested_by,
        )
        for kind, fields in _derived_tool_events(core_name, result):
            self._kernel._emit(kind, **fields)
        if core_name in _TEST_TOOLS:
            exit_code = result.data.get("exit_code")
            self._kernel._emit(
                AgentEventKind.TEST_FINISHED,
                tool=display,
                call_id=call_id or None,
                ok=result.ok,
                detail=(
                    f"exit {exit_code}"
                    if isinstance(exit_code, int) and not isinstance(exit_code, bool)
                    else None
                ),
            )
        self._kernel._emit(
            AgentEventKind.TOOL_FINISHED,
            tool=display,
            call_id=call_id or None,
            ok=result.ok,
            summary=result_summary(core_name, data, result.ok, arguments),
            detail=result_preview(core_name, data, result.ok, arguments) or None,
            duration_seconds=round(max(0.0, self._kernel.monotonic() - started), 3),
        )
        if core_name == "visual.check" and isinstance(result.data, dict):
            if result.data.get("method") == "limit":
                # No further verdict can arrive, so no further edit can make
                # this run verified. Stop at the next step boundary instead of
                # burning the step budget (26 and 20 wasted steps in the first
                # aquarium runs) -- unless a pass still covers the current
                # bytes: then the run can finish verified without an edit.
                self._kernel.note_visual_limit(proven=self._visual_still_proven())
            elif result.data.get("checks_left") == 0:
                # The last real verdict. Allow the edit turn and the closing
                # look, then stop; otherwise the model edits to the step limit.
                # A pass that still covers a deliverable owing an attestation
                # also leaves room to probe and attest.
                self._kernel.note_visual_last_check(attest=self._attestation_owed())
        return output







































#: A diff longer than this is summarized in the model's view. The model wrote
#: every changed line itself one message earlier; echoing a 3.6 KB diff back
#: doubled what each edit cost on every later request (eng-csv-report: 15
#: mutations, about a fifth of the final context), while the line numbers are
#: the only part a next edit uses.






