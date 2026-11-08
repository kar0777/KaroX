"""OAuth bridge approval pages and ASGI registration."""

from __future__ import annotations

from urllib.parse import parse_qs

async def _body(request: Request) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > _MAX_BODY_BYTES:
                raise OAuthBridgeError("request body is too large")
        except ValueError as exc:
            raise OAuthBridgeError("content-length is invalid") from exc
    value = await request.body()
    if len(value) > _MAX_BODY_BYTES:
        raise OAuthBridgeError("request body is too large")
    return value

async def _form(request: Request) -> dict[str, list[str]]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
    if content_type != "application/x-www-form-urlencoded":
        raise OAuthBridgeError("request must use application/x-www-form-urlencoded")
    raw = await _body(request)
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OAuthBridgeError("request body must be UTF-8") from exc
    return parse_qs(decoded, keep_blank_values=True, strict_parsing=True)


from .bridge_oauth_types import (  # noqa: F401
    OAuthBridgeError as OAuthBridgeError,
    _ACCESS_TTL_SECONDS as _ACCESS_TTL_SECONDS,
    _CLIENT_METADATA_TIMEOUT_SECONDS as _CLIENT_METADATA_TIMEOUT_SECONDS,
    _CODE_TTL_SECONDS as _CODE_TTL_SECONDS,
    _Client as _Client,
    _Code as _Code,
    _Grant as _Grant,
    _MAX_BODY_BYTES as _MAX_BODY_BYTES,
    _MAX_CLIENTS as _MAX_CLIENTS,
    _MAX_CLIENT_METADATA_BYTES as _MAX_CLIENT_METADATA_BYTES,
    _PENDING_TTL_SECONDS as _PENDING_TTL_SECONDS,
    _Pending as _Pending,
    _REFRESH_TTL_SECONDS as _REFRESH_TTL_SECONDS,
    _SCOPES as _SCOPES,
    _STATE_VERSION as _STATE_VERSION,
    _client_metadata_url as _client_metadata_url,
    _code_from_state as _code_from_state,
    _digest as _digest,
    _form_action as _form_action,
    _grant_from_state as _grant_from_state,
    _json_object as _json_object,
    _pending_from_state as _pending_from_state,
    _public_origin as _public_origin,
    _redirect_uri as _redirect_uri,
    _scopes as _scopes,
    _single as _single,
    _single_resource as _single_resource,
    _stored_digest as _stored_digest,
    _stored_scopes as _stored_scopes,
    _token as _token,
    _valid_challenge as _valid_challenge,
)
import html
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit
import anyio
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from .browser_approval import set_trusted_approval_cookie
from .bridge_proxy import (
    build_proxy_asgi_app,
    rebinding_rejection,
    resolve_allowed_hosts,
)
from .hosted_core import DEFAULT_HOSTED_DEADLINE_SECONDS, HostedToolRuntime

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .bridge_oauth import OAuthBridgeService


_REQUEST_PROBE_LIMITER = anyio.CapacityLimiter(1)

def _record_request_probe(state_path: Path, method: str, request_path: str) -> None:
    """Append one method+path line to the bounded OAuth interop trace.

    Records only the HTTP method and URL path: never query strings, headers,
    bodies, cookies, client IDs, authorization codes, or tokens.
    """
    try:
        probe_path = state_path.with_suffix(".request-probe.jsonl")
        if probe_path.is_file() and probe_path.stat().st_size > 262_144:
            keep = probe_path.read_text(encoding="utf-8").splitlines()[-512:]
            probe_path.write_text("".join(line + "\n" for line in keep), encoding="utf-8")
        with probe_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"method": method, "path": request_path}) + "\n")
    except OSError:
        pass

def _oauth_error(message: str, *, status_code: int = 400) -> JSONResponse:
    return JSONResponse(
        {"error": "invalid_request", "error_description": message},
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )

def _approval_language(values: Mapping[str, list[str]]) -> str:
    """Choose one of the approval page's supported languages from OIDC ui_locales."""
    for value in values.get("ui_locales", []):
        for tag in value.split():
            language = tag.split("-", 1)[0].lower()
            if language in {"ru", "en"}:
                return language
    return "en"

