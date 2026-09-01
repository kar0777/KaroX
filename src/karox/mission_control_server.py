"""Small authenticated web surface for KaroX Mission Control.

Designed for loopback or a user's private Tailscale address. Remote use always
requires a short-lived pairing code and then an HttpOnly SameSite=Strict cookie.
No bridge/API credential is ever placed in a URL.
"""

from __future__ import annotations

import hashlib
import html
import hmac
import ipaddress
import json
import secrets
import threading
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlsplit

from .mission_control import COMMAND_TYPES, MissionControlStore


class MissionControlServerError(RuntimeError):
    pass


_TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")
_TAILSCALE_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")


def _safe_bind_host(value: str) -> str:
    """Allow Mission Control only on loopback or a real Tailscale address."""

    host = str(value).strip()
    if host.casefold() == "localhost":
        return "127.0.0.1"
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise MissionControlServerError(
            "Mission Control host must be loopback or a Tailscale IP address"
        ) from exc
    if address.is_loopback or address in _TAILSCALE_V4 or address in _TAILSCALE_V6:
        return str(address)
    raise MissionControlServerError(
        "Mission Control refuses public, wildcard, and non-Tailscale LAN bind addresses"
    )


class PairingAuthority:
    def __init__(self, *, ttl_seconds: float = 600.0) -> None:
        if ttl_seconds <= 0 or ttl_seconds > 3600:
            raise ValueError("pairing TTL must be between 0 and 3600 seconds")
        self.code = secrets.token_urlsafe(8)
        self.expires_at = time.time() + ttl_seconds
        self._sessions: dict[str, float] = {}
        self._code_used = False
        self._lock = threading.RLock()

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def pair(self, code: str) -> Optional[str]:
        """Exchange the short-lived code exactly once for one durable session token.

        The code is intentionally single-use.  Without this guard, anyone who saw
        the code once could create additional 24-hour sessions until the pairing
        TTL expired.  Validation and consumption happen under one lock so two
        concurrent requests cannot both win the race.
        """

        now = time.time()
        with self._lock:
            if self._code_used or now > self.expires_at:
                return None
            if not hmac.compare_digest(str(code), self.code):
                return None
            self._code_used = True
            token = secrets.token_urlsafe(32)
            self._sessions[self._hash(token)] = now + 24 * 3600
            return token

    def valid(self, token: Optional[str]) -> bool:
        if not token:
            return False
        digest = self._hash(token)
        now = time.time()
        with self._lock:
            expiry = self._sessions.get(digest)
            if expiry is None:
                return False
            if expiry < now:
                self._sessions.pop(digest, None)
                return False
            return True


