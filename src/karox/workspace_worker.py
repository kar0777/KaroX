"""Reloadable implementation for stable repository and test commands."""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Optional

from .core import InvalidCommand
from .models import EvidenceRecord
from .unified_patch import execute_apply_patch, expected_map, parse_unified_patch
from .workspace_transaction import MAX_BATCH_OPERATIONS, WorkspaceTransaction


MAX_TEST_TARGETS = 200
WORKER_PROTOCOL_VERSION = 1


def _finite_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidCommand(f"{name} must be a finite number")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise InvalidCommand(f"{name} must be a finite number") from exc
    if not math.isfinite(number):
        raise InvalidCommand(f"{name} must be a finite number")
    return number


def _reject_unknown_keys(
    value: dict[str, Any], allowed: set[str], label: str
) -> None:
    unknown = sorted(set(value).difference(allowed))
    if unknown:
        raise InvalidCommand(
            f"{label} contains unsupported fields: " + ", ".join(unknown)
        )


def _string(operation: dict[str, Any], name: str, index: int) -> str:
    value = operation.get(name)
    if not isinstance(value, str):
        raise InvalidCommand(f"operations[{index}].{name} must be string")
    return value


def _optional_string(
    operation: dict[str, Any], name: str, index: int
) -> Optional[str]:
    value = operation.get(name)
    if value is not None and not isinstance(value, str):
        raise InvalidCommand(f"operations[{index}].{name} must be string")
    return value


def _optional_bool(
    operation: dict[str, Any], name: str, index: int, default: bool
) -> bool:
    value = operation.get(name, default)
    if not isinstance(value, bool):
        raise InvalidCommand(f"operations[{index}].{name} must be boolean")
    return value


def _optional_sha256(
    operation: dict[str, Any], name: str, index: int
) -> Optional[str]:
    value = _optional_string(operation, name, index)
    if value is None:
        return None
    if len(value) != 64:
        raise InvalidCommand(f"operations[{index}].{name} must be a sha256 digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise InvalidCommand(
            f"operations[{index}].{name} must be a sha256 digest"
        ) from exc
    return value.lower()


def _validate_batch_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    _reject_unknown_keys(payload, {"operations", "dry_run"}, "batch payload")
    operations = payload.get("operations")
    dry_run = payload.get("dry_run", False)
    if not isinstance(operations, list) or not operations:
        raise InvalidCommand("batch requires a non-empty operations array")
    if len(operations) > MAX_BATCH_OPERATIONS:
        raise InvalidCommand(
            f"batch supports at most {MAX_BATCH_OPERATIONS} operations"
        )
    if not isinstance(dry_run, bool):
        raise InvalidCommand("dry_run must be boolean")
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            raise InvalidCommand(f"operations[{index}] must be an object")
        op = operation.get("op")
        if op == "write":
            _reject_unknown_keys(
                operation,
                {"op", "path", "content", "expected_sha256", "allow_secret_literal"},
                f"operations[{index}]",
            )
            _string(operation, "path", index)
            _string(operation, "content", index)
            _optional_sha256(operation, "expected_sha256", index)
            _optional_bool(operation, "allow_secret_literal", index, False)
        elif op == "delete":
            _reject_unknown_keys(
                operation,
                {"op", "path", "expected_sha256", "missing_ok"},
                f"operations[{index}]",
            )
            _string(operation, "path", index)
            _optional_sha256(operation, "expected_sha256", index)
            _optional_bool(operation, "missing_ok", index, False)
        elif op == "move":
            _reject_unknown_keys(
                operation,
                {"op", "source", "destination", "expected_sha256", "overwrite"},
                f"operations[{index}]",
            )
            _string(operation, "source", index)
            _string(operation, "destination", index)
            _optional_sha256(operation, "expected_sha256", index)
            _optional_bool(operation, "overwrite", index, False)
        elif op == "mkdir":
            _reject_unknown_keys(
                operation,
                {"op", "path"},
                f"operations[{index}]",
            )
            _string(operation, "path", index)
        else:
            raise InvalidCommand(
                f"operations[{index}].op must be write, delete, move, or mkdir"
            )
    return operations


