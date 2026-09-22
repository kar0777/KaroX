from __future__ import annotations

import hashlib
import json
from typing import Any, Optional
from urllib.parse import parse_qs, urlencode, urlsplit

from _support import SRC  # noqa: F401
from karox.hosted_bridge import HostedApprovalRequired
from karox.proxy import ProxyToolDescriptor
from karox.proxy_server import MODERN_MCP_PROTOCOL_VERSION, build_proxy_asgi_app
from test_hosted_bridge import _asgi_request, _memory_stream_pair, _wire_requests


class _ModernRuntime:
    def descriptors(self) -> list[ProxyToolDescriptor]:
        return [
            ProxyToolDescriptor(
                name="karox.repo.read_file",
                description="Synthetic read",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
                read_only=True,
            )
        ]

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: Optional[str] = None,
        deadline_seconds: float = 30.0,
    ) -> dict[str, Any]:
        del idempotency_key, deadline_seconds
        return {"ok": True, "tool": tool_name, "path": arguments.get("path")}


class _ApprovalRuntime:
    def __init__(self) -> None:
        self.approved_digests: list[str] = []

    def descriptors(self) -> list[ProxyToolDescriptor]:
        return [
            ProxyToolDescriptor(
                name="karox.git.push",
                description="Synthetic approval-gated push",
                input_schema={
                    "type": "object",
                    "properties": {
                        "remote": {"type": "string"},
                        "branch": {"type": "string"},
                    },
                    "required": ["remote", "branch"],
                    "additionalProperties": False,
                },
                read_only=False,
            )
        ]

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: Optional[str] = None,
        deadline_seconds: float = 30.0,
    ) -> dict[str, Any]:
        del arguments, idempotency_key, deadline_seconds
        raise HostedApprovalRequired(
            tool_name=tool_name,
            action_digest="a" * 64,
            action_kind="git.push",
            risk="high",
            consequence="external",
            preview={"remote": "origin", "branch": "main"},
            message="Allow one synthetic push?",
        )

    def execute_approved(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        expected_action_digest: str,
        idempotency_key: Optional[str] = None,
        deadline_seconds: float = 30.0,
    ) -> dict[str, Any]:
        del idempotency_key, deadline_seconds
        self.approved_digests.append(expected_action_digest)
        return {
            "ok": True,
            "tool": tool_name,
            "arguments": dict(arguments),
            "approved": True,
        }


class _WorkspaceApprovalRuntime(_ApprovalRuntime):
    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: Optional[str] = None,
        deadline_seconds: float = 30.0,
    ) -> dict[str, Any]:
        del arguments, idempotency_key, deadline_seconds
        raise HostedApprovalRequired(
            tool_name=tool_name,
            action_digest="b" * 64,
            action_kind="repo.delete",
            risk="high",
            consequence="workspace",
            preview={"path": "src/old.py"},
            message="Allow deleting src/old.py?",
        )


def _modern_request(method: str, params: dict[str, Any], *, name: str | None = None) -> dict[str, Any]:
    headers = [
        ("host", "127.0.0.1:8765"),
        ("authorization", "Bearer modern-test-token"),
        ("content-type", "application/json"),
        ("accept", "application/json, text/event-stream"),
        ("mcp-protocol-version", MODERN_MCP_PROTOCOL_VERSION),
        ("mcp-method", method),
    ]
    if name is not None:
        headers.append(("mcp-name", name))
    meta = dict(params.get("_meta") or {})
    meta.update(
        {
            "io.modelcontextprotocol/protocolVersion": MODERN_MCP_PROTOCOL_VERSION,
            "io.modelcontextprotocol/clientCapabilities": {"elicitation": {"form": {}}},
            "io.modelcontextprotocol/clientInfo": {"name": "test-client", "version": "1"},
        }
    )
    params = dict(params)
    params["_meta"] = meta
    return {
        "method": "POST",
        "path": "/mcp",
        "headers": headers,
        "body": json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        ).encode("utf-8"),
    }


