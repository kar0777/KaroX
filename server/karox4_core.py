"""KaroX 4.0 — Phase 0 core: persistent sessions & resume, idempotency keys,
request queue with honest busy answers, event bus, watchdog support.

Registered on the existing repo_tools FastAPI app. All functions call through
``core.`` attributes so hardened overrides from app_entry (secure audit/auth)
are always picked up.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Header, HTTPException
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel

import repo_tools as core

app = core.app
STARTED_AT = time.time()

# --------------------------------------------------------------------------
# Session state locations
# --------------------------------------------------------------------------

def sessions_root() -> Path:
    env = os.environ.get("KAROX_SESSIONS_DIR")
    if env:
        return Path(env)
    try:
        parent = core.LOG_FILE.parent
        if parent.name == "logs":
            return parent.parent.parent
        return parent.parent
    except Exception:
        return Path.home() / ".karox" / "sessions"


def session_id() -> str:
    env = os.environ.get("KAROX_SESSION_ID")
    if env:
        return env
    try:
        parent = core.LOG_FILE.parent
        return parent.parent.name if parent.name == "logs" else parent.name
    except Exception:
        return "default"


def state_dir() -> Path:
    d = sessions_root() / session_id()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def karox_home() -> Path:
    root = sessions_root()
    return root.parent if root.name == "sessions" else root


STATE_FILE = state_dir() / "session-state.json"
IDEM_FILE = state_dir() / "idempotency.json"

# Session-scoped opt-in flags (deleted dirs, desktop input, ...). Default off.
SESSION_FLAGS: Dict[str, bool] = {"deleteDirAllowed": False, "desktopInputAllowed": False}

# --------------------------------------------------------------------------
# Persistent session state + resume
# --------------------------------------------------------------------------

_PERSISTED_KEYS = [
    "mode", "sessionTitle", "task", "taskNote", "branch", "commitAllowed",
    "startedAt", "finishedAt", "status",
]


def save_session_state() -> Optional[str]:
    try:
        payload = {
            "sessionId": session_id(),
            "savedAt": core.now(),
            "repoRoot": str(core.REPO_ROOT),
            "actualBranch": core.current_branch() if (core.REPO_ROOT / ".git").exists() else None,
            "taskState": {k: core.TASK_STATE.get(k) for k in _PERSISTED_KEYS},
            "flags": dict(SESSION_FLAGS),
        }
        STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        return str(STATE_FILE)
    except Exception as exc:  # never break requests because of persistence
        try:
            core.audit("session_persist_failed", {"error": str(exc)})
        except Exception:
            pass
        return None


def _read_state_file(path: Path) -> Optional[Dict[str, Any]]:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None
    return None


def _apply_saved_state(saved: Dict[str, Any]) -> List[str]:
    applied: List[str] = []
    ts = saved.get("taskState") or {}
    for key in _PERSISTED_KEYS:
        value = ts.get(key)
        if value is not None:
            core.TASK_STATE[key] = value
            applied.append(key)
    flags = saved.get("flags") or {}
    # Safety: opt-in flags are NEVER auto-restored; each session must re-enable.
    _ = flags
    return applied


class ResumeBody(BaseModel):
    sessionId: Optional[str] = None
    switchBranch: bool = True


@app.get("/session/list")
def session_list(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    root = sessions_root()
    items = []
    try:
        for child in sorted(root.iterdir()):
            state = _read_state_file(child / "session-state.json")
            if state:
                items.append({
                    "sessionId": child.name,
                    "savedAt": state.get("savedAt"),
                    "repoRoot": state.get("repoRoot"),
                    "branch": (state.get("taskState") or {}).get("branch"),
                    "task": (state.get("taskState") or {}).get("task"),
                    "status": (state.get("taskState") or {}).get("status"),
                })
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Не удалось перечислить сессии: {exc}")
    return {"current": session_id(), "sessions": items}


@app.get("/session/persisted")
def session_persisted(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    return {"sessionId": session_id(), "stateFile": str(STATE_FILE), "state": _read_state_file(STATE_FILE)}


@app.post("/session/resume")
def session_resume(body: ResumeBody, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    sid = body.sessionId or session_id()
    state_path = sessions_root() / sid / "session-state.json"
    saved = _read_state_file(state_path)
    if not saved:
        raise HTTPException(status_code=404, detail=f"Нет сохранённого состояния для сессии {sid}")
    if saved.get("repoRoot") and str(saved["repoRoot"]) != str(core.REPO_ROOT):
        raise HTTPException(status_code=409, detail={
            "message": "Сессия относится к другому репозиторию",
            "sessionRepo": saved.get("repoRoot"),
            "currentRepo": str(core.REPO_ROOT),
        })
    applied = _apply_saved_state(saved)
    branch_switch: Optional[Dict[str, Any]] = None
    wanted = str((saved.get("taskState") or {}).get("branch") or "")
    actual = core.current_branch() if (core.REPO_ROOT / ".git").exists() else ""
    if body.switchBranch and wanted and actual and wanted != actual:
        dirty = core.changed_files_porcelain()
        if dirty:
            branch_switch = {"switched": False, "reason": f"Рабочее дерево содержит {len(dirty)} изменённых путей", "wanted": wanted, "actual": actual}
        else:
            r = core.run_git(["switch", wanted], timeout=120)
            branch_switch = {"switched": r.get("exitCode") == 0, "wanted": wanted, "result": r}
    save_session_state()
    core.audit("session_resume", {"sessionId": sid, "applied": applied, "branchSwitch": branch_switch})
    return {
        "ok": True,
        "sessionId": sid,
        "appliedKeys": applied,
        "taskState": {k: core.TASK_STATE.get(k) for k in _PERSISTED_KEYS},
        "branchSwitch": branch_switch,
        "currentBranch": core.current_branch() if (core.REPO_ROOT / ".git").exists() else None,
    }

# --------------------------------------------------------------------------
# Route swapping helper (used by other karox4 modules too)
# --------------------------------------------------------------------------

def swap_route(method: str, path: str, endpoint) -> bool:
    """Replace an existing APIRoute handler, or add the route if missing."""
    for index, route in enumerate(list(app.router.routes)):
        if isinstance(route, APIRoute) and route.path == path and method in (route.methods or set()):
            del app.router.routes[index]
            app.add_api_route(path, endpoint, methods=[method])
            return True
    app.add_api_route(path, endpoint, methods=[method])
    return False


# Persist session state whenever a task starts/finishes.

def _task_start_v2(body: core.TaskStartBody, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    result = core.task_start(body, x_api_key)
    save_session_state()
    return result


def _task_finish_v2(body: core.TaskFinishBody, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    result = core.task_finish(body, x_api_key)
    save_session_state()
    return result


swap_route("POST", "/task/start", _task_start_v2)
swap_route("POST", "/task/finish", _task_finish_v2)

# --------------------------------------------------------------------------
# Idempotency keys for mutating requests
# --------------------------------------------------------------------------

_IDEM: Dict[str, Dict[str, Any]] = {}
_IDEM_TTL_SECONDS = 24 * 3600
_IDEM_MAX = 500


def _idem_load() -> None:
    global _IDEM
    try:
        if IDEM_FILE.exists():
            data = json.loads(IDEM_FILE.read_text(encoding="utf-8", errors="replace"))
            if isinstance(data, dict):
                _IDEM = data
    except Exception:
        _IDEM = {}


def _idem_save() -> None:
    try:
        IDEM_FILE.write_text(json.dumps(_IDEM, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def idem_lookup(scope: str, key: Optional[str]) -> Optional[Dict[str, Any]]:
    if not key:
        return None
    entry = _IDEM.get(scope + "\n" + key)
    if not entry:
        return None
    if time.time() > float(entry.get("expiresAt", 0)):
        _IDEM.pop(scope + "\n" + key, None)
        return None
    response = entry.get("response")
    if isinstance(response, dict):
        replay = dict(response)
        replay["idempotentReplay"] = True
        return replay
    return None


def idem_store(scope: str, key: Optional[str], response: Any) -> None:
    if not key or not isinstance(response, dict):
        return
    if len(_IDEM) >= _IDEM_MAX:
        oldest = sorted(_IDEM.items(), key=lambda kv: kv[1].get("expiresAt", 0))[: len(_IDEM) - _IDEM_MAX + 1]
        for stale_key, _ in oldest:
            _IDEM.pop(stale_key, None)
    _IDEM[scope + "\n" + key] = {"expiresAt": time.time() + _IDEM_TTL_SECONDS, "response": response}
    _idem_save()


_idem_load()


# Wrap the highest-risk mutating endpoint (/git/commit) with idempotency.

def _git_commit_v2(
    body: core.GitCommitBody,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    core.check_auth(x_api_key)
    cached = idem_lookup("POST /git/commit", idempotency_key)
    if cached is not None:
        core.audit("idempotent_replay", {"endpoint": "/git/commit"})
        return cached
    result = core.git_commit(body, x_api_key)
    idem_store("POST /git/commit", idempotency_key, result)
    save_session_state()
    return result


swap_route("POST", "/git/commit", _git_commit_v2)

# --------------------------------------------------------------------------
# Request queue: honest busy answers instead of connection refused
# --------------------------------------------------------------------------

def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


MAX_CONCURRENCY = max(1, _int_env("KAROX_MAX_CONCURRENCY", 6))
MAX_QUEUE = max(1, _int_env("KAROX_MAX_QUEUE", 32))
DEFAULT_QUEUE_WAIT = max(1, _int_env("KAROX_QUEUE_WAIT_SECONDS", 120))

_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENCY)
_WAITING = 0
_ACTIVE = 0
_QUEUE_SKIP_PREFIXES = ("/watchdog", "/events")
_QUEUE_SKIP_PATHS = {"/", "/health", "/docs", "/openapi.json"}


@app.middleware("http")
async def karox4_queue_middleware(request, call_next):
    global _WAITING, _ACTIVE
    path = request.url.path
    if path in _QUEUE_SKIP_PATHS or any(path.startswith(p) for p in _QUEUE_SKIP_PREFIXES):
        return await call_next(request)
    priority = (request.headers.get("X-Priority") or "normal").lower()
    try:
        wait_seconds = float(request.headers.get("X-Request-Timeout") or DEFAULT_QUEUE_WAIT)
    except ValueError:
        wait_seconds = float(DEFAULT_QUEUE_WAIT)
    if priority == "low":
        wait_seconds = min(wait_seconds, 15.0)
    if _WAITING >= MAX_QUEUE and priority != "high":
        return JSONResponse(status_code=503, content={
            "busy": True,
            "retryable": True,
            "queuePosition": _WAITING + 1,
            "active": _ACTIVE,
            "detail": f"Сервер занят: очередь заполнена ({_WAITING} ожидающих). Повторите позже.",
            "retryAfterSeconds": 5,
        })
    _WAITING += 1
    position = _WAITING
    acquired = False
    try:
        try:
            await asyncio.wait_for(_SEMAPHORE.acquire(), timeout=wait_seconds)
            acquired = True
        except asyncio.TimeoutError:
            return JSONResponse(status_code=503, content={
                "busy": True,
                "retryable": True,
                "queuePosition": position,
                "active": _ACTIVE,
                "detail": f"Сервер занят: не дождались слота за {wait_seconds:.0f}с (позиция в очереди {position}).",
                "retryAfterSeconds": 5,
            })
    finally:
        _WAITING = max(0, _WAITING - 1)
    _ACTIVE += 1
    try:
        return await call_next(request)
    finally:
        _ACTIVE = max(0, _ACTIVE - 1)
        if acquired:
            _SEMAPHORE.release()

# --------------------------------------------------------------------------
# Event bus (Phase 5 notifications; polled, MCP layer can forward as pushes)
# --------------------------------------------------------------------------

_EVENTS: deque = deque(maxlen=1000)
_EVENT_SEQ = 0


def emit_event(kind: str, data: Dict[str, Any]) -> int:
    global _EVENT_SEQ
    _EVENT_SEQ += 1
    _EVENTS.append({"id": _EVENT_SEQ, "at": core.now(), "kind": kind, "data": data})
    return _EVENT_SEQ


@app.get("/events/poll")
def events_poll(
    after_id: int = 0,
    limit: int = 50,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    core.check_auth(x_api_key)
    limit = max(1, min(int(limit), 500))
    items = [e for e in list(_EVENTS) if e["id"] > int(after_id)][:limit]
    return {"events": items, "lastId": _EVENT_SEQ}

# --------------------------------------------------------------------------
# Watchdog support
# --------------------------------------------------------------------------

@app.get("/watchdog/ping")
def watchdog_ping(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    return {
        "ok": True,
        "pid": os.getpid(),
        "version": core.VERSION,
        "uptimeSeconds": round(time.time() - STARTED_AT, 1),
        "queue": {"waiting": _WAITING, "active": _ACTIVE, "maxConcurrency": MAX_CONCURRENCY, "maxQueue": MAX_QUEUE},
        "sessionId": session_id(),
    }

# --------------------------------------------------------------------------
# Boot: auto-resume the current session's persisted state after restart
# --------------------------------------------------------------------------

try:
    _saved = _read_state_file(STATE_FILE)
    if _saved and str(core.TASK_STATE.get("status") or "") != "running":
        _applied = _apply_saved_state(_saved)
        core.audit("session_auto_resume", {"sessionId": session_id(), "applied": _applied})
except Exception:
    pass

save_session_state()
