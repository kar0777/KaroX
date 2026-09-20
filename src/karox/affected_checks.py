"""Deterministic affected-check selection and bounded supplied-fix execution.

KaroX never asks an internal model to invent a fix.  The engine selects checks
from changed paths, imports, naming, bounded Git co-change history, project
configuration, and the user-approved verification allowlist.  Optional fix
attempts are exact guarded transactions supplied by the external coding agent;
they are bounded, scope-checked, deduplicated and stopped on an identical error.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from mcp.types import CallToolResult

from .artifacts import ArtifactStore
from .plan_executor import PlanExecutor, ToolRuntime
from .repo_context import RepositoryContextEngine, _safe_relative
from .repository_lease import (
    RepositoryLease,
    RepositoryLeaseConflict,
    RepositoryLeaseError,
    RepositoryLeaseStore,
)
from .security import redact
from .task_state import FactOrigin, TaskStateStore, fact

AFFECTED_CHECKS_SCHEMA_VERSION = 1
_MAX_CHANGED_FILES = 500
_MAX_TEST_TARGETS = 200
_MAX_HISTORY_COMMITS = 12
_MAX_FIX_ATTEMPTS = 3
_FAILURE_PATTERN = re.compile(
    r"(?i)(traceback|assertionerror|\bfailed\b|\bfailure\b|\berror\b|exception)"
)
_PYTHON_CONFIG = frozenset(
    {
        "pyproject.toml",
        "pytest.ini",
        "setup.cfg",
        "setup.py",
        "tox.ini",
        "mypy.ini",
        ".ruff.toml",
        "ruff.toml",
        "requirements.txt",
    }
)
_JS_CONFIG = frozenset(
    {
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "tsconfig.json",
        "vite.config.js",
        "vite.config.ts",
    }
)


class AffectedChecksError(RuntimeError):
    def __init__(self, code: str, message: str, details: Optional[dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_result(result: dict[str, Any] | CallToolResult) -> dict[str, Any]:
    if isinstance(result, CallToolResult):
        structured = result.structuredContent
        value = dict(structured) if isinstance(structured, dict) else {}
        value.setdefault("ok", not bool(result.isError))
        value["is_error"] = bool(result.isError)
        return value
    return dict(result)


def _result_data(result: Mapping[str, Any]) -> Mapping[str, Any]:
    data = result.get("data")
    return data if isinstance(data, Mapping) else result


def _result_success(result: Mapping[str, Any]) -> bool:
    data = _result_data(result)
    if result.get("ok") is False or result.get("is_error") is True:
        return False
    if data.get("ok") is False:
        return False
    exit_code = data.get("exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        return exit_code == 0 and not bool(data.get("timed_out"))
    return True


def _first_failure(result: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    data = _result_data(result)
    for stream_name in ("stderr", "stdout", "error", "summary"):
        value = data.get(stream_name)
        if not isinstance(value, str):
            value = result.get(stream_name)
        if not isinstance(value, str):
            continue
        for index, line in enumerate(value.splitlines(), start=1):
            if _FAILURE_PATTERN.search(line):
                return {
                    "stream": stream_name,
                    "line": index,
                    "text": str(redact(line))[:1000],
                }
    error_code = result.get("error_code") or data.get("error_code")
    if isinstance(error_code, str):
        return {"stream": "structured", "line": 0, "text": error_code}
    if not _result_success(result):
        return {"stream": "structured", "line": 0, "text": "check failed"}
    return None


def _failure_signature(result: Mapping[str, Any]) -> Optional[str]:
    if _result_success(result):
        return None
    data = _result_data(result)
    failure = _first_failure(result) or {}
    payload = {
        "exit_code": data.get("exit_code"),
        "timed_out": bool(data.get("timed_out")),
        "error_code": result.get("error_code") or data.get("error_code"),
        "first_failure": failure.get("text"),
    }
    return _canonical_digest(payload)


def _delegate_exception_leaves(exc: BaseException) -> list[BaseException]:
    """Flatten ExceptionGroup-like wrappers without importing 3.11-only names.

    anyio/MCP commonly wraps a transport failure in an ExceptionGroup whose
    outer message is only ``unhandled errors in a TaskGroup``.  KaroX supports
    Python 3.10+, so inspect the structural ``exceptions`` attribute rather than
    naming BaseExceptionGroup directly.
    """

    nested = getattr(exc, "exceptions", None)
    if isinstance(nested, (list, tuple)) and nested:
        leaves: list[BaseException] = []
        for item in nested:
            if isinstance(item, BaseException):
                leaves.extend(_delegate_exception_leaves(item))
        return leaves or [exc]
    return [exc]


def _retryable_delegate_exception(exc: BaseException) -> bool:
    """Whether replay with the *same* idempotency key is transport-safe."""

    transient_names = {
        "EndOfStream",
        "ClosedResourceError",
        "BrokenResourceError",
        "WouldBlock",
    }
    transient_markers = (
        "connection closed",
        "connection reset",
        "broken pipe",
        "end of stream",
        "timed out",
        "timeout",
        "transport closed",
    )
    leaves = _delegate_exception_leaves(exc)
    for leaf in leaves:
        if isinstance(leaf, (TimeoutError, ConnectionError, BrokenPipeError, EOFError)):
            return True
        if type(leaf).__name__ in transient_names:
            return True
        lowered = str(leaf).lower()
        if any(marker in lowered for marker in transient_markers):
            return True
    # Some transports can lose the inner exception while preserving the anyio
    # wrapper.  Retry only this recognizable wrapper, and still only once with
    # the same idempotency key.
    return len(leaves) == 1 and "taskgroup" in str(exc).lower()


def _delegate_exception_summary(exc: BaseException) -> str:
    leaves = _delegate_exception_leaves(exc)
    leaf = leaves[0] if leaves else exc
    return str(redact(f"{type(leaf).__name__}: {leaf}"))[:1000]


def _compact_result(result: Mapping[str, Any]) -> dict[str, Any]:
    data = _result_data(result)
    return {
        "ok": _result_success(result),
        "exit_code": data.get("exit_code"),
        "timed_out": bool(data.get("timed_out")),
        "error_code": result.get("error_code") or data.get("error_code"),
        "first_failure": _first_failure(result),
        "failure_signature": _failure_signature(result),
        "artifact_id": result.get("artifact_id") or data.get("artifact_id"),
        "duration_ms": data.get("duration_ms") or result.get("duration_ms"),
    }


class AffectedChecksEngine:
    def __init__(
        self,
        *,
        repository: Path,
        session_id: str,
        connection_id: str,
        delegate: Optional[ToolRuntime],
        verification_commands: Sequence[Sequence[str]],
        artifacts: ArtifactStore,
        repo_context: RepositoryContextEngine,
        task_states: TaskStateStore,
        lease_store: RepositoryLeaseStore,
    ) -> None:
        self.repository = repository.expanduser().resolve(strict=True)
        self.session_id = session_id
        self.connection_id = connection_id
        self.delegate = delegate
        self.verification_commands = tuple(tuple(item) for item in verification_commands)
        self.artifacts = artifacts
        self.repo_context = repo_context
        self.task_states = task_states
        self.lease_store = lease_store

    def _git(
        self,
        *arguments: str,
        allow_failure: bool = False,
        preserve_whitespace: bool = False,
    ) -> str:
        completed = subprocess.run(
            ["git", "-C", str(self.repository), *arguments],
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0:
            if allow_failure:
                return ""
            raise AffectedChecksError("git_inspection_failed", "affected check Git inspection failed")
        return completed.stdout if preserve_whitespace else completed.stdout.strip()

    def _delegate_names(self) -> set[str]:
        if self.delegate is None:
            return set()
        return {str(item.name) for item in self.delegate.descriptors()}

    def _changed_files(self, supplied: Any) -> list[str]:
        if supplied is None:
            raw = self._git(
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "-z",
                preserve_whitespace=True,
            )
            candidates: list[str] = []
            entries = iter(raw.split("\0"))
            for entry in entries:
                if len(entry) < 4:
                    continue
                candidates.append(entry[3:])
                # Porcelain -z emits destination NUL source for renames/copies;
                # the source is not another status record. Both paths matter.
                if "R" in entry[:2] or "C" in entry[:2]:
                    source = next(entries, "")
                    if not source:
                        raise AffectedChecksError(
                            "git_inspection_failed", "incomplete Git rename/copy record"
                        )
                    candidates.append(source)
        else:
            if not isinstance(supplied, list) or not all(isinstance(item, str) for item in supplied):
                raise AffectedChecksError("invalid_request", "changed_files must be a string array")
            candidates = list(supplied)
        if len(candidates) > _MAX_CHANGED_FILES:
            raise AffectedChecksError(
                "budget_exceeded",
                f"changed_files exceeds {_MAX_CHANGED_FILES} entries",
            )
        normalized: list[str] = []
        for candidate in candidates:
            relative = _safe_relative(self.repository, candidate)
            if relative is None:
                raise AffectedChecksError(
                    "scope_violation",
                    f"changed file escapes repository: {candidate}",
                )
            if relative not in normalized:
                normalized.append(relative)
        return normalized

    def _test_files(self) -> list[str]:
        root = self.repository / "tests"
        if not root.is_dir():
            return []
        return sorted(
            path.relative_to(self.repository).as_posix()
            for path in root.rglob("*.py")
            if (path.name.startswith("test_") or path.name.endswith("_test.py"))
            and path.is_file()
        )

    def _historical_tests(self, changed_files: Sequence[str], test_files: set[str]) -> dict[str, int]:
        scores: dict[str, int] = {}
        commits: list[str] = []
        for changed in changed_files[:50]:
            output = self._git(
                "log",
                f"-{_MAX_HISTORY_COMMITS}",
                "--format=%H",
                "--",
                changed,
                allow_failure=True,
            )
            for commit in output.splitlines():
                if commit and commit not in commits:
                    commits.append(commit)
                if len(commits) >= _MAX_HISTORY_COMMITS:
                    break
            if len(commits) >= _MAX_HISTORY_COMMITS:
                break
        for commit in commits:
            output = self._git(
                "show",
                "--name-only",
                "--format=",
                commit,
                allow_failure=True,
            )
            for path in output.splitlines():
                normalized = path.replace("\\", "/").strip()
                if normalized in test_files:
                    scores[normalized] = scores.get(normalized, 0) + 1
        return scores

    def _select_tests(self, changed_files: Sequence[str]) -> tuple[list[str], dict[str, list[str]]]:
        tests = self._test_files()
        test_set = set(tests)
        reasons: dict[str, list[str]] = {}
        selected: set[str] = set()
        # Per-selection only: avoid C x T reads without stale cross-run contents.
        contents: dict[str, str] = {}
        broad_python_config = any(Path(path).name in _PYTHON_CONFIG for path in changed_files)
        for changed in changed_files:
            if changed in test_set:
                selected.add(changed)
                reasons.setdefault(changed, []).append("changed_test")
            path = Path(changed)
            if path.suffix.lower() not in {".py", ".pyi"}:
                continue
            stem = path.stem
            dotted = changed.removesuffix(path.suffix).replace("/", ".")
            if dotted.startswith("src."):
                dotted = dotted[4:]
            for test in tests:
                test_path = self.repository / test
                direct_names = {
                    f"test_{stem}.py",
                    f"{stem}_test.py",
                }
                if Path(test).name in direct_names:
                    selected.add(test)
                    reasons.setdefault(test, []).append(f"name_match:{changed}")
                    continue
                try:
                    if test not in contents:
                        contents[test] = test_path.read_text(encoding="utf-8", errors="replace")
                    content = contents[test]
                except OSError as exc:
                    raise AffectedChecksError(
                        "test_inspection_failed", f"cannot inspect test: {test}"
                    ) from exc
                if stem in content or dotted in content:
                    selected.add(test)
                    reasons.setdefault(test, []).append(f"import_or_symbol_match:{changed}")
        historical = self._historical_tests(changed_files, test_set)
        for test, score in sorted(historical.items(), key=lambda item: (-item[1], item[0])):
            selected.add(test)
            reasons.setdefault(test, []).append(f"historical_cochange:{score}")
        if broad_python_config:
            for test in tests:
                selected.add(test)
                reasons.setdefault(test, []).append("python_project_config_changed")
        ordered = sorted(selected)
        return ordered, reasons

    @staticmethod
    def _command_kind(argv: Sequence[str]) -> str:
        lowered = " ".join(argv).lower()
        if "pytest" in lowered or " unittest" in lowered:
            return "test"
        if "ruff" in lowered or "flake8" in lowered or "eslint" in lowered:
            return "lint"
        if "mypy" in lowered or "pyright" in lowered or "tsc" in lowered:
            return "typecheck"
        if "build" in lowered or "wheel" in lowered or "npm run build" in lowered:
            return "build"
        return "verification"

    def _select_commands(self, changed_files: Sequence[str]) -> list[dict[str, Any]]:
        suffixes = {Path(path).suffix.lower() for path in changed_files}
        names = {Path(path).name for path in changed_files}
        python_change = bool(suffixes.intersection({".py", ".pyi"}) or names.intersection(_PYTHON_CONFIG))
        js_change = bool(suffixes.intersection({".js", ".jsx", ".ts", ".tsx"}) or names.intersection(_JS_CONFIG))
        broad = bool(names.intersection(_PYTHON_CONFIG | _JS_CONFIG))
        selected: list[dict[str, Any]] = []
        for argv in self.verification_commands:
            lowered = " ".join(argv).lower()
            kind = self._command_kind(argv)
            relevant = broad
            if python_change and any(token in lowered for token in ("python", "pytest", "ruff", "mypy")):
                relevant = True
            if js_change and any(token in lowered for token in ("node", "npm", "pnpm", "yarn", "eslint", "tsc")):
                relevant = True
            if relevant:
                selected.append(
                    {
                        "tool": "karox.checks.run",
                        "arguments": {"argv": list(argv)},
                        "reasons": [f"approved_{kind}_for_changed_language"],
                        "kind": kind,
                    }
                )
        return selected

    def select(self, changed_files: Sequence[str]) -> list[dict[str, Any]]:
        names = self._delegate_names()
        tests, reasons = self._select_tests(changed_files)
        selected: list[dict[str, Any]] = []
        full_reasons: set[str] = set()
        if any(Path(path).name in _PYTHON_CONFIG for path in changed_files):
            full_reasons.add("python_project_config_changed")
        if len(tests) > _MAX_TEST_TARGETS:
            # The target bound is a transport budget, not permission to drop
            # checks. Full collection also covers configured tests outside tests/.
            full_reasons.add("affected_test_target_budget_exceeded")
        mapped_reasons = {reason for values in reasons.values() for reason in values}
        # Preserve checks-only projects without inventing a test capability;
        # discovered tests still require the test delegate (never lint-only).
        has_test_surface = "karox.tests.run" in names or bool(self._test_files())
        for path in changed_files:
            if Path(path).name in {"conftest.py", "__init__.py"} or (
                path.startswith("tests/") and "changed_test" not in reasons.get(path, [])
            ):
                full_reasons.add("shared_test_support_or_collection_changed")
            if Path(path).suffix.lower() not in {".py", ".pyi"} or path in tests:
                continue
            # History is additive evidence, never proof that an individual
            # changed source is covered by another source's mapped tests.
            if has_test_surface and not any(
                f"{prefix}:{path}" in mapped_reasons
                for prefix in ("name_match", "import_or_symbol_match")
            ):
                full_reasons.add("python_source_changed_without_deterministic_test_mapping")
        if tests or full_reasons:
            if "karox.tests.run" not in names:
                raise AffectedChecksError(
                    "capability_unavailable", "affected tests require karox.tests.run"
                )
            selected.append(
                {
                    "tool": "karox.tests.run",
                    "arguments": (
                        {"suite": "full"} if full_reasons
                        else {"suite": "focused", "targets": tests}
                    ),
                    "reasons": sorted(full_reasons or mapped_reasons),
                    "kind": "fallback_full_tests" if full_reasons else "affected_tests",
                }
            )
        if "karox.checks.run" in names:
            selected.extend(self._select_commands(changed_files))
        deduplicated: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in selected:
            digest = _canonical_digest({"tool": item["tool"], "arguments": item["arguments"]})
            if digest in seen:
                continue
            seen.add(digest)
            item = dict(item)
            item["check_id"] = f"check-{digest[:20]}"
            deduplicated.append(item)
        return deduplicated

    def _run_selected(
        self,
        selected: Sequence[Mapping[str, Any]],
        operation_key: str,
        attempt: int,
        timeout_seconds: float,
    ) -> list[dict[str, Any]]:
        if self.delegate is None:
            raise AffectedChecksError("capability_unavailable", "no operation runtime is available")
        results: list[dict[str, Any]] = []
        for index, check in enumerate(selected):
            tool = str(check["tool"])
            arguments = dict(check["arguments"])
            if "timeout_seconds" not in arguments and tool == "karox.checks.run":
                arguments["timeout_seconds"] = timeout_seconds
            idempotency = hashlib.sha256(
                f"{operation_key}\0attempt:{attempt}\0check:{index}\0{check['check_id']}".encode("utf-8")
            ).hexdigest()
            transport_retries = 0
            try:
                raw = self.delegate.execute(
                    tool,
                    arguments,
                    idempotency_key=idempotency,
                    deadline_seconds=timeout_seconds,
                )
                normalized = _normalize_result(raw)
            except Exception as first_exc:
                if _retryable_delegate_exception(first_exc):
                    transport_retries = 1
                    try:
                        raw = self.delegate.execute(
                            tool,
                            arguments,
                            idempotency_key=idempotency,
                            deadline_seconds=timeout_seconds,
                        )
                        normalized = _normalize_result(raw)
                        normalized["delegate_transport_retries"] = transport_retries
                    except Exception as retry_exc:
                        normalized = {
                            "ok": False,
                            "error_code": "check_execution_failed",
                            "error": _delegate_exception_summary(retry_exc),
                            "delegate_transport_retries": transport_retries,
                        }
                else:
                    normalized = {
                        "ok": False,
                        "error_code": "check_execution_failed",
                        "error": _delegate_exception_summary(first_exc),
                        "delegate_transport_retries": transport_retries,
                    }
            results.append(
                {
                    "check_id": check["check_id"],
                    "tool": tool,
                    "arguments": arguments,
                    "reasons": list(check.get("reasons", [])),
                    "result": normalized,
                    "compact": _compact_result(normalized),
                }
            )
        return results

    @staticmethod
    def _overall(results: Sequence[Mapping[str, Any]]) -> tuple[bool, list[str], Optional[dict[str, Any]]]:
        signatures: list[str] = []
        first: Optional[dict[str, Any]] = None
        success = True
        for item in results:
            result = item.get("result")
            if not isinstance(result, Mapping):
                continue
            if not _result_success(result):
                success = False
                signature = _failure_signature(result)
                if signature is not None:
                    signatures.append(signature)
                if first is None:
                    failure = _first_failure(result)
                    first = {
                        "check_id": item.get("check_id"),
                        "tool": item.get("tool"),
                        "failure": failure,
                        "failure_signature": signature,
                    }
        return success, sorted(set(signatures)), first

    def _baseline_evidence(
        self,
        artifact_id: Optional[str],
        plan_digest: str,
    ) -> Optional[dict[str, Any]]:
        if artifact_id is None:
            return None
        try:
            data, record = self.artifacts.read(artifact_id)
            payload = json.loads(data)
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AffectedChecksError("baseline_invalid", "baseline artifact is unavailable") from exc
        if not isinstance(payload, dict) or payload.get("kind") != "affected_checks_baseline":
            raise AffectedChecksError("baseline_invalid", "artifact is not affected-check baseline evidence")
        if payload.get("check_plan_digest") != plan_digest:
            raise AffectedChecksError(
                "baseline_mismatch",
                "baseline used a different affected-check plan",
            )
        if payload.get("content_hash") not in {None, record.sha256}:
            raise AffectedChecksError("baseline_invalid", "baseline content hash mismatch")
        return payload

    @staticmethod
    def _classification(
        mode: str,
        success: bool,
        signatures: Sequence[str],
        baseline: Optional[Mapping[str, Any]],
    ) -> dict[str, Any]:
        if mode == "baseline":
            return {
                "status": "baseline",
                "evidence": "controlled_run_before_candidate_changes",
            }
        if success:
            return {
                "status": "none",
                "evidence": "current_checks_passed",
            }
        if baseline is None:
            return {
                "status": "unknown",
                "evidence": "no_controlled_baseline",
            }
        baseline_signatures = {
            str(item) for item in baseline.get("failure_signatures", [])
        }
        current = set(signatures)
        if current and current.issubset(baseline_signatures):
            return {
                "status": "pre_existing",
                "evidence": "same_failure_signature_in_controlled_baseline",
            }
        return {
            "status": "new",
            "evidence": "failure_absent_from_same_check_plan_baseline",
        }

    def _checkpoint_attempt(
        self,
        attempt: int,
        result: Mapping[str, Any],
        next_action: str,
        *,
        workstream_id: Optional[str] = None,
    ) -> None:
        try:
            self.task_states.checkpoint(
                self.session_id,
                {
                    "checks_executed": fact(
                        {
                            "attempt": attempt,
                            "ok": result.get("ok"),
                            "failure_signatures": result.get("failure_signatures", []),
                        },
                        FactOrigin.REPORTED_BY_AGENT,
                        "checks.run_affected",
                    ),
                    "next_safe_action": fact(
                        next_action,
                        FactOrigin.PENDING,
                        "checks.run_affected",
                    ),
                },
                workstream_id=workstream_id,
            )
        except Exception:
            pass

    def _apply_fix(
        self,
        attempt: Mapping[str, Any],
        operation_key: str,
        index: int,
        allowed_scope: set[str],
        seen_patches: set[str],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        if self.delegate is None:
            raise AffectedChecksError("capability_unavailable", "no operation runtime is available")
        command = attempt.get("command")
        expected_paths = attempt.get("expected_paths")
        if not isinstance(command, dict):
            raise AffectedChecksError("invalid_fix", "fix attempt command must be an object")
        if not isinstance(expected_paths, list) or not expected_paths or not all(
            isinstance(path, str) for path in expected_paths
        ):
            raise AffectedChecksError("invalid_fix", "fix expected_paths must be a non-empty string array")
        normalized_paths: set[str] = set()
        for path in expected_paths:
            relative = _safe_relative(self.repository, path)
            if relative is None:
                raise AffectedChecksError("scope_violation", "fix path escapes repository")
            normalized_paths.add(relative)
        if not normalized_paths.issubset(allowed_scope):
            raise AffectedChecksError(
                "scope_growth",
                "fix attempt expands beyond the approved fix scope",
                {"unexpected_paths": sorted(normalized_paths.difference(allowed_scope))},
            )
        patch_digest = _canonical_digest(command)
        if patch_digest in seen_patches:
            raise AffectedChecksError("duplicate_fix", "identical fix attempt was already applied")
        seen_patches.add(patch_digest)
        before = self.repo_context._revision_identity()
        idempotency = hashlib.sha256(
            f"{operation_key}\0fix:{index}\0{patch_digest}".encode("utf-8")
        ).hexdigest()
        raw = self.delegate.execute(
            "karox.repo.command",
            command,
            idempotency_key=idempotency,
            deadline_seconds=timeout_seconds,
        )
        result = _normalize_result(raw)
        if not _result_success(result):
            raise AffectedChecksError(
                str(result.get("error_code") or "fix_failed"),
                f"guarded fix attempt failed: {index}",
            )
        data = _result_data(result)
        reported_raw = data.get("changed_files")
        reported = {
            str(path) for path in reported_raw
        } if isinstance(reported_raw, list) else set()
        after = self.repo_context._revision_identity()
        observed = PlanExecutor._repository_changes(before, after)
        if "__repository_revision__" in observed:
            raise AffectedChecksError("scope_drift", "fix attempt changed repository revision")
        if not observed.issubset(reported):
            raise AffectedChecksError(
                "scope_drift",
                "fix changed paths not reported by guarded transaction",
                {"unexplained_paths": sorted(observed.difference(reported))},
            )
        if not reported.issubset(normalized_paths):
            raise AffectedChecksError(
                "scope_drift",
                "fix transaction changed an unexpected path",
            )
        return {
            "attempt": index,
            "patch_digest": patch_digest,
            "expected_paths": sorted(normalized_paths),
            "changed_files": sorted(reported),
            "result": _compact_result(result),
        }

    def run(
        self,
        arguments: dict[str, Any],
        operation_key: str,
        *,
        workstream_id: Optional[str] = None,
    ) -> dict[str, Any]:
        mode = arguments.get("mode", "current")
        if mode not in {"baseline", "current"}:
            raise AffectedChecksError("invalid_request", "mode must be baseline or current")
        timeout_seconds = arguments.get("timeout_seconds", 300)
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise AffectedChecksError("invalid_request", "timeout_seconds must be numeric")
        timeout = float(timeout_seconds)
        if not 1 <= timeout <= 1800:
            raise AffectedChecksError("invalid_request", "timeout_seconds must be between 1 and 1800")
        changed_files = self._changed_files(arguments.get("changed_files"))
        selected = self.select(changed_files)
        plan_digest = _canonical_digest(
            [{"tool": item["tool"], "arguments": item["arguments"]} for item in selected]
        )
        baseline_id = arguments.get("baseline_artifact_id")
        if baseline_id is not None and not isinstance(baseline_id, str):
            raise AffectedChecksError("invalid_request", "baseline_artifact_id must be a string")
        baseline = self._baseline_evidence(baseline_id, plan_digest)
        fix_attempts = arguments.get("fix_attempts", [])
        if not isinstance(fix_attempts, list):
            raise AffectedChecksError("invalid_request", "fix_attempts must be an array")
        if len(fix_attempts) > _MAX_FIX_ATTEMPTS:
            raise AffectedChecksError(
                "budget_exceeded",
                f"fix_attempts supports at most {_MAX_FIX_ATTEMPTS} attempts",
            )
        if mode == "baseline" and fix_attempts:
            raise AffectedChecksError("invalid_request", "baseline mode cannot apply fixes")
        allowed_fix_paths = arguments.get("allowed_fix_paths", [])
        if not isinstance(allowed_fix_paths, list) or not all(
            isinstance(path, str) for path in allowed_fix_paths
        ):
            raise AffectedChecksError("invalid_request", "allowed_fix_paths must be a string array")
        allowed_scope = set(changed_files)
        for path in allowed_fix_paths:
            relative = _safe_relative(self.repository, path)
            if relative is None:
                raise AffectedChecksError("scope_violation", "allowed fix path escapes repository")
            allowed_scope.add(relative)
        task_state = self.task_states.load_optional(
            self.session_id,
            workstream_id=workstream_id,
        )
        task_id = (
            task_state.task_id
            if task_state is not None
            else f"task-{self.session_id}-{workstream_id or 'default'}"
        )
        lease: Optional[RepositoryLease] = None
        try:
            try:
                lease, recovered_stale = self.lease_store.acquire(
                    self.repository,
                    session_id=self.session_id,
                    task_id=task_id,
                    connection_id=self.connection_id,
                    current_operation="checks.run_affected",
                    ttl_seconds=min(1800.0, timeout + 60.0),
                )
            except RepositoryLeaseConflict as exc:
                raise AffectedChecksError(
                    "repository_lease_conflict",
                    str(exc),
                    exc.details,
                ) from exc
            started = time.perf_counter()
            results = self._run_selected(selected, operation_key, 0, timeout)
            success, signatures, first_failure = self._overall(results)
            attempts: list[dict[str, Any]] = []
            previous_signatures = signatures
            seen_patches: set[str] = set()
            self._checkpoint_attempt(
                0,
                {"ok": success, "failure_signatures": signatures},
                "review affected checks" if success else "apply first bounded fix candidate",
                workstream_id=workstream_id,
            )
            if not success:
                for index, candidate in enumerate(fix_attempts, start=1):
                    if not isinstance(candidate, Mapping):
                        raise AffectedChecksError("invalid_fix", "fix attempt must be an object")
                    lease = self.lease_store.heartbeat(
                        self.repository,
                        lease,
                        current_operation=f"checks.run_affected.fix.{index}",
                        ttl_seconds=min(1800.0, timeout + 60.0),
                    )
                    fix_result = self._apply_fix(
                        candidate,
                        operation_key,
                        index,
                        allowed_scope,
                        seen_patches,
                        timeout,
                    )
                    rerun = self._run_selected(selected, operation_key, index, timeout)
                    rerun_success, rerun_signatures, rerun_first = self._overall(rerun)
                    attempt_result = {
                        **fix_result,
                        "checks": [
                            {
                                "check_id": item["check_id"],
                                "compact": item["compact"],
                            }
                            for item in rerun
                        ],
                        "ok": rerun_success,
                        "failure_signatures": rerun_signatures,
                    }
                    attempts.append(attempt_result)
                    results = rerun
                    success = rerun_success
                    signatures = rerun_signatures
                    first_failure = rerun_first
                    self._checkpoint_attempt(
                        index,
                        {"ok": success, "failure_signatures": signatures},
                        "review successful fix" if success else "stop or apply next distinct bounded fix",
                        workstream_id=workstream_id,
                    )
                    if success:
                        break
                    if rerun_signatures == previous_signatures:
                        attempts[-1]["stopped_reason"] = "identical_failure"
                        break
                    previous_signatures = rerun_signatures
            classification = self._classification(mode, success, signatures, baseline)
            evidence_payload: dict[str, Any] = {
                "kind": "affected_checks_baseline" if mode == "baseline" else "affected_checks_run",
                "schema_version": AFFECTED_CHECKS_SCHEMA_VERSION,
                "workstream_id": workstream_id or "default",
                "repository_identity": self.repo_context._revision_identity(),
                "changed_files": changed_files,
                "check_plan_digest": plan_digest,
                "selected_checks": selected,
                "results": results,
                "attempts": attempts,
                "ok": success,
                "failure_signatures": signatures,
                "first_failure": first_failure,
                "classification": classification,
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "recovered_stale_lease": recovered_stale,
            }
            encoded = json.dumps(
                redact(evidence_payload),
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
            record = self.artifacts.put(
                encoded,
                name=(
                    "affected-checks-baseline.json"
                    if mode == "baseline"
                    else "affected-checks-run.json"
                ),
                mime="application/json",
            )
            recommended = "no affected checks were selected"
            if selected and success:
                recommended = "proceed to the next broader verification gate when justified"
            elif selected and not success and attempts:
                recommended = "review the last distinct failure before proposing another scoped fix"
            elif selected and not success:
                recommended = "prepare a scoped fix candidate; do not classify without baseline evidence"
            return {
                "ok": success,
                "schema_version": AFFECTED_CHECKS_SCHEMA_VERSION,
                "workstream_id": workstream_id or "default",
                "mode": mode,
                "changed_files": changed_files,
                "selected_checks": [
                    {
                        "check_id": item["check_id"],
                        "tool": item["tool"],
                        "arguments": item["arguments"],
                        "reasons": item["reasons"],
                    }
                    for item in selected
                ],
                "results": [
                    {
                        "check_id": item["check_id"],
                        "tool": item["tool"],
                        "compact": item["compact"],
                    }
                    for item in results
                ],
                "first_failure": first_failure,
                "failure_signatures": signatures,
                "classification": classification,
                "fix_attempts": attempts,
                "artifact_id": record.artifact_id,
                "baseline_artifact_id": record.artifact_id if mode == "baseline" else baseline_id,
                "content_hash": record.sha256,
                "total_size": record.size,
                "recommended_next_action": recommended,
                "recovered_stale_lease": recovered_stale,
            }
        finally:
            if lease is not None:
                try:
                    self.lease_store.release(self.repository, lease)
                except RepositoryLeaseError:
                    pass
