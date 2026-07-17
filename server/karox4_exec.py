"""KaroX 4.0 — Phase 1: argv exec, UTF-8 normalization, async jobs,
wait_for_port/http, run_checks v2 with structured error extraction.
"""
from __future__ import annotations

import base64
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from fastapi import Header, HTTPException
from pydantic import BaseModel, Field

import repo_tools as core
import karox4_core as k4

app = core.app
IS_WINDOWS = os.name == "nt"

# --------------------------------------------------------------------------
# UTF-8 everywhere: robust output normalization (cp866/cp1251 mojibake fix)
# --------------------------------------------------------------------------

def _cyrillic_score(text: str) -> int:
    score = 0
    for ch in text:
        o = ord(ch)
        if 0x0400 <= o <= 0x04FF or ch.isascii():
            score += 1
        elif ch == "\ufffd":
            score -= 5
    return score


def normalize_bytes(data: Optional[bytes]) -> str:
    """Decode process output: try UTF-8 first, then legacy Windows codepages."""
    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    best_text: Optional[str] = None
    best_score = -(10 ** 9)
    for enc in ("cp866", "cp1251"):
        try:
            text = data.decode(enc)
        except Exception:
            continue
        score = _cyrillic_score(text)
        if score > best_score:
            best_text, best_score = text, score
    if best_text is not None:
        return best_text
    return data.decode("utf-8", errors="replace")


# Upgrade the legacy decoder globally so /run output is normalized too.
core.decode_bytes = normalize_bytes

# --------------------------------------------------------------------------
# Shared guards
# --------------------------------------------------------------------------

SENSITIVE_ENV_HINTS = ("key", "token", "secret", "passw", "credential", "auth")


def mask_env_for_audit(env: Optional[Dict[str, str]]) -> Dict[str, str]:
    masked: Dict[str, str] = {}
    for name, value in (env or {}).items():
        lowered = name.lower()
        if any(h in lowered for h in SENSITIVE_ENV_HINTS):
            masked[name] = "***"
        else:
            masked[name] = value if len(value) <= 60 else value[:57] + "..."
    return masked


def command_guards(display_cmd: str) -> None:
    reason = core.hard_block_reason(display_cmd)
    if reason:
        core.audit("exec_blocked", {"cmd": display_cmd[:500], "reason": reason})
        raise HTTPException(status_code=403, detail=reason)
    helper = core.command_mentions_agent_helper(display_cmd)
    if helper:
        core.audit("exec_blocked_helper", {"cmd": display_cmd[:500], "helper": helper})
        raise HTTPException(status_code=403, detail=f"Запуск служебного helper-скрипта запрещён: {helper}")
    if core.mode() == "autopilot" and not core.autopilot_allowed(display_cmd):
        core.audit("exec_blocked_autopilot", {"cmd": display_cmd[:500]})
        raise HTTPException(status_code=403, detail="Команда заблокирована в режиме autopilot")


def resolve_cwd(cwd: Optional[str]) -> Path:
    if not cwd:
        return core.REPO_ROOT
    target = (core.REPO_ROOT / cwd).resolve()
    try:
        target.relative_to(core.REPO_ROOT.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail=f"cwd вне песочницы репозитория: {cwd}")
    if not target.is_dir():
        raise HTTPException(status_code=404, detail=f"cwd не найдена: {cwd}")
    return target


def utf8_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["LC_ALL"] = env.get("LC_ALL") or "C.UTF-8"
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


def build_argv(argv: Optional[List[str]], shell: Optional[str], shell_cmd: Optional[str]) -> tuple[List[str], str]:
    """Return (final_argv, display_cmd). Either argv (verbatim, no shell) or shell+shell_cmd."""
    if argv:
        display = subprocess.list2cmdline(argv) if IS_WINDOWS else " ".join(argv)
        return list(argv), display
    if not shell or not (shell_cmd or "").strip():
        raise HTTPException(status_code=400, detail="Нужен либо argv, либо shell + cmd")
    cmd = shell_cmd.strip()
    if shell == "cmd":
        if not IS_WINDOWS:
            raise HTTPException(status_code=400, detail="shell=cmd доступен только на Windows")
        return ["cmd", "/d", "/s", "/c", f"chcp 65001>nul & ({cmd})"], cmd
    if shell == "powershell":
        prefix = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", prefix + cmd], cmd
    if shell == "bash":
        return ["bash", "-lc", cmd], cmd
    if shell == "sh":
        return ["/bin/sh", "-c", cmd], cmd
    raise HTTPException(status_code=400, detail=f"Неизвестный shell: {shell}")


