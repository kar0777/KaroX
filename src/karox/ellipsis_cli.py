"""PowerShell-friendly interactive CLI for local Ellipsis Opus 5 sessions."""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional, Sequence

from .ellipsis_runtime import (
    EllipsisAgentConfig,
    EllipsisAgentManager,
    EllipsisAgentState,
    EllipsisRuntimeError,
    infer_command_allowlists,
)
from .models import AccessProfile


def _json_command(value: str) -> tuple[str, ...]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError("command must be a JSON array") from exc
    if (
        not isinstance(payload, list)
        or not payload
        or not all(isinstance(item, str) and item for item in payload)
    ):
        raise argparse.ArgumentTypeError(
            "command must be a JSON array of non-empty strings"
        )
    return tuple(payload)


def _currency(value: str) -> str:
    normalized = value.upper()
    if not normalized.isalpha() or len(normalized) != 3:
        raise argparse.ArgumentTypeError("currency must be a three-letter code")
    return normalized


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="karox agent")
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("ellipsis", aliases=["run"])
    run.add_argument("--repository", type=Path, default=Path.cwd())
    run.add_argument("--task")
    run.add_argument(
        "--access-profile",
        choices=[item.value for item in AccessProfile],
        default=AccessProfile.WORKSPACE_WRITE.value,
    )
    run.add_argument("--model", choices=["claude-opus-5"], default="claude-opus-5")
    run.add_argument("--budget", type=float, default=0.10)
    run.add_argument("--currency", type=_currency, default="USD")
    run.add_argument(
        "--tunnel",
        choices=["tailscale", "cloudflare", "custom"],
        default="tailscale",
    )
    run.add_argument("--public-url")
    run.add_argument("--port", type=int, default=8766)
    run.add_argument("--ttl-seconds", type=float, default=14_400.0)
    run.add_argument("--deadline-seconds", type=float, default=600.0)
    run.add_argument(
        "--verification-command",
        action="append",
        type=_json_command,
        default=[],
        help='Approved check argv, for example ["python","-m","pytest","*"]',
    )
    run.add_argument(
        "--allow-command",
        action="append",
        type=_json_command,
        default=[],
        help='Approved command argv, for example ["npm","run","dev","*"]',
    )
    run.add_argument("--no-inferred-commands", action="store_true")
    run.add_argument("--allow-commit", action="store_true")
    run.add_argument("--api-base-url", default="")
    run.add_argument("--preflight-only", action="store_true")
    run.add_argument("--non-interactive", action="store_true")
    run.add_argument("--json", action="store_true")

    for name in ("attach", "status", "stop", "result", "rollback"):
        command = sub.add_parser(name)
        command.add_argument("session_id", nargs="?")
        if name == "rollback":
            command.add_argument("--yes", action="store_true")
        if name in {"status", "result"}:
            command.add_argument("--json", action="store_true")

    send = sub.add_parser("send")
    send.add_argument("session_id", nargs="?")
    send.add_argument("message")
    return parser


def _prompt_task() -> str:
    if not sys.stdin.isatty():
        raise EllipsisRuntimeError("--task is required in non-interactive mode")
    print("Задача для Ellipsis Opus 5:")
    value = input("> ").strip()
    if not value:
        raise EllipsisRuntimeError("task must not be empty")
    return value


def _commands(
    repository: Path,
    verification: Sequence[tuple[str, ...]],
    commands: Sequence[tuple[str, ...]],
    *,
    infer: bool,
) -> tuple[tuple[tuple[str, ...], ...], tuple[tuple[str, ...], ...]]:
    inferred = infer_command_allowlists(repository) if infer else ()
    return (
        tuple(dict.fromkeys((*inferred, *verification))),
        tuple(dict.fromkeys((*inferred, *commands))),
    )