def _approval_page(
    service: OAuthBridgeService,
    request_id: str,
    client: _Client,
    *,
    language: str = "en",
) -> str:
    russian = language == "ru"
    title = "Подключение KaroX" if russian else "Authorize KaroX"
    heading = "Разрешить доступ к инструментам KaroX" if russian else "Authorize KaroX tools"
    request_text = (
        "запрашивает доступ к выбранным инструментам на этом компьютере."
        if russian
        else "requests access to the explicitly selected tools on this computer."
    )
    password_label = (
        "Пароль подтверждения из окна KaroX"
        if russian
        else "Approval password from the KaroX window"
    )
    help_text = (
        "Скопируйте строку «OAuth approval password» из окна KaroX и вставьте её сюда. "
        "Не закрывайте KaroX до завершения подключения."
        if russian
        else "Copy the “OAuth approval password” from the KaroX window and paste it here. "
        "Keep KaroX open until the connection finishes."
    )
    button = "Разрешить" if russian else "Authorize"
    return f"""<!doctype html>
<html lang="{language}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{title}</title>
<style>body{{font:16px system-ui;max-width:42rem;margin:4rem auto;padding:0 1rem;background:#111;color:#eee}}
main{{border:1px solid #555;border-radius:12px;padding:1.5rem}}input,button{{font:inherit;width:100%;box-sizing:border-box;padding:.75rem;margin-top:.75rem}}
code{{overflow-wrap:anywhere;color:#d9bd7b}}small{{color:#aaa}}</style></head>
<body><main><h1>{heading}</h1>
<p><strong>{html.escape(client.client_name)}</strong> {request_text}</p>
<p><small>Redirect: {html.escape(client.redirect_uris[0])}<br>Resource: <code>{html.escape(service.resource)}</code></small></p>
<p>{help_text}</p>
<form method="post" action="/oauth/authorize">
<input type="hidden" name="request_id" value="{html.escape(request_id)}">
<label>{password_label}<input type="password" name="password" required autocomplete="current-password"></label>
<button type="submit">{button}</button></form></main></body></html>"""

def _approval_complete_page(location: str) -> str:
    """Return a new document that navigates to the exact registered callback.

    A 303 directly from the form POST makes Chromium apply the approval page's
    ``form-action`` policy to the whole external redirect chain. A separate 200
    document ends the form submission first; its meta refresh is then an ordinary
    navigation. The visible link is a no-script fallback.
    """
    target = html.escape(location, quote=True)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<meta http-equiv="refresh" content="0;url={target}"><title>OAuth approved</title>