def validate_repo_command(arguments: dict[str, Any]) -> None:
    action = arguments.get("action")
    payload = arguments.get("payload", {})
    if not isinstance(action, str):
        raise InvalidCommand("repo.command action must be string")
    if not isinstance(payload, dict):
        raise InvalidCommand("repo.command payload must be object")
    if action == "apply_patch":
        _reject_unknown_keys(
            payload,
            {"patch", "expected_sha256", "dry_run", "allow_secret_literal"},
            "apply_patch payload",
        )
        patch = payload.get("patch")
        if not isinstance(patch, str):
            raise InvalidCommand("apply_patch requires patch string")
        for name in ("dry_run", "allow_secret_literal"):
            if not isinstance(payload.get(name, False), bool):
                raise InvalidCommand(
                    "dry_run and allow_secret_literal must be boolean"
                )
        expected_map(payload)
        parse_unified_patch(patch)
        return
    if action == "batch":
        _validate_batch_payload(payload)
        return
    raise InvalidCommand(
        "repo.command action must be one of: apply_patch, batch"
    )


def _execute_batch(runtime: Any, payload: dict[str, Any]) -> dict[str, Any]:
    operations = _validate_batch_payload(payload)
    dry_run = payload.get("dry_run", False)
    transaction = WorkspaceTransaction(runtime)
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            raise InvalidCommand(f"operations[{index}] must be an object")
        op = operation.get("op")
        if op == "write":
            transaction.stage_write(
                _string(operation, "path", index),
                _string(operation, "content", index),
                expected_sha256=_optional_sha256(
                    operation, "expected_sha256", index
                ),
                allow_secret_literal=_optional_bool(
                    operation,
                    "allow_secret_literal",
                    index,
                    False,
                ),
            )
        elif op == "delete":
            transaction.stage_delete(
                _string(operation, "path", index),
                expected_sha256=_optional_sha256(
                    operation, "expected_sha256", index
                ),
                missing_ok=_optional_bool(
                    operation, "missing_ok", index, False
                ),
            )
        elif op == "move":
            transaction.stage_move(
                _string(operation, "source", index),
                _string(operation, "destination", index),
                expected_sha256=_optional_sha256(
                    operation, "expected_sha256", index
                ),
                overwrite=_optional_bool(
                    operation, "overwrite", index, False
                ),
            )
        elif op == "mkdir":
            transaction.stage_mkdir(_string(operation, "path", index))
        else:
            raise InvalidCommand(
                f"operations[{index}].op must be write, delete, move, or mkdir"
            )
    if dry_run:
        return transaction.preview()
    result = transaction.commit()
    result["action"] = "batch"
    return result


def execute_repo_command(
    runtime: Any,
    arguments: dict[str, Any],
    deadline_seconds: float,
) -> dict[str, Any]:
    del deadline_seconds
    validate_repo_command(arguments)
    action = arguments["action"]
    payload = arguments.get("payload", {})
    if action == "apply_patch":
        result = execute_apply_patch(runtime, payload)
    elif action == "batch":
        result = _execute_batch(runtime, payload)
    else:
        raise InvalidCommand(
            "repo.command action must be one of: apply_patch, batch"
        )
    result["worker_protocol_version"] = WORKER_PROTOCOL_VERSION
    return result


def _test_files(repository: Path) -> list[str]:
    root = repository / "tests"
    if not root.is_dir():
        raise InvalidCommand("repository has no tests directory")
    return sorted(
        path.relative_to(repository).as_posix()
        for path in root.rglob("test_*.py")
        if path.is_file()
    )


