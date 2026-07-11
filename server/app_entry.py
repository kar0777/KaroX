"""Hardened KaroX application entrypoint.

This module wraps the existing ``repo_tools`` FastAPI application without changing
its public endpoints. It adds security headers, request correlation, bounded audit
logs, safer error responses, authenticated capability endpoints, and a local-first
Control Center UI.
"""
from __future__ import annotations

import hmac
import html
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from fastapi import Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

import repo_tools as core

app = core.app
VERSION = core.VERSION
MAX_REQUEST_BYTES = int(os.environ.get("REPO_TOOLS_MAX_REQUEST_BYTES", "30000000"))
AUDIT_MAX_BYTES = int(os.environ.get("REPO_TOOLS_AUDIT_MAX_BYTES", "10000000"))
AUDIT_BACKUPS = max(1, min(10, int(os.environ.get("REPO_TOOLS_AUDIT_BACKUPS", "3"))))
DEBUG_ERRORS = os.environ.get("REPO_TOOLS_DEBUG_ERRORS", "0") == "1"

_ALLOWED_ORIGINS_RAW = os.environ.get("REPO_TOOLS_CORS_ORIGINS", "*")
ALLOWED_ORIGINS = {item.strip() for item in _ALLOWED_ORIGINS_RAW.split(",") if item.strip()}
if not ALLOWED_ORIGINS:
    ALLOWED_ORIGINS = {"*"}

_original_audit = core.audit
_audit_lock = threading.Lock()
_auth_lock = threading.Lock()
_auth_failures: Dict[str, list[float]] = {}

_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|authorization|bearer|token|password|secret|cookie|credential)",
    re.IGNORECASE,
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\b(?:github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,})\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{12,}=*"),
)


def _redact_string(value: str, limit: int = 6000) -> str:
    text = value
    for pattern in _SECRET_VALUE_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    if core.API_KEY and core.API_KEY in text:
        text = text.replace(core.API_KEY, "[REDACTED_SESSION_KEY]")
    if len(text) > limit:
        text = text[:limit] + f"… [truncated {len(text) - limit} chars]"
    return text


def redact(value: Any, *, key: str = "") -> Any:
    if key and _SECRET_KEY_RE.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value[:500]]
    if isinstance(value, str):
        return _redact_string(value)
    return value


def _rotate_audit_if_needed() -> None:
    path: Path = core.LOG_FILE
    try:
        if not path.exists() or path.stat().st_size < AUDIT_MAX_BYTES:
            return
        for index in range(AUDIT_BACKUPS, 0, -1):
            src = path.with_name(path.name + ("" if index == 1 else f".{index - 1}"))
            dst = path.with_name(path.name + f".{index}")
            if dst.exists():
                dst.unlink()
            if src.exists():
                src.replace(dst)
    except OSError:
        # Audit rotation must never break an API request.
        return


def secure_audit(action: str, data: Dict[str, Any]) -> None:
    with _audit_lock:
        _rotate_audit_if_needed()
        _original_audit(action, redact(data))


def secure_check_auth(x_api_key: Optional[str]) -> None:
    supplied = core.normalize_supplied_api_key(x_api_key)
    valid = bool(supplied) and hmac.compare_digest(
        supplied.encode("utf-8", errors="ignore"),
        core.API_KEY.encode("utf-8", errors="ignore"),
    )
    if not valid:
        secure_audit(
            "auth_failed",
            {
                "hasCredential": bool(supplied),
                "credentialLength": len(supplied),
                "expectedLength": len(core.API_KEY),
                "acceptedHeaders": ["X-API-Key", "Authorization: Bearer"],
            },
        )
        raise HTTPException(
            status_code=401,
            detail="Invalid session key. Paste the KaroX session key into the protected credential field.",
        )


# Existing endpoint functions resolve these globals at request time, so replacing
# them hardens the whole API without duplicating or changing the endpoint surface.
core.audit = secure_audit
core.check_auth = secure_check_auth


def _client_id(request: Request) -> str:
    forwarded = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()[:128]
    if request.client:
        return str(request.client.host)[:128]
    return "unknown"


def _too_many_auth_failures(client: str, now_value: float) -> bool:
    with _auth_lock:
        recent = [stamp for stamp in _auth_failures.get(client, []) if now_value - stamp < 60]
        _auth_failures[client] = recent
        return len(recent) >= 30


def _record_auth_result(client: str, status_code: int, now_value: float) -> None:
    with _auth_lock:
        if status_code == 401:
            values = [stamp for stamp in _auth_failures.get(client, []) if now_value - stamp < 60]
            values.append(now_value)
            _auth_failures[client] = values[-50:]
        elif status_code < 400:
            _auth_failures.pop(client, None)