<style>body{{font:16px system-ui;max-width:42rem;margin:4rem auto;padding:0 1rem;background:#111;color:#eee}}
main{{border:1px solid #555;border-radius:12px;padding:1.5rem}}a{{color:#d9bd7b}}</style></head>
<body><main><h1>Authorization approved</h1><p>Returning to the client…</p>
<p><a href="{target}" rel="noreferrer">Continue</a></p></main></body></html>"""

def build_oauth_proxy_asgi_app(
    proxy: HostedToolRuntime,
    approval_secret: str | Callable[[], str],
    *,
    public_url: str,
    path: str = "/mcp",
    deadline_seconds: float = DEFAULT_HOSTED_DEADLINE_SECONDS,
    state_dir: Optional[Path] = None,
    allowed_redirect_hosts: Optional[frozenset[str]] = None,
    allowed_client_metadata_hosts: Optional[frozenset[str]] = None,
) -> Any:
    """Expose an MCP bridge with OAuth discovery, DCR, PKCE, and refresh.

    ``state_dir`` is where registrations and refresh grants survive a restart.
    Omitting it keeps every one of them in RAM, which means a connector added in
    ChatGPT or Claude stops working the moment this process exits.

    ``allowed_redirect_hosts`` pins the exact client hostnames a strict profile
    (such as ``hyperagent-web``) will redirect authorization codes to. ``None``
    keeps the permissive default that admits any HTTPS redirect, which the
    chatgpt-web/claude-web profiles and library callers rely on.
    """

    effective_client_metadata_hosts = allowed_client_metadata_hosts
    if effective_client_metadata_hosts is None and path == "/karox/mcp":
        effective_client_metadata_hosts = allowed_redirect_hosts
    from .bridge_oauth import OAuthBridgeService

    service = OAuthBridgeService(
        public_url,
        approval_secret,
        path=path,
        state_dir=state_dir,
        allowed_redirect_hosts=allowed_redirect_hosts,
        allowed_client_metadata_hosts=effective_client_metadata_hosts,
    )
    metadata_url = (
        f"{service.public_url}/.well-known/oauth-protected-resource{service.path}"
    )
    # This app already validated the public origin, so the MCP wire underneath it
    # need not be told the tunnel host name a second time through the environment.
    public_host = urlsplit(service.public_url).hostname or ""
    allowed = resolve_allowed_hosts((public_host,))
    mcp_app = build_proxy_asgi_app(
        proxy,
        path=path,
        deadline_seconds=deadline_seconds,
        bearer_authorizer=service.authorize_access_token,
        unauthorized_headers={
            "WWW-Authenticate": (
                'Bearer realm="OAuth", '
                f'resource_metadata="{metadata_url}", '
                'error="invalid_token", '
                'error_description="Missing or invalid access token"'
            )
        },
        allowed_hosts=(public_host,),
    )

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "lifespan":
            await mcp_app(scope, receive, send)
            return
        if scope.get("type") != "http":
            await Response("not found", status_code=404)(scope, receive, send)
            return
        # The OAuth endpoints are mounted beside /mcp on the same public tunnel,
        # so the rebinding guard has to cover them too: registration and the
        # approval page are reachable without any credential.
        rejection = rebinding_rejection(scope, allowed)
        if rejection is not None:
            await rejection(scope, receive, send)
            return
        request = Request(scope, receive=receive)
        request_path = scope.get("path", "")
        method = scope.get("method", "GET").upper()
        # Interoperability trace for the OAuth discovery edge. It records only
        # the HTTP method and URL path: never query strings, headers, bodies,
        # cookies, client IDs, authorization codes, or tokens. Live connector
        # platforms report broad OAuth failures ("does not implement OAuth")
        # with no other server-side evidence, so this probe is the record of
        # what actually reached the bridge.
        if service.state_path is not None:
            # File I/O (and the occasional trim) runs off the event loop so a slow
            # disk or antivirus scan cannot stall health probes and live MCP
            # traffic; a single writer keeps append and trim consistent.
            await anyio.to_thread.run_sync(
                _record_request_probe,
                service.state_path,
                method,
                str(request_path),
                limiter=_REQUEST_PROBE_LIMITER,
            )
        try:
            if request_path in {
                "/.well-known/oauth-protected-resource",
                f"/.well-known/oauth-protected-resource{service.path}",
                f"{service.path}/.well-known/oauth-protected-resource",
            } and method == "GET":
                response: Response = JSONResponse(
                    service.protected_resource_metadata(),
                    headers={"Cache-Control": "no-store"},
                )
            elif request_path in {
                "/.well-known/oauth-authorization-server",
                # RFC 8414 path-inserted discovery: several connector platforms
                # probe the authorization-server metadata at the MCP path suffix,
                # and one probe failure reads as "does not implement OAuth".
                f"/.well-known/oauth-authorization-server{service.path}",
                f"{service.path}/.well-known/oauth-authorization-server",
                # OpenID-style alias: HyperAgent's connector discovers the
                # authorization server through this path, and its removal left
                # the hyperagent-web profile with no working discovery route.
                "/.well-known/openid-configuration",
                f"/.well-known/openid-configuration{service.path}",
                f"{service.path}/.well-known/openid-configuration",
            } and method == "GET":
                response = JSONResponse(
                    service.authorization_server_metadata(),
                    headers={"Cache-Control": "no-store"},
                )
            elif request_path in {"/register", "/oauth/register"} and method == "POST":
                if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
                    raise OAuthBridgeError("client registration must use application/json")
                raw = await _body(request)
                payload = _json_object(raw)
                response = JSONResponse(
                    service.register(payload),
                    status_code=201,
                    headers={"Cache-Control": "no-store"},
                )
            elif request_path in {"/authorize", "/oauth/authorize"} and method == "GET":
                values: dict[str, list[str]] = {}
                for key, value in request.query_params.multi_items():
                    values.setdefault(key, []).append(value)
                request_id, client = service.begin_authorization(values)
                response = HTMLResponse(
                    _approval_page(
                        service,
                        request_id,
                        client,
                        language=_approval_language(values),
                    ),
                    headers={
                        "Cache-Control": "no-store",
                        "Content-Security-Policy": (
                            "default-src 'none'; style-src 'unsafe-inline'; "
                            f"form-action {_form_action(service.public_url)}; "
                            "base-uri 'none'; frame-ancestors 'none'"
                        ),
                        "X-Frame-Options": "DENY",
                        # Chromium may serialize Origin as ``null`` for a basic
                        # form POST under ``no-referrer``. The rebinding guard
                        # then rejects KaroX's own approval form before it can
                        # check the password. ``same-origin`` preserves the
                        # real origin for this POST and still sends no referrer
                        # to ChatGPT/Claude on the cross-origin OAuth redirect.
                        "Referrer-Policy": "same-origin",
                    },
                )
            elif request_path in {"/authorize", "/oauth/authorize"} and method == "POST":
                values = await _form(request)
                location = service.approve(
                    _single(values, "request_id"),
                    _single(values, "password"),
                )
                response = HTMLResponse(
                    _approval_complete_page(location),
                    headers={
                        "Cache-Control": "no-store",
                        "Content-Security-Policy": (
                            "default-src 'none'; style-src 'unsafe-inline'; "
                            "base-uri 'none'; frame-ancestors 'none'"
                        ),
                        "Referrer-Policy": "no-referrer",
                        "Refresh": f"0; url={location}",
                        "X-Frame-Options": "DENY",
                    },
                )
                set_trusted_approval_cookie(response, approval_secret)
            elif request_path in {"/token", "/oauth/token"} and method == "POST":
                response = JSONResponse(
                    service.token(await _form(request)),
                    headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
                )
            else:
                await mcp_app(scope, receive, send)
                return
        except OAuthBridgeError as exc:
            response = _oauth_error(str(exc))
        await response(scope, receive, send)

    app.oauth_service = service
    return app
