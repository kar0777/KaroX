"""KaroX 4.0 — Phase 4: managed dev-servers, localhost http_fetch,
Playwright headless browser, allowlisted package managers (publish hard-blocked).
"""
from __future__ import annotations

import base64
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from fastapi import Header, HTTPException
from pydantic import BaseModel, Field

import repo_tools as core
import karox4_core as k4
import karox4_exec as kexec

app = core.app

# --------------------------------------------------------------------------
# Managed dev-server
# --------------------------------------------------------------------------

_PORT_PATTERNS = [
    re.compile(r"(?:https?://)?(?:localhost|127\.0\.0\.1|0\.0\.0\.0):(\d{2,5})"),
    re.compile(r"(?i)port[\s:=]+(\d{2,5})"),
]


class DevServerStartBody(BaseModel):
    cmd: Optional[str] = None
    argv: Optional[List[str]] = None
    shell: Optional[Literal["cmd", "powershell", "bash", "sh"]] = None
    name: str = "devserver"
    cwd: Optional[str] = None
    env: Optional[Dict[str, str]] = None
    expectedPort: Optional[int] = None
    waitTimeoutSeconds: float = Field(default=90, ge=1, le=600)


@app.post("/devserver/start")
def devserver_start(body: DevServerStartBody, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    core.ensure_not_read_only("POST /devserver/start")
    job_body = kexec.JobStartBody(
        argv=body.argv,
        shell=body.shell or (None if body.argv else ("cmd" if kexec.IS_WINDOWS else "sh")),
        cmd=body.cmd,
        name=body.name,
        cwd=body.cwd,
        env=body.env,
    )
    started = kexec.job_start(job_body, x_api_key)
    job_id = started["jobId"]
    log_path = Path(started["log"])
    deadline = time.time() + body.waitTimeoutSeconds
    port: Optional[int] = body.expectedPort
    detected_from = "expectedPort" if body.expectedPort else None
    while time.time() < deadline:
        job = kexec._refresh_job(job_id)
        if job.get("state") not in ("running",):
            tail = ""
            if log_path.exists():
                tail = kexec.normalize_bytes(log_path.read_bytes()[-20000:])
            raise HTTPException(status_code=500, detail={
                "message": "Dev-сервер завершился до готовности",
                "jobId": job_id,
                "state": job.get("state"),
                "exitCode": job.get("exitCode"),
                "logTail": tail[-4000:],
            })
        if port is None and log_path.exists():
            text = kexec.normalize_bytes(log_path.read_bytes()[-100000:])
            for rx in _PORT_PATTERNS:
                m = rx.search(text)
                if m:
                    port = int(m.group(1))
                    detected_from = "log"
                    break
        if port is not None:
            probe = kexec.wait_for_port(port=port, timeout_seconds=1.5, x_api_key=x_api_key)
            if probe.get("open"):
                url = f"http://localhost:{port}"
                core.audit("devserver_start", {"jobId": job_id, "port": port, "detectedFrom": detected_from})
                k4.emit_event("devserver_ready", {"jobId": job_id, "port": port, "url": url})
                return {"ok": True, "jobId": job_id, "port": port, "url": url, "detectedFrom": detected_from}
        time.sleep(0.5)
    return {
        "ok": False,
        "jobId": job_id,
        "port": port,
        "detail": "Порт не обнаружен/не открылся за отведённое время; джоб продолжает работать. Проверьте job_tail.",
    }


class DevServerStopBody(BaseModel):
    jobId: str


@app.post("/devserver/stop")
def devserver_stop(body: DevServerStopBody, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    core.ensure_not_read_only("POST /devserver/stop")
    result = kexec.job_signal(body.jobId, kexec.JobSignalBody(signal="kill"), x_api_key)
    core.audit("devserver_stop", {"jobId": body.jobId, "ok": result.get("ok")})
    return result

# --------------------------------------------------------------------------
# http_fetch — localhost only
# --------------------------------------------------------------------------

class HttpFetchBody(BaseModel):
    url: str
    method: Literal["GET", "POST", "HEAD"] = "GET"
    headers: Optional[Dict[str, str]] = None
    body: Optional[str] = None
    timeoutSeconds: float = Field(default=15, ge=1, le=120)
    maxBytes: int = Field(default=200000, ge=1000, le=2_000_000)


@app.post("/http/fetch")
def http_fetch(body: HttpFetchBody, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    kexec.ensure_local_url(body.url)
    headers = {k: v for k, v in (body.headers or {}).items() if k.lower() not in ("host",)}
    req = Request(body.url, method=body.method, headers=headers, data=body.body.encode("utf-8") if body.body else None)
    started = time.time()
    try:
        with urlopen(req, timeout=body.timeoutSeconds) as resp:
            final_host = (urlparse(resp.geturl()).hostname or "").lower()
            if final_host not in kexec.LOCAL_HOSTS:
                raise HTTPException(status_code=403, detail=f"Редирект на внешний адрес запрещён: {final_host}")
            raw = resp.read(body.maxBytes + 1)
            truncated = len(raw) > body.maxBytes
            text = kexec.normalize_bytes(raw[: body.maxBytes])
            return {
                "status": resp.status,
                "headers": dict(resp.headers.items()),
                "body": text,
                "truncated": truncated,
                "elapsedSeconds": round(time.time() - started, 2),
            }
    except HTTPError as e:
        raw = e.read(body.maxBytes) if hasattr(e, "read") else b""
        return {
            "status": e.code,
            "headers": dict(e.headers.items()) if e.headers else {},
            "body": kexec.normalize_bytes(raw),
            "elapsedSeconds": round(time.time() - started, 2),
        }
    except URLError as e:
        raise HTTPException(status_code=502, detail=f"Локальный запрос не удался: {e}")

# --------------------------------------------------------------------------
# Headless browser (Playwright, optional dependency)
# --------------------------------------------------------------------------

import os as _os
_BROWSER_ALLOW_EXTERNAL = _os.environ.get("KAROX_BROWSER_ALLOW_EXTERNAL") == "1"


def _ensure_browser_url(url: str) -> None:
    if _BROWSER_ALLOW_EXTERNAL:
        return
    kexec.ensure_local_url(url)


def _with_page(url: str, viewport_w: int, viewport_h: int, wait_ms: int, fn):
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        raise HTTPException(status_code=501, detail="Playwright не установлен. Установка: pip install playwright && python -m playwright install chromium")
    _ensure_browser_url(url)
    console: List[Dict[str, str]] = []
    page_errors: List[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": max(320, min(viewport_w, 3840)), "height": max(240, min(viewport_h, 2160))})
            page.on("console", lambda msg: console.append({"type": msg.type, "text": msg.text[:500]}))
            page.on("pageerror", lambda err: page_errors.append(str(err)[:500]))
            page.goto(url, wait_until="load", timeout=30000)
            if wait_ms:
                page.wait_for_timeout(max(0, min(int(wait_ms), 15000)))
            result = fn(page)
        finally:
            browser.close()
    return result, console, page_errors


class BrowserBody(BaseModel):
    action: Literal["screenshot", "dom", "console", "click", "type"]
    url: str
    selector: Optional[str] = None
    text: Optional[str] = None
    fullPage: bool = True
    viewportWidth: int = 1280
    viewportHeight: int = 800
    waitMs: int = 500


@app.post("/browser")
def browser_action(body: BrowserBody, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)

    def run(page):
        if body.action == "screenshot":
            return {"imageBase64": base64.b64encode(page.screenshot(full_page=body.fullPage)).decode("ascii"), "mimeType": "image/png"}
        if body.action == "dom":
            if body.selector:
                node = page.query_selector(body.selector)
                if node is None:
                    return {"found": False, "selector": body.selector}
                return {"found": True, "selector": body.selector, "html": node.evaluate("el => el.outerHTML")[:100000], "text": (node.inner_text() or "")[:20000]}
            return {"html": page.content()[:200000]}
        if body.action == "console":
            return {}
        if body.action == "click":
            if not body.selector:
                raise HTTPException(status_code=400, detail="Нужен selector")
            page.click(body.selector, timeout=10000)
            page.wait_for_timeout(300)
            return {"clicked": body.selector, "imageBase64": base64.b64encode(page.screenshot(full_page=False)).decode("ascii"), "mimeType": "image/png"}
        if body.action == "type":
            if not body.selector or body.text is None:
                raise HTTPException(status_code=400, detail="Нужны selector и text")
            page.fill(body.selector, body.text, timeout=10000)
            return {"typed": True, "selector": body.selector}
        raise HTTPException(status_code=400, detail=f"Неизвестное действие: {body.action}")

    result, console, page_errors = _with_page(body.url, body.viewportWidth, body.viewportHeight, body.waitMs, run)
    core.audit("browser", {"action": body.action, "url": body.url[:300], "selector": (body.selector or "")[:200]})
    return {"ok": True, "action": body.action, "result": result, "console": console[-50:], "pageErrors": page_errors[-20:]}

# --------------------------------------------------------------------------
# Package managers (allowlist; publish is hard-blocked)
# --------------------------------------------------------------------------

_PKG_MANAGERS: Dict[str, Dict[str, Any]] = {
    "npm": {"exe": "npm", "actions": {"install", "ci", "uninstall", "update", "dedupe", "audit", "ls"}},
    "pnpm": {"exe": "pnpm", "actions": {"install", "add", "remove", "update", "audit", "list"}},
    "yarn": {"exe": "yarn", "actions": {"install", "add", "remove", "upgrade", "audit", "list"}},
    "pip": {"exe": "pip", "actions": {"install", "uninstall", "list", "show", "freeze", "download"}},
    "poetry": {"exe": "poetry", "actions": {"install", "add", "remove", "update", "lock", "show"}},
    "cargo": {"exe": "cargo", "actions": {"add", "remove", "update", "fetch", "tree"}},
    "gradle": {"exe": "gradle", "actions": {"dependencies", "build", "test", "assemble", "tasks"}},
    "maven": {"exe": "mvn", "actions": {"dependency:tree", "dependency:resolve", "compile", "test", "package"}},
}
_PKG_BLOCKED_TOKENS = {"publish", "upload", "deploy", "login", "adduser", "token", "release", "push", "owner", "twine"}
_LOCKFILES = ["package-lock.json", "pnpm-lock.yaml", "yarn.lock", "poetry.lock", "Cargo.lock", "requirements.txt", "gradle.lockfile"]


class PkgBody(BaseModel):
    manager: str
    action: str
    args: List[str] = []
    timeoutSeconds: int = Field(default=900, ge=10, le=7200)


@app.post("/pkg/run")
def pkg_run(body: PkgBody, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    core.ensure_not_read_only("POST /pkg/run")
    manager = _PKG_MANAGERS.get(body.manager.lower())
    if not manager:
        raise HTTPException(status_code=400, detail=f"Менеджер не в allowlist: {body.manager}")
    action = body.action.strip().lower()
    tokens = {action, *[a.strip().lower() for a in body.args]}
    blocked = tokens & _PKG_BLOCKED_TOKENS
    if blocked:
        core.audit("pkg_blocked", {"manager": body.manager, "action": action, "blocked": sorted(blocked)})
        raise HTTPException(status_code=403, detail=f"Жёсткая блокировка публикации/аутентификации: {sorted(blocked)}")
    if action not in manager["actions"]:
        raise HTTPException(status_code=403, detail=f"Действие '{action}' не в allowlist для {body.manager}: {sorted(manager['actions'])}")
    for arg in body.args:
        if re.search(r"[&|;<>`$]", arg):
            raise HTTPException(status_code=400, detail=f"Недопустимый символ в аргументе: {arg}")
    argv = [manager["exe"], action, *body.args]
    before = {name: core.run_git(["hash-object", "--", name], timeout=60).get("stdout", "").strip()
              for name in _LOCKFILES if (core.REPO_ROOT / name).exists()}
    result = kexec.run_argv(argv, cwd=core.REPO_ROOT, timeout=body.timeoutSeconds, tail=60000)
    lock_diffs: List[Dict[str, Any]] = []
    for name in _LOCKFILES:
        if not (core.REPO_ROOT / name).exists():
            continue
        after = core.run_git(["hash-object", "--", name], timeout=60).get("stdout", "").strip()
        if before.get(name) != after:
            stat = core.run_git(["diff", "--stat", "--", name], timeout=120)
            lock_diffs.append({"lockfile": name, "changed": True, "diffStat": (stat.get("stdout") or "")[:2000]})
    core.audit("pkg_run", {"manager": body.manager, "action": action, "args": body.args[:20], "exitCode": result.get("exitCode")})
    return {"ok": result.get("exitCode") == 0, "result": result, "lockfileChanges": lock_diffs}