@app.middleware("http")
async def karox_runtime_guard(request: Request, call_next):
    started = time.perf_counter()
    request_id = request.headers.get("x-request-id", "").strip()[:128] or uuid.uuid4().hex
    client = _client_id(request)
    now_value = time.time()

    if _too_many_auth_failures(client, now_value):
        return JSONResponse(
            status_code=429,
            content={"ok": False, "error": "Too many failed authentication attempts. Retry in one minute."},
            headers={"Retry-After": "60", "X-KaroX-Request-ID": request_id},
        )

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_REQUEST_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={"ok": False, "error": "Request body is too large."},
                    headers={"X-KaroX-Request-ID": request_id},
                )
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "Invalid Content-Length header."},
                headers={"X-KaroX-Request-ID": request_id},
            )

    origin = request.headers.get("origin", "").strip()
    if origin and "*" not in ALLOWED_ORIGINS and origin not in ALLOWED_ORIGINS:
        return JSONResponse(
            status_code=403,
            content={"ok": False, "error": "Origin is not allowed by REPO_TOOLS_CORS_ORIGINS."},
            headers={"X-KaroX-Request-ID": request_id},
        )

    request.state.karox_request_id = request_id
    response = await call_next(request)
    _record_auth_result(client, response.status_code, now_value)

    response.headers["X-KaroX-Request-ID"] = request_id
    response.headers["X-KaroX-Version"] = VERSION
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
    response.headers["Cache-Control"] = "no-store, max-age=0"
    if request.url.path == "/control":
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'"
        )
    else:
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"

    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    if request.url.path not in {"/health"}:
        secure_audit(
            "http_request",
            {
                "requestId": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "elapsedMs": elapsed_ms,
                "client": client,
            },
        )
    return response


@app.exception_handler(Exception)
async def safe_unhandled_exception_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, "karox_request_id", uuid.uuid4().hex)
    secure_audit(
        "unhandled_error",
        {
            "requestId": request_id,
            "path": request.url.path,
            "errorType": type(exc).__name__,
            "error": repr(exc) if DEBUG_ERRORS else "hidden",
        },
    )
    content: Dict[str, Any] = {
        "ok": False,
        "error": "Internal KaroX error. The server is still running.",
        "requestId": request_id,
        "hint": "Retry the request and use the request ID when creating a support bundle.",
    }
    if DEBUG_ERRORS:
        content["detail"] = _redact_string(str(exc), 1000)
    return JSONResponse(status_code=500, content=content)


def _auth(x_api_key: Optional[str]) -> None:
    core.check_auth(x_api_key)