def _validated_targets(runtime: Any, raw: Any) -> list[str]:
    if not isinstance(raw, list) or not raw:
        raise InvalidCommand("focused tests require non-empty targets")
    if len(raw) > MAX_TEST_TARGETS:
        raise InvalidCommand(
            f"targets supports at most {MAX_TEST_TARGETS} entries"
        )
    targets: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item:
            raise InvalidCommand("targets items must be non-empty strings")
        path_part = item.split("::", 1)[0]
        path = runtime.safe_path(path_part)
        relative = path.relative_to(runtime.repository).as_posix()
        if (
            not relative.startswith("tests/")
            or not relative.endswith(".py")
            or not path.is_file()
        ):
            raise InvalidCommand(
                "test targets must point to existing Python files under tests/"
            )
        normalized = relative
        if "::" in item:
            normalized += "::" + item.split("::", 1)[1]
        if normalized not in targets:
            targets.append(normalized)
    return targets


def _node_test_command(repository: Path) -> tuple[list[str], str] | None:
    """Return a one-shot package test command for a Node/Vite repository."""
    manifest = repository / "package.json"
    if not manifest.is_file():
        return None
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    scripts = payload.get("scripts") if isinstance(payload, dict) else None
    test_script = scripts.get("test") if isinstance(scripts, dict) else None
    if not isinstance(test_script, str) or not test_script.strip():
        return None

    if (repository / "pnpm-lock.yaml").is_file():
        argv = ["pnpm", "test"]
        separator = ["--", "--run"]
    elif (repository / "yarn.lock").is_file():
        argv = ["yarn", "test"]
        separator = ["--run"]
    elif (repository / "bun.lockb").is_file() or (repository / "bun.lock").is_file():
        argv = ["bun", "run", "test"]
        separator = ["--", "--run"]
    else:
        argv = ["npm", "test"]
        separator = ["--", "--run"]

    # `vitest` without `run` may enter watch mode on an interactive machine.
    # Force one-shot execution while leaving already-one-shot scripts untouched.
    lowered = test_script.lower()
    if re.search(r"(^|\s)vitest(?:\s|$)", lowered) and not re.search(
        r"(^|\s)(?:run|--run)(?:\s|$)", lowered
    ):
        argv.extend(separator)
    return argv, test_script.strip()