def _config(args: argparse.Namespace) -> EllipsisAgentConfig:
    repository = args.repository.expanduser().resolve(strict=True)
    verification, commands = _commands(
        repository,
        args.verification_command,
        args.allow_command,
        infer=not args.no_inferred_commands,
    )
    return EllipsisAgentConfig(
        repository=repository,
        task=args.task or _prompt_task(),
        access_profile=AccessProfile(args.access_profile),
        model=args.model,
        budget=args.budget,
        currency=args.currency,
        tunnel=args.tunnel,
        public_url=args.public_url,
        port=args.port,
        ttl_seconds=args.ttl_seconds,
        deadline_seconds=args.deadline_seconds,
        verification_commands=verification,
        command_commands=commands,
        allow_commit=args.allow_commit,
        api_base_url=args.api_base_url,
    )


def _event_line(event: Any) -> str:
    parts = [f"[{event.kind}]"]
    for value in (event.tool, event.status, event.message):
        if value:
            parts.append(str(value))
    if event.cost is not None:
        parts.append(f"cost={event.cost:g} {event.currency or ''}".strip())
    return " ".join(parts)


class EventPump:
    def __init__(self, manager: EllipsisAgentManager, state: EllipsisAgentState) -> None:
        self.manager = manager
        self.state = state
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self._run,
            name=f"karox-ellipsis-events-{state.local_session_id}",
            daemon=True,
        )

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=3.0)

    def _run(self) -> None:
        while not self.stop_event.wait(1.0):
            try:
                events = self.manager.poll_events(self.state)
            except Exception:
                continue
            for event in events:
                print("\n" + _event_line(event), flush=True)


def _status_payload(
    manager: EllipsisAgentManager,
    state: EllipsisAgentState,
    *,
    refresh: bool = True,
) -> dict[str, Any]:
    state, _ = manager.status(state.local_session_id, refresh=refresh)
    payload = state.public_dict()
    try:
        report = manager.report(state.local_session_id)
    except Exception:
        report = {}
    data = report.get("data", report) if isinstance(report, dict) else {}
    if isinstance(data, dict):
        payload["changed_files"] = data.get("changed_files", [])
        payload["tests"] = data.get("checks", [])
        payload["git_status"] = data.get("git_status", {})
    payload["approval_requests"] = []
    return payload


def _print_status(
    manager: EllipsisAgentManager,
    state: EllipsisAgentState,
    *,
    refresh: bool = True,
    as_json: bool = False,
) -> None:
    payload = _status_payload(manager, state, refresh=refresh)
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print(f"Project: {payload['repository']}")
    print(f"Branch: {payload['branch'] or '(detached)'}")
    print(f"Access profile: {payload['access_profile']}")
    tunnel = "online" if payload["tunnel_running"] else "offline"
    print(f"Tunnel: {tunnel} ({payload['tunnel']})")
    print(f"Ellipsis session: {payload['ellipsis_session_id']}")
    print(f"Model: {payload['model']}")
    print(f"Status: {payload['status']}")
    elapsed = payload.get("elapsed_seconds")
    if elapsed is None:
        elapsed = max(0.0, time.time() - float(payload["started_at"]))
    print(f"Elapsed: {elapsed:.1f}s")
    cost = payload.get("cost")
    print(
        f"Cost: {cost:g} {payload['currency']}"
        if isinstance(cost, (int, float))
        else "Cost: not reported yet"
    )
    changed = payload.get("changed_files", [])
    print("Changed local files: " + (", ".join(changed) if changed else "none"))
    print(f"Tests/checks: {len(payload.get('tests', []))} recorded")
    print("Approval requests: none")
    blocker = payload.get("last_error")
    print("Blockers: " + (str(blocker) if blocker else "none"))
    actions = payload.get("last_actions", [])
    if actions:
        print("Recent actions:")
        for action in actions[-5:]:
            text = " | ".join(
                str(value)
                for value in (
                    action.get("kind"),
                    action.get("tool"),
                    action.get("status"),
                    action.get("message"),
                )
                if value
            )
            print(f"  - {text}")


