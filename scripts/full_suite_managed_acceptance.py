"""Run the authoritative source-tree pytest suite as a detached managed job."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SCRIPTS = ROOT / "scripts"
for entry in (SRC, SCRIPTS):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import long_job_bridge_acceptance as lifecycle  # noqa: E402
from karox.hosted_tools_runtime import CHECKS_LOGS, CHECKS_STATUS  # noqa: E402
from karox.proxy_server import wire_tool_name  # noqa: E402


def _parse_summary(text: str) -> dict[str, Any]:
    final_line = None
    first_failure = None
    progress = None
    for line in text.splitlines():
        stripped = line.strip()
        match = re.search(r"\[\s*(\d{1,3})%\]", stripped)
        if match:
            progress = int(match.group(1))
        if first_failure is None and stripped.startswith(("FAILED ", "ERROR ", "E   ")):
            first_failure = stripped[:1000]
        if re.search(r"\b(passed|failed|skipped|xfailed|errors?)\b", stripped, re.I):
            final_line = stripped[:1000]
    return {
        "final_line": final_line,
        "first_failure": first_failure,
        "progress": progress,
    }


def _copy_full_log(runtime: Path, session_id: str, job_id: str, output: Path) -> dict[str, Any]:
    state_path = runtime / "vnext" / "check-jobs" / session_id / f"{job_id}.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    log_path = Path(str(state["log_path"]))
    raw = log_path.read_bytes()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_bytes(raw)
    os.replace(temporary, output)
    text = raw.decode("utf-8", errors="replace")
    return {
        "path": str(output),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        **_parse_summary(text),
    }


def _orchestrate(result_path: Path, repository: Path) -> int:
    result_path = result_path.resolve()
    repository = repository.resolve(strict=True)
    output_path = result_path.with_name(result_path.stem + "_output.txt")
    work = Path(tempfile.mkdtemp(prefix="karox-full-suite-managed-"))
    runtime = work / "runtime"
    config_root = work / "config"
    sessions = work / "sessions"
    server_config = work / "server.json"
    token = "synthetic-full-suite-" + os.urandom(12).hex()
    port = lifecycle._free_port()
    session_id = "full-suite-managed-acceptance"
    command = (sys.executable, "-m", "pytest")
    lifecycle._atomic_json(
        server_config,
        {
            "repository": str(repository),
            "runtime": str(runtime),
            "config": str(config_root),
            "sessions": str(sessions),
            "port": port,
            "session_id": session_id,
            "command": list(command),
            "commands": [list(command)],
        },
    )
    environment = lifecycle._source_environment(runtime, config_root)
    environment["KAROX_ACCEPTANCE_TOKEN"] = token
    server = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "scripts" / "long_job_bridge_acceptance.py"),
            "--server",
            str(server_config),
        ],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        creationflags=lifecycle._creation_flags(breakaway=False),
        start_new_session=(os.name != "nt"),
    )
    started_at = time.time()
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "status": "running",
        "started_at": started_at,
        "repository": str(repository),
        "exact_command": list(command),
        "isolated": {
            "runtime": str(runtime),
            "session_id": session_id,
            "port": port,
            "profile": "chatgpt-autonomy-full-suite-acceptance",
            "tunnel": "none",
        },
        "bridge_pid_before": server.pid,
        "bridge_pid_after": None,
        "runtime_probes": [],
        "job": {},
        "output": None,
        "error": None,
    }
    lifecycle._atomic_json(result_path, evidence)
    try:
        lifecycle._wait_port(port, server)
        url = f"http://127.0.0.1:{port}/mcp"
        before = lifecycle._payload(
            lifecycle._call(url, token, wire_tool_name("karox.runtime.status"), {})
        )
        start_clock = time.perf_counter()
        started = lifecycle._start_job(
            url,
            token,
            command,
            key="authoritative-full-suite-v1",
            timeout_seconds=3600.0,
        )
        start_latency = time.perf_counter() - start_clock
        if not started.get("ok"):
            raise RuntimeError(f"checks.start failed: {started}")
        job_id = str(started["job_id"])
        job_started_at = time.time()
        evidence["job"] = {
            "job_id": job_id,
            "start_latency_seconds": round(start_latency, 3),
            "initial_status": started.get("status"),
            "worker_pid": started.get("worker_pid"),
            "child_pid": started.get("child_pid"),
            "command_fingerprint": started.get("command_fingerprint"),
            "started_at": job_started_at,
        }
        evidence["runtime_probes"].append(
            {
                "elapsed_seconds": 0.0,
                "runtime_ok": bool(before.get("ok", True)),
                "bridge_pid": server.pid,
                "job_status": started.get("status"),
            }
        )
        lifecycle._atomic_json(result_path, evidence)
        next_runtime_probe = 30.0
        while True:
            if server.poll() is not None:
                raise RuntimeError(f"isolated full-suite bridge died: {server.returncode}")
            status = lifecycle._payload(
                lifecycle._call(
                    url,
                    token,
                    wire_tool_name(CHECKS_STATUS),
                    {"job_id": job_id},
                )
            )
            elapsed = time.time() - job_started_at
            evidence["job"]["latest"] = status
            if elapsed >= next_runtime_probe:
                runtime_status = lifecycle._payload(
                    lifecycle._call(url, token, wire_tool_name("karox.runtime.status"), {})
                )
                evidence["runtime_probes"].append(
                    {
                        "elapsed_seconds": round(elapsed, 3),
                        "runtime_ok": bool(runtime_status.get("ok", True)),
                        "bridge_pid": server.pid,
                        "job_status": status.get("status"),
                    }
                )
                next_runtime_probe += 30.0
            lifecycle._atomic_json(result_path, evidence)
            if status.get("status") in {"passed", "failed", "cancelled", "timed_out"}:
                evidence["job"]["final"] = status
                break
            time.sleep(2.0)

        logs = lifecycle._payload(
            lifecycle._call(
                url,
                token,
                wire_tool_name(CHECKS_LOGS),
                {"job_id": job_id, "limit": 1024 * 1024},
            )
        )
        evidence["job"]["bounded_log"] = {
            "bytes": logs.get("log", {}).get("bytes"),
            "truncated": logs.get("log", {}).get("truncated"),
            "artifact_id": logs.get("artifact_id"),
        }
        evidence["output"] = _copy_full_log(runtime, session_id, job_id, output_path)
        after = lifecycle._payload(
            lifecycle._call(url, token, wire_tool_name("karox.runtime.status"), {})
        )
        evidence["bridge_pid_after"] = server.pid
        evidence["same_bridge_pid"] = evidence["bridge_pid_before"] == server.pid
        evidence["bridge_alive_after_suite"] = server.poll() is None
        evidence["runtime_after_suite_ok"] = bool(after.get("ok", True))
        evidence["finished_at"] = time.time()
        evidence["duration_seconds"] = round(time.time() - started_at, 3)
        evidence["status"] = (
            "passed" if evidence["job"]["final"].get("status") == "passed" else "failed"
        )
        lifecycle._atomic_json(result_path, evidence)
        return 0 if evidence["status"] == "passed" else 1
    except BaseException as exc:
        evidence["status"] = "failed"
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        evidence["bridge_pid_after"] = server.pid
        evidence["bridge_alive_after_error"] = server.poll() is None
        evidence["finished_at"] = time.time()
        evidence["duration_seconds"] = round(time.time() - started_at, 3)
        evidence["retained_work"] = str(work)
        lifecycle._atomic_json(result_path, evidence)
        return 1
    finally:
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=10)
        if evidence.get("status") == "passed":
            try:
                shutil.rmtree(work)
            except OSError:
                pass


def _wmi_launch(result_path: Path, repository: Path) -> int:
    argv = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--orchestrate",
        str(result_path.resolve()),
        "--repository",
        str(repository.resolve()),
    ]
    if os.name != "nt":
        process = subprocess.Popen(
            argv,
            cwd=ROOT,
            env=lifecycle._source_environment(
                Path(tempfile.gettempdir()) / "karox-full-suite-launch-runtime",
                Path(tempfile.gettempdir()) / "karox-full-suite-launch-config",
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        print(process.pid)
        return 0
    command_line = subprocess.list2cmdline(argv).replace("'", "''")
    powershell = (
        "$result = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
        f"-Arguments @{{CommandLine='{command_line}'}}; "
        "if ($result.ReturnValue -ne 0) { exit $result.ReturnValue }; "
        "Write-Output $result.ProcessId"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", powershell],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"WMI full-suite launch failed: {completed.stderr[:500]}")
    print(int(completed.stdout.strip().splitlines()[-1]))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--orchestrate", type=Path)
    mode.add_argument("--wmi-launch", type=Path)
    parser.add_argument("--repository", type=Path, required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.orchestrate is not None:
        return _orchestrate(args.orchestrate, args.repository)
    assert args.wmi_launch is not None
    return _wmi_launch(args.wmi_launch, args.repository)


if __name__ == "__main__":
    raise SystemExit(main())