def run_argv(
    argv: List[str],
    *,
    cwd: Path,
    env_extra: Optional[Dict[str, str]] = None,
    stdin: Optional[str] = None,
    timeout: int = 600,
    tail: int = core.DEFAULT_TAIL,
) -> Dict[str, Any]:
    started = time.time()
    try:
        r = subprocess.run(
            argv,
            cwd=str(cwd),
            input=stdin.encode("utf-8") if stdin else None,
            capture_output=True,
            timeout=timeout,
            shell=False,
            env=utf8_env(env_extra),
        )
        return {
            "command": argv,
            "exitCode": r.returncode,
            "elapsedSeconds": round(time.time() - started, 2),
            "stdout": normalize_bytes(r.stdout)[-tail:],
            "stderr": normalize_bytes(r.stderr)[-tail:],
        }
    except subprocess.TimeoutExpired as e:
        return {
            "command": argv,
            "exitCode": 408,
            "timeout": True,
            "elapsedSeconds": timeout,
            "stdout": normalize_bytes(e.stdout if isinstance(e.stdout, bytes) else None)[-tail:],
            "stderr": normalize_bytes(e.stderr if isinstance(e.stderr, bytes) else None)[-tail:],
        }
    except FileNotFoundError as e:
        return {"command": argv, "exitCode": 127, "elapsedSeconds": 0, "stdout": "", "stderr": f"Команда не найдена: {e}", "error": "command_not_found"}
    except OSError as e:
        return {"command": argv, "exitCode": 126, "elapsedSeconds": 0, "stdout": "", "stderr": f"Ошибка запуска: {e}", "error": "os_error"}

# --------------------------------------------------------------------------
# /exec — argv execution without shell quoting damage
# --------------------------------------------------------------------------

class ExecBody(BaseModel):
    argv: Optional[List[str]] = None
    shell: Optional[Literal["cmd", "powershell", "bash", "sh"]] = None
    cmd: Optional[str] = None
    cwd: Optional[str] = None
    env: Optional[Dict[str, str]] = None
    stdin: Optional[str] = None
    timeoutSeconds: int = Field(default=600, ge=1, le=21600)
    tail: int = Field(default=core.DEFAULT_TAIL, ge=1000, le=500000)


