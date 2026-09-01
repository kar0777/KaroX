"""One-shot localhost credential capture for provider API keys.

The hosted agent may open the local page but never sees the submitted value.
The form POST is handled inside the KaroX process and stores the secret directly
in the OS keyring, then updates only the opaque ProviderRecord.credential_ref.
"""

from __future__ import annotations

import dataclasses
import html
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from .credentials import CredentialStore
from .paths import config_dir
from .registry import ProviderRegistry


@dataclass
class CaptureState:
    token: str
    provider_id: str
    url: str
    created_at: float
    expires_at: float
    status: str = "pending"
    fingerprint: Optional[str] = None
    error: Optional[str] = None

    def public(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "provider_id": self.provider_id,
            "url": self.url,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "status": self.status,
            "fingerprint": self.fingerprint,
            "error": self.error,
        }


_LOCK = threading.RLock()
_CAPTURES: dict[str, CaptureState] = {}
_SERVERS: dict[str, ThreadingHTTPServer] = {}


def _provider_label(provider_id: str) -> str:
    return "OpenRouter" if provider_id == "openrouter" else provider_id


def _page(provider_id: str, token: str, *, message: str = "", success: bool = False) -> bytes:
    label = html.escape(_provider_label(provider_id))
    note = html.escape(message)
    body = (
        f"<h1>{'Saved' if success else 'Connect ' + label}</h1>"
        f"<p>{note or ('Enter the API key for ' + label + '. It is stored only in the local OS credential manager.')}</p>"
    )
    if not success:
        body += f"""
<form method="post" action="/capture" autocomplete="off">
<input type="hidden" name="token" value="{html.escape(token)}">
<label for="api-key">API key</label>
<input id="api-key" name="api_key" type="password" required autocomplete="off" spellcheck="false" autofocus>
<button type="submit">Save locally</button>
</form>"""
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>KaroX · {label}</title><style>
:root{{font-family:Inter,system-ui,sans-serif;color-scheme:dark}}*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#111;color:#eee}}
main{{width:min(520px,calc(100% - 32px));padding:28px;border:1px solid #333;border-radius:18px;background:#181818;box-shadow:0 20px 60px #0008}}h1{{font-size:24px;margin:0 0 8px}}p{{color:#aaa;line-height:1.5;margin:0 0 22px}}label{{display:block;margin-bottom:8px;color:#ccc}}
input{{width:100%;padding:13px;border:1px solid #444;border-radius:10px;background:#101010;color:#fff;font:inherit}}input:focus{{outline:2px solid #d4b676;outline-offset:2px}}button{{margin-top:16px;width:100%;padding:12px;border:0;border-radius:10px;background:#d4b676;color:#111;font-weight:700;cursor:pointer}}
small{{color:#777}}</style></head><body><main>{body}<small>No key value is returned to the hosted agent.</small></main></body></html>""".encode("utf-8")


def _safe_key(provider_id: str, raw: str) -> str:
    value = raw.strip()
    if not value or len(value) > 65536 or any(c in value for c in "\r\n\x00"):
        raise ValueError("credential format is invalid")
    if provider_id == "openrouter" and not value.startswith("sk-or-v1-"):
        raise ValueError("credential does not look like an OpenRouter API key")
    return value


def start_capture(provider_id: str, *, ttl_seconds: float = 600.0) -> CaptureState:
    registry = ProviderRegistry(config_dir() / "vnext" / "providers.json")
    provider = registry.provider(provider_id)
    if not provider.enabled:
        raise ValueError("provider is disabled")
    if not 60 <= float(ttl_seconds) <= 1800:
        raise ValueError("capture TTL must be between 60 and 1800 seconds")
    token = secrets.token_urlsafe(32)
    now = time.time()

    class Handler(BaseHTTPRequestHandler):
        server_version = "KaroXCredentialCapture/1"

        def log_message(self, _format: str, *args: object) -> None:
            # Request paths and form bodies never enter stdout/stderr.
            return

        def _headers(self, status: int, length: int) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlsplit(self.path)
            supplied = urllib.parse.parse_qs(parsed.query).get("token", [""])[0]
            with _LOCK:
                state = _CAPTURES.get(token)
            if state is None or supplied != token or time.time() >= state.expires_at:
                payload = _page(provider_id, token, message="This credential link is invalid or expired.")
                self._headers(HTTPStatus.GONE, len(payload)); self.wfile.write(payload); return
            payload = _page(provider_id, token)
            self._headers(HTTPStatus.OK, len(payload)); self.wfile.write(payload)

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/capture":
                self.send_error(HTTPStatus.NOT_FOUND); return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0 or length > 70_000:
                self.send_error(HTTPStatus.BAD_REQUEST); return
            raw = self.rfile.read(length)
            try:
                form = urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True)
            except UnicodeDecodeError:
                self.send_error(HTTPStatus.BAD_REQUEST); return
            supplied = form.get("token", [""])[0]
            value = form.get("api_key", [""])[0]
            with _LOCK:
                state = _CAPTURES.get(token)
            if state is None or supplied != token or state.status != "pending" or time.time() >= state.expires_at:
                payload = _page(provider_id, token, message="This credential link is invalid or expired.")
                self._headers(HTTPStatus.GONE, len(payload)); self.wfile.write(payload); return
            try:
                secret = _safe_key(provider_id, value)
                store = CredentialStore()
                stored = store.set(provider_id, secret)
                reg = ProviderRegistry(config_dir() / "vnext" / "providers.json")
                current = reg.provider(provider_id)
                reg.put_provider(dataclasses.replace(current, credential_ref=stored["reference"]))
                with _LOCK:
                    state.status = "saved"
                    state.fingerprint = stored["fingerprint"]
                    state.error = None
                payload = _page(provider_id, token, message="Credential saved locally. You can return to ChatGPT.", success=True)
                self._headers(HTTPStatus.OK, len(payload)); self.wfile.write(payload)
            except Exception as exc:
                with _LOCK:
                    state.status = "error"
                    state.error = type(exc).__name__
                payload = _page(provider_id, token, message="Could not save the credential. Check the value and try again.")
                self._headers(HTTPStatus.BAD_REQUEST, len(payload)); self.wfile.write(payload)
                return
            finally:
                # Drop references to the submitted value as soon as practical.
                value = ""
                raw = b""
            threading.Thread(target=self.server.shutdown, daemon=True).start()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = int(server.server_address[1])
    url = f"http://127.0.0.1:{port}/?token={urllib.parse.quote(token)}"
    state = CaptureState(token, provider_id, url, now, now + float(ttl_seconds))
    with _LOCK:
        _CAPTURES[token] = state
        _SERVERS[token] = server
    thread = threading.Thread(target=server.serve_forever, name=f"karox-credential-{provider_id}", daemon=True)
    thread.start()
    return state


def capture_status(token: str) -> CaptureState:
    with _LOCK:
        state = _CAPTURES.get(token)
        if state is None:
            raise KeyError("credential capture does not exist")
        if state.status == "pending" and time.time() >= state.expires_at:
            state.status = "expired"
        return state


__all__ = ["CaptureState", "capture_status", "start_capture"]
