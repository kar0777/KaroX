"""KaroX 4.0 — Phase 6: persistent REPL sessions (python/node) and a generic
DAP (Debug Adapter Protocol) bridge.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Dict, List, Literal, Optional

from fastapi import Header, HTTPException
from pydantic import BaseModel, Field

import repo_tools as core
import karox4_core as k4
import karox4_exec as kexec

app = core.app

# --------------------------------------------------------------------------
# Persistent REPL sessions
# --------------------------------------------------------------------------

_REPL_COMMANDS = {
    "python": [sys.executable, "-u", "-i", "-q"],
    "node": ["node", "-i"],
}
_REPLS: Dict[str, Dict[str, Any]] = {}


def _repl_reader(proc: subprocess.Popen, out_queue: "queue.Queue[str]") -> None:
    try:
        while True:
            chunk = proc.stdout.read(1)
            if not chunk:
                break
            out_queue.put(chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else chunk)
    except Exception:
        pass


class ReplOpenBody(BaseModel):
    language: Literal["python", "node"]


@app.post("/repl/open")
def repl_open(body: ReplOpenBody, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    core.ensure_not_read_only("POST /repl/open")
    argv = _REPL_COMMANDS[body.language]
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(core.REPO_ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=kexec.utf8_env(),
        )
    except FileNotFoundError:
        raise HTTPException(status_code=501, detail=f"Интерпретатор не найден: {argv[0]}")
    repl_id = "repl-" + uuid.uuid4().hex[:8]
    out_queue: "queue.Queue[str]" = queue.Queue()
    thread = threading.Thread(target=_repl_reader, args=(proc, out_queue), daemon=True)
    thread.start()
    _REPLS[repl_id] = {"id": repl_id, "language": body.language, "proc": proc, "queue": out_queue, "openedAt": core.now()}
    time.sleep(0.4)
    _drain(out_queue)  # discard banner
    core.audit("repl_open", {"id": repl_id, "language": body.language})
    return {"ok": True, "replId": repl_id, "language": body.language}


def _drain(q: "queue.Queue[str]") -> str:
    parts: List[str] = []
    try:
        while True:
            parts.append(q.get_nowait())
    except queue.Empty:
        pass
    return "".join(parts)


def _get_repl(repl_id: str) -> Dict[str, Any]:
    repl = _REPLS.get(repl_id)
    if not repl:
        raise HTTPException(status_code=404, detail=f"REPL не найден: {repl_id}")
    if repl["proc"].poll() is not None:
        raise HTTPException(status_code=409, detail=f"REPL завершён (exitCode={repl['proc'].returncode})")
    return repl


class ReplEvalBody(BaseModel):
    replId: str
    code: str
    timeoutSeconds: float = Field(default=15, ge=1, le=300)


@app.post("/repl/eval")
def repl_eval(body: ReplEvalBody, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    core.ensure_not_read_only("POST /repl/eval")
    reason = core.hard_block_reason(body.code)
    if reason:
        raise HTTPException(status_code=403, detail=reason)
    repl = _get_repl(body.replId)
    proc: subprocess.Popen = repl["proc"]
    out_queue: "queue.Queue[str]" = repl["queue"]
    _drain(out_queue)
    sentinel = "__KAROX_DONE_" + uuid.uuid4().hex[:8] + "__"
    if repl["language"] == "python":
        payload = body.code.rstrip("\n") + "\n" + f"print({sentinel!r})\n"
    else:
        payload = body.code.rstrip("\n") + "\n" + f"console.log({sentinel!r})\n"
    try:
        proc.stdin.write(payload.encode("utf-8"))
        proc.stdin.flush()
    except OSError as e:
        raise HTTPException(status_code=409, detail=f"REPL недоступен: {e}")
    deadline = time.time() + body.timeoutSeconds
    collected = ""
    done = False
    while time.time() < deadline:
        collected += _drain(out_queue)
        if sentinel in collected:
            done = True
            break
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    output = collected.split(sentinel)[0] if done else collected
    output_lines = [ln for ln in output.splitlines() if not ln.strip().startswith((">>>", "...", "> ")) or ln.strip() not in (">>>", "...", ">")]
    cleaned = "\n".join(output_lines).strip()
    core.audit("repl_eval", {"id": body.replId, "codeChars": len(body.code), "done": done})
    return {"replId": body.replId, "completed": done, "output": cleaned[-30000:], "timedOut": not done}


@app.post("/repl/{repl_id}/close")
def repl_close(repl_id: str, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    repl = _REPLS.pop(repl_id, None)
    if repl:
        try:
            repl["proc"].kill()
        except Exception:
            pass
    core.audit("repl_close", {"id": repl_id})
    return {"ok": True, "closed": repl_id}


@app.get("/repl/list")
def repl_list(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    out = []
    for repl in _REPLS.values():
        out.append({"id": repl["id"], "language": repl["language"], "openedAt": repl["openedAt"], "alive": repl["proc"].poll() is None})
    return {"repls": out}

# --------------------------------------------------------------------------
# DAP bridge (generic Debug Adapter Protocol client)
# --------------------------------------------------------------------------

_DAP_SESSIONS: Dict[str, Dict[str, Any]] = {}


def _dap_reader(proc: subprocess.Popen, session: Dict[str, Any]) -> None:
    buf = b""
    try:
        while True:
            chunk = proc.stdout.read(1)
            if not chunk:
                break
            buf += chunk
            while b"\r\n\r\n" in buf:
                header, rest = buf.split(b"\r\n\r\n", 1)
                length = 0
                for line in header.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":", 1)[1].strip())
                if len(rest) < length:
                    break
                payload, buf = rest[:length], rest[length:]
                try:
                    message = json.loads(payload.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                if message.get("type") == "event":
                    session["events"].append(message)
                    if len(session["events"]) > 500:
                        session["events"] = session["events"][-300:]
                else:
                    session["responses"][message.get("request_seq", -1)] = message
    except Exception:
        pass


_DAP_ADAPTERS = {
    "python": lambda: [sys.executable, "-m", "debugpy.adapter"],
}


class DapStartBody(BaseModel):
    adapter: str = "python"
    adapterArgv: Optional[List[str]] = None


@app.post("/dap/start")
def dap_start(body: DapStartBody, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    core.ensure_not_read_only("POST /dap/start")
    if body.adapterArgv:
        argv = body.adapterArgv
        display = " ".join(argv)
        reason = core.hard_block_reason(display)
        if reason:
            raise HTTPException(status_code=403, detail=reason)
    else:
        factory = _DAP_ADAPTERS.get(body.adapter)
        if not factory:
            raise HTTPException(status_code=400, detail=f"Неизвестный адаптер: {body.adapter}. Известные: {sorted(_DAP_ADAPTERS)} или передайте adapterArgv")
        argv = factory()
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(core.REPO_ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=kexec.utf8_env(),
        )
    except FileNotFoundError:
        raise HTTPException(status_code=501, detail=f"Адаптер не найден: {argv[0]}")
    time.sleep(0.3)
    if proc.poll() is not None:
        raise HTTPException(status_code=501, detail=f"Адаптер завершился сразу (для python: pip install debugpy)")
    session_id = "dap-" + uuid.uuid4().hex[:8]
    session: Dict[str, Any] = {"id": session_id, "proc": proc, "seq": 0, "events": [], "responses": {}, "adapter": body.adapter}
    thread = threading.Thread(target=_dap_reader, args=(proc, session), daemon=True)
    thread.start()
    _DAP_SESSIONS[session_id] = session
    core.audit("dap_start", {"id": session_id, "adapter": body.adapter})
    return {"ok": True, "sessionId": session_id}


class DapRequestBody(BaseModel):
    sessionId: str
    command: str
    arguments: Optional[Dict[str, Any]] = None
    timeoutSeconds: float = Field(default=10, ge=1, le=120)


@app.post("/dap/request")
def dap_request(body: DapRequestBody, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    session = _DAP_SESSIONS.get(body.sessionId)
    if not session:
        raise HTTPException(status_code=404, detail=f"DAP-сессия не найдена: {body.sessionId}")
    proc: subprocess.Popen = session["proc"]
    if proc.poll() is not None:
        raise HTTPException(status_code=409, detail="DAP-адаптер завершён")
    session["seq"] += 1
    seq = session["seq"]
    message = {"seq": seq, "type": "request", "command": body.command}
    if body.arguments is not None:
        message["arguments"] = body.arguments
    payload = json.dumps(message).encode("utf-8")
    frame = b"Content-Length: " + str(len(payload)).encode() + b"\r\n\r\n" + payload
    try:
        proc.stdin.write(frame)
        proc.stdin.flush()
    except OSError as e:
        raise HTTPException(status_code=409, detail=f"DAP недоступен: {e}")
    deadline = time.time() + body.timeoutSeconds
    while time.time() < deadline:
        if seq in session["responses"]:
            response = session["responses"].pop(seq)
            core.audit("dap_request", {"id": body.sessionId, "command": body.command, "success": response.get("success")})
            return {"response": response}
        time.sleep(0.05)
    return {"response": None, "timedOut": True}


@app.get("/dap/{session_id}/events")
def dap_events(session_id: str, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    session = _DAP_SESSIONS.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"DAP-сессия не найдена: {session_id}")
    events = session["events"][:]
    session["events"] = []
    return {"events": events}


@app.post("/dap/{session_id}/stop")
def dap_stop(session_id: str, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    session = _DAP_SESSIONS.pop(session_id, None)
    if session:
        try:
            session["proc"].kill()
        except Exception:
            pass
    core.audit("dap_stop", {"id": session_id})
    return {"ok": True, "stopped": session_id}