def _run_with_lifespan(app: Any, scenario: Any) -> Any:
    import anyio

    async def run() -> Any:
        to_app_send, to_app_receive = _memory_stream_pair()
        from_app_send, from_app_receive = _memory_stream_pair()
        async with (
            to_app_send,
            to_app_receive,
            from_app_send,
            from_app_receive,
            anyio.create_task_group() as group,
        ):
            group.start_soon(
                app, {"type": "lifespan"}, to_app_receive.receive, from_app_send.send
            )
            await to_app_send.send({"type": "lifespan.startup"})
            await from_app_receive.receive()
            try:
                return await scenario()
            finally:
                await to_app_send.send({"type": "lifespan.shutdown"})
                await from_app_receive.receive()

    return anyio.run(run)


def test_modern_discover_lists_2026_and_tools_capability() -> None:
    app = build_proxy_asgi_app(_ModernRuntime(), "modern-test-token")
    response = _wire_requests(app, [_modern_request("server/discover", {})])[0]
    assert response.status == 200
    result = response.json()["result"]
    assert result["supportedVersions"][0] == MODERN_MCP_PROTOCOL_VERSION
    assert result["capabilities"]["tools"] == {}
    assert result["resultType"] == "complete"


def test_modern_diagnostics_refresh_stale_owner_catalog_from_live_child() -> None:
    diagnostics = {
        "schema_version": 1,
        "available_tools": [],
        "client_capabilities": {
            "available_tool_count": 0,
            "available_tools_digest": hashlib.sha256(b"").hexdigest(),
        },
        "tool_catalog": {
            "advertised_tool_count": 0,
            "groups": {"core": []},
        },
        "mode_restrictions": {
            "read_only": False,
            "no_git_push": True,
            "no_publish": True,
            "no_auth_commands": True,
            "no_deploy_release": True,
        },
    }
    app = build_proxy_asgi_app(
        _ApprovalRuntime(),
        "modern-test-token",
        diagnostics=diagnostics,
    )

    response = _wire_requests(
        app,
        [
            _modern_request(
                "tools/call",
                {"name": "karox_bridge_diagnostics", "arguments": {}},
                name="karox_bridge_diagnostics",
            )
        ],
    )[0]
    assert response.status == 200
    live = response.json()["result"]["structuredContent"]
    assert live["available_tools"] == ["karox.git.push"]
    assert live["client_capabilities"]["available_tool_count"] == 1
    assert live["client_capabilities"]["available_tools_digest"] == hashlib.sha256(
        b"karox.git.push"
    ).hexdigest()
    assert live["tool_catalog"]["advertised_tool_count"] == 1
    assert "karox.git.push" in live["tool_catalog"]["groups"]["core"]
    assert live["mode_restrictions"]["no_git_push"] is False
    assert (
        live["mode_restrictions"]["git_push_approval"]
        == "one_shot_mcp_user_confirmation"
    )


def test_modern_tools_list_and_call_use_same_runtime() -> None:
    app = build_proxy_asgi_app(_ModernRuntime(), "modern-test-token")
    listed, called = _wire_requests(
        app,
        [
            _modern_request("tools/list", {}),
            _modern_request(
                "tools/call",
                {"name": "karox_repo_read_file", "arguments": {"path": "README.md"}},
                name="karox_repo_read_file",
            ),
        ],
    )
    assert listed.status == 200
    tools = listed.json()["result"]["tools"]
    names = [item["name"] for item in tools]
    assert "karox_repo_read_file" in names
    assert set(names).issubset({"karox_repo_read_file", "karox_bridge_diagnostics"})
    assert called.status == 200
    result = called.json()["result"]
    assert result["resultType"] == "complete"
    assert result["isError"] is False
    assert result["structuredContent"] == {
        "ok": True,
        "tool": "karox.repo.read_file",
        "path": "README.md",
    }


