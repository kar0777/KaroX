"""Bearer-authenticated OpenAPI surface for hosted KaroX clients."""

from __future__ import annotations

import hmac
import json
import os
import re
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .hosted_bridge import (
    DEFAULT_HOSTED_DEADLINE_SECONDS,
    HostedToolRuntime,
)
# Both wires are published through the same tunnel to the same agent, so they
# share one host and Origin policy and one error vocabulary rather than each
# inventing its own.
from .proxy_server import (
    BRIDGE_ERROR_MESSAGES,
    HOST_REJECTION_HINT,
    bridge_error_code,
    normalize_host,
    origin_is_allowed,
    resolve_allowed_hosts,
)


MAX_REQUEST_BYTES = 2_000_000
# The status each fixed error code answers with. Reflecting the exception text
# instead used to hand a third-party agent the absolute repository path, and with
# it the user's home directory and OS account name.
_ERROR_STATUS: dict[str, int] = {
    "tool_not_exposed": 404,
    "not_found": 404,
    "executable_not_found": 500,
    "denied": 403,
    "invalid_request": 400,
    "idempotency_key_required": 400,
    "idempotency_key_invalid": 400,
    # The caller disconnected mid-call; the answer is best-effort because the
    # recipient is usually already gone (nginx-style 499).
    "request_interrupted": 499,
    "internal": 500,
}


def _token_resolver(value: str | Callable[[], str]) -> Callable[[], str]:
    if not isinstance(value, str) and not callable(value):
        raise ValueError("bridge bearer token must be a string or resolver")

    def resolve() -> str:
        token = value() if callable(value) else value
        if (
            not isinstance(token, str)
            or not token
            or len(token) > 65_536
            or any(char in token for char in ("\x00", "\r", "\n"))
        ):
            raise ValueError("bridge bearer token is invalid")
        return token

    resolve()
    return resolve


