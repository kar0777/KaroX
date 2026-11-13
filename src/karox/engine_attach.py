"""Quick-check/affected-test attachment and rendered output."""

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
from typing import Any, Dict, List, Mapping
from .auto_verify import quick_checks, summarize as summarize_quick_checks
from .check_report import render_failure_map, rerun_targets
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
    _plain_body as _plain_body,
    _failed_check_report as _failed_check_report,
)
from .models import CoreCommand, CoreResult


class EngineAttachMixin:
    """Methods composed into CoreToolEnvironment; see engine_tool_env.py."""

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

        arguments = (action or {}).get("arguments") if isinstance(action, Mapping) else None
        if isinstance(arguments, Mapping) and str(arguments.get("path") or "").endswith(".py"):
            pieces = [arguments.get(k) for k in ("new_string", "content", "new")]
            pieces += [e.get("new") for e in arguments.get("edits") or () if isinstance(e, Mapping)]
            text = "\n".join(p for p in pieces if isinstance(p, str))
            if text:
                # The code just written names what its tests use.
                self._code_hint = (text + "\n" + str(getattr(self, "_code_hint", "")))[:20_000]
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
        if report is not None:
            targets = rerun_targets(report, AFFECTED_TEST_LIMIT)
        else:
            # No failing check yet: run the tests that belong to the edited
            # source, so the edit result already says whether they still pass.
            from . import task_dossier

            sources = [
                p for p in dict.fromkeys(getattr(record, "changed_files", ()) or ())
                if isinstance(p, str) and p.endswith(".py")
            ][-3:]
            try:
                targets = task_dossier.related_tests(self._core, sources, getattr(self, "_code_hint", ""))
            except Exception:  # noqa: BLE001 - advisory only
                targets = []
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
            from . import check_baseline

            passed = check_baseline.of_result(payload).passed
            count = f"{passed} passed" if passed else "passed"
            line = (
                f"affected tests: KaroX ran {', '.join(targets)} on the edited code: {count}. "
                "Do not run these again until you change code; run other tests only "
                "if the change reaches beyond them."
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
                suite, count = data.get("suite"), data.get("target_count")
                detail += (f" · {suite}" if isinstance(suite, str) else "") + (
                    f" · {count} target(s)" if isinstance(count, int) and not isinstance(count, bool) else "")
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
        elif result.ok and (plain := _plain_body(core_name, data)) is not None:
            text = plain
        else:
            visible = {key: value for key, value in data.items() if key not in _BOOKKEEPING_KEYS}
            text = json.dumps({"ok": result.ok, "data": visible, "evidence": [item.evidence_id for item in result.evidence]},
                              ensure_ascii=False, sort_keys=True)
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