def test_modern_approval_round_requires_real_elicitation_and_exact_retry() -> None:
    runtime = _ApprovalRuntime()
    app = build_proxy_asgi_app(runtime, "modern-test-token")
    arguments = {"remote": "origin", "branch": "main"}

    async def scenario() -> tuple[Any, Any, Any]:
        first = await _asgi_request(
            app,
            _modern_request(
                "tools/call",
                {"name": "karox_git_push", "arguments": arguments},
                name="karox_git_push",
            ),
        )
        required = first.json()["result"]
        retry = _modern_request(
            "tools/call",
            {
                "name": "karox_git_push",
                "arguments": arguments,
                "requestState": required["requestState"],
                "inputResponses": {
                    "karox_approval": {
                        "action": "accept",
                        "content": {"approve": True},
                    }
                },
            },
            name="karox_git_push",
        )
        approved = await _asgi_request(app, retry)
        replay = await _asgi_request(app, retry)
        return first, approved, replay

    first, approved, replay = _run_with_lifespan(app, scenario)
    assert first.status == 200
    required = first.json()["result"]
    assert required["resultType"] == "input_required"
    assert required["inputRequests"]["karox_approval"]["method"] == "elicitation/create"
    assert "requestState" in required
    assert approved.status == 200
    result = approved.json()["result"]
    assert result["resultType"] == "complete"
    assert result["structuredContent"]["approved"] is True
    assert runtime.approved_digests == ["a" * 64]
    assert replay.status == 400
    assert replay.json()["error"]["code"] == -32602


def _without_elicitation(request: dict[str, Any]) -> dict[str, Any]:
    value = dict(request)
    payload = json.loads(bytes(value["body"]).decode("utf-8"))
    meta = payload["params"]["_meta"]
    meta["io.modelcontextprotocol/clientCapabilities"] = {}
    value["body"] = json.dumps(payload).encode("utf-8")
    return value


def test_non_external_approval_is_deferred_without_blocking_the_turn() -> None:
    runtime = _WorkspaceApprovalRuntime()
    app = build_proxy_asgi_app(
        runtime,
        "modern-test-token",
    )
    request = _without_elicitation(
        _modern_request(
            "tools/call",
            {
                "name": "karox_git_push",
                "arguments": {"remote": "origin", "branch": "main"},
            },
            name="karox_git_push",
        )
    )

    async def scenario() -> Any:
        return await _asgi_request(app, request)

    response = _run_with_lifespan(app, scenario)
    assert response.status == 200
    result = response.json()["result"]
    assert result["resultType"] == "complete"
    assert result["isError"] is True
    payload = result["structuredContent"]
    assert payload["error_code"] == "approval_deferred"
    assert payload["action_kind"] == "repo.delete"
    assert payload["continue_independent_work"] is True
    assert payload["defer_until_blocked"] is True
    assert "approval_url" not in payload
    assert runtime.approved_digests == []


def test_modern_approval_never_offers_a_browser_url_to_the_model() -> None:
    """Approvals arrive in the chat; the model is never handed a URL to give out.

    A client that cannot carry an MCP elicitation round used to receive an
    approval page URL it could forward. A URL the model can pass on is not proof
    that the owner answered, and the owner asked for chat-only approvals, so the
    action now fails closed and names the two supported channels: an approval
    relayed in the chat, or a client that can carry the elicitation round. The
    approval page itself is gone from this app, so the old route is a 404.
    """
    runtime = _ApprovalRuntime()
    app = build_proxy_asgi_app(runtime, "modern-test-token")
    arguments = {"remote": "origin", "branch": "main"}

    async def scenario() -> tuple[Any, Any]:
        tool_request = _without_elicitation(
            _modern_request(
                "tools/call",
                {"name": "karox_git_push", "arguments": arguments},
                name="karox_git_push",
            )
        )
        first = await _asgi_request(app, tool_request)
        page = await _asgi_request(
            app,
            {
                "method": "GET",
                "path": "/mcp/approval",
                "query_string": b"state=whatever",
                "headers": [("host", "127.0.0.1:8765")],
            },
        )
        retry = await _asgi_request(app, tool_request)
        return first, (page, retry)

    first, (page, retry) = _run_with_lifespan(app, scenario)
    assert first.status == 200
    result = first.json()["result"]
    payload = result["structuredContent"]
    assert payload["error_code"] == "denied"
    assert "approval_url" not in json.dumps(result)
    assert "chat" in payload["error"]
    assert page.status == 404
    assert retry.json()["result"]["structuredContent"]["error_code"] == "denied"
    assert runtime.approved_digests == []


