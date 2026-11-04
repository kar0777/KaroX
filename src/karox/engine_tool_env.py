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
from .models import CoreCommand, CoreResult, EvidenceRecord, Origin  # noqa: E402
from .security import redact  # noqa: E402
from .tool_display import call_detail, result_preview, result_summary  # noqa: E402
from .sessions import MutationLease  # noqa: E402
from .test_output import (  # noqa: E402
    CONDENSED_MAX_CHARS_WITH_MAP,
    condense_test_output,
)

if TYPE_CHECKING:
    from .engine_kernel import EngineKernel


_READABLE_TOOLS = frozenset({"repo.read_file", "repo.read_lines"})
_TEST_TOOLS = frozenset({"checks.run", "checks.run_affected", "tests.run"})
_PROOF_TOOLS = frozenset({"checks.run", "visual.check", "git.status", "git.diff"})
#: Runs whose verdict depends only on the repository bytes. On the W&B
#: benchmark about 28 of 92 check and test runs repeated an identical call that
#: had already passed on identical bytes; a SWE suite costs a minute each time.
_REUSABLE_RUNS = frozenset({"checks.run", "tests.run"})
#: Tools that cannot change what a check or test run would see. Anything else
#: (an edit, a shell command, a dev server, a browser action, a fetch) ends
#: every reuse: the next identical run executes again.
_RUN_NEUTRAL_TOOLS = frozenset(
    {
        "repo.read_file",
        "repo.read_lines",
        "repo.list_files",
        "repo.search",
        "repo.inspect",
        "git.status",
        "git.diff",
        "git.log",
        "lsp.diagnostics",
        "plan.update",
        "runtime.status",
        "skill.list",
        "skill.load",
        "skill.read",
        "checks.run",
        "tests.run",
        "visual.check",
        "disk.scan",
        "disk.plan_cleanup",
    }
)
#: File edits that report exactly what they wrote. One that failed or wrote
#: identical bytes changed nothing a run can see, so it does not end a reuse.
#: ``repo.command`` is not here: a failed command may still have written files.
_EXACT_EDIT_TOOLS = frozenset({"repo.edit_file", "repo.write_file", "repo.apply_patch"})
#: Proof bookkeeping, not an observation. ``completion_identity`` carries the
#: whole task text and every file hash; the completion gate reads it from the
#: durable result, so putting it in the model-facing body only makes every later
#: request re-send an 8 KB echo of the brief. Six visual checks paid about
#: 640k prompt tokens for that in the aquarium run.
_BOOKKEEPING_KEYS = frozenset({"completion_identity", "completion_target"})
#: Process bookkeeping that differs between two otherwise identical runs of
#: the same call. The model still sees it; the no-progress check compares
#: results without it, or a repeated `git diff` would never look repeated.
_VOLATILE_RESULT_KEYS = frozenset({"bridge_pid", "child_pid", "duration_ms", "elapsed_ms"})
#: A finish call found in a batch with other calls. It is rewritten to this
#: marker so the other calls still execute and every call gets its observation;
#: the model must then submit again with finish alone.
FINISH_BATCH_MARKER = "__karox_finish_not_alone__"
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
        "a whole file."
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
#: A web deliverable's finish must attest every requirement against results
#: observed after the last edit. A refused finish says which are open; after
#: this many refusals the submission is accepted and the completion floor
#: decides, so a model that cannot attest still ends -- unverified.
COVERAGE_REFUSAL_LIMIT = 4
#: Each refused finish reopens a little browser budget: the attestation asks
#: for fresh observations, and a run that spent its probes before its last
#: edit must still be able to produce them.
COVERAGE_BROWSER_GRANT = 6
COVERAGE_INPUT_GRANT = 3
_BROWSER_INPUT_ACTIONS = frozenset(
    {"click", "dblclick", "fill", "select", "type", "hover"}
)
#: Once this little wall time remains, every observation carries one line: stop
#: exploring and finish. The kernel also reserves this window for final proof.
TIME_LEFT_NOTICE_SECONDS = 300.0
#: The completion gate requires a real input for a task that names one of these
#: controls. The model must hear that before it spends the visual budget, not
#: only when it tries to finish.
_INTERACTION_TASK = re.compile(r"\b(?:restart|hotspot)\b", re.IGNORECASE)
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
#: What resets the research clock. Downloading an upstream reference file is
#: still research, not progress: in the fix-only run the model saved a dozen
#: `_tmp_*` copies and every save reset the budget, so the notice never fired
#: and the first real edit waited until step 26.
_CODE_CHANGE_TOOLS = frozenset(
    {"repo.edit_file", "repo.write_file", "repo.apply_patch", "repo.command"}
)