def execute_tests(
    runtime: Any,
    arguments: dict[str, Any],
    deadline_seconds: float,
) -> dict[str, Any]:
    suite = arguments.get("suite", "focused")
    if suite not in {"focused", "full", "split"}:
        raise InvalidCommand(
            "tests.run suite must be focused, full, or split"
        )
    timeout = arguments.get(
        "timeout_seconds",
        min(600.0, deadline_seconds),
    )
    timeout_value = _finite_number(timeout, "timeout_seconds")
    if timeout_value <= 0:
        raise InvalidCommand("timeout_seconds must be a positive finite number")
    effective = min(
        timeout_value,
        float(deadline_seconds),
        runtime.MAX_PROCESS_TIMEOUT_SECONDS,
    )

    test_root = runtime.repository / "tests"
    has_python_tests = test_root.is_dir() and any(test_root.rglob("test_*.py"))
    node_test = _node_test_command(runtime.repository)
    if node_test is not None and not has_python_tests:
        if suite == "split":
            raise InvalidCommand("Node test suites do not support split/part through tests.run")
        if arguments.get("targets") not in (None, []):
            raise InvalidCommand(
                "Node/Vitest tests.run does not accept Python pytest targets; use the project test script"
            )
        if arguments.get("split") is not None or arguments.get("part") is not None:
            raise InvalidCommand("Node test suites do not accept split or part")
        argv, test_script = node_test
        result = runtime._run(argv, effective)
        result.update(
            {
                "suite": suite,
                "runner": "package-script",
                "package_test_script": test_script,
                "targets": [],
                "target_count": 0,
                "split": None,
                "part": None,
                "requested_timeout": timeout_value,
                "effective_timeout": effective,
                "timeout_clamped_by": (
                    None
                    if effective == timeout_value
                    else "request_deadline_or_runtime_maximum"
                ),
                "verification_eligible": True,
            }
        )
        result["_evidence"] = [
            EvidenceRecord(
                kind="test_run",
                summary=("Passed" if result["exit_code"] == 0 else "Failed")
                + " structured package test run",
                command=result["argv"],
                exit_code=result["exit_code"],
                metadata={
                    "suite": suite,
                    "runner": "package-script",
                    "timed_out": result["timed_out"],
                },
            )
        ]
        return result

    argv = [sys.executable, "-m", "pytest"]
    selected: list[str] = []
    part: Optional[int] = None
    split: Optional[int] = None
    if suite == "focused":
        if arguments.get("split") is not None or arguments.get("part") is not None:
            raise InvalidCommand("focused suite does not accept split or part")
        selected = _validated_targets(runtime, arguments.get("targets"))
        argv.extend(selected)
    elif suite == "split":
        if arguments.get("targets") not in (None, []):
            raise InvalidCommand("split suite does not accept targets")
        raw_split = arguments.get("split", 2)
        raw_part = arguments.get("part")
        split_value = _finite_number(raw_split, "split")
        if int(split_value) != split_value or not 2 <= int(split_value) <= 32:
            raise InvalidCommand("split must be an integer between 2 and 32")
        split = int(split_value)
        part_value = _finite_number(raw_part, "part")
        if int(part_value) != part_value or not 1 <= int(part_value) <= split:
            raise InvalidCommand("part must be an integer between 1 and split")
        part = int(part_value)
        all_files = _test_files(runtime.repository)
        selected = [
            item
            for index, item in enumerate(all_files)
            if index % split == part - 1
        ]
        if not selected:
            raise InvalidCommand("selected test split is empty")
        argv.extend(selected)
    else:
        if arguments.get("targets") not in (None, []):
            raise InvalidCommand("full suite does not accept targets")
        if arguments.get("split") is not None or arguments.get("part") is not None:
            raise InvalidCommand("full suite does not accept split or part")

    result = runtime._run(argv, effective)
    result.update(
        {
            "suite": suite,
            "targets": selected,
            "target_count": len(selected),
            "split": split,
            "part": part,
            "requested_timeout": timeout_value,
            "effective_timeout": effective,
            "timeout_clamped_by": (
                None
                if effective == timeout_value
                else "request_deadline_or_runtime_maximum"
            ),
            "verification_eligible": True,
        }
    )
    result["_evidence"] = [
        EvidenceRecord(
            kind="test_run",
            summary=(
                "Passed" if result["exit_code"] == 0 else "Failed"
            )
            + f" structured pytest {suite} run",
            command=result["argv"],
            exit_code=result["exit_code"],
            metadata={
                "suite": suite,
                "target_count": len(selected),
                "split": split,
                "part": part,
                "timed_out": result["timed_out"],
            },
        )
    ]
    return result


