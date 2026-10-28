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
import shlex
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional

# The engine prints a startup banner and loads a global dotenv on import. This
# module can be imported before the kernel that normally sets the guard, so it
# carries the same promise: a product import is silent and adopts no config.
os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")

from . import completion_gate  # noqa: E402
from .agent import AgentEventKind, _derived_tool_events  # noqa: E402
from .auto_verify import quick_checks, summarize as summarize_quick_checks  # noqa: E402
from .check_report import render_failure_map, select_test_targets, rerun_targets  # noqa: E402
from .core import CoreRuntime, ToolDefinition  # noqa: E402
from .engine.environments.local import clip_output  # noqa: E402
from .engine.exceptions import Submitted  # noqa: E402
from .engine_provider_bridge import FINISH_TOOL_NAME  # noqa: E402
from .models import CoreCommand, CoreResult, EvidenceRecord, Origin  # noqa: E402
from .security import redact  # noqa: E402
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
#: A repair edit is followed by exactly the tests that failed, once, inside a
#: budget that cannot turn one edit into a long detour.
AFFECTED_TESTS_TIMEOUT_SECONDS = 20.0
AFFECTED_TEST_LIMIT = 4
#: Tools whose result says "a file changed". The model needs the diff, the
#: quick-check verdict and the affected-test verdict; hashes, previous hashes,
#: secret flags and evidence ids stay in the durable result but do not ride in
#: every later request's history.
_MUTATION_TOOLS = {"repo.edit_file", "repo.write_file", "repo.apply_patch", "repo.command"}


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
        started = self._kernel.monotonic()
        self._kernel._emit(AgentEventKind.TOOL_STARTED, tool=display, call_id=call_id or None)
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
            summary=_result_summary(core_name, result),
            duration_seconds=round(max(0.0, self._kernel.monotonic() - started), 3),
        )
        if (
            core_name == "visual.check"
            and isinstance(result.data, dict)
            and result.data.get("method") == "limit"
        ):
            # No further verdict can arrive, so no further edit can make this
            # run verified. Stop at the next step boundary instead of burning
            # the step budget (26 and 20 wasted steps in the aquarium runs).
            self._kernel.note_visual_budget_exhausted()
        return output

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
        }


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
    if method == "limit":
        # The budget exists to stop polishing, not to make the model guess at
        # feedback it can no longer get. The aquarium run spent 26 more steps
        # editing after this verdict and broke a working resize.
        lines.append("No checks remain: stop changing the page and finish.")
    elif left == 0:
        lines.append("This was the last check: fix only what it lists, then finish.")
    issues = data.get("issues")
    if isinstance(issues, list) and issues:
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


def _result_summary(core_name: str, result: CoreResult) -> str:
    del core_name
    data = result.data if isinstance(result.data, dict) else {}
    parts: List[str] = []
    exit_code = data.get("exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        parts.append(f"exit {exit_code}")
    path = data.get("path")
    if isinstance(path, str) and path:
        parts.append(path)
    for key in ("matches", "files", "entries"):
        value = data.get(key)
        if isinstance(value, (list, tuple)):
            parts.append(f"{key}={len(value)}")
    if not parts:
        parts.append("ok" if result.ok else "failed")
    return str(redact(", ".join(parts)))