def test_trusted_approval_cookie_is_signed_and_time_bounded() -> None:
    """The OAuth consent surface still trusts a browser it approved before.

    The cookie is the part of the former approval page that stays in the
    product, so its guarantees are asserted directly: a signed cookie validates
    inside its lifetime, and an expired one never does.
    """
    from karox.browser_approval import (
        TRUST_COOKIE_MAX_AGE_SECONDS,
        issue_trusted_approval_cookie,
        validate_trusted_approval_cookie,
    )

    secret = "human-approval-secret"
    issued = issue_trusted_approval_cookie(secret, now=1_000.0)
    assert validate_trusted_approval_cookie(issued, secret, now=1_001.0) is True
    assert (
        validate_trusted_approval_cookie(
            issued, secret, now=1_000.0 + TRUST_COOKIE_MAX_AGE_SECONDS + 1
        )
        is False
    )
    assert validate_trusted_approval_cookie(issued, "another-secret", now=1_001.0) is False


def test_tampered_trusted_cookie_never_validates() -> None:
    """A rewritten cookie cannot be presented as a previous approval."""
    from karox.browser_approval import (
        issue_trusted_approval_cookie,
        validate_trusted_approval_cookie,
    )

    secret = "human-approval-secret"
    issued = issue_trusted_approval_cookie(secret, now=1_000.0)
    header, payload, signature = issued.split(".")

    rewritten = ("A" if payload[-1] != "A" else "B") + payload[1:]
    for candidate in (
        f"{header}.{rewritten}.{signature}",
        f"{header}.{payload}.{signature[:-2]}AA",
        f"{header}.{payload}",
        "v1.invalid.invalid",
        "",
    ):
        assert validate_trusted_approval_cookie(candidate, secret, now=1_001.0) is False, candidate


def test_modern_approval_decline_and_tampered_state_never_execute() -> None:
    runtime = _ApprovalRuntime()
    app = build_proxy_asgi_app(runtime, "modern-test-token")
    arguments = {"remote": "origin", "branch": "main"}

    async def scenario() -> tuple[Any, Any, Any, Any]:
        first = await _asgi_request(
            app,
            _modern_request(
                "tools/call",
                {"name": "karox_git_push", "arguments": arguments},
                name="karox_git_push",
            ),
        )
        state = first.json()["result"]["requestState"]
        tampered = state[:-1] + ("A" if state[-1] != "A" else "B")
        rejected = await _asgi_request(
            app,
            _modern_request(
                "tools/call",
                {
                    "name": "karox_git_push",
                    "arguments": arguments,
                    "requestState": tampered,
                    "inputResponses": {
                        "karox_approval": {
                            "action": "accept",
                            "content": {"approve": True},
                        }
                    },
                },
                name="karox_git_push",
            ),
        )
        declined_request = _modern_request(
            "tools/call",
            {
                "name": "karox_git_push",
                "arguments": arguments,
                "requestState": state,
                "inputResponses": {
                    "karox_approval": {"action": "decline"}
                },
            },
            name="karox_git_push",
        )
        declined = await _asgi_request(app, declined_request)
        replay_after_decline = await _asgi_request(
            app,
            _modern_request(
                "tools/call",
                {
                    "name": "karox_git_push",
                    "arguments": arguments,
                    "requestState": state,
                    "inputResponses": {
                        "karox_approval": {
                            "action": "accept",
                            "content": {"approve": True},
                        }
                    },
                },
                name="karox_git_push",
            ),
        )
        return first, rejected, declined, replay_after_decline

    _first, rejected, declined, replay_after_decline = _run_with_lifespan(app, scenario)
    assert rejected.status == 400
    assert rejected.json()["error"]["code"] == -32602
    assert declined.status == 200
    assert declined.json()["result"]["structuredContent"]["error_code"] == "approval_declined"
    assert replay_after_decline.status == 400
    assert runtime.approved_digests == []


def test_modern_header_mismatch_fails_without_execution() -> None:
    app = build_proxy_asgi_app(_ModernRuntime(), "modern-test-token")
    request = _modern_request("tools/list", {})
    request["headers"] = [
        (header, "tools/call" if header == "mcp-method" else value)
        for header, value in request["headers"]
    ]
    response = _wire_requests(app, [request])[0]
    assert response.status == 400
    assert response.json()["error"]["code"] == -32600