def _counts_as_repository_change(
    core_name: str, data: Mapping[str, Any], ok: bool
) -> bool:
    return (
        ok
        and core_name in _CODE_CHANGE_TOOLS
        and (data.get("changed") is True or bool(data.get("changed_files")))
    )


def _wrote_anything(data: Mapping[str, Any], ok: bool) -> bool:
    """Whether an exact edit tool reports any written byte (fail-safe: yes)."""

    if not ok:
        # Edits are atomic: a failed edit leaves the file as it was.
        return False
    if data.get("changed") is True or bool(data.get("changed_files")):
        return True
    # Only an explicit "nothing changed" counts as no write; a result that does
    # not say is treated as a write.
    return data.get("changed") is not False


def _run_key(core_name: str, arguments: Mapping[str, Any]) -> str:
    """The exact call a passed run answers; a timeout is not part of it."""

    material = {
        key: value for key, value in arguments.items() if key != "timeout_seconds"
    }
    try:
        encoded = json.dumps(material, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return ""
    return f"{core_name}\0{encoded}"


def _batch_tail(action: Mapping[str, Any] | None) -> bool:
    """True when this action ends a batched response (or carries no batch data)."""

    if not isinstance(action, Mapping):
        return True
    index = action.get("batch_index")
    size = action.get("batch_size")
    if isinstance(index, int) and isinstance(size, int) and not isinstance(size, bool):
        return size <= 1 or index == size - 1
    return True


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
        #: Passing check/test runs by their exact call, with the repository
        #: identity they ran on and the state epoch. Any call that may change
        #: what a run sees bumps the epoch and so ends every reuse.
        self._state_epoch = 0
        self._passed_runs: Dict[str, Dict[str, Any]] = {}
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
        if name is None:
            # An approved command run from text can write anything a check
            # would read; no earlier verdict survives it.
            self._state_epoch += 1
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
        display = core_name
        call_id = str(action.get("tool_call_id", "") or "")
        reuse_key = (
            _run_key(core_name, arguments)
            if core_name in _REUSABLE_RUNS and requested_by == "model"
            else ""
        )
        if reuse_key:
            reused = self._reuse_passed_run(
                reuse_key, core_name, str(name), call_id, call_detail(core_name, arguments)
            )
            if reused is not None:
                return reused
        epoch_entering = self._state_epoch
        if core_name not in _RUN_NEUTRAL_TOOLS:
            self._state_epoch += 1
        epoch_before = self._state_epoch
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
        run_identity_before = (
            proof_before
            if proof_before is not None or not reuse_key
            else completion_gate.identity(
                self._core, self._core.sessions.load(self._session_id)
            )
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
            if core_name in _EXACT_EDIT_TOOLS and self._state_epoch == epoch_before:
                # A refused edit wrote nothing.
                self._state_epoch = epoch_entering
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
        if _counts_as_repository_change(core_name, data, result.ok):
            self._last_mutation_step = self._kernel._step
        elif (
            core_name in _EXACT_EDIT_TOOLS
            and self._state_epoch == epoch_before
            and not _wrote_anything(data, result.ok)
        ):
            # A failed edit, or one that wrote the bytes already on disk.
            self._state_epoch = epoch_entering
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
        if reuse_key:
            self._remember_passed_run(
                reuse_key, result, run_identity_before, epoch_before, call_id
            )
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
        """A pass covers the current bytes and finish owes coverage; fail-closed."""

        if self._coverage_refusals >= COVERAGE_REFUSAL_LIMIT:
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
        if not answer.strip():
            return self._refusal(
                action,
                "malformed_arguments",
                'finish requires {"answer": "<final answer text>"}',
            )
        problems = self._coverage_problems(arguments)
        if problems:
            self._coverage_refusals += 1
            keep_open = getattr(self._kernel, "note_coverage_refusal", None)
            if callable(keep_open):
                keep_open()
            message = requirement_coverage.refusal_message(problems)
            reopened = self._browser_reopened_note()
            if reopened:
                message = f"{message}\n\n{reopened}"
            return self._refusal(action, "requirement_coverage", message)
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

        if self._coverage_refusals >= COVERAGE_REFUSAL_LIMIT:
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
            return requirement_coverage.judge(
                record,
                attestation,
                use_transcript=False,
                diagnostic=completion_gate.diagnostic_paths(self._core, record),
            )
        except Exception:  # noqa: BLE001 - the floor still judges the submission
            return []

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
            "The run cannot be verified without that input."
        )

    def _remember_passed_run(
        self,
        key: str,
        result: CoreResult,
        identity_before: Optional[Dict[str, Any]],
        epoch_before: int,
        call_id: str,
    ) -> None:
        data = result.data if isinstance(result.data, dict) else {}
        passed = (
            result.ok
            and data.get("exit_code") == 0
            and not data.get("timed_out")
        )
        if not passed or not identity_before or self._state_epoch != epoch_before:
            self._passed_runs.pop(key, None)
            return
        try:
            identity_after = completion_gate.identity(
                self._core, self._core.sessions.load(self._session_id)
            )
        except Exception:  # noqa: BLE001 - no identity means no reuse
            return
        if identity_after != identity_before:
            # Something changed while it ran: the verdict belongs to neither
            # state, so it cannot stand in for a later run.
            self._passed_runs.pop(key, None)
            return
        try:
            kept = CoreResult.from_dict(result.to_dict())
        except Exception:  # noqa: BLE001 - an uncopyable result is simply rerun
            self._passed_runs.pop(key, None)
            return
        self._passed_runs[key] = {
            "identity": identity_after,
            "epoch": epoch_before,
            "step": int(getattr(self._kernel, "_step", 0) or 0),
            "call_id": call_id,
            "result": kept,
        }

    def _reuse_passed_run(
        self, key: str, core_name: str, alias: str, call_id: str, detail: str = ""
    ) -> Optional[Dict[str, Any]]:
        """The earlier verdict of this exact call, when nothing has changed.

        Nothing changed means: no state-changing tool ran since (the epoch),
        the task is the same, and every file the earlier run was bound to has
        the same bytes now. Requiring the whole identity to be equal also
        refused a pass after a mere read: reading one more file grows the
        identity's read set without changing a byte the run executes. A failed
        or timed-out run is never reused: a retry there may be the point (a
        flaky test, a dependency installed in between).
        """

        entry = self._passed_runs.get(key)
        if entry is None or entry["epoch"] != self._state_epoch:
            return None
        try:
            current = completion_gate.identity(
                self._core, self._core.sessions.load(self._session_id)
            )
        except Exception:  # noqa: BLE001 - when in doubt, run it
            return None
        if not current or not completion_gate.same_run_inputs(
            self._core, entry["identity"], current
        ):
            return None
        prior: CoreResult = entry["result"]
        result = CoreResult.from_dict(prior.to_dict())
        result.data["reused_from"] = {
            "call_id": entry["call_id"],
            "step": entry["step"],
        }
        if "completion_identity" in result.data:
            # The epoch proves no tool changed a byte since the run, and every
            # file the run was bound to still hashes the same. Files read since
            # then (and a ledger change) widen the identity without changing
            # anything a check executes, so the verdict is re-bound to the
            # current identity instead of going stale on a mere read.
            result.data["completion_identity"] = current
            for item in result.evidence:
                item.metadata["completion_identity"] = current
        entry["identity"] = current
        step = entry["step"]
        text = (
            f"[{core_name} exit 0 · reused]\n"
            f"This exact call already passed at step {step} and nothing that a "
            "run can see has changed since, so it was not run again; that "
            "result stands. Run it again only after an edit."
        )
        output = {
            "output": text,
            "observation": text,
            "returncode": 0,
            "exception_info": "",
        }
        self._kernel._emit(
            AgentEventKind.TOOL_STARTED,
            tool=core_name,
            call_id=call_id or None,
            detail=detail or None,
        )
        self._kernel._persist_engine_tool_result(
            call_id=call_id,
            alias=alias,
            core_name=core_name,
            result=result,
            output=output,
            requested_by="model",
        )
        self._kernel._emit(
            AgentEventKind.TOOL_FINISHED,
            tool=core_name,
            call_id=call_id or None,
            ok=True,
            summary="reused: unchanged since step " + str(step),
            duration_seconds=0.0,
        )
        return output

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


def _stable_result(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _stable_result(item)
            for key, item in value.items()
            if key not in _VOLATILE_RESULT_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_stable_result(item) for item in value]
    return value


def _progress_key(core_name: str, ok: bool, data: Mapping[str, Any]) -> str:
    """A digest of what a result says, without pids, timings or evidence ids."""

    try:
        text = json.dumps(
            [core_name, bool(ok), _stable_result(dict(data))],
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
    except (TypeError, ValueError):
        return ""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


PYTEST_MISSING_HINT = (
    "pytest is not installed in this project's interpreter, so tests.run "
    "cannot run here. This is not a test failure. Use checks.run with the "
    "approved command (it uses the project's own runner) and do not retry "
    "tests.run."
)
_PYTEST_MISSING = re.compile(r"No module named '?pytest'?(?:\s|$)")


def _pytest_missing(data: Mapping[str, Any]) -> bool:
    for key in ("stderr", "stdout"):
        value = data.get(key)
        if isinstance(value, str) and _PYTEST_MISSING.search(value[-4_000:]):
            return True
    return False


def _read_lines_body(data: Mapping[str, Any]) -> str:
    """The window as plain lines, not a JSON envelope around a list.

    The tool already returns the window start and the total; re-encoding every
    line as a JSON string costs a quote and a comma per line, escapes the
    content, and hides the numbers the header already carries. Hashes and the
    secret flag stay in the durable result.
    """

    path = data.get("path")
    lines = data.get("lines")
    start = data.get("start")
    if not isinstance(lines, list) or not isinstance(start, int) or isinstance(start, bool):
        return json.dumps(data, ensure_ascii=False, sort_keys=True)
    total = data.get("total_lines")
    last = start + len(lines) - 1 if lines else start
    header = f"[repo.read_lines {path} {start}-{last}"
    if isinstance(total, int) and not isinstance(total, bool):
        header += f" of {total}"
    header += "]"
    body = "\n".join(str(line) for line in lines)
    text = f"{header}\n{body}" if body else header
    if data.get("has_more"):
        text += "\n…more lines below"
    return text


def _visual_check_body(data: Mapping[str, Any], ok: bool) -> str:
    """The verdict the model acts on, without the task echo.

    The durable result keeps ``completion_identity`` so the completion gate can
    bind the verdict to the bytes; this body keeps only what changes the next
    action: the verdict, the named issues, runtime errors, the resize verdict
    and where the frames are. The whole task text is already in the run's first
    message and does not belong in every later request.
    """

    verdict = str(data.get("verdict") or ("pass" if ok else "failed"))
    method = data.get("method")
    header = f"[visual.check {verdict}"
    if isinstance(method, str) and method:
        header += f" · {method}"
    header += "]"
    lines = [header]
    left = data.get("checks_left")
    if isinstance(left, int) and not isinstance(left, bool):
        lines.append(f"checks left: {left}")
    if verdict == "pass":
        # A pass is the end of the visual loop, not permission to keep
        # polishing. The 3d-viewer run saw a pass, then spent twenty more
        # steps on an unrequested pan-offset fix, invalidated the verdict and
        # hit the wall clock with a page the grader accepted.
        lines.append(
            "This pass covers the current bytes: run the final change review "
            "and finish. Do not edit to address the notes below; an edit "
            "invalidates this pass. Edit again only if you found a defect the "
            "checker could not see."
        )
    elif method == "limit":
        # The budget exists to stop polishing, not to make the model guess at
        # feedback it can no longer get. The aquarium run spent 26 more steps
        # editing after this verdict and broke a working resize.
        lines.append("No checks remain: stop changing the page and finish.")
    elif left == 0:
        lines.append("This was the last check: fix only what it lists, then finish.")
    issues = data.get("issues")
    if isinstance(issues, list) and issues:
        if verdict == "pass":
            lines.append(
                "optional cosmetic notes (do not edit for these; editing "
                "invalidates the pass):"
            )
        else:
            lines.append("issues:")
        lines.extend(f"- {str(item)[:200]}" for item in issues[:5])
    for label, key in (
        ("console errors", "console_errors"),
        ("page errors", "page_errors"),
    ):
        values = data.get(key)
        if isinstance(values, list) and values:
            lines.append(label + ": " + "; ".join(str(item)[:200] for item in values[:3]))
    resize = data.get("resize")
    if isinstance(resize, dict):
        lines.append(f"resize: {'ok' if resize.get('ok') else 'failed'}")
    frames = data.get("frames")
    if isinstance(frames, list) and frames:
        lines.append("frames: " + ", ".join(str(item) for item in frames[:2]))
    return "\n".join(lines)


def _mutation_body(core_name: str, data: Mapping[str, Any], ok: bool) -> str:
    """The small, decision-shaped view of a successful or failed mutation.

    The full result (hashes, previous hashes, secret flags, evidence ids) stays
    in the session record. This body is what the model reads and what every
    later request re-sends, so it keeps only the facts a next edit needs.
    """

    path = data.get("path")
    header = f"[{core_name} {path}]" if isinstance(path, str) and path else f"[{core_name}]"
    lines = [header]
    facts: List[str] = []
    if data.get("changed") is True:
        facts.append("changed")
    replacements = data.get("replacements")
    if isinstance(replacements, int) and not isinstance(replacements, bool) and replacements > 0:
        facts.append(f"{replacements} replacement(s)")
    changed_files = data.get("changed_files")
    if isinstance(changed_files, list) and changed_files:
        shown = [str(item) for item in changed_files[:8]]
        suffix = f" (+{len(changed_files) - 8})" if len(changed_files) > 8 else ""
        facts.append("files: " + ", ".join(shown) + suffix)
    size = data.get("bytes")
    if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
        facts.append(f"{size} bytes")
    if facts:
        lines.append(" · ".join(facts))
    diff = data.get("diff")
    if isinstance(diff, str) and diff.strip():
        lines.append(diff.strip())
    for key in ("quick_checks_line", "affected_tests_line"):
        line = data.get(key)
        if isinstance(line, str) and line.strip():
            lines.append(line.strip())
    if not ok:
        error = data.get("error") or data.get("detail") or data.get("message")
        if isinstance(error, dict):
            error = json.dumps(error, ensure_ascii=False, sort_keys=True)
        if isinstance(error, str) and error.strip():
            lines.append("error: " + error.strip()[:2_000])
        elif len(lines) == 1:
            lines.append("failed")
    return "\n".join(lines)


def _checks_body(data: Mapping[str, Any]) -> str:
    """What the model reads for one finished check.

    The failure map comes first: it is the part that survives clipping and it
    names the file and line a repair has to edit. The raw streams follow,
    condensed when long. stderr is appended whenever it spoke, because Jest and
    cargo report there; reading stdout alone made a red run look silent.
    """

    stdout = str(data.get("stdout") or "")
    stderr = str(data.get("stderr") or "")
    if stdout.strip():
        primary, secondary = stdout, stderr
    else:
        primary, secondary = stderr, ""
    text = primary
    rendered = render_failure_map(data.get("failure_map"))
    if rendered:
        # The map already names every failure, file and line. The raw log is
        # corroboration, not the primary signal, so keep a short slice instead
        # of the full condensed log on every later request.
        condensed = condense_test_output(
            text, max_chars=CONDENSED_MAX_CHARS_WITH_MAP, threshold=0
        )
    else:
        condensed = condense_test_output(text)
    if condensed is not None:
        text = str(condensed.get("text") or text)
    if secondary.strip():
        text = f"{text}\n[stderr]\n{secondary}" if text.strip() else secondary
    if rendered:
        text = f"{rendered}\n{text}" if text.strip() else rendered
    return text


def _failed_check_report(record: Any) -> Optional[Dict[str, Any]]:
    """The parsed failure map of the latest check-family call, if it failed.

    The latest call decides: a green check after a red one means the repair
    already landed, and an old map must not drive another advisory run.
    """

    for entry in reversed(record.provider_history):
        if entry.get("role") != "tool":
            continue
        if entry.get("core_name") not in {"checks.run", "tests.run"}:
            continue
        result = entry.get("result")
        if not isinstance(result, dict):
            continue
        data = result.get("data")
        if not isinstance(data, dict):
            continue
        passed = (
            result.get("ok") is True
            and data.get("exit_code") == 0
            and not data.get("timed_out")
        )
        if passed:
            return None
        report = data.get("failure_map")
        return report if isinstance(report, dict) and report.get("failures") else None
    return None