_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>KaroX Mission Control</title>
<style>
:root{color-scheme:dark;--bg:#090b10;--panel:#121620;--line:#2a3140;--text:#f5f7fb;--muted:#9aa5b5;--accent:#c5ff4a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,sans-serif}main{max-width:920px;margin:auto;padding:24px}.top{display:flex;justify-content:space-between;align-items:center;gap:16px}.brand{font-size:24px;font-weight:800}.muted{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin-top:18px}.card{border:1px solid var(--line);background:var(--panel);border-radius:14px;padding:14px}.agent{margin-top:10px}.pill{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:4px 8px}.controls{display:flex;gap:8px;flex-wrap:wrap;margin-top:18px}button,input{font:inherit;border-radius:9px;border:1px solid var(--line);padding:10px;background:#171d29;color:var(--text)}button{cursor:pointer}button.primary{background:var(--accent);color:#101307;border:0;font-weight:700}button.danger{border-color:#7b3941;color:#ffb5bd}input{flex:1;min-width:220px}#screenshot{display:none;width:100%;max-height:520px;object-fit:contain;margin-top:18px;border:1px solid var(--line);border-radius:14px;background:#05070a}
</style></head><body><main><div class="top"><div><div class="brand">KaroX Mission Control</div><div class="muted" id="objective"></div></div><div class="pill" id="status">loading</div></div><div class="grid" id="stats"></div><div id="agents"></div><img id="screenshot" alt="Latest KaroX screenshot"><div class="controls"><input id="steer" placeholder="Instruction for orchestrator"><button class="primary" onclick="cmd('steer')">Steer</button><button onclick="cmd('pause')">Pause</button><button onclick="cmd('resume')">Resume</button><button class="danger" onclick="cmd('stop')">Stop</button></div><p class="muted">Approval requests never bypass KaroX Smart Stop. Stop is applied only at a safe orchestration boundary.</p></main>
<script>
const roleLabels={orchestrator:'Orchestrator',planner:'Planner',scout:'Project scout',implementer:'Implementer',tester:'Tester',reviewer:'Independent reviewer',security:'Security reviewer',summarizer:'Summarizer',ui:'UI verifier'};
let shownScreenshot='',pendingScreenshot='';
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function roleName(value){let key=String(value||'worker');return roleLabels[key]||key.replace(/[_-]+/g,' ').replace(/\\b\\w/g,c=>c.toUpperCase())}
function refreshScreenshot(id){if(!id||id===shownScreenshot||id===pendingScreenshot)return;pendingScreenshot=id;let i=document.querySelector('#screenshot');i.onload=()=>{shownScreenshot=id;pendingScreenshot='';i.style.display='block'};i.onerror=()=>{pendingScreenshot='';i.style.display='none'};i.src='/api/screenshot?v='+encodeURIComponent(id)}
async function refresh(){if(document.hidden)return;let r=await fetch('/api/status',{credentials:'same-origin'});if(r.status===401){location='/pair';return}if(!r.ok)return;let d=await r.json();document.querySelector('#objective').textContent=d.objective||'';document.querySelector('#status').textContent=d.status||'unknown';let cache=d.cache_hit_rate==null?'':`<div class=card>Cache<br><b>${Math.max(0,Math.min(100,Number(d.cache_hit_rate)*100)).toFixed(0)}%</b></div>`;document.querySelector('#stats').innerHTML=`<div class=card>Progress<br><b>${Number(d.progress_percent||0).toFixed(0)}%</b></div><div class=card>Cost<br><b>$${Number(d.actual_cost_usd||0).toFixed(4)}</b></div>${cache}`;document.querySelector('#agents').innerHTML=(d.agents||[]).map(a=>`<div class="card agent"><b>${esc(roleName(a.role))}</b> <span class=pill>${esc(a.status)}</span><div class=muted>${esc(a.activity||'')}</div></div>`).join('');refreshScreenshot(d.latest_screenshot_artifact_id||'')}
async function cmd(type){let text=type==='steer'?document.querySelector('#steer').value:'';let r=await fetch('/api/command',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({type,text,target:'orchestrator'}),credentials:'same-origin'});if(!r.ok)alert(await r.text());else{if(type==='steer')document.querySelector('#steer').value='';refresh()}}
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh()});
refresh();setInterval(refresh,5000);
</script></body></html>"""

_PAIR_HTML = """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Pair KaroX</title><style>body{font-family:system-ui;background:#090b10;color:#fff;display:grid;place-items:center;height:100vh;margin:0}form{padding:24px;border:1px solid #303848;border-radius:16px;background:#121620}input,button{font:inherit;padding:12px;border-radius:8px;border:1px solid #303848;background:#171d29;color:#fff}button{cursor:pointer}</style></head><body><form method="post" action="/pair"><h2>Pair with KaroX</h2><p>Enter the short-lived code shown by KaroX on the computer.</p><input name="code" autocomplete="one-time-code" autofocus><button>Pair</button></form></body></html>"""


class MissionControlServer:
    def __init__(
        self,
        store: MissionControlStore,
        *,
        host: str = "127.0.0.1",
        port: int = 8766,
        pairing: Optional[PairingAuthority] = None,
        image_reader: Optional[Callable[[str], tuple[bytes, str]]] = None,
    ) -> None:
        if not 0 <= int(port) <= 65535:
            raise ValueError("Mission Control port must be between 0 and 65535")
        self.store = store
        self.host = _safe_bind_host(host)
        self.port = int(port)
        self.pairing = pairing or PairingAuthority()
        self.image_reader = image_reader
        self._server: Optional[ThreadingHTTPServer] = None
        self._serving = False

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "KaroXMissionControl/1"

            def log_message(self, fmt: str, *args: Any) -> None:
                # Do not log pairing codes, cookies, steering text or URLs.
                return

            def _session_token(self) -> Optional[str]:
                raw = self.headers.get("Cookie", "")
                cookie = SimpleCookie()
                try:
                    cookie.load(raw)
                except Exception:
                    return None
                morsel = cookie.get("karox_mc")
                return None if morsel is None else morsel.value

            def _authorized(self) -> bool:
                return outer.pairing.valid(self._session_token())

            def _send(self, status: int, body: bytes, content_type: str = "text/plain; charset=utf-8", *, headers: Optional[dict[str, str]] = None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'")
                if headers:
                    for key, value in headers.items():
                        self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def _json(self, status: int, value: Any) -> None:
                self._send(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"), "application/json; charset=utf-8")

            def do_GET(self) -> None:  # noqa: N802
                path = urlsplit(self.path).path
                if path == "/pair":
                    self._send(HTTPStatus.OK, _PAIR_HTML.encode("utf-8"), "text/html; charset=utf-8")
                    return
                if not self._authorized():
                    self._send(HTTPStatus.UNAUTHORIZED, b"Pairing required")
                    return
                if path == "/":
                    self._send(HTTPStatus.OK, _HTML.encode("utf-8"), "text/html; charset=utf-8")
                    return
                if path == "/api/status":
                    snapshot = outer.store.snapshot()
                    self._json(HTTPStatus.OK, {} if snapshot is None else snapshot.to_dict())
                    return
                if path == "/api/screenshot":
                    snapshot = outer.store.snapshot()
                    artifact_id = None if snapshot is None else snapshot.latest_screenshot_artifact_id
                    if not artifact_id or outer.image_reader is None:
                        self._send(HTTPStatus.NOT_FOUND, b"Screenshot unavailable")
                        return
                    try:
                        data, mime = outer.image_reader(artifact_id)
                    except Exception:
                        self._send(HTTPStatus.NOT_FOUND, b"Screenshot unavailable")
                        return
                    if mime != "image/png":
                        self._send(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, b"Only PNG screenshots are supported")
                        return
                    self._send(HTTPStatus.OK, data, "image/png")
                    return
                self._send(HTTPStatus.NOT_FOUND, b"Not found")

            def do_POST(self) -> None:  # noqa: N802
                path = urlsplit(self.path).path
                length_raw = self.headers.get("Content-Length", "0")
                try:
                    length = int(length_raw)
                except ValueError:
                    self._send(HTTPStatus.BAD_REQUEST, b"Invalid Content-Length")
                    return
                if length < 0 or length > 16_384:
                    self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, b"Request too large")
                    return
                body = self.rfile.read(length)
                if path == "/pair":
                    fields = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
                    token = outer.pairing.pair((fields.get("code") or [""])[0])
                    if token is None:
                        self._send(HTTPStatus.UNAUTHORIZED, b"Pairing code is invalid or expired")
                        return
                    self._send(
                        HTTPStatus.SEE_OTHER,
                        b"",
                        headers={
                            "Location": "/",
                            "Set-Cookie": f"karox_mc={token}; Path=/; HttpOnly; SameSite=Strict",
                        },
                    )
                    return
                if not self._authorized():
                    self._send(HTTPStatus.UNAUTHORIZED, b"Pairing required")
                    return
                if path != "/api/command":
                    self._send(HTTPStatus.NOT_FOUND, b"Not found")
                    return
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                    self._send(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, b"application/json required")
                    return
                try:
                    payload = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._send(HTTPStatus.BAD_REQUEST, b"Invalid JSON")
                    return
                if not isinstance(payload, dict):
                    self._send(HTTPStatus.BAD_REQUEST, b"Command must be an object")
                    return
                command_type = payload.get("type")
                if command_type not in COMMAND_TYPES:
                    self._send(HTTPStatus.BAD_REQUEST, b"Unsupported command")
                    return
                try:
                    command = outer.store.enqueue(
                        command_type,
                        target=str(payload.get("target") or "orchestrator"),
                        text=str(payload.get("text") or ""),
                    )
                except (TypeError, ValueError) as exc:
                    self._send(HTTPStatus.BAD_REQUEST, html.escape(str(exc)).encode("utf-8"))
                    return
                self._json(HTTPStatus.ACCEPTED, command.to_dict())

        return Handler

    def bind(self) -> tuple[str, int]:
        """Bind once and return the actual address without starting the loop.

        ``port=0`` is supported so a detached mobile Mission Control worker can
        let the OS choose a private free port without a find-free/bind-later
        race. Calling :meth:`serve_forever` afterwards reuses this exact bound
        server socket.
        """

        if self._server is None:
            server = ThreadingHTTPServer((self.host, self.port), self._handler())
            server.daemon_threads = True
            self._server = server
        address = self._server.server_address
        return str(address[0]), int(address[1])

    def serve_forever(self) -> None:
        if self._serving:
            raise MissionControlServerError("Mission Control server is already running")
        self.bind()
        server = self._server
        if server is None:  # pragma: no cover - defensive invariant
            raise MissionControlServerError("Mission Control server failed to bind")
        self._serving = True
        try:
            server.serve_forever(poll_interval=0.25)
        finally:
            server.server_close()
            self._server = None
            self._serving = False

    def shutdown(self) -> None:
        server = self._server
        if server is not None and self._serving:
            server.shutdown()
        elif server is not None:
            server.server_close()
            self._server = None


__all__ = ["MissionControlServer", "MissionControlServerError", "PairingAuthority"]
