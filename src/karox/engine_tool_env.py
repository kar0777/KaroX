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

import hashlib
import json
import os
import re
import shlex
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional

# The engine prints a startup banner and loads a global dotenv on import. This
# module can be imported before the kernel that normally sets the guard, so it
# carries the same promise: a product import is silent and adopts no config.
os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")

from . import completion_gate  # noqa: E402
from . import requirement_coverage  # noqa: E402
from .agent import AgentEventKind, _derived_tool_events  # noqa: E402
from .auto_verify import quick_checks, summarize as summarize_quick_checks  # noqa: E402
from .check_report import render_failure_map, select_test_targets, rerun_targets  # noqa: E402
from .core import CoreRuntime, ToolDefinition  # noqa: E402
from .engine.environments.local import clip_output  # noqa: E402
from .engine.exceptions import Submitted  # noqa: E402
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
from .models import CoreCommand, CoreResult, EvidenceRecord, Origin  # noqa: E402
from .security import redact  # noqa: E402
from .tool_display import call_detail, result_preview, result_summary  # noqa: E402
from .sessions import MutationLease  # noqa: E402

if TYPE_CHECKING:
    from .engine_kernel import EngineKernel


_READABLE_TOOLS = frozenset({"repo.read_file", "repo.read_lines"})
_TEST_TOOLS = frozenset({"checks.run", "checks.run_affected", "tests.run"})
_PROOF_TOOLS = frozenset({"checks.run", "visual.check", "git.status", "git.diff"})
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

#: Proof bookkeeping, not an observation. ``completion_identity`` carries the
#: whole task text and every file hash; the completion gate reads it from the
#: durable result, so putting it in the model-facing body only makes every later
#: request re-send an 8 KB echo of the brief. Six visual checks paid about
#: 640k prompt tokens for that in the aquarium run.
_BOOKKEEPING_KEYS = frozenset({"completion_identity", "completion_target"})
#: A finish call found in a batch with other calls. It is rewritten to this
#: marker so the other calls still execute and every call gets its observation;
#: the model must then submit again with finish alone.
FINISH_BATCH_MARKER = "__karox_finish_not_alone__"


#: finish argument naming files whose exact contents belong in the reply.
ANSWER_FILES_FIELD = "answer_files"
ANSWER_FILES_LIMIT = 8
ANSWER_FILES_MAX_BYTES = 1_000_000



#: A repair edit is followed by exactly the tests that failed, once, inside a
#: budget that cannot turn one edit into a long detour.
AFFECTED_TESTS_TIMEOUT_SECONDS = 20.0
AFFECTED_TEST_LIMIT = 4
#: Tools whose result says "a file changed". The model needs the diff, the
#: quick-check verdict and the affected-test verdict; hashes, previous hashes,
#: secret flags and evidence ids stay in the durable result but do not ride in
#: every later request's history.
_MUTATION_TOOLS = {"repo.edit_file", "repo.write_file", "repo.apply_patch", "repo.command"}
#: A refused call is not progress, and a model that keeps reshaping the same
#: rejected arguments spends a whole run on it: one benchmark run sent 37
#: repo.command variants and 2.03M tokens to delete two scratch files. After
#: this many consecutive refusals of one tool the observation starts telling the
#: model to stop; after the higher number the tool is disabled for the run.
REFUSAL_GUIDANCE_AFTER = 2
REFUSAL_DISABLE_AFTER = 6
#: Some tools are cheap to stop early: a patch that fails to parse twice will
#: not parse on the third try, and ``tests.run`` with a target that is not a
#: test file keeps the same bad path. In the sympy run these two shapes used
#: seven turns. The refusal message names the working alternative.
TOOL_FAILURE_LIMITS = {"repo.apply_patch": 2, "tests.run": 2}
_TOOL_ALTERNATIVE = {
    "repo.apply_patch": (
        "Use repo.edit_file with old_string/new_string, or repo.write_file for "
        "a whole file. To delete a file this session created, use repo_command "
        '{"action": "batch", "payload": {"operations": [{"op": "delete", '
        '"path": "<file>"}]}}.'
    ),
    "tests.run": (
        "Use checks.run with the approved command, or point targets at "
        "existing test files (test_*.py, *_test.py, or files under a tests/ "
        "directory)."
    ),
}
#: A browser can turn verification into an unbounded exploration: the two 3D
#: re-runs used 57 and 61 browser calls, fixed the actual defects, then hit the
#: wall clock without finishing their proof. Warn at the lower number, stop
#: accepting browser calls at the higher one. The model can still read the
#: repository and ask visual.check; it just cannot keep probing.
BROWSER_CALLS_NOTICE = 20
BROWSER_CALLS_EXHAUSTED = 36
#: Input actions stay available after the observation budget: the model may
#: spend 36 calls looking and only then reach the restart it must press. One
#: click is cheap; the churn was repeated open/screenshot/wait. A separate cap
#: keeps a click loop from replacing the observation loop.
BROWSER_INPUT_LIMIT = 12
#: Key presses have their own cap. A line that names keys is verified only by
#: pressing each of them on the deliverable after the last edit; a platformer's
#: controls name nine keys, so counting presses against the click cap (or the
#: observation budget) made honest verification of one line cost the whole
#: input budget. A press loop is still bounded.
BROWSER_PRESS_LIMIT = 30
#: A web deliverable's finish may attest each requirement against results
#: observed after the last edit. A finish is refused for a missing or stale
#: attestation at most this many times; after that the submission is accepted
#: and the completion floor reports every line left unattested, so the run
#: ends honestly unverified instead of buying quotes. Zero: the attestation is
#: advisory. In the diag1 sandbox round (2026-11-05) the refusals cost 57 model
#: steps across seven browser tasks -- the most expensive steps of each run,
#: at full context -- and no web deliverable changed after any of them.
COVERAGE_REFUSAL_LIMIT = 0