def _operation_id(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


def bridge_error_response(code: str, detail: str | None = None) -> JSONResponse:
    """Build the only tool-error shape this wire is allowed to send.

    ``detail`` replaces the code's generic wording where the reason is a fixed
    literal that tells the client more than "the call was rejected as invalid".
    It is never an exception string -- reflecting those is what handed a remote
    agent the absolute repository path.
    """
    return JSONResponse(
        {
            "ok": False,
            "error_code": code,
            "error": detail or BRIDGE_ERROR_MESSAGES[code],
        },
        status_code=_ERROR_STATUS[code],
    )


def build_openapi_bridge_app(
    runtime: HostedToolRuntime,
    bearer_token: str | Callable[[], str],
    *,
    deadline_seconds: float = DEFAULT_HOSTED_DEADLINE_SECONDS,
    title: str = "KaroX Hosted Bridge",
    allowed_hosts: Optional[Sequence[str]] = None,
    diagnostics: Optional[Mapping[str, Any]] = None,
) -> FastAPI:
    """Expose selected hosted tools as a small importable OpenAPI service."""
    if not 0.1 <= float(deadline_seconds) <= 3600.0:
        raise ValueError("bridge deadline must be between 0.1 and 3600 seconds")
    diagnostics_payload: Optional[dict[str, Any]] = None
    diagnostics_source: Any = diagnostics
    if diagnostics_source is None:
        raw_diagnostics = os.environ.get("KAROX_BRIDGE_DIAGNOSTICS_JSON", "").strip()
        if raw_diagnostics:
            try:
                diagnostics_source = json.loads(raw_diagnostics)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "KAROX_BRIDGE_DIAGNOSTICS_JSON must contain valid JSON"
                ) from exc
    if diagnostics_source is not None:
        try:
            diagnostics_payload = json.loads(
                json.dumps(dict(diagnostics_source), ensure_ascii=False, sort_keys=True)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("bridge diagnostics must be JSON serializable") from exc
        if not isinstance(diagnostics_payload, dict):
            raise ValueError("bridge diagnostics must be a JSON object")
    resolve_token = _token_resolver(bearer_token)
    allowed = resolve_allowed_hosts(allowed_hosts)
    descriptors = runtime.descriptors()
    by_name = {item.name: item for item in descriptors}
    if len(by_name) != len(descriptors):
        raise ValueError("hosted bridge contains duplicate tool names")

    app = FastAPI(
        title=title,
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json",
    )

    def authorized(request: Request) -> bool:
        candidates: list[str] = []
        authorization = request.headers.get("authorization")
        if authorization and authorization.lower().startswith("bearer "):
            candidates.append(authorization[7:].strip())
        api_key = request.headers.get("x-api-key")
        if api_key:
            candidates.append(api_key.strip())
        if len(candidates) != 1:
            return False
        try:
            expected = resolve_token()
        except Exception:
            return False
        # compare_digest rejects a non-ASCII str with TypeError, which turned a
        # bad credential into a 500 instead of a 401.
        return hmac.compare_digest(
            candidates[0].encode("utf-8"), expected.encode("utf-8")
        )

    def unauthorized() -> JSONResponse:
        return JSONResponse(
            {"ok": False, "error": "unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )

    def rebinding_rejection(request: Request) -> Optional[JSONResponse]:
        hosts = request.headers.getlist("host")
        if len(hosts) != 1 or normalize_host(hosts[0]) not in allowed:
            return JSONResponse(
                {"ok": False, "error": HOST_REJECTION_HINT}, status_code=421
            )
        origins = request.headers.getlist("origin")
        if len(origins) > 1 or not origin_is_allowed(
            origins[0] if origins else None, allowed
        ):
            return JSONResponse(
                {"ok": False, "error": "invalid Origin header"}, status_code=421
            )
        return None

    @app.middleware("http")
    async def bridge_guard(request: Request, call_next: Any) -> Any:
        rejection = rebinding_rejection(request)
        if rejection is not None:
            return rejection
        # Only the static discovery document at / is open. The schema names every
        # exposed tool, its description and its path, which told an anonymous
        # scanner exactly which mutating tools this repository is publishing; the
        # client that legitimately imports it already holds the credential.
        if request.url.path != "/":
            if not authorized(request):
                return unauthorized()
            raw_length = request.headers.get("content-length")
            if raw_length:
                try:
                    if int(raw_length) > MAX_REQUEST_BYTES:
                        return JSONResponse(
                            {"ok": False, "error": "request body is too large"},
                            status_code=413,
                        )
                except ValueError:
                    return JSONResponse(
                        {"ok": False, "error": "invalid Content-Length"},
                        status_code=400,
                    )
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, Any]:
        return {
            "name": title,
            "protocol": "openapi",
            "openapi": "/openapi.json",
        }

    @app.get("/health", operation_id="karox_health")
    async def health() -> dict[str, Any]:
        current = runtime.descriptors()
        return {"ok": True, "tool_count": len(current)}

    @app.get("/tools", operation_id="karox_list_tools")
    async def list_tools() -> dict[str, Any]:
        return {"tools": [item.to_dict() for item in runtime.descriptors()]}

    @app.get("/diagnostics", operation_id="karox_bridge_diagnostics")
    async def bridge_diagnostics() -> dict[str, Any]:
        if diagnostics_payload is None:
            return {
                "schema_version": 1,
                "available_tools": [item.name for item in runtime.descriptors()],
                "disabled_tools": [],
                "effective_deadline_seconds": deadline_seconds,
            }
        return dict(diagnostics_payload)

    def session_payload() -> dict[str, Any]:
        reader = getattr(runtime, "session_info", None)
        info = dict(reader()) if callable(reader) else {}
        return {"ok": True, **info}

    @app.get("/session", operation_id="karox_session")
    async def session() -> dict[str, Any]:
        return session_payload()

    @app.get("/context/brief", operation_id="karox_context_brief")
    async def context_brief() -> dict[str, Any]:
        return {
            **session_payload(),
            "tools": [item.name for item in runtime.descriptors()],
            "bridge_diagnostics": await bridge_diagnostics(),
            "recommended_next_action": (
                "call karox_bridge_diagnostics before invoking optional tools"
            ),
        }

    @app.post("/tools/{tool_name:path}", include_in_schema=False)
    async def call_tool(tool_name: str, request: Request) -> Any:
        descriptor = by_name.get(tool_name)
        if descriptor is None:
            return bridge_error_response("tool_not_exposed")
        # These three carry `error_code` like every other error from this handler.
        # Emitting two shapes from one endpoint meant a client that read
        # `error_code` found it absent exactly for the errors it could have fixed.
        try:
            arguments = await request.json()
        except Exception:
            return bridge_error_response("invalid_request", "request body must be JSON")
        if not isinstance(arguments, dict):
            return bridge_error_response(
                "invalid_request", "tool arguments must be an object"
            )
        idempotency_key = request.headers.get("x-karox-idempotency-key")
        if not descriptor.read_only and not idempotency_key:
            return bridge_error_response(
                "idempotency_key_required",
                "mutating calls require X-KaroX-Idempotency-Key",
            )
        try:
            return runtime.execute(
                tool_name,
                arguments,
                idempotency_key=idempotency_key,
                deadline_seconds=deadline_seconds,
            )
        except Exception as exc:
            return bridge_error_response(bridge_error_code(exc))

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema: dict[str, Any] = {
            "openapi": "3.1.0",
            "info": {"title": title, "version": "1.0.0"},
            "paths": {
                "/health": {
                    "get": {
                        "operationId": "karox_health",
                        "summary": "Verify the authenticated KaroX bridge",
                        "security": [{"bearerAuth": []}, {"apiKeyAuth": []}],
                        "responses": {"200": {"description": "Bridge status"}},
                    }
                },
                "/tools": {
                    "get": {
                        "operationId": "karox_list_tools",
                        "summary": "List tools exposed to this hosted client",
                        "security": [{"bearerAuth": []}, {"apiKeyAuth": []}],
                        "responses": {"200": {"description": "Tool descriptors"}},
                    }
                },
                "/diagnostics": {
                    "get": {
                        "operationId": "karox_bridge_diagnostics",
                        "summary": (
                            "Return available and disabled tools, verification "
                            "allowlist, deadline, tunnel, and session lifetime"
                        ),
                        "security": [{"bearerAuth": []}, {"apiKeyAuth": []}],
                        "responses": {"200": {"description": "Bridge diagnostics"}},
                    }
                },
                "/session": {
                    "get": {
                        "operationId": "karox_session",
                        "summary": "Return the bound KaroX session and repository",
                        "security": [{"bearerAuth": []}, {"apiKeyAuth": []}],
                        "responses": {"200": {"description": "Session binding"}},
                    }
                },
                "/context/brief": {
                    "get": {
                        "operationId": "karox_context_brief",
                        "summary": "Return session context and exposed tool names",
                        "security": [{"bearerAuth": []}, {"apiKeyAuth": []}],
                        "responses": {"200": {"description": "Hosted context brief"}},
                    }
                },
            },
            "components": {
                "securitySchemes": {
                    "bearerAuth": {"type": "http", "scheme": "bearer"},
                    "apiKeyAuth": {
                        "type": "apiKey",
                        "in": "header",
                        "name": "X-API-Key",
                    },
                }
            },
        }
        paths = schema["paths"]
        for descriptor in descriptors:
            parameters: list[dict[str, Any]] = []
            if not descriptor.read_only:
                parameters.append(
                    {
                        "name": "X-KaroX-Idempotency-Key",
                        "in": "header",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1, "maxLength": 200},
                        "description": "Unique stable key for this mutation and its retries.",
                    }
                )
            paths[f"/tools/{quote(descriptor.name, safe='._-')}"] = {
                "post": {
                    "operationId": _operation_id(descriptor.name),
                    "summary": descriptor.description,
                    "security": [{"bearerAuth": []}, {"apiKeyAuth": []}],
                    "parameters": parameters,
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {"schema": descriptor.input_schema}
                        },
                    },
                    "responses": {
                        "200": {"description": "KaroX Core result"},
                        "400": {"description": "Invalid request"},
                        "401": {"description": "Invalid bridge credential"},
                        "403": {"description": "Policy denied"},
                    },
                }
            }
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi
    return app