@app.get("/meta", tags=["system"])
def karox_meta(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    _auth(x_api_key)
    return {
        "name": "KaroX",
        "version": VERSION,
        "api": "local-agent",
        "platform": os.name,
        "python": os.sys.version.split()[0],
        "repoRoot": str(core.REPO_ROOT),
        "mode": core.mode(),
        "branch": core.current_branch(),
        "controlCenter": "/control",
    }


@app.get("/capabilities", tags=["system"])
def karox_capabilities(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    _auth(x_api_key)
    writable = core.mode() != "read_only"
    return {
        "version": VERSION,
        "read": True,
        "write": writable,
        "run": writable,
        "safeCommit": writable and bool(core.TASK_STATE.get("commitAllowed")),
        "push": False,
        "missionControl": True,
        "largeOutputCapture": True,
        "supportBundle": True,
        "controlCenter": True,
        "authentication": ["X-API-Key", "Authorization: Bearer"],
        "limits": {
            "maxFileBytes": core.MAX_FILE_SIZE,
            "maxInlineOutputChars": core.MAX_INLINE_OUTPUT,
            "maxRequestBytes": MAX_REQUEST_BYTES,
        },
    }


@app.get("/security/status", tags=["system"])
def karox_security_status(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    _auth(x_api_key)
    return {
        "constantTimeKeyComparison": True,
        "requestBodyLimit": MAX_REQUEST_BYTES,
        "auditRotationBytes": AUDIT_MAX_BYTES,
        "auditBackups": AUDIT_BACKUPS,
        "secureResponseHeaders": True,
        "failedAuthRateLimitPerMinute": 30,
        "corsOrigins": sorted(ALLOWED_ORIGINS),
        "debugErrors": DEBUG_ERRORS,
        "repositoryScoped": True,
        "pushAllowed": False,
    }


CONTROL_CENTER_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>KaroX Control Center</title>
<style>
:root{color-scheme:dark;--bg:#080a10;--panel:#111522;--line:#29304a;--text:#f4f6ff;--muted:#939bb8;--accent:#9d7cff;--ok:#57dc9b;--warn:#ffc857;--bad:#ff6b81}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 15% 0,#211844 0,transparent 34%),var(--bg);color:var(--text);font:14px/1.5 Inter,ui-sans-serif,system-ui,sans-serif}
main{max-width:1180px;margin:auto;padding:30px 20px 70px}.hero{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;margin-bottom:24px}.brand{font-size:28px;font-weight:850;letter-spacing:-.04em}.brand b{color:var(--accent)}.sub{color:var(--muted);max-width:650px}.pill{border:1px solid var(--line);background:#0d101a;padding:7px 11px;border-radius:999px;color:var(--muted)}
.connect,.card{background:linear-gradient(180deg,rgba(21,26,43,.96),rgba(13,16,27,.96));border:1px solid var(--line);border-radius:16px;box-shadow:0 18px 55px #0007}.connect{padding:16px;display:grid;grid-template-columns:1fr auto auto;gap:10px;margin-bottom:18px}.connect input{width:100%;border:1px solid var(--line);background:#090c14;color:var(--text);border-radius:10px;padding:11px 13px;outline:none}.connect input:focus{border-color:var(--accent)}button{border:0;border-radius:10px;padding:10px 14px;font-weight:750;cursor:pointer;background:var(--accent);color:#090713}button.secondary{background:#20263a;color:var(--text);border:1px solid var(--line)}button:hover{filter:brightness(1.08)}
.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:14px}.card{padding:17px;min-height:130px}.span4{grid-column:span 4}.span6{grid-column:span 6}.span12{grid-column:span 12}.label{font-size:11px;text-transform:uppercase;letter-spacing:.12em;color:var(--muted);margin-bottom:8px}.value{font-size:20px;font-weight:800;word-break:break-word}.mini{color:var(--muted);margin-top:6px}.status{display:inline-flex;align-items:center;gap:7px}.dot{width:9px;height:9px;border-radius:50%;background:var(--warn);box-shadow:0 0 14px currentColor}.ok .dot{background:var(--ok)}.bad .dot{background:var(--bad)}pre{white-space:pre-wrap;word-break:break-word;background:#080b13;border:1px solid #242a3d;padding:13px;border-radius:10px;max-height:420px;overflow:auto;color:#d9def2}.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}.empty{color:var(--muted);padding:32px;text-align:center}.error{color:var(--bad)}
@media(max-width:780px){.hero{display:block}.pill{display:inline-block;margin-top:12px}.connect{grid-template-columns:1fr}.span4,.span6{grid-column:span 12}}
</style>
</head>
<body><main>
<section class="hero"><div><div class="brand">★ Karo<b>X</b> Control Center</div><div class="sub">A local-first dashboard for the current repository session. The key stays in this browser tab and is never placed in the URL.</div></div><div class="pill" id="version">Disconnected</div></section>
<section class="connect"><input id="key" type="password" autocomplete="off" placeholder="Paste the session key copied with K"><button id="connect">Connect</button><button class="secondary" id="forget">Forget key</button></section>
<section id="content" class="empty card">Connect to inspect the live session, Mission Control, Git state and capabilities.</section>
</main>
<script>
const $=s=>document.querySelector(s), keyInput=$('#key'), content=$('#content');
keyInput.value=sessionStorage.getItem('karoxKey')||'';
async function api(path){const key=keyInput.value.trim();if(!key)throw new Error('Enter the session key first.');const r=await fetch(path,{headers:{'X-API-Key':key,'Accept':'application/json'}});let data;try{data=await r.json()}catch{data={error:await r.text()}}if(!r.ok)throw new Error(data.detail||data.error||('HTTP '+r.status));return data}
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function card(label,value,mini,span=4){return `<article class="card span${span}"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div>${mini?`<div class="mini">${esc(mini)}</div>`:''}</article>`}
async function refresh(){content.className='empty card';content.textContent='Loading live KaroX context…';try{sessionStorage.setItem('karoxKey',keyInput.value.trim());const [meta,session,git,brief,caps,changed]=await Promise.all([api('/meta'),api('/session'),api('/git/status'),api('/context/brief'),api('/capabilities'),api('/git/changed-files')]);$('#version').textContent='KaroX '+meta.version;const statusText=(session.status||'running').toUpperCase();content.className='grid';content.innerHTML=card('Session',session.sessionTitle||'KaroX session',statusText,4)+card('Repository',meta.repoRoot,meta.branch,4)+card('Access',meta.mode,caps.write?'Writable with guardrails':'Read only',4)+card('Changed files',(changed.files||[]).length,'Push is always blocked',4)+card('Task',session.task||'Waiting for a real task',session.finishedAt?('Finished '+session.finishedAt):'Live task state',4)+card('Capabilities',caps.safeCommit?'Safe commit enabled':'Commit disabled','Mission Control enabled',4)+`<article class="card span6"><div class="label">Git status</div><pre>${esc(git.stdout||JSON.stringify(git,null,2))}</pre></article>`+`<article class="card span6"><div class="label">Mission Control</div><pre id="brief">${esc(JSON.stringify(brief,null,2))}</pre><div class="actions"><button id="copy">Copy context</button><button class="secondary" id="refresh">Refresh</button><button class="secondary" id="docs">Open API docs</button></div></article>`;$('#copy').onclick=()=>navigator.clipboard.writeText(JSON.stringify(brief,null,2));$('#refresh').onclick=refresh;$('#docs').onclick=()=>window.open('/docs','_blank','noopener');}catch(e){content.className='empty card error';content.textContent=e.message}}
$('#connect').onclick=refresh;$('#forget').onclick=()=>{sessionStorage.removeItem('karoxKey');keyInput.value='';content.className='empty card';content.textContent='Key removed from this browser tab.'};keyInput.addEventListener('keydown',e=>{if(e.key==='Enter')refresh()});if(keyInput.value)refresh();
</script></body></html>'''


@app.get("/control", include_in_schema=False, response_class=HTMLResponse)
def karox_control_center():
    return HTMLResponse(CONTROL_CENTER_HTML)