@app.post("/exec")
def exec_command(
    body: ExecBody,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    core.check_auth(x_api_key)
    core.ensure_not_read_only("POST /exec")
    cached = k4.idem_lookup("POST /exec", idempotency_key)
    if cached is not None:
        return cached
    argv, display = build_argv(body.argv, body.shell, body.cmd)
    command_guards(display)
    cwd = resolve_cwd(body.cwd)
    core.audit("exec_start", {
        "argv": [a[:200] for a in (body.argv or [])],
        "shell": body.shell,
        "cmd": (body.cmd or "")[:500],
        "cwd": str(cwd),
        "env": mask_env_for_audit(body.env),
        "stdinChars": len(body.stdin or ""),
    })
    result = run_argv(argv, cwd=cwd, env_extra=body.env, stdin=body.stdin, timeout=body.timeoutSeconds, tail=body.tail)
    core.audit("exec_result", {"exitCode": result.get("exitCode"), "elapsedSeconds": result.get("elapsedSeconds")})
    k4.idem_store("POST /exec", idempotency_key, result)
    return result

# --------------------------------------------------------------------------
# Async jobs
# --------------------------------------------------------------------------

JOBS: Dict[str, Dict[str, Any]] = {}
_JOB_PROCS: Dict[str, subprocess.Popen] = {}
JOBS_FILE = k4.state_dir() / "jobs.json"


def _persist_jobs() -> None:
    try:
        JOBS_FILE.write_text(json.dumps(JOBS, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def _load_jobs() -> None:
    global JOBS
    try:
        if JOBS_FILE.exists():
            data = json.loads(JOBS_FILE.read_text(encoding="utf-8", errors="replace"))
            if isinstance(data, dict):
                JOBS = data
                for job in JOBS.values():
                    if job.get("state") == "running":
                        job["state"] = "orphaned"  # server restarted; process may still be alive
    except Exception:
        JOBS = {}


_load_jobs()


def _pid_alive(pid: int) -> Optional[bool]:
    try:
        import psutil  # type: ignore
        return psutil.pid_exists(pid)
    except Exception:
        pass
    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    return None


def _job_resources(pid: int) -> Optional[Dict[str, Any]]:
    try:
        import psutil  # type: ignore
        p = psutil.Process(pid)
        with p.oneshot():
            return {
                "cpuPercent": p.cpu_percent(interval=0.1),
                "memoryMb": round(p.memory_info().rss / (1024 * 1024), 1),
                "threads": p.num_threads(),
            }
    except Exception:
        return None


def _refresh_job(job_id: str) -> Dict[str, Any]:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Джоб не найден: {job_id}")
    proc = _JOB_PROCS.get(job_id)
    if proc is not None:
        rc = proc.poll()
        if rc is not None and job.get("state") == "running":
            job["state"] = "finished" if rc == 0 else "failed"
            job["exitCode"] = rc
            job["finishedAt"] = core.now()
            _persist_jobs()
            k4.emit_event("job_exit", {"jobId": job_id, "name": job.get("name"), "exitCode": rc})
    elif job.get("state") == "orphaned":
        alive = _pid_alive(int(job.get("pid") or -1))
        if alive is False:
            job["state"] = "finished_orphaned"
            _persist_jobs()
    return job


class JobStartBody(BaseModel):
    argv: Optional[List[str]] = None
    shell: Optional[Literal["cmd", "powershell", "bash", "sh"]] = None
    cmd: Optional[str] = None
    name: str = ""
    cwd: Optional[str] = None
    env: Optional[Dict[str, str]] = None


@app.post("/jobs/start")
def job_start(body: JobStartBody, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    core.ensure_not_read_only("POST /jobs/start")
    argv, display = build_argv(body.argv, body.shell, body.cmd)
    command_guards(display)
    cwd = resolve_cwd(body.cwd)
    job_id = "job-" + uuid.uuid4().hex[:10]
    log_path = core.RUNS_DIR / f"{job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0
    try:
        log_handle = log_path.open("wb")
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=utf8_env(body.env),
            shell=False,
            creationflags=creationflags,
        )
    except (OSError, FileNotFoundError) as e:
        raise HTTPException(status_code=400, detail=f"Не удалось запустить джоб: {e}")
    JOBS[job_id] = {
        "id": job_id,
        "name": body.name or (body.argv[0] if body.argv else (body.cmd or ""))[:80],
        "argv": argv,
        "pid": proc.pid,
        "log": str(log_path),
        "cwd": str(cwd),
        "startedAt": core.now(),
        "state": "running",
        "exitCode": None,
    }
    _JOB_PROCS[job_id] = proc
    _persist_jobs()
    core.audit("job_start", {"jobId": job_id, "name": JOBS[job_id]["name"], "pid": proc.pid, "env": mask_env_for_audit(body.env)})
    k4.emit_event("job_started", {"jobId": job_id, "name": JOBS[job_id]["name"], "pid": proc.pid})
    return {"ok": True, "jobId": job_id, "pid": proc.pid, "log": str(log_path)}


@app.get("/jobs")
def job_list(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    for job_id in list(JOBS.keys()):
        try:
            _refresh_job(job_id)
        except HTTPException:
            continue
    return {"jobs": sorted(JOBS.values(), key=lambda j: j.get("startedAt") or "", reverse=True)}


@app.get("/jobs/{job_id}")
def job_status(job_id: str, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    job = dict(_refresh_job(job_id))
    started = job.get("startedAt")
    if job.get("state") in ("running", "orphaned") and job.get("pid"):
        job["resources"] = _job_resources(int(job["pid"]))
    try:
        log = Path(job.get("log") or "")
        job["logBytes"] = log.stat().st_size if log.exists() else 0
    except Exception:
        job["logBytes"] = None
    return job


@app.get("/jobs/{job_id}/tail")
def job_tail(
    job_id: str,
    lines: int = 100,
    pattern: Optional[str] = None,
    wait_seconds: float = 0,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    core.check_auth(x_api_key)
    job = _refresh_job(job_id)
    log_path = Path(job.get("log") or "")
    lines = max(1, min(int(lines), 5000))
    wait_seconds = max(0.0, min(float(wait_seconds), 600.0))
    compiled = None
    if pattern:
        try:
            compiled = re.compile(pattern)
        except re.error as e:
            raise HTTPException(status_code=400, detail=f"Некорректный регулярный паттерн: {e}")

    def read_tail() -> str:
        if not log_path.exists():
            return ""
        data = log_path.read_bytes()
        return normalize_bytes(data[-500_000:])

    deadline = time.time() + wait_seconds
    matched = False
    matched_line: Optional[str] = None
    waited = 0.0
    while True:
        text = read_tail()
        if compiled:
            for line in text.splitlines():
                if compiled.search(line):
                    matched = True
                    matched_line = line[-500:]
                    break
        if matched or not compiled or time.time() >= deadline:
            break
        _refresh_job(job_id)
        if job.get("state") not in ("running", "orphaned"):
            text = read_tail()
            break
        time.sleep(0.5)
        waited = round(min(wait_seconds, waited + 0.5), 1)
    tail_lines = text.splitlines()[-lines:]
    return {
        "jobId": job_id,
        "state": job.get("state"),
        "exitCode": job.get("exitCode"),
        "lines": tail_lines,
        "matched": matched if compiled else None,
        "matchedLine": matched_line,
        "waitedSeconds": waited,
    }


class JobSignalBody(BaseModel):
    signal: Literal["kill", "int"] = "kill"


@app.post("/jobs/{job_id}/signal")
def job_signal(job_id: str, body: JobSignalBody, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    core.ensure_not_read_only("POST /jobs/signal")
    job = _refresh_job(job_id)
    pid = int(job.get("pid") or -1)
    proc = _JOB_PROCS.get(job_id)
    result: Dict[str, Any] = {"jobId": job_id, "signal": body.signal}
    try:
        if body.signal == "int":
            if proc is not None and IS_WINDOWS:
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            elif proc is not None:
                proc.send_signal(signal.SIGINT)
            elif IS_WINDOWS:
                run_argv(["taskkill", "/PID", str(pid)], cwd=core.REPO_ROOT, timeout=30)
            else:
                os.kill(pid, signal.SIGINT)
        else:
            if IS_WINDOWS:
                run_argv(["taskkill", "/PID", str(pid), "/T", "/F"], cwd=core.REPO_ROOT, timeout=30)
            elif proc is not None:
                proc.kill()
            else:
                os.kill(pid, signal.SIGKILL)
        result["ok"] = True
    except Exception as e:
        result["ok"] = False
        result["error"] = str(e)
    job["state"] = "signaled"
    _persist_jobs()
    core.audit("job_signal", {"jobId": job_id, "signal": body.signal, "ok": result.get("ok")})
    k4.emit_event("job_signaled", {"jobId": job_id, "signal": body.signal})
    return result

# --------------------------------------------------------------------------
# wait_for_port / wait_for_http
# --------------------------------------------------------------------------

@app.get("/wait/port")
def wait_for_port(
    port: int,
    timeout_seconds: float = 60,
    host: str = "127.0.0.1",
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    core.check_auth(x_api_key)
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise HTTPException(status_code=403, detail="Только локальные адреса")
    timeout_seconds = max(0.5, min(float(timeout_seconds), 600))
    deadline = time.time() + timeout_seconds
    started = time.time()
    while time.time() < deadline:
        try:
            with socket.create_connection((host, int(port)), timeout=1.5):
                return {"open": True, "port": int(port), "waitedSeconds": round(time.time() - started, 1)}
        except OSError:
            time.sleep(0.5)
    return {"open": False, "port": int(port), "waitedSeconds": round(time.time() - started, 1)}


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def ensure_local_url(url: str) -> None:
    host = (urlparse(url).hostname or "").lower()
    if host not in LOCAL_HOSTS:
        raise HTTPException(status_code=403, detail=f"Разрешены только локальные URL, получено: {host or url}")


@app.get("/wait/http")
def wait_for_http(
    url: str,
    expect_status: int = 200,
    timeout_seconds: float = 60,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    core.check_auth(x_api_key)
    ensure_local_url(url)
    timeout_seconds = max(0.5, min(float(timeout_seconds), 600))
    deadline = time.time() + timeout_seconds
    started = time.time()
    last: Any = None
    while time.time() < deadline:
        try:
            with urlopen(Request(url, method="GET"), timeout=3) as resp:
                last = resp.status
                if int(resp.status) == int(expect_status):
                    return {"ready": True, "status": resp.status, "waitedSeconds": round(time.time() - started, 1)}
        except Exception as e:
            last = str(e)
        time.sleep(0.5)
    return {"ready": False, "lastResult": last, "waitedSeconds": round(time.time() - started, 1)}

# --------------------------------------------------------------------------
# Structured error extraction (javac, kotlin/gradle, tsc, pytest, eslint, gcc)
# --------------------------------------------------------------------------

_LINE_PATTERNS = [
    ("javac", re.compile(r"^(?P<file>[^\s:][^:]*?\.java):(?P<line>\d+):\s*(?:error|ошибка):\s*(?P<msg>.+)$")),
    ("kotlin", re.compile(r"^e:\s*(?:file://)?(?P<file>.+?):(?P<line>\d+):(?:\d+:?)?\s*(?P<msg>.+)$")),
    ("tsc", re.compile(r"^(?P<file>.+?\.[cm]?[jt]sx?)\((?P<line>\d+),\d+\):\s*error\s+TS\d+:\s*(?P<msg>.+)$")),
    ("pytest", re.compile(r"^(?P<file>[^\s:][^:]*?\.py):(?P<line>\d+):\s*(?P<msg>[A-Za-z_].*(?:Error|Exception|error).*)$")),
    ("gcc", re.compile(r"^(?P<file>[^\s:][^:]*?):(?P<line>\d+):(?:\d+:)?\s*(?:fatal\s+)?error:\s*(?P<msg>.+)$")),
]
_ESLINT_FILE = re.compile(r"^(?P<file>[^\s].*\.[cm]?[jt]sx?)$")
_ESLINT_ITEM = re.compile(r"^\s+(?P<line>\d+):\d+\s+error\s+(?P<msg>.+?)(?:\s\s+[\w@/-]+)?$")
_GRADLE_FAIL = re.compile(r"^\*\s+What went wrong:")


def parse_errors(output: str, max_errors: int = 50) -> List[Dict[str, Any]]:
    errors: List[Dict[str, Any]] = []
    eslint_file: Optional[str] = None
    lines = output.splitlines()
    for idx, raw in enumerate(lines):
        line = raw.rstrip()
        if len(errors) >= max_errors:
            break
        matched = False
        for tool, rx in _LINE_PATTERNS:
            m = rx.match(line)
            if m:
                errors.append({"tool": tool, "file": m.group("file").strip(), "line": int(m.group("line")), "message": m.group("msg").strip()[:500]})
                matched = True
                break
        if matched:
            continue
        fm = _ESLINT_FILE.match(line)
        if fm and ("/" in line or "\\" in line):
            eslint_file = fm.group("file")
            continue
        im = _ESLINT_ITEM.match(raw)
        if im and eslint_file:
            errors.append({"tool": "eslint", "file": eslint_file, "line": int(im.group("line")), "message": im.group("msg").strip()[:500]})
            continue
        if _GRADLE_FAIL.match(line):
            detail = " ".join(l.strip() for l in lines[idx + 1 : idx + 4]).strip()
            errors.append({"tool": "gradle", "file": None, "line": None, "message": detail[:500]})
    return errors


class ParseErrorsBody(BaseModel):
    output: str


@app.post("/parse/errors")
def parse_errors_endpoint(body: ParseErrorsBody, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    errors = parse_errors(body.output)
    return {"errors": errors, "firstError": errors[0] if errors else None}

# --------------------------------------------------------------------------
# run_checks v2: allow_failure, retries, structured summary
# --------------------------------------------------------------------------

class CheckItem(BaseModel):
    name: Optional[str] = None
    cmd: Optional[str] = None
    argv: Optional[List[str]] = None
    shell: Optional[Literal["cmd", "powershell", "bash", "sh"]] = None
    allowFailure: bool = False
    retries: int = Field(default=0, ge=0, le=5)
    timeoutSeconds: int = Field(default=1800, ge=5, le=7200)


class ChecksV2Body(BaseModel):
    checks: List[CheckItem]
    stopOnFailure: bool = True


@app.post("/checks/v2")
def run_checks_v2(body: ChecksV2Body, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    core.ensure_not_read_only("POST /checks/v2")
    if not body.checks or len(body.checks) > 30:
        raise HTTPException(status_code=400, detail="От 1 до 30 проверок")
    results: List[Dict[str, Any]] = []
    passed_count = 0
    failed_count = 0
    allowed_failures = 0
    first_error: Optional[Dict[str, Any]] = None
    stop = False
    for item in body.checks:
        if item.argv:
            argv, display = build_argv(item.argv, None, None)
        else:
            shell = item.shell or ("cmd" if IS_WINDOWS else "sh")
            argv, display = build_argv(None, shell, item.cmd)
        command_guards(display)
        attempts = 0
        result: Dict[str, Any] = {}
        while attempts <= item.retries:
            attempts += 1
            result = run_argv(argv, cwd=core.REPO_ROOT, timeout=item.timeoutSeconds, tail=60000)
            if result.get("exitCode") == 0:
                break
        ok = result.get("exitCode") == 0
        output = (result.get("stdout") or "") + "\n" + (result.get("stderr") or "")
        errors = [] if ok else parse_errors(output)
        entry = {
            "name": item.name or display[:80],
            "command": display[:300],
            "passed": ok,
            "allowFailure": item.allowFailure,
            "attempts": attempts,
            "flaky": bool(ok and attempts > 1),
            "exitCode": result.get("exitCode"),
            "elapsedSeconds": result.get("elapsedSeconds"),
            "firstError": errors[0] if errors else None,
            "errors": errors[:10],
            "stdoutTail": (result.get("stdout") or "")[-4000:],
            "stderrTail": (result.get("stderr") or "")[-4000:],
        }
        results.append(entry)
        if ok:
            passed_count += 1
        elif item.allowFailure:
            allowed_failures += 1
        else:
            failed_count += 1
            if first_error is None:
                first_error = entry["firstError"] or {"tool": None, "file": None, "line": None, "message": (entry["stderrTail"] or entry["stdoutTail"])[-500:]}
            if body.stopOnFailure:
                stop = True
        if stop:
            break
    summary = {
        "total": len(body.checks),
        "executed": len(results),
        "passed": passed_count,
        "failed": failed_count,
        "allowedFailures": allowed_failures,
        "flakyRetried": sum(1 for r in results if r["flaky"]),
        "ok": failed_count == 0,
        "firstError": first_error,
    }
    core.audit("checks_v2", {"summary": {k: v for k, v in summary.items() if k != "firstError"}})
    if not summary["ok"]:
        k4.emit_event("checks_failed", {"firstError": first_error})
    return {"summary": summary, "results": results}