#: A page checked at a viewport and changed afterwards is checked again before
#: finish: at most this many of those viewports, most recent first, and the
#: finish is refused at most ``LAYOUT_REFUSAL_LIMIT`` times when one of them now
#: scrolls sideways. In the r2b sandbox round (2026-11-05) a game page fitted a
#: 390x844 window when the run measured it; a later sizing edit made it 678 px
#: wide there, the run re-checked only 1440x900, and the delivered page failed
#: its mobile layout requirement.
LAYOUT_RECHECK_VIEWPORTS = 3
LAYOUT_REFUSAL_LIMIT = 1
#: Page width and the element that sticks out furthest, read after layout and
#: any load-time sizing script have settled.
LAYOUT_PROBE_JS = """async () => {
  await new Promise((resolve) => setTimeout(resolve, 300));
  const root = document.documentElement;
  const width = window.innerWidth;
  const out = {scroll_width: root.scrollWidth, viewport_width: width};
  if (root.scrollWidth <= width + 1) return out;
  let worst = null;
  let right = width + 1;
  for (const el of document.querySelectorAll('body *')) {
    const box = el.getBoundingClientRect();
    if (box.width > 0 && box.right > right) { right = box.right; worst = el; }
  }
  if (worst) {
    const box = worst.getBoundingClientRect();
    let name = worst.tagName.toLowerCase();
    if (worst.id) name += '#' + worst.id;
    else if (worst.classList.length) name += '.' + Array.from(worst.classList).slice(0, 2).join('.');
    out.widest = {element: name, left: Math.round(box.left), right: Math.round(box.right), width: Math.round(box.width)};
  }
  return out;
}"""
#: Each refused finish reopens a little browser budget: the attestation asks
#: for fresh observations, and a run that spent its probes before its last
#: edit must still be able to produce them.
COVERAGE_BROWSER_GRANT = 6
COVERAGE_INPUT_GRANT = 3
_BROWSER_INPUT_ACTIONS = frozenset(
    {"click", "dblclick", "drag", "fill", "select", "type", "hover"}
)
#: Once this little wall time remains, every observation carries one line: stop
#: exploring and finish. The kernel also reserves this window for final proof.
TIME_LEFT_NOTICE_SECONDS = 300.0
#: The completion gate requires a real input for a task that names one of these
#: controls. The model must hear that before it spends the visual budget, not
#: only when it tries to finish.
_INTERACTION_TASK = re.compile(r"\b(?:restart|hotspot)\b", re.IGNORECASE)
#: A reset is exercised where it is hard: while something still moves. A
#: measured 3d-viewer run (f3) copied the camera home on reset but left the
#: orbit controls' damping running; the user's check clicked reset about two
#: seconds after an orbit, the camera kept drifting and the next hotspot never
#: held still, so its click failed. The model's own reset check ran in the
#: next model turn, about ten seconds after its drag, when the damping had died
#: out, and read a perfect initial state. The passing f2 page stopped all
#: motion on reset.
RESET_IN_MOTION_HINT = (
    "Test a reset or restart while something is still moving: drag or play, "
    "then click it in the same response and read the state at once -- "
    "momentum or damping that survives the reset carries the page away from "
    "its initial state."
)
#: A code task is not a reading exercise. On swe-requests-6028 the model spent
#: about two dozen model steps downloading and comparing three upstream
#: releases before its first edit; one such run cost 2.1M tokens with only one
#: failed tool. These milestones put a direct "stop reading, make the change"
#: line into the observation. They are advice, not a hard stop: a genuinely
#: unfamiliar repository can ignore the first one and keep going.
RESEARCH_BUDGET_STEPS = 12
RESEARCH_HARD_BUDGET_STEPS = 24
#: The budget only speaks to tasks whose deliverable is a change. A read-only
#: question may legitimately survey twenty files; telling it to edit would be
#: wrong. Plan/ideate and maintenance runs also never mutate.
_CHANGE_INTENT = re.compile(
    r"\b(?:build|create|implement|fix|bug|refactor|add|update|change|remove|"
    r"delete|migrate|rename|repair|patch)\b"
    r"|исправ|добав|созда|реализ|почини|переимен|обнови|удали",
    re.IGNORECASE,
)