def execute_browser_command(
    runtime: Any,
    arguments: dict[str, Any],
    deadline_seconds: float,
) -> dict[str, Any]:
    """Dispatch a stable browser action through the guarded session manager."""
    action = arguments.get("action")
    payload = arguments.get("payload", {})
    if not isinstance(action, str) or not action:
        raise InvalidCommand("browser.command action must be a non-empty string")
    if not isinstance(payload, dict):
        raise InvalidCommand("browser.command payload must be object")
    if action.startswith("model."):
        # Model delegation actions are developed behind this stable hot command
        # surface. Reload their isolated module so new guarded actions become
        # available without rotating the durable hosted bridge.
        from importlib import reload
        from . import model_hot_actions

        handler = reload(model_hot_actions).execute_model_action
        return {
            "action": action,
            **handler(runtime, action, payload, deadline_seconds),
        }
    if action.startswith("app."):
        from .desktop_apps import execute_desktop_app_action

        return {
            "action": action,
            **execute_desktop_app_action(runtime, action, payload, deadline_seconds),
        }
    manager = runtime._browser
    if action == "_temporary_fill_plain_text":
        if getattr(manager, "engine", "") != "chrome_extension_mv3" or not hasattr(manager, "_call"):
            raise InvalidCommand("temporary plain-text fill requires the extension browser backend")
        from .browser_session import BrowserSecurityError
        from .extension_browser import _SECRET_INPUT_HINT

        manager._assert_agent_input_allowed()
        selector = manager._selector(payload.get("selector"))
        value = payload.get("value")
        if not isinstance(value, str) or len(value) > 100_000:
            raise InvalidCommand("temporary plain-text fill value must be text")
        metadata = manager._inspect(selector, deadline_seconds)
        field_type = str(metadata.get("type", "")).lower()
        descriptor = " ".join(
            str(metadata.get(key, ""))
            for key in ("type", "name", "id", "aria", "placeholder")
        )
        if field_type == "password" or _SECRET_INPUT_HINT.search(descriptor):
            raise BrowserSecurityError("temporary plain-text fill refuses sensitive fields")
        result = manager._call("fill", {"selector": selector, "value": value}, deadline_seconds)
        return {"action": action, "filled": True, "selector": selector, "value_length": len(value), **result}
    if action in {"debug_overlay_state", "geometry"}:
        if getattr(manager, "engine", "") != "chrome_extension_mv3" or not hasattr(manager, "_call"):
            raise InvalidCommand(f"browser.command {action} requires the extension browser backend")
        return {"action": action, **manager._call(action, payload, deadline_seconds)}
    if action == "show_window":
        if getattr(manager, "engine", "") != "chrome_extension_mv3" or not hasattr(manager, "_call"):
            raise InvalidCommand("browser.command show_window requires the extension browser backend")
        left = int(payload.get("left", 100))
        top = int(payload.get("top", 100))
        if not 0 <= left <= 10_000 or not 0 <= top <= 10_000:
            raise InvalidCommand("browser show_window coordinates must be between 0 and 10000")
        return {"action": action, **manager._call("show_window", {"left": left, "top": top}, deadline_seconds)}
    # Bound lazily so a partial engine is an engine-honest refusal at
    # dispatch time, never an AttributeError while the table is built.
    known_actions = (
        "open",
        "tabs",
        "new_tab",
        "switch_tab",
        "close_tab",
        "snapshot",
        "click",
        "fill",
        "fill_credential",
        "select",
        "press",
        "wait_for",
        "get_text",
        "screenshot",
        "console",
        "network_failures",
        "network_requests",
        "request_user_takeover",
        "resume_after_user_takeover",
        # Guarded actions added inside the stable browser.command schema:
        # navigation history, reads, pointer/keyboard verbs, deterministic
        # state setters, scrolling, and file transfer.
        ("back", "back"),
        ("forward", "forward"),
        ("reload", "reload"),
        ("page_info", "page_info"),
        ("hover", "hover"),
        ("focus", "focus_element"),
        ("clear", "clear"),
        ("dblclick", "dblclick"),
        ("type", "type_text"),
        ("set_checked", "set_checked"),
        ("scroll", "scroll"),
        ("upload", "upload"),
        ("download", "download"),
    )
    handlers = {}
    for entry in known_actions:
        action_name, method_name = entry if isinstance(entry, tuple) else (entry, entry)
        handlers[action_name] = getattr(manager, method_name, None)
    if action == "close":
        if payload.get("user_confirmed") is not True:
            from .browser_session import BrowserSecurityError

            raise BrowserSecurityError(
                "browser.close requires explicit user confirmation and is never automatic cleanup"
            )
        reason = payload.get("reason", "")
        if reason is not None and (
            not isinstance(reason, str) or len(reason) > 500
        ):
            raise InvalidCommand(
                "browser close reason must be a string up to 500 characters"
            )
        if manager.takeover_active:
            from .browser_session import BrowserSecurityError

            raise BrowserSecurityError(
                "browser cannot be closed while user takeover is active"
            )
        return {"action": action, **manager.close()}
    if action in handlers:
        handler = handlers[action]
        if handler is None:
            # A real action this engine has not implemented is a typed
            # refusal, not a generic unsupported-action error.
            raise InvalidCommand(
                f"browser.command {action} is not supported by this browser engine"
            )
        return {"action": action, **handler(payload, deadline_seconds)}
    raise InvalidCommand(
        "unsupported browser.command action: " + action
    )
