"""Notion Custom Agent gateway for KaroX.

The normal KaroX FastAPI application remains available at its existing paths.
A protected Streamable HTTP MCP server is added at /mcp and translates a small,
well-described tool set into in-process calls to the existing KaroX API. The
same per-session key protects both interfaces.
"""
from __future__ import annotations

import hmac
import json
import os
from collections.abc import Mapping
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mcp_host_security import is_allowed_mcp_host
from repo_tools import app as karox_app


API_KEY = os.environ["REPO_TOOLS_API_KEY"]
_MCP_PATH = "/mcp"
_MCP_RESPONSE_HEADERS = (
    (b"cache-control", b"no-store"),
    (b"x-accel-buffering", b"no"),
)


def _scope_headers(scope: Scope) -> dict[str, str]:
    """Decode request headers without consuming the ASGI receive channel."""
    headers: dict[str, str] = {}
    for raw_name, raw_value in scope.get("headers", []):
        name = raw_name.decode("latin-1").lower()
        value = raw_value.decode("latin-1").strip()
        if name in headers:
            headers[name] = f"{headers[name]}, {value}"
        else:
            headers[name] = value
    return headers


def _extract_token(headers: Mapping[str, str]) -> str:
    auth = headers.get("authorization", "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    if auth:
        return auth
    return headers.get("x-api-key", "").strip()


async def _json_response(send: Send, status: int, payload: dict[str, Any], *, allow: str = "") -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
        *_MCP_RESPONSE_HEADERS,
    ]
    if allow:
        headers.append((b"allow", allow.encode("ascii")))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class McpAuthMiddleware:
    """Raw ASGI auth/host middleware that preserves Streamable HTTP channels.

    Starlette's BaseHTTPMiddleware wraps the ASGI receive/send streams and is
    incompatible with FastMCP's Streamable HTTP transport. This middleware never
    reads the request body and forwards the original channels unchanged.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path", ""))
        is_mcp = path in {_MCP_PATH, f"{_MCP_PATH}/"}
        if not is_mcp:
            await self.app(scope, receive, send)
            return

        # Accept both /mcp and /mcp/ without a 307 redirect. Some MCP clients do
        # not resend Authorization headers reliably across redirects.
        if path.endswith("/"):
            scope = dict(scope)
            scope["path"] = _MCP_PATH
            scope["raw_path"] = _MCP_PATH.encode("ascii")

        headers = _scope_headers(scope)
        host = headers.get("host", "")
        if not host:
            server = scope.get("server")
            if server:
                host = str(server[0])
        if not is_allowed_mcp_host(host):
            await _json_response(
                send,
                421,
                {
                    "error": "invalid_host",
                    "hint": "Use the current KaroX Cloudflare Tunnel or Tailscale Funnel URL.",
                },
            )
            return

        supplied = _extract_token(headers)
        valid = bool(supplied) and hmac.compare_digest(
            supplied.encode("utf-8", errors="ignore"),
            API_KEY.encode("utf-8", errors="ignore"),
        )
        if not valid:
            await _json_response(
                send,
                401,
                {
                    "error": "unauthorized",
                    "hint": "Use the current KaroX session key as a Bearer token.",
                },
            )
            return

        # KaroX runs the recommended stateless JSON Streamable HTTP mode. A GET
        # probe therefore has no event stream to attach to; return the spec-friendly
        # 405 so clients can immediately fall back to POST instead of reporting an
        # opaque SSE/fetch failure.
        if str(scope.get("method", "GET")).upper() in {"GET", "HEAD"}:
            await _json_response(
                send,
                405,
                {
                    "error": "method_not_allowed",
                    "hint": "This MCP endpoint uses Streamable HTTP POST requests.",
                },
                allow="POST, DELETE",
            )
            return

        async def send_with_transport_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                outgoing = list(message.get("headers", []))
                existing = {name.lower() for name, _ in outgoing}
                for name, value in _MCP_RESPONSE_HEADERS:
                    if name not in existing:
                        outgoing.append((name, value))
                message = dict(message)
                message["headers"] = outgoing
            await send(message)

        await self.app(scope, receive, send_with_transport_headers)


# KaroX tools do not keep per-client server state. Stateless HTTP with JSON
# responses avoids fragile SSE sessions and is the production configuration
# recommended by the official MCP Python SDK.
mcp = FastMCP(
    "KaroX Notion Provider",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


async def _call(
    method: str,
    path: str,
    *,
    params: Optional[dict[str, Any]] = None,
    body: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if not path.startswith("/") or path.startswith("/mcp"):
        return {"ok": False, "status": 400, "error": "Only KaroX API paths are allowed."}
    transport = httpx.ASGITransport(app=karox_app)
    headers = {"X-API-Key": API_KEY}
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://karox.local") as client:
            response = await client.request(method.upper(), path, params=params, json=body, headers=headers)
    except Exception as exc:
        return {
            "ok": False,
            "status": 503,
            "error": {
                "code": "karox_internal_api_unavailable",
                "type": type(exc).__name__,
                "message": "The local KaroX API could not complete the request. Check the session logs and retry.",
            },
        }
    try:
        payload: Any = response.json()
    except Exception:
        payload = {"text": response.text}
    if response.is_success:
        return {"ok": True, "status": response.status_code, "data": payload}
    return {"ok": False, "status": response.status_code, "error": payload}


@mcp.tool()
async def karox_ping() -> dict[str, Any]:
    """Check that the MCP transport and the local KaroX API are responding."""
    health = await _call("GET", "/health")
    return {
        "ok": bool(health.get("ok")),
        "transport": "streamable-http-stateless-json",
        "health": health,
    }


@mcp.tool()
async def karox_preflight() -> dict[str, Any]:
    """Verify the exact KaroX session before doing any repository work.

    Always call this first. It returns session identity, health, Git status, and
    Mission Control context. Stop if repoRoot, branch, mode, or permissions do
    not match the connection prompt.
    """
    result: dict[str, Any] = {}
    for name, path in (
        ("session", "/session"),
        ("health", "/health"),
        ("gitStatus", "/git/status"),
        ("context", "/context/brief"),
    ):
        result[name] = await _call("GET", path)
    result["ok"] = all(value.get("ok") for value in result.values() if isinstance(value, dict))
    return result


@mcp.tool()
async def karox_start_task(task: str) -> dict[str, Any]:
    """Register the real user task after preflight. Do not use the session label as a task."""
    return await _call("POST", "/task/start", body={"task": task})


@mcp.tool()
async def karox_context() -> dict[str, Any]:
    """Refresh KaroX Mission Control context and recommended next action."""
    return await _call("GET", "/context/brief")


@mcp.tool()
async def karox_tree(max_files: int = 20000) -> dict[str, Any]:
    """List repository files while KaroX filters secrets and ignored build directories."""
    return await _call("GET", "/tree", params={"max_files": max_files})


@mcp.tool()
async def karox_read_file(path: str) -> dict[str, Any]:
    """Read one UTF-8 text file inside the selected repository."""
    return await _call("GET", "/file", params={"path": path})


@mcp.tool()
async def karox_write_file(path: str, content: str) -> dict[str, Any]:
    """Create or replace one repository file. Observe/read-only sessions reject this."""
    return await _call("POST", "/file", body={"path": path, "content": content})


@mcp.tool()
async def karox_batch_write(files_json: str) -> dict[str, Any]:
    """Write several files atomically after validation.

    files_json must be a JSON array like [{"path":"src/a.py","content":"..."}].
    """
    try:
        files = json.loads(files_json)
    except json.JSONDecodeError as exc:
        return {"ok": False, "status": 400, "error": f"Invalid JSON: {exc}"}
    if not isinstance(files, list):
        return {"ok": False, "status": 400, "error": "files_json must decode to an array"}
    return await _call("POST", "/files/batch-write", body={"files": files})


@mcp.tool()
async def karox_delete_file(path: str) -> dict[str, Any]:
    """Delete one file inside the repository. Directory deletion is not exposed."""
    return await _call("DELETE", "/file", params={"path": path})


@mcp.tool()
async def karox_run(
    command: str = "",
    timeout_seconds: int = 7200,
    capture_to_file: bool = False,
    output_file: str = "",
    tail: int = 60000,
    argv_json: str = "",
    shell: str = "",
    cwd: str = "",
    env_json: str = "",
    stdin: str = "",
) -> dict[str, Any]:
    """Run a development command under KaroX command and mode guardrails.

    Classic mode: pass a shell string via command; use capture_to_file for
    builds or tests with large output. Exact mode: pass argv_json (JSON array,
    verbatim argv, no shell quoting) with optional shell (cmd|powershell|bash|sh),
    cwd, env_json, and stdin. Dangerous system, credential, publish, and push
    commands remain blocked by KaroX.
    """
    advanced = bool(argv_json.strip() or shell or cwd or env_json.strip() or stdin)
    if advanced:
        exec_body: dict[str, Any] = {"timeoutSeconds": max(1, min(int(timeout_seconds), 21600))}
        if argv_json.strip():
            try:
                argv = json.loads(argv_json)
            except json.JSONDecodeError as exc:
                return {"ok": False, "status": 400, "error": f"Invalid argv_json: {exc}"}
            if not isinstance(argv, list):
                return {"ok": False, "status": 400, "error": "argv_json must decode to an array"}
            exec_body["argv"] = argv
        if command:
            exec_body["cmd"] = command
        if shell:
            exec_body["shell"] = shell
        if cwd:
            exec_body["cwd"] = cwd
        if stdin:
            exec_body["stdin"] = stdin
        if env_json.strip():
            try:
                env = json.loads(env_json)
            except json.JSONDecodeError as exc:
                return {"ok": False, "status": 400, "error": f"Invalid env_json: {exc}"}
            if not isinstance(env, dict):
                return {"ok": False, "status": 400, "error": "env_json must decode to an object"}
            exec_body["env"] = env
        return await _call("POST", "/exec", body=exec_body)
    if not command.strip():
        return {"ok": False, "status": 400, "error": "Provide command or argv_json"}
    timeout_seconds = max(1, min(int(timeout_seconds), 7200))
    tail = max(1000, min(int(tail), 300000))
    body: dict[str, Any] = {
        "cmd": command,
        "timeoutSeconds": timeout_seconds,
        "capture": "file" if capture_to_file else "inline",
        "tail": tail,
    }
    if output_file:
        body["outputFile"] = output_file
    return await _call("POST", "/run", body=body)


@mcp.tool()
async def karox_git_status() -> dict[str, Any]:
    """Return the current branch and working-tree status."""
    return await _call("GET", "/git/status")


@mcp.tool()
async def karox_git_diff(path: str = "", max_chars: int = 300000) -> dict[str, Any]:
    """Review the current diff, optionally restricted to one file."""
    if path:
        return await _call("GET", "/git/diff/file", params={"path": path})
    return await _call("GET", "/git/diff/full", params={"max_chars": max_chars})


@mcp.tool()
async def karox_cleanup_generated() -> dict[str, Any]:
    """Remove or restore KaroX-generated temporary output before committing."""
    return await _call("POST", "/git/cleanup-generated", body={})


@mcp.tool()
async def karox_commit(
    message: str,
    include_json: str,
    run_pre_commit_checks: bool = False,
) -> dict[str, Any]:
    """Create a reviewed commit through the KaroX safe commit endpoint.

    include_json must be a JSON array of explicit repository paths. Never use
    this before reviewing the diff and cleaning generated files. KaroX never
    pushes.
    """
    try:
        include = json.loads(include_json)
    except json.JSONDecodeError as exc:
        return {"ok": False, "status": 400, "error": f"Invalid JSON: {exc}"}
    if not isinstance(include, list) or not all(isinstance(item, str) for item in include):
        return {"ok": False, "status": 400, "error": "include_json must be a JSON array of paths"}
    return await _call(
        "POST",
        "/git/commit",
        body={
            "message": message,
            "include": include,
            "cleanupGenerated": True,
            "runPreCommitChecks": run_pre_commit_checks,
        },
    )


@mcp.tool()
async def karox_request(method: str, path: str, params_json: str = "{}", body_json: str = "") -> dict[str, Any]:
    """Call a KaroX API endpoint not covered by a dedicated tool.

    This tool accepts only relative KaroX paths and cannot call external hosts.
    Use GET, POST, PATCH, PUT, or DELETE. Prefer dedicated tools when available.
    """
    method = method.upper().strip()
    if method not in {"GET", "POST", "PATCH", "PUT", "DELETE"}:
        return {"ok": False, "status": 400, "error": "Unsupported HTTP method"}
    try:
        params = json.loads(params_json or "{}")
        body = json.loads(body_json) if body_json else None
    except json.JSONDecodeError as exc:
        return {"ok": False, "status": 400, "error": f"Invalid JSON: {exc}"}
    if not isinstance(params, dict) or (body is not None and not isinstance(body, dict)):
        return {"ok": False, "status": 400, "error": "params_json and body_json must decode to objects"}
    return await _call(method, path, params=params, body=body)


@mcp.tool()
async def karox_finish_task(status: str = "finished") -> dict[str, Any]:
    """Mark the task complete and return the final KaroX report."""
    finish = await _call("POST", "/task/finish", body={"status": status})
    report = await _call("GET", "/task/report")
    return {"ok": bool(finish.get("ok") and report.get("ok")), "finish": finish, "report": report}


mcp_app = mcp.streamable_http_app()

# Keep FastMCP as the inner top-level Starlette app so its lifespan/session
# manager runs. Its explicit /mcp route wins; every other path falls through to
# KaroX. The raw ASGI wrapper preserves Streamable HTTP receive/send channels.
mcp_app.mount("/", karox_app)
app = McpAuthMiddleware(mcp_app)
