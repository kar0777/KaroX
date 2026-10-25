"""Run and compare identical KaroX tasks without sharing runtime state.

The runner deliberately uses argument vectors rather than a shell. Agent output,
independent-check output, and a compact metrics document are preserved together;
inherited credential values are redacted from every captured log.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Sequence

_SCHEMA_VERSION = 1
_SECRET_NAME = re.compile(r"(?:API.?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", re.IGNORECASE)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _command_json(value: str, *, label: str = "command JSON") -> list[str]:
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a JSON array of arguments") from exc
    if (
        not isinstance(decoded, list)
        or not decoded
        or any(not isinstance(item, str) or not item for item in decoded)
    ):
        raise ValueError(f"{label} must be a non-empty JSON array of strings")
    return list(decoded)


def _secret_values(extra_names: Sequence[str] = ()) -> list[str]:
    names = {name for name in os.environ if _SECRET_NAME.search(name)}
    names.update(extra_names)
    values = {
        os.environ[name]
        for name in names
        if name in os.environ and len(os.environ[name]) >= 4
    }
    return sorted(values, key=len, reverse=True)


def _redact(text: str, secrets: Sequence[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, "<redacted>")
    return text


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _outside_repository(path: Path, repository: Path) -> bool:
    try:
        path.relative_to(repository)
    except ValueError:
        return True
    return False


def _prepare_output(output: Path, repository: Path) -> None:
    resolved = output.expanduser().resolve()
    if not _outside_repository(resolved, repository):
        raise ValueError("evidence, config, and runtime output must live outside the repository")
    if resolved.exists() and any(resolved.iterdir()):
        raise ValueError(f"output directory is not empty: {resolved}")
    resolved.mkdir(parents=True, exist_ok=True)


def _run_process(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout: float,
) -> dict[str, Any]:
    started = time.monotonic()
    timed_out = False
    try:
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            start_new_session=os.name != "nt",
        )
    except OSError as exc:
        return {
            "exit_code": 127,
            "timed_out": False,
            "wall_seconds": round(time.monotonic() - started, 3),
            "stdout": "",
            "stderr": f"cannot start process: {exc}",
        }
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        exit_code = int(process.returncode)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        try:
            stdout, stderr = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
        exit_code = 124
    return {
        "exit_code": exit_code,
        "timed_out": timed_out,
        "wall_seconds": round(time.monotonic() - started, 3),
        "stdout": stdout or "",
        "stderr": stderr or "",
    }


def _stream_metrics(stdout: str) -> tuple[dict[str, int], int, dict[str, Any] | None]:
    counts: Counter[str] = Counter()
    malformed = 0
    report: dict[str, Any] | None = None
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            document = json.loads(line)
        except ValueError:
            malformed += 1
            continue
        if not isinstance(document, dict):
            malformed += 1
            continue
        if document.get("type") == "event" and isinstance(document.get("kind"), str):
            counts[document["kind"]] += 1
        if document.get("type") == "result" and isinstance(document.get("report"), dict):
            report = document["report"]
    return dict(sorted(counts.items())), malformed, report


def _usage(report: dict[str, Any] | None) -> dict[str, Any]:
    if report is None or not isinstance(report.get("usage"), dict):
        return {}
    usage = report["usage"]
    keys = (
        "requests",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "total_tokens",
        "costs",
    )
    return {key: usage[key] for key in keys if key in usage}


def _engine(report: dict[str, Any] | None) -> Any:
    if report is None:
        return None
    if "engine" in report:
        return report["engine"]
    if "engine_version" in report:
        return {"name": "karo-core", "version": report["engine_version"]}
    return None


def run_task(args: argparse.Namespace) -> int:
    repository = args.repository.expanduser().resolve(strict=True)
    if not repository.is_dir():
        raise ValueError(f"repository is not a directory: {repository}")
    if not args.task.strip() or not args.label.strip():
        raise ValueError("label and task must not be empty")
    if bool(args.model) != bool(args.base_url):
        raise ValueError("--model and --base-url must be supplied together")
    for name in ("max_steps", "max_seconds", "timeout_seconds", "check_timeout_seconds"):
        if getattr(args, name) <= 0:
            raise ValueError(f"--{name.replace('_', '-')} must be positive")
    for name in ("max_output_tokens", "context_window"):
        value = getattr(args, name)
        if value is not None and value <= 0:
            raise ValueError(f"--{name.replace('_', '-')} must be positive")

    output = args.output_dir.expanduser().resolve()
    _prepare_output(output, repository)
    base_command = (
        _command_json(args.command_json)
        if args.command_json
        else [sys.executable, "-m", "karox.cli"]
    )
    independent_check = (
        _command_json(args.independent_check_json, label="independent-check command JSON")
        if args.independent_check_json
        else None
    )
    session_id = args.session_id or f"race-{uuid.uuid4().hex[:16]}"
    command = [
        *base_command,
        "agent",
        "run",
        "--repository",
        str(repository),
        "--task",
        args.task,
        "--session-id",
        session_id,
        "--stream-json",
        "--max-steps",
        str(args.max_steps),
        "--max-seconds",
        str(args.max_seconds),
    ]
    for flag, value in (
        ("--max-output-tokens", args.max_output_tokens),
        ("--context-window", args.context_window),
        ("--model", args.model),
        ("--base-url", args.base_url),
        ("--api-key-env", args.api_key_env),
    ):
        if value is not None:
            command.extend((flag, str(value)))
    for verification in args.verification_command:
        command.extend(("--verification-command", verification))

    run_spec = {
        "task": args.task,
        "model": args.model,
        "base_url": args.base_url,
        "api_key_env": args.api_key_env,
        "max_steps": args.max_steps,
        "max_seconds": args.max_seconds,
        "max_output_tokens": args.max_output_tokens,
        "context_window": args.context_window,
        "timeout_seconds": args.timeout_seconds,
        "check_timeout_seconds": args.check_timeout_seconds,
        "verification_commands": list(args.verification_command),
        "independent_check": independent_check,
    }
    fingerprint = sha256(
        json.dumps(run_spec, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    environment = os.environ.copy()
    environment.update(
        {
            "KAROX_CONFIG_DIR": str(output / "config"),
            "KAROX_RUNTIME_DIR": str(output / "runtime"),
            "MSWEA_GLOBAL_CONFIG_DIR": str(output / "mswea-config"),
            "MSWEA_SILENT_STARTUP": "1",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    secrets = _secret_values((args.api_key_env,) if args.api_key_env else ())
    started_at = _utc_now()
    completed = _run_process(
        command,
        cwd=repository,
        environment=environment,
        timeout=args.timeout_seconds,
    )
    finished_at = _utc_now()
    stdout = _redact(completed["stdout"], secrets)
    stderr = _redact(completed["stderr"], secrets)
    (output / "stdout.ndjson").write_text(stdout, encoding="utf-8")
    (output / "stderr.txt").write_text(stderr, encoding="utf-8")
    event_counts, malformed, report = _stream_metrics(stdout)

    check_metrics: dict[str, Any] | None = None
    if independent_check is not None:
        check = _run_process(
            independent_check,
            cwd=repository,
            environment=environment,
            timeout=args.check_timeout_seconds,
        )
        check_stdout = _redact(check["stdout"], secrets)
        check_stderr = _redact(check["stderr"], secrets)
        (output / "check.stdout.txt").write_text(check_stdout, encoding="utf-8")
        (output / "check.stderr.txt").write_text(check_stderr, encoding="utf-8")
        check_metrics = {
            "command": [_redact(item, secrets) for item in independent_check],
            "exit_code": check["exit_code"],
            "timed_out": check["timed_out"],
            "wall_seconds": check["wall_seconds"],
            "passed": check["exit_code"] == 0 and not check["timed_out"],
        }

    independently_verified = check_metrics["passed"] if check_metrics is not None else None
    task_passed = None
    if independently_verified is not None:
        task_passed = bool(
            completed["exit_code"] == 0
            and not completed["timed_out"]
            and report is not None
            and independently_verified
        )
    agent_verified = report.get("verified") if report is not None else None
    if not isinstance(agent_verified, bool):
        agent_verified = None
    metrics = {
        "schema_version": _SCHEMA_VERSION,
        "label": args.label,
        "session_id": session_id,
        "repository": str(repository),
        "started_at": started_at,
        "finished_at": finished_at,
        "wall_seconds": completed["wall_seconds"],
        "exit_code": completed["exit_code"],
        "timed_out": completed["timed_out"],
        "command": [_redact(item, secrets) for item in command],
        "run_spec": run_spec,
        "fairness_sha256": fingerprint,
        "event_counts": event_counts,
        "malformed_output_lines": malformed,
        "result_present": report is not None,
        "status": report.get("status") if report is not None else None,
        "engine": _engine(report),
        "usage": _usage(report),
        "agent_verified": agent_verified,
        "independent_check": check_metrics,
        "independently_verified": independently_verified,
        "task_passed": task_passed,
    }
    _write_json(output / "metrics.json", metrics)
    print(json.dumps(metrics, ensure_ascii=False, sort_keys=True))
    if completed["timed_out"]:
        return 124
    if completed["exit_code"] != 0:
        return int(completed["exit_code"])
    if report is None:
        return 1
    if independently_verified is False:
        return 1
    return 0


def _comparison_run(metrics: dict[str, Any]) -> dict[str, Any]:
    usage = metrics.get("usage") if isinstance(metrics.get("usage"), dict) else {}
    return {
        "label": metrics.get("label"),
        "wall_seconds": metrics.get("wall_seconds"),
        "total_tokens": usage.get("total_tokens"),
        "costs": usage.get("costs"),
        "engine": metrics.get("engine"),
        "agent_verified": metrics.get("agent_verified"),
        "independently_verified": metrics.get("independently_verified"),
        "task_passed": metrics.get("task_passed"),
    }


def compare_tasks(args: argparse.Namespace) -> int:
    first = json.loads(args.first.read_text(encoding="utf-8"))
    second = json.loads(args.second.read_text(encoding="utf-8"))
    if not isinstance(first, dict) or not isinstance(second, dict):
        raise ValueError("metrics documents must be JSON objects")
    first_spec = first.get("run_spec")
    second_spec = second.get("run_spec")
    if not isinstance(first_spec, dict) or not isinstance(second_spec, dict):
        raise ValueError("metrics documents must contain run_spec objects")
    mismatches = sorted(
        key
        for key in set(first_spec) | set(second_spec)
        if first_spec.get(key) != second_spec.get(key)
    )
    first_run = _comparison_run(first)
    second_run = _comparison_run(second)
    wall_delta = None
    token_delta = None
    if isinstance(first_run["wall_seconds"], (int, float)) and isinstance(
        second_run["wall_seconds"], (int, float)
    ):
        wall_delta = round(second_run["wall_seconds"] - first_run["wall_seconds"], 3)
    if isinstance(first_run["total_tokens"], (int, float)) and isinstance(
        second_run["total_tokens"], (int, float)
    ):
        token_delta = second_run["total_tokens"] - first_run["total_tokens"]
    comparison = {
        "schema_version": _SCHEMA_VERSION,
        "fair": not mismatches,
        "mismatches": mismatches,
        "first": first_run,
        "second": second_run,
        "delta": {"wall_seconds": wall_delta, "total_tokens": token_delta},
        "note": "second minus first; fairness does not declare a winner",
    }
    _write_json(args.output, comparison)
    print(json.dumps(comparison, ensure_ascii=False, sort_keys=True))
    return 0 if not mismatches else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run one isolated contender")
    run.add_argument("--label", required=True)
    run.add_argument("--repository", required=True, type=Path)
    run.add_argument("--task", required=True)
    run.add_argument("--output-dir", required=True, type=Path)
    run.add_argument("--command-json", help="JSON argv prefix; defaults to python -m karox.cli")
    run.add_argument("--session-id")
    run.add_argument("--model")
    run.add_argument("--base-url")
    run.add_argument("--api-key-env")
    run.add_argument("--max-steps", type=int, default=20)
    run.add_argument("--max-seconds", type=int, default=600)
    run.add_argument("--max-output-tokens", type=int)
    run.add_argument("--context-window", type=int)
    run.add_argument("--timeout-seconds", type=float, default=630)
    run.add_argument("--check-timeout-seconds", type=float, default=120)
    run.add_argument("--verification-command", action="append", default=[])
    run.add_argument("--independent-check-json")
    run.set_defaults(handler=run_task)

    compare = commands.add_parser("compare", help="compare two metrics documents")
    compare.add_argument("first", type=Path)
    compare.add_argument("second", type=Path)
    compare.add_argument("--output", type=Path, required=True)
    compare.set_defaults(handler=compare_tasks)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (OSError, ValueError) as exc:
        print(f"race_task: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
