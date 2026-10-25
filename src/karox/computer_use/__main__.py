"""``python -m karox.computer_use`` — the CLI surface karo-core drives.

The engine's tool protocol is bash, so the real agent path to computer use is
this stable JSON command line: one action per invocation, one JSON document on
stdout, machine-stable exit codes. Every flag maps onto the same typed adapter
payloads a native tool integration would use.

The ``approve`` command is deliberately the only human entry point: it refuses
to run without an interactive TTY, so an agent looping through bash can never
issue itself a one-shot approval token.

Exit codes: 0 ok · 2 invalid action/payload · 3 denied/approval · 4 timeout ·
5 target lost · 6 platform unsupported · 1 everything else.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Optional

from .adapter import ACTION_KINDS, TOOL_SPECS, ComputerUseAdapter
from .approval import action_digest, normalize_payload

EXIT_BY_ERROR_TYPE = {
    "invalid_action": 2,
    "approval_required": 3,
    "approval_rejected": 3,
    "desktop_security": 3,
    "desktop_timeout": 4,
    "target_lost": 5,
    "platform_unsupported": 6,
}

_INT_FLAGS = {
    "--window-id": "window_id",
    "--x": "x",
    "--y": "y",
    "--clicks": "clicks",
    "--horizontal": "horizontal",
    "--max-dim": "max_dim",
    "--limit": "limit",
    "--max-nodes": "max_nodes",
    "--pace-ms": "pace_ms",
}
_STR_FLAGS = {
    "--app-id": "app_id",
    "--title-contains": "title_contains",
    "--access": "access",
    "--text": "text",
    "--key": "key",
    "--button": "button",
    "--shell": "shell",
    "--shortcut-path": "shortcut_path",
    "--mode": "mode",
}
_BOOL_FLAGS = {
    "--visible": ("visible", True),
    "--keep-open": ("keep_open", True),
    "--no-keep-open": ("keep_open", False),
    "--include-text": ("include_text", True),
    "--no-include-text": ("include_text", False),
}


def _emit(document: dict[str, Any], exit_code: int) -> int:
    print(json.dumps(document, ensure_ascii=False, default=str))
    return exit_code


def _error(message: str, error_type: str = "invalid_action", **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": False, "error": {"type": error_type, "message": message}}
    payload["error"].update(extra)
    return payload


def _normalize_action(name: str) -> Optional[str]:
    candidate = name.replace("-", "_")
    if candidate in ACTION_KINDS:
        return candidate
    prefixed = f"desktop.{candidate}"
    if prefixed in ACTION_KINDS:
        return prefixed
    return None


def _parse_args(argv: list[str]) -> tuple[dict[str, Any], list[str]]:
    options: dict[str, Any] = {}
    problems: list[str] = []
    payload: dict[str, Any] = {}
    index = 0
    while index < len(argv):
        token = argv[index]
        index += 1
        if token == "--payload":
            if index >= len(argv):
                problems.append("--payload requires a JSON value")
                break
            try:
                parsed = json.loads(argv[index])
            except ValueError:
                problems.append("--payload must be valid JSON")
                break
            if not isinstance(parsed, dict):
                problems.append("--payload must be a JSON object")
                break
            payload.update(parsed)
            index += 1
        elif token in _INT_FLAGS:
            if index >= len(argv):
                problems.append(f"{token} requires a value")
                break
            try:
                payload[_INT_FLAGS[token]] = int(argv[index])
            except ValueError:
                problems.append(f"{token} requires an integer")
                break
            index += 1
        elif token in _STR_FLAGS:
            if index >= len(argv):
                problems.append(f"{token} requires a value")
                break
            payload[_STR_FLAGS[token]] = argv[index]
            index += 1
        elif token in _BOOL_FLAGS:
            key, value = _BOOL_FLAGS[token]
            payload[key] = value
        elif token == "--session":
            options["session"] = argv[index] if index < len(argv) else ""
            index += 1
        elif token == "--timeout":
            try:
                options["timeout"] = float(argv[index])
            except (IndexError, ValueError):
                problems.append("--timeout requires a number")
            index += 1
        elif token == "--confirm-token":
            options["confirmation_token"] = argv[index] if index < len(argv) else ""
            index += 1
        elif token == "--action":
            options["approve_action"] = argv[index] if index < len(argv) else ""
            index += 1
        elif token == "--ttl":
            try:
                options["ttl"] = float(argv[index])
            except (IndexError, ValueError):
                problems.append("--ttl requires a number")
            index += 1
        elif token == "--json":
            options["json"] = True
        else:
            problems.append(f"unknown flag: {token}")
            break
    options["payload"] = payload
    return options, problems


def _cmd_actions() -> int:
    return _emit({"ok": True, "tools": [TOOL_SPECS[name] for name in sorted(TOOL_SPECS)]}, 0)


def _cmd_approve(options: dict[str, Any]) -> int:
    action = _normalize_action(str(options.get("approve_action") or ""))
    if action is None:
        return _emit(_error("approve requires --action naming a known desktop action"), 2)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return _emit(
            _error(
                "approve requires an interactive TTY: a human must issue this "
                "token in their own terminal; the agent path must never "
                "self-approve",
                error_type="tty_required",
            ),
            3,
        )
    payload = dict(options.get("payload") or {})
    digest = action_digest(action, payload)
    session = str(options.get("session") or "cli")
    adapter = ComputerUseAdapter(session_id=session)
    effective = json.dumps(
        normalize_payload(action, payload), sort_keys=True, ensure_ascii=False, default=str
    )
    print(
        f"Approve ONE execution of:\n  action:  {action}\n  payload: {effective}\n"
        f"  session: {session}\n  digest:  {digest}",
        file=sys.stderr,
    )
    try:
        answer = input("Type YES to issue a one-shot approval token: ")
    except (EOFError, KeyboardInterrupt):
        return _emit(_error("approval aborted", error_type="approval_aborted"), 3)
    if answer.strip() != "YES":
        return _emit(_error("approval aborted: answer was not YES", error_type="approval_aborted"), 3)
    token = adapter.approvals.issue(
        digest, session_id=session, ttl_seconds=options.get("ttl")
    )
    return _emit(
        {
            "ok": True,
            "action": action,
            "action_digest": digest,
            "session_id": session,
            "token": token,
            "ttl_seconds": adapter.approvals.ttl_seconds,
            "warning": "single use; retry the exact action once with --confirm-token",
        },
        0,
    )


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help", "help"}:
        print(__doc__ or "")
        print("commands: actions | approve | " + " | ".join(sorted(ACTION_KINDS)))
        return 0 if argv else 2
    command = argv[0]
    if command == "actions":
        return _cmd_actions()
    options, problems = _parse_args(argv[1:])
    if problems:
        return _emit(_error("; ".join(problems)), 2)
    if command == "approve":
        return _cmd_approve(options)
    action = _normalize_action(command)
    if action is None:
        return _emit(_error(f"unknown desktop action: {command!r}"), 2)
    try:
        adapter = ComputerUseAdapter(session_id=str(options.get("session") or "cli"))
        result = adapter.execute(
            action,
            dict(options.get("payload") or {}),
            timeout=options.get("timeout"),
            confirmation_token=options.get("confirmation_token"),
        )
    except Exception as exc:  # never leave the CLI without a JSON document
        return _emit(
            _error(f"{type(exc).__name__}: {exc}"[:500], error_type="internal_error"), 1
        )
    exit_code = 0 if result.ok else EXIT_BY_ERROR_TYPE.get(
        str((result.error or {}).get("type", "")), 1
    )
    return _emit(json.loads(result.to_json_line()), exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
