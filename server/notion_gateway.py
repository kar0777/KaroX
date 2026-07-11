"""Notion Custom Agent gateway for KaroX.

The normal KaroX FastAPI application remains available at its existing paths.
A protected Streamable HTTP MCP server is added at /mcp and translates a small,
well-described tool set into in-process calls to the existing KaroX API. The
same per-session key protects both interfaces.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from repo_tools import app as karox_app


API_KEY = os.environ["REPO_TOOLS_API_KEY"]


def _extract_token(request: Any) -> str:
    auth = request.headers.get("authorization", "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    if auth:
        return auth
    return request.headers.get("x-api-key", "").strip()


class McpAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Any, call_next: Any):
        supplied = _extract_token(request)
        if supplied != API_KEY:
            return JSONResponse(
                {"error": "unauthorized", "hint": "Use the current KaroX session key as a Bearer token."},
                status_code=401,
            )
        return await call_next(request)


mcp = FastMCP("KaroX Notion Provider")


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
    async with httpx.AsyncClient(transport=transport, base_url="http://karox.local") as client:
        response = await client.request(method.upper(), path, params=params, json=body, headers=headers)
    try:
        payload: Any = response.json()
    except Exception:
        payload = {"text": response.text}
    if response.is_success:
        return {"ok": True, "status": response.status_code, "data": payload}
    return {"ok": False, "status": response.status_code, "error": payload}


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
    command: str,
    timeout_seconds: int = 7200,
    capture_to_file: bool = False,
    output_file: str = "",
    tail: int = 60000,
) -> dict[str, Any]:
    """Run a development command under KaroX command and mode guardrails.

    Use capture_to_file for builds or tests with large output. Dangerous system,
    credential, publish, and push commands remain blocked by KaroX.
    """
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
mcp_app.add_middleware(McpAuthMiddleware)

# Keep FastMCP as the top-level ASGI app so its lifespan/session manager runs.
# Its explicit /mcp route wins; every other path falls through to KaroX.
mcp_app.mount("/", karox_app)
app = mcp_app
