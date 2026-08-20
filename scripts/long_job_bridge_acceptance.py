"""Isolated Windows acceptance for bridge-safe long-running verification jobs.

The orchestrator creates a disposable repository/runtime, starts a local-only
MCP bridge on a separate port, starts a managed command that outlives the former
~200 second hosted-request threshold, and uses fresh MCP clients to prove the
same bridge PID still answers after 210+ seconds. No live saved profile, tunnel,
credential, browser, or user repository is touched.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from karox.hosted_bridge import CompositeHostedBridge, CoreToolBridge  # noqa: E402
from karox.hosted_tools_runtime import (  # noqa: E402
    CHECKS_CANCEL,
    CHECKS_LOGS,
    CHECKS_START,
    CHECKS_STATUS,
    HostedToolsRuntime,
)
from karox.mcp_client import streamable_http_transport  # noqa: E402
from karox.models import AccessProfile, Origin, OriginKind  # noqa: E402
from karox.proxy_server import build_proxy_asgi_app, wire_tool_name  # noqa: E402
from karox.sessions import SessionStore  # noqa: E402


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as handle:
        handle.bind(("127.0.0.1", 0))
        return int(handle.getsockname()[1])


def _wait_port(port: int, process: subprocess.Popen[Any], timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"isolated bridge exited early with code {process.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("isolated bridge did not open its local port")


def _abrupt_client_disconnect(port: int, token: str) -> None:
    """Send a valid MCP initialize request and abort before reading its response."""

    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 987654,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "disconnect-probe", "version": "1"},
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")
    request = (
        f"POST /mcp HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        f"Authorization: Bearer {token}\r\n"
        "Content-Type: application/json\r\n"
        "Accept: application/json, text/event-stream\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii") + body
    handle = socket.create_connection(("127.0.0.1", port), timeout=5.0)
    try:
        handle.sendall(request)
        # Abortive close forces a real client disconnect instead of a graceful
        # response drain. The next fresh client must still reach the same bridge.
        handle.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("hh", 1, 0))
    finally:
        handle.close()


def _creation_flags(*, breakaway: bool = False) -> int:
    if os.name != "nt":
        return 0
    flags = int(
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    )
    if breakaway:
        flags |= int(getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000))
    return flags


def _source_environment(runtime: Path, config: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(SRC)
    environment["KAROX_VNEXT_RUNTIME_DIR"] = str(runtime)
    environment["KAROX_VNEXT_CONFIG_DIR"] = str(config)
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    return environment


async def _call_async(
    url: str,
    token: str,
    name: str,
    arguments: dict[str, object],
    *,
    meta: Optional[dict[str, object]] = None,
    timeout_seconds: float = 30.0,
) -> Any:
    from mcp import ClientSession

    headers = {"Authorization": f"Bearer {token}"}
    async with streamable_http_transport(
        url,
        headers=headers,
        timeout_seconds=timeout_seconds,
    ) as streams:
        async with ClientSession(
            streams[0],
            streams[1],
            read_timeout_seconds=timedelta(seconds=timeout_seconds),
        ) as session:
            await session.initialize()
            return await session.call_tool(name, arguments, meta=meta)


def _call(
    url: str,
    token: str,
    name: str,
    arguments: dict[str, object],
    *,
    meta: Optional[dict[str, object]] = None,
    timeout_seconds: float = 30.0,
) -> Any:
    import anyio

    async def invoke() -> Any:
        return await _call_async(
            url,
            token,
            name,
            arguments,
            meta=meta,
            timeout_seconds=timeout_seconds,
        )

    return anyio.run(invoke)


def _payload(result: Any) -> dict[str, Any]:
    value = getattr(result, "structuredContent", None)
    if not isinstance(value, dict):
        text = "".join(getattr(item, "text", "") for item in getattr(result, "content", []))
        value = json.loads(text)
    if not isinstance(value, dict):
        raise RuntimeError("MCP tool returned no structured object")
    return value


def _start_job(
    url: str,
    token: str,
    command: Sequence[str],
    *,
    key: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    return _payload(
        _call(
            url,
            token,
            wire_tool_name(CHECKS_START),
            {
                "kind": "check",
                "argv": list(command),
                "timeout_seconds": timeout_seconds,
            },
            meta={"karoxIdempotencyKey": key},
        )
    )


def _wait_job(
    url: str,
    token: str,
    job_id: str,
    *,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = _payload(
            _call(url, token, wire_tool_name(CHECKS_STATUS), {"job_id": job_id})
        )
        if last.get("status") in {"passed", "failed", "cancelled", "timed_out"}:
            return last
        time.sleep(0.1)
    raise RuntimeError(f"managed job did not finish: {last}")


def _git_init(repository: Path) -> None:
    repository.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        ["git", "init", "--quiet"],
        cwd=repository,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=20,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"git init failed: {completed.stderr[:300]}")
    (repository / "README.md").write_text("# isolated acceptance\n", encoding="utf-8")


def _server(config_path: Path) -> int:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    repository = Path(config["repository"]).resolve(strict=True)
    runtime_root = Path(config["runtime"]).resolve()
    config_root = Path(config["config"]).resolve()
    os.environ["KAROX_VNEXT_RUNTIME_DIR"] = str(runtime_root)
    os.environ["KAROX_VNEXT_CONFIG_DIR"] = str(config_root)
    token = os.environ.get("KAROX_ACCEPTANCE_TOKEN", "")
    if not token:
        raise RuntimeError("synthetic acceptance token is missing")
    port = int(config["port"])
    session_id = str(config["session_id"])
    raw_commands = config.get("commands")
    if isinstance(raw_commands, list) and raw_commands:
        commands = tuple(tuple(str(item) for item in command) for command in raw_commands)
    else:
        commands = (tuple(str(item) for item in config["command"]),)
    sessions = SessionStore(Path(config["sessions"]))
    if not sessions.state_path(session_id).exists():
        sessions.create(
            repository,
            "isolated long-job acceptance",
            AccessProfile.ELEVATED,
            session_id=session_id,
        )
    core = CoreToolBridge(
        repository,
        sessions,
        session_id,
        ("karox.runtime.status",),
        hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "acceptance-core"),
    )
    checks = HostedToolsRuntime(
        repository,
        sessions,
        session_id,
        (CHECKS_START, CHECKS_STATUS, CHECKS_LOGS, CHECKS_CANCEL),
        access_profile=AccessProfile.ELEVATED,
        hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "acceptance-checks"),
        verification_commands=commands,
    )
    app = build_proxy_asgi_app(
        CompositeHostedBridge((core, checks)),
        token,
        deadline_seconds=3600.0,
        allowed_hosts=("127.0.0.1", "localhost"),
        diagnostics={
            "saved_profile": "chatgpt-autonomy-lifecycle-acceptance",
            "session_id": session_id,
            "repository": str(repository),
            "public_url": None,
            "tunnel": "none",
            "effective_deadline_seconds": 3600.0,
            "available_tools": [
                "karox.runtime.status",
                CHECKS_START,
                CHECKS_STATUS,
                CHECKS_LOGS,
                CHECKS_CANCEL,
            ],
        },
    )
    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
        )
    )
    # The orchestrator owns this isolated server's lifecycle. It has no console
    # and no signal handler capable of turning a child/request interrupt into a
    # bridge shutdown.
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    server.run()
    return 0


def _orchestrate(result_path: Path, duration: float, probe_after: float) -> int:
    result_path = result_path.resolve()
    work = Path(tempfile.mkdtemp(prefix="karox-long-job-acceptance-"))
    repository = work / "repository"
    runtime = work / "runtime"
    config_root = work / "config"
    sessions = work / "sessions"
    server_config = work / "server.json"
    token = "synthetic-acceptance-" + os.urandom(12).hex()
    port = _free_port()
    session_id = "lifecycle-acceptance"
    command = (
        sys.executable,
        "-c",
        (
            "import time; "
            "print('LONG_JOB_STARTED', flush=True); "
            f"time.sleep({duration!r}); "
            "print('LONG_JOB_FINISHED', flush=True)"
        ),
    )
    fail_command = (sys.executable, "-c", "print('EXPECTED_FAILURE'); raise SystemExit(1)")
    interrupt_command = (sys.executable, "-c", "raise KeyboardInterrupt")
    timeout_command = (sys.executable, "-c", "import time; time.sleep(10)")
    cancel_command = (sys.executable, "-c", "import time; time.sleep(20)")
    allowed_commands = (
        command,
        fail_command,
        interrupt_command,
        timeout_command,
        cancel_command,
    )
    _git_init(repository)
    server_payload = {
        "repository": str(repository),
        "runtime": str(runtime),
        "config": str(config_root),
        "sessions": str(sessions),
        "port": port,
        "session_id": session_id,
        "command": list(command),
        "commands": [list(item) for item in allowed_commands],
    }
    _atomic_json(server_config, server_payload)
    environment = _source_environment(runtime, config_root)
    environment["KAROX_ACCEPTANCE_TOKEN"] = token
    server_argv = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--server",
        str(server_config),
    ]
    server = subprocess.Popen(
        server_argv,
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        creationflags=_creation_flags(breakaway=False),
        start_new_session=(os.name != "nt"),
    )
    started_at = time.time()
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "status": "running",
        "started_at": started_at,
        "duration_target_seconds": duration,
        "probe_after_seconds": probe_after,
        "isolated": {
            "repository": str(repository),
            "runtime": str(runtime),
            "session_id": session_id,
            "port": port,
            "profile": "chatgpt-autonomy-lifecycle-acceptance",
            "tunnel": "none",
        },
        "bridge_pid_before": server.pid,
        "bridge_pid_after": None,
        "runtime_probes": [],
        "quick_scenarios": {},
        "job": {},
        "error": None,
    }
    _atomic_json(result_path, evidence)
    try:
        _wait_port(port, server)
        url = f"http://127.0.0.1:{port}/mcp"

        fail_started = _start_job(
            url,
            token,
            fail_command,
            key="expected-failure-v1",
            timeout_seconds=20.0,
        )
        fail_final = _wait_job(url, token, str(fail_started["job_id"]))
        fail_replay = _start_job(
            url,
            token,
            fail_command,
            key="expected-failure-v1",
            timeout_seconds=20.0,
        )
        if fail_final.get("status") != "failed" or fail_final.get("exit_code") != 1:
            raise RuntimeError(f"exit-code scenario failed: {fail_final}")
        if fail_replay.get("job_id") != fail_started.get("job_id") or not fail_replay.get(
            "idempotent_replay"
        ):
            raise RuntimeError("idempotent replay created or reported a different job")
        evidence["quick_scenarios"]["exit_code_1"] = fail_final
        evidence["quick_scenarios"]["idempotent_replay"] = {
            "same_job_id": fail_replay.get("job_id") == fail_started.get("job_id"),
            "reported": bool(fail_replay.get("idempotent_replay")),
        }

        interrupt_started = _start_job(
            url,
            token,
            interrupt_command,
            key="child-keyboard-interrupt-v1",
            timeout_seconds=20.0,
        )
        interrupt_final = _wait_job(url, token, str(interrupt_started["job_id"]))
        if interrupt_final.get("status") != "failed":
            raise RuntimeError(f"child KeyboardInterrupt scenario failed: {interrupt_final}")
        evidence["quick_scenarios"]["child_keyboard_interrupt"] = interrupt_final

        timeout_started = _start_job(
            url,
            token,
            timeout_command,
            key="child-timeout-v1",
            timeout_seconds=1.0,
        )
        timeout_final = _wait_job(url, token, str(timeout_started["job_id"]))
        if timeout_final.get("status") != "timed_out":
            raise RuntimeError(f"child timeout scenario failed: {timeout_final}")
        evidence["quick_scenarios"]["child_timeout"] = timeout_final

        cancel_started = _start_job(
            url,
            token,
            cancel_command,
            key="child-cancel-v1",
            timeout_seconds=30.0,
        )
        time.sleep(0.5)
        cancel_result = _payload(
            _call(
                url,
                token,
                wire_tool_name(CHECKS_CANCEL),
                {"job_id": str(cancel_started["job_id"])},
            )
        )
        cancel_final = _wait_job(url, token, str(cancel_started["job_id"]))
        if cancel_final.get("status") != "cancelled":
            raise RuntimeError(f"child cancel scenario failed: {cancel_final}")
        evidence["quick_scenarios"]["cancel"] = {
            "request": cancel_result,
            "final": cancel_final,
        }
        quick_runtime = _payload(
            _call(url, token, wire_tool_name("karox.runtime.status"), {})
        )
        if server.poll() is not None or not bool(quick_runtime.get("ok", True)):
            raise RuntimeError("bridge did not survive quick lifecycle scenarios")
        evidence["quick_scenarios"]["bridge_alive"] = True
        _atomic_json(result_path, evidence)

        before = _payload(
            _call(url, token, wire_tool_name("karox.runtime.status"), {})
        )
        start_call_started = time.perf_counter()
        started = _payload(
            _call(
                url,
                token,
                wire_tool_name(CHECKS_START),
                {
                    "kind": "check",
                    "argv": list(command),
                    "timeout_seconds": duration + 90.0,
                },
                meta={"karoxIdempotencyKey": "long-job-acceptance-v1"},
            )
        )
        start_latency = time.perf_counter() - start_call_started
        if not started.get("ok"):
            raise RuntimeError(f"checks.start failed: {started}")
        job_id = str(started["job_id"])
        long_started_at = time.time()
        evidence["long_job_started_at"] = long_started_at
        evidence["job"] = {
            "job_id": job_id,
            "start_latency_seconds": round(start_latency, 3),
            "child_pid": started.get("child_pid"),
            "worker_pid": started.get("worker_pid"),
            "command_fingerprint": started.get("command_fingerprint"),
            "initial_status": started.get("status"),
        }
        evidence["runtime_probes"].append(
            {
                "elapsed_seconds": round(time.time() - long_started_at, 3),
                "client": "B-before-threshold",
                "ok": bool(before.get("ok", True)),
                "bridge_pid": server.pid,
            }
        )
        _atomic_json(result_path, evidence)

        _abrupt_client_disconnect(port, token)
        time.sleep(0.25)
        disconnect_runtime = _payload(
            _call(url, token, wire_tool_name("karox.runtime.status"), {})
        )
        disconnect_job = _payload(
            _call(url, token, wire_tool_name(CHECKS_STATUS), {"job_id": job_id})
        )
        if server.poll() is not None or disconnect_job.get("status") != "running":
            raise RuntimeError("client disconnect stopped the bridge or long job")
        evidence["client_disconnect"] = {
            "bridge_pid": server.pid,
            "bridge_alive": True,
            "runtime_ok": bool(disconnect_runtime.get("ok", True)),
            "job_status": disconnect_job.get("status"),
            "fresh_client_succeeded": True,
        }
        _atomic_json(result_path, evidence)

        next_probe = 30.0
        threshold_probed = False
        while True:
            elapsed = time.time() - long_started_at
            if server.poll() is not None:
                raise RuntimeError(f"isolated bridge died with code {server.returncode}")
            if elapsed >= next_probe and not threshold_probed:
                runtime_status = _payload(
                    _call(url, token, wire_tool_name("karox.runtime.status"), {})
                )
                job_status = _payload(
                    _call(url, token, wire_tool_name(CHECKS_STATUS), {"job_id": job_id})
                )
                evidence["runtime_probes"].append(
                    {
                        "elapsed_seconds": round(elapsed, 3),
                        "client": "B-periodic",
                        "runtime_ok": bool(runtime_status.get("ok", True)),
                        "job_status": job_status.get("status"),
                        "bridge_pid": server.pid,
                    }
                )
                next_probe += 60.0
                _atomic_json(result_path, evidence)
            if elapsed >= probe_after and not threshold_probed:
                runtime_status = _payload(
                    _call(url, token, wire_tool_name("karox.runtime.status"), {})
                )
                job_status = _payload(
                    _call(url, token, wire_tool_name(CHECKS_STATUS), {"job_id": job_id})
                )
                if job_status.get("status") != "running":
                    raise RuntimeError(
                        f"long job was not running at threshold probe: {job_status}"
                    )
                evidence["runtime_probes"].append(
                    {
                        "elapsed_seconds": round(elapsed, 3),
                        "client": "C-after-210-seconds",
                        "runtime_ok": bool(runtime_status.get("ok", True)),
                        "job_status": job_status.get("status"),
                        "bridge_pid": server.pid,
                    }
                )
                threshold_probed = True
                _atomic_json(result_path, evidence)
            status = _payload(
                _call(url, token, wire_tool_name(CHECKS_STATUS), {"job_id": job_id})
            )
            if status.get("status") in {"passed", "failed", "cancelled", "timed_out"}:
                evidence["job"]["final"] = status
                break
            time.sleep(2.0)

        logs = _payload(
            _call(
                url,
                token,
                wire_tool_name(CHECKS_LOGS),
                {"job_id": job_id, "limit": 65536},
            )
        )
        if evidence["job"]["final"].get("status") != "passed":
            raise RuntimeError(f"long job did not pass: {evidence['job']['final']}")
        log_text = str(logs.get("log", {}).get("text", ""))
        if "LONG_JOB_FINISHED" not in log_text:
            raise RuntimeError("bounded job log does not contain completion marker")
        after = _payload(
            _call(url, token, wire_tool_name("karox.runtime.status"), {})
        )
        bridge_pid_during_job = server.pid
        evidence["bridge_pid_after"] = bridge_pid_during_job
        evidence["same_bridge_pid"] = (
            evidence["bridge_pid_before"] == bridge_pid_during_job
        )
        evidence["bridge_alive_after_job"] = server.poll() is None
        evidence["runtime_after_job_ok"] = bool(after.get("ok", True))
        evidence["threshold_probe_completed"] = threshold_probed

        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=10)
        server = subprocess.Popen(
            server_argv,
            cwd=ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            creationflags=_creation_flags(breakaway=False),
            start_new_session=(os.name != "nt"),
        )
        _wait_port(port, server)
        restored_status = _payload(
            _call(url, token, wire_tool_name(CHECKS_STATUS), {"job_id": job_id})
        )
        restored_logs = _payload(
            _call(
                url,
                token,
                wire_tool_name(CHECKS_LOGS),
                {"job_id": job_id, "limit": 65536},
            )
        )
        restored_runtime = _payload(
            _call(url, token, wire_tool_name("karox.runtime.status"), {})
        )
        restored_text = str(restored_logs.get("log", {}).get("text", ""))
        if restored_status.get("status") != "passed" or "LONG_JOB_FINISHED" not in restored_text:
            raise RuntimeError("restarted bridge did not recover durable job status/logs")
        evidence["restart_recovery"] = {
            "previous_bridge_pid": bridge_pid_during_job,
            "restarted_bridge_pid": server.pid,
            "new_process": server.pid != bridge_pid_during_job,
            "status": restored_status.get("status"),
            "artifact_id": restored_logs.get("artifact_id"),
            "logs_available": True,
            "runtime_ok": bool(restored_runtime.get("ok", True)),
        }
        evidence["status"] = "passed"
        evidence["finished_at"] = time.time()
        evidence["duration_seconds"] = round(time.time() - started_at, 3)
        _atomic_json(result_path, evidence)
        return 0
    except BaseException as exc:
        evidence["status"] = "failed"
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        evidence["bridge_pid_after"] = server.pid
        evidence["bridge_alive_after_error"] = server.poll() is None
        evidence["finished_at"] = time.time()
        evidence["duration_seconds"] = round(time.time() - started_at, 3)
        _atomic_json(result_path, evidence)
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
        else:
            evidence["retained_work"] = str(work)
            _atomic_json(result_path, evidence)


def _wmi_launch(result_path: Path, duration: float, probe_after: float) -> int:
    if os.name != "nt":
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--orchestrate",
                str(result_path),
                "--duration",
                str(duration),
                "--probe-after",
                str(probe_after),
            ],
            cwd=ROOT,
            env=_source_environment(
                Path(tempfile.gettempdir()) / "karox-acceptance-launch-runtime",
                Path(tempfile.gettempdir()) / "karox-acceptance-launch-config",
            ),
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(process.pid)
        return 0
    orchestrator_argv = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--orchestrate",
        str(result_path.resolve()),
        "--duration",
        str(duration),
        "--probe-after",
        str(probe_after),
    ]
    command_line = subprocess.list2cmdline(orchestrator_argv).replace("'", "''")
    script = (
        "$result = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
        f"-Arguments @{{CommandLine='{command_line}'}}; "
        "if ($result.ReturnValue -ne 0) { exit $result.ReturnValue }; "
        "Write-Output $result.ProcessId"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
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
        raise RuntimeError(f"WMI launch failed: {completed.stderr[:500]}")
    pid_text = completed.stdout.strip().splitlines()[-1]
    print(int(pid_text))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--server", type=Path)
    mode.add_argument("--orchestrate", type=Path)
    mode.add_argument("--wmi-launch", type=Path)
    parser.add_argument("--duration", type=float, default=230.0)
    parser.add_argument("--probe-after", type=float, default=212.0)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.duration <= 0 or args.probe_after <= 0 or args.probe_after >= args.duration:
        raise SystemExit("duration/probe-after are invalid")
    if args.server is not None:
        return _server(args.server)
    if args.orchestrate is not None:
        return _orchestrate(args.orchestrate, args.duration, args.probe_after)
    assert args.wmi_launch is not None
    return _wmi_launch(args.wmi_launch, args.duration, args.probe_after)


if __name__ == "__main__":
    raise SystemExit(main())
