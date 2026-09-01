"""KaroX autonomy/reliability release gate.

This orchestrator is intentionally repository-local and non-interactive. It runs
small deterministic regression groups plus the isolated long-job/reconnect
acceptance. It never touches a saved bridge, browser profile, credential, tunnel,
or user project outside this checkout.

Use ``--quick`` during development and ``--full`` before a release. The full mode
keeps the long job alive beyond the historical hosted-request threshold.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Scenario:
    name: str
    argv: tuple[str, ...]
    timeout: float


def _run(scenario: Scenario) -> dict[str, object]:
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            list(scenario.argv),
            cwd=ROOT,
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=scenario.timeout,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        exit_code: int | None = int(completed.returncode)
        timed_out = False
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as exc:
        exit_code = None
        timed_out = True
        stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
    duration = round(time.perf_counter() - started, 3)
    return {
        "name": scenario.name,
        "ok": exit_code == 0 and not timed_out,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_seconds": duration,
        "stdout_tail": stdout[-4000:],
        "stderr_tail": stderr[-4000:],
    }


def _scenarios(*, full: bool, result_path: Path) -> tuple[Scenario, ...]:
    py = sys.executable
    pytest_targets = (
        "tests/test_task_state.py",
        "tests/test_autonomy_runtime.py",
        "tests/test_repository_lease.py",
        "tests/test_core_bridge_repository_lease.py",
        "tests/test_command_guard.py",
        "tests/test_durable_command_compat.py",
        "tests/test_check_jobs.py",
        "tests/test_durable_job_workstream_scope.py",
        "tests/test_runtime_restart.py",
        "tests/test_hosted_bridge_health.py",
        "tests/test_route_health_fast_recovery.py",
        "tests/test_saved_bridge_service.py",
        "tests/test_proxy_transport_telemetry.py",
        "tests/test_browser_takeover_persistence.py",
        "tests/test_hosted_tools_runtime.py",
        "tests/test_plan_executor.py",
        "tests/test_cli_doctor_autonomy.py",
        "tests/test_cli_status_autonomy.py",
        "tests/test_cli_jobs.py",
        "tests/test_status_command.py",
        "tests/test_tui_jobs_command.py",
    )
    duration = 230.0 if full else 45.0
    probe_after = 212.0 if full else 25.0
    return (
        Scenario(
            "focused-autonomy-regressions",
            (py, "-m", "pytest", "-q", *pytest_targets),
            300.0,
        ),
        Scenario(
            "reliability-ruff",
            (
                py,
                "-m",
                "ruff",
                "check",
                "src/karox/autonomy_runtime.py",
                "src/karox/task_state.py",
                "src/karox/repository_lease.py",
                "src/karox/command_guard.py",
                "src/karox/core_tools.py",
                "src/karox/check_jobs.py",
                "src/karox/repo_context.py",
                "src/karox/disk_maintenance.py",
                "src/karox/hosted_tools_runtime.py",
                "src/karox/hosted_bridge.py",
                "src/karox/proxy_server.py",
                "src/karox/web_bridge_launcher.py",
                "src/karox/cli.py",
                "src/karox/tui.py",
                "tests/test_autonomy_runtime.py",
                "tests/test_task_state.py",
                "tests/test_repository_lease.py",
                "tests/test_core_bridge_repository_lease.py",
                "tests/test_command_guard.py",
                "tests/test_durable_command_compat.py",
                "tests/test_durable_job_workstream_scope.py",
                "tests/test_cli_doctor_autonomy.py",
                "tests/test_cli_status_autonomy.py",
                "tests/test_cli_jobs.py",
                "tests/test_status_command.py",
                "tests/test_tui_jobs_command.py",
            ),
            60.0,
        ),
        Scenario(
            "reliability-mypy",
            (
                py,
                "-m",
                "mypy",
                "src/karox/command_guard.py",
                "src/karox/core_tools.py",
                "src/karox/check_jobs.py",
                "src/karox/repository_lease.py",
                "src/karox/hosted_bridge.py",
                "src/karox/task_state.py",
                "src/karox/autonomy_runtime.py",
                "src/karox/repo_context.py",
                "src/karox/disk_maintenance.py",
                "src/karox/cli.py",
            ),
            60.0,
        ),
        Scenario(
            "isolated-long-job-reconnect-restart",
            (
                py,
                "scripts/long_job_bridge_acceptance.py",
                "--orchestrate",
                str(result_path),
                "--duration",
                str(duration),
                "--probe-after",
                str(probe_after),
            ),
            duration + 90.0,
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the KaroX autonomy reliability gauntlet")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--quick", action="store_true", help="45s isolated long-job acceptance")
    mode.add_argument("--full", action="store_true", help="230s acceptance beyond the old request threshold")
    parser.add_argument("--json", action="store_true", help="emit one machine-readable report")
    parser.add_argument("--output", type=Path, default=None, help="optional report path")
    args = parser.parse_args(list(argv) if argv is not None else None)

    full = bool(args.full)
    temp_result = Path(tempfile.gettempdir()) / f"karox-autonomy-gauntlet-{os.getpid()}.json"
    results: list[dict[str, object]] = []
    started = time.perf_counter()
    for scenario in _scenarios(full=full, result_path=temp_result):
        result = _run(scenario)
        results.append(result)
        if not result["ok"]:
            break

    acceptance: dict[str, object] | None = None
    if temp_result.is_file():
        try:
            raw = json.loads(temp_result.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                acceptance = raw
        except (OSError, json.JSONDecodeError):
            acceptance = None
        try:
            temp_result.unlink()
        except OSError:
            pass

    passed = sum(bool(item["ok"]) for item in results)
    report = {
        "schema_version": 1,
        "mode": "full" if full else "quick",
        "ok": bool(results) and passed == len(results),
        "passed": passed,
        "total": len(results),
        "duration_seconds": round(time.perf_counter() - started, 3),
        "scenarios": results,
        "acceptance": (
            {
                "status": acceptance.get("status"),
                "same_bridge_pid": acceptance.get("same_bridge_pid"),
                "bridge_alive_after_job": acceptance.get("bridge_alive_after_job"),
                "runtime_after_job_ok": acceptance.get("runtime_after_job_ok"),
                "threshold_probe_completed": acceptance.get("threshold_probe_completed"),
                "restart_recovery": acceptance.get("restart_recovery"),
                "client_disconnect": acceptance.get("client_disconnect"),
                "idempotent_replay": (
                    (acceptance.get("quick_scenarios") or {}).get("idempotent_replay")
                    if isinstance(acceptance.get("quick_scenarios"), dict)
                    else None
                ),
            }
            if acceptance is not None
            else None
        ),
    }
    if args.output is not None:
        target = args.output.expanduser().resolve(strict=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        status = "PASS" if report["ok"] else "FAIL"
        print(f"KaroX autonomy gauntlet: {status} ({passed}/{len(results)})")
        for item in results:
            print(f"- {item['name']}: {'PASS' if item['ok'] else 'FAIL'} ({item['duration_seconds']}s)")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