#: Action names a model sends when it means "delete these files". The prompt
#: names the batch form, but repo.command is usually not advertised, so the
#: model writes it from memory: a sandbox pytest run (2026-11-04) sent {},
#: then action=delete, then a JSON-string payload -- three refused steps for
#: one scratch-file cleanup.












#: Spellings of one replacement a model uses at the top level because the
#: ``edits=[{old, new}]`` form names them so. Measured runs (2026-11-05) sent
#: {"old", "new"} and {"old", "new_string"} three times; each was refused as
#: unknown arguments and cost a step.








class CoreToolEnvironment:
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
                    "repository change. Stop collecting references: make the smallest "
                    "edit that addresses the public check, then run it. More reading "
                    "will not produce a different answer."
                )
            return (
                f"Research budget exhausted: {milestone} steps without a change. Make "
                "an edit now, or finish and say why no change is needed. Further "
                "reading is not the task."
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

    def _attach_quick_checks(self, result: CoreResult) -> None:
        """The harness's cheap per-file checks, inside the result the model reads.

        Same rule as the legacy kernel: a syntax check is a catch-now aid, not
        verification evidence.
        """
        if not result.ok or not isinstance(result.data, dict):
            return
        data = result.data
        if data.get("changed") is not True:
            return
        paths: List[str] = []
        path = data.get("path")
        if isinstance(path, str) and path:
            paths.append(path)
        else:
            changed = data.get("changed_files")
            if isinstance(changed, list):
                paths.extend(item for item in changed if isinstance(item, str) and item)
        if not paths:
            return
        try:
            checks = quick_checks(self._core.repository, paths)
        except Exception:  # noqa: BLE001 - a broken helper must not break the run
            return
        if not checks:
            return
        data["quick_checks"] = [check.to_dict() for check in checks]
        data["quick_checks_line"] = summarize_quick_checks(checks)

    def _attach_affected_tests(
        self, result: CoreResult, action: Mapping[str, Any] | None = None
    ) -> None:
        """Re-run the tests that failed, in the same result as the repair edit.

        In a repair loop the model's next turn is almost always "run the check
        again". The failing test files are already known from the parsed map,
        so the harness can answer that question before the model asks it. The
        run is advisory: it never counts as evidence, and only the approved
        command can discharge the floor.

        Only the last action of a batched response triggers the rerun: a
        three-edit batch would otherwise run the same suite three times and
        leave two stale results beside the current one.

        Everything here fails closed to silence: a missing runtime, a vanished
        test file or an exhausted budget leaves the edit result unchanged.
        """

        if not _batch_tail(action):
            return
        if "tests.run" not in self._definitions or not isinstance(result.data, dict):
            return
        data = result.data
        if data.get("changed") is not True:
            return
        try:
            record = self._core.sessions.load(self._session_id)
        except Exception:  # noqa: BLE001 - an advisory helper must not break a turn
            return
        report = _failed_check_report(record)
        if report is None:
            return
        targets = rerun_targets(report, AFFECTED_TEST_LIMIT)
        if not targets:
            return
        digest = str(data.get("content_sha256") or data.get("sha256") or "")
        key = (digest, tuple(targets))
        if key == self._affected_key:
            return
        self._affected_key = key
        material = f"{self._session_id}\0affected\0{digest}\0{'|'.join(targets)}"
        token = hashlib.sha256(material.encode("utf-8")).hexdigest()
        command = CoreCommand(
            name="tests.run",
            arguments={
                "suite": "focused",
                "targets": list(targets),
                "timeout_seconds": AFFECTED_TESTS_TIMEOUT_SECONDS,
            },
            session_id=self._session_id,
            origin=self._origin,
            correlation_id=f"agent-affected-{token[:24]}",
            idempotency_key=f"agent-affected-{token}",
            deadline_seconds=min(3600.0, max(0.1, self._kernel.remaining_seconds())),
        )
        try:
            outcome = self._core.execute(command, lease=self._lease)
        except Exception:  # noqa: BLE001 - a refused advisory run is not a failure
            return
        payload = outcome.data if isinstance(outcome.data, dict) else {}
        failure_map = payload.get("failure_map") or {}
        parsed = failure_map.get("total") if isinstance(failure_map, dict) else None
        data["affected_tests"] = {
            "advisory": True,
            "targets": list(targets),
            "ok": bool(outcome.ok),
            "exit_code": payload.get("exit_code"),
            "timed_out": payload.get("timed_out"),
            "failure_map": failure_map,
        }
        if payload.get("timed_out"):
            line = "affected tests: timed out (advisory, not evidence)"
        elif outcome.ok:
            line = (
                f"affected tests: passed ({len(targets)} target(s); "
                "advisory, not evidence)"
            )
        elif not isinstance(parsed, int) or isinstance(parsed, bool) or parsed <= 0:
            # A red exit code with no parsed failure is a runner problem (a
            # missing interpreter, a collection error), not a test verdict.
            # Calling it "failed" points the model at code that was never run.
            data["affected_tests"]["unavailable"] = True
            line = "affected tests: could not run (advisory, not evidence)"
        else:
            first = ""
            for candidate in render_failure_map(failure_map).splitlines()[1:]:
                first = candidate
                break
            line = (
                f"affected tests: failed — {first or 'see the map'} "
                "(advisory, not evidence)"
            )
        data["affected_tests_line"] = line

    def _render_output(self, core_name: str, result: CoreResult) -> Dict[str, Any]:
        data = result.data if isinstance(result.data, dict) else {}
        returncode = 0 if result.ok else 1
        if core_name in {"checks.run", "tests.run"}:
            # The fast focused-test path deserves the same readable body as an
            # approved check: file:line of each failure, then the raw log. It
            # stays non-evidence; only checks.run can satisfy the floor.
            text = _checks_body(data)
            exit_code = data.get("exit_code")
            detail = f"exit {exit_code}" if isinstance(exit_code, int) else "finished"
            if data.get("timed_out"):
                detail = "timed out"
            if data.get("probe") is True:
                detail += " · probe (not verification evidence)"
            if core_name == "tests.run":
                suite = data.get("suite")
                count = data.get("target_count")
                if isinstance(suite, str):
                    detail += f" · {suite}"
                if isinstance(count, int) and not isinstance(count, bool):
                    detail += f" · {count} target(s)"
            text = f"[{core_name} {detail}]\n{text}".strip()
            if isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0:
                returncode = exit_code if exit_code > 0 else 1
                if core_name == "tests.run" and _pytest_missing(data):
                    # The django venv has no pytest: every tests.run there is
                    # the same red "No module named pytest", and a model that
                    # reads it as a test verdict keeps retrying.
                    text = f"{PYTEST_MISSING_HINT}\n{text}"
        elif core_name == "repo.read_lines" and result.ok:
            text = _read_lines_body(data)
        elif core_name in _READABLE_TOOLS and result.ok:
            path = data.get("path")
            content = data.get("content")
            header = f"[{core_name} {path}]" if isinstance(path, str) else f"[{core_name}]"
            text = (
                f"{header}\n{content}"
                if isinstance(content, str)
                else json.dumps(data, ensure_ascii=False, sort_keys=True)
            )
        elif core_name in _MUTATION_TOOLS:
            text = _mutation_body(core_name, data, result.ok)
        elif core_name == "visual.check":
            text = _visual_check_body(data, result.ok)
        else:
            visible = {
                key: value
                for key, value in data.items()
                if key not in _BOOKKEEPING_KEYS
            }
            text = json.dumps(
                {
                    "ok": result.ok,
                    "data": visible,
                    "evidence": [item.evidence_id for item in result.evidence],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            line = data.get("affected_tests_line")
            if isinstance(line, str) and line:
                # The verdict of the tests that failed goes before the JSON:
                # it is the one fact that decides the next edit.
                text = f"{line}\n{text}"
        clipped = clip_output(text, int(self.config.output_char_limit))
        return {
            "output": clipped,
            "observation": clipped,
            "returncode": returncode,
            "exception_info": "",
            "progress_key": _progress_key(core_name, result.ok, data),
        }














#: A diff longer than this is summarized in the model's view. The model wrote
#: every changed line itself one message earlier; echoing a 3.6 KB diff back
#: doubled what each edit cost on every later request (eng-csv-report: 15
#: mutations, about a fifth of the final context), while the line numbers are
#: the only part a next edit uses.