def interactive(manager: EllipsisAgentManager, state: EllipsisAgentState) -> int:
    print()
    _print_status(manager, state, refresh=False)
    print(
        "\nCommands: /status /cost /diff /tests /continue /stop /detach "
        "/attach /rollback /report"
    )
    pump = EventPump(manager, state)
    pump.start()
    try:
        while True:
            try:
                line = input("\nkarox> ").strip()
            except EOFError:
                line = "/detach"
            if not line:
                continue
            if line == "/status":
                _print_status(manager, state)
            elif line == "/cost":
                state, _ = manager.status(state.local_session_id)
                print(
                    f"{state.cost:g} {state.currency}"
                    if state.cost is not None
                    else "Ellipsis has not reported cost yet."
                )
            elif line == "/diff":
                result = manager.local_call(state.local_session_id, "karox.git.diff", {})
                print(result.get("data", {}).get("stdout", ""))
            elif line == "/tests":
                result = manager.report(state.local_session_id)
                data = result.get("data", result)
                checks = data.get("checks", []) if isinstance(data, dict) else []
                print(json.dumps(checks, ensure_ascii=False, indent=2))
            elif line == "/continue":
                manager.send(
                    state.local_session_id,
                    "Continue the current task. Re-check local state through "
                    "karox-remote before taking any action.",
                )
            elif line == "/stop":
                pump.stop()
                manager.stop(state.local_session_id)
                print("Stopped. Local working-tree changes were kept.")
                return 0
            elif line == "/detach":
                pump.stop()
                detached = manager.detach(state.local_session_id)
                print(f"Detached from {detached.local_session_id}; session remains active.")
                return 0
            elif line == "/attach":
                print("Already attached to this session.")
            elif line == "/rollback":
                print(
                    "Rollback is user-only and separate: run /stop, then "
                    f"`karox agent rollback {state.local_session_id}`."
                )
            elif line == "/report":
                print(
                    json.dumps(
                        manager.report(state.local_session_id),
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            elif line.startswith("/"):
                print("Unknown command.")
            else:
                manager.send(state.local_session_id, line)
    finally:
        pump.stop()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    manager = EllipsisAgentManager()
    command = args.command or "ellipsis"
    try:
        if command in {"ellipsis", "run"}:
            config = _config(args)
            if args.preflight_only:
                result = manager.preflight(config)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            print(f"Local project: {config.repository}")
            print(f"Access profile: {config.access_profile.value}")
            print(f"Model: {config.model}")
            print(f"Budget: {config.budget:g} {config.currency}")
            print(f"Tunnel: {config.tunnel}")
            state = manager.start(config)
            if args.json:
                print(json.dumps(state.public_dict(), ensure_ascii=False, indent=2))
            if args.non_interactive:
                manager.detach(state.local_session_id)
                return 0
            return interactive(manager, state)
        if command == "attach":
            state = manager.states.resolve(args.session_id)
            state.detached = False
            manager.states.save(state)
            return interactive(manager, state)
        if command == "status":
            state = manager.states.resolve(args.session_id)
            _print_status(manager, state, as_json=args.json)
            return 0
        if command == "send":
            print(manager.send(args.session_id, args.message).local_session_id)
            return 0
        if command == "stop":
            state = manager.stop(args.session_id)
            print(f"Stopped {state.local_session_id}. Local changes were kept.")
            return 0
        if command == "result":
            print(json.dumps(manager.report(args.session_id), ensure_ascii=False, indent=2))
            return 0
        if command == "rollback":
            state = manager.states.resolve(args.session_id)
            if not args.yes:
                if not sys.stdin.isatty():
                    raise EllipsisRuntimeError("rollback requires --yes")
                value = input(
                    f"Type ROLLBACK to restore checkpoint {state.checkpoint_id}: "
                ).strip()
                if value != "ROLLBACK":
                    print("Rollback cancelled.")
                    return 1
            print(
                json.dumps(
                    manager.rollback(state.local_session_id),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        parser.print_help()
        return 2
    except (EllipsisRuntimeError, ValueError, OSError) as exc:
        print(f"KaroX Ellipsis error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
