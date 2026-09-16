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
        human_approval_secret="human-approval-secret",
        human_approval_base_url="https://public.example.test",
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


def test_modern_approval_browser_fallback_is_human_bound_and_one_shot() -> None:
    runtime = _ApprovalRuntime()
    app = build_proxy_asgi_app(
        runtime,
        "modern-test-token",
        human_approval_secret="human-approval-secret",
        human_approval_base_url="https://public.example.test",
    )
    arguments = {"remote": "origin", "branch": "main"}

    async def scenario() -> tuple[Any, Any, Any, Any, Any, Any]:
        tool_request = _without_elicitation(
            _modern_request(
                "tools/call",
                {"name": "karox_git_push", "arguments": arguments},
                name="karox_git_push",
            )
        )
        first = await _asgi_request(app, tool_request)
        first_payload = first.json()["result"]["structuredContent"]
        approval_url = first_payload["approval_url"]
        assert approval_url.startswith("https://public.example.test/mcp/approval?")
        parsed = urlsplit(approval_url)
        states = parse_qs(parsed.query).get("state", [])
        assert len(states) == 1
        state = states[0]
        page = await _asgi_request(
            app,
            {
                "method": "GET",
                "path": parsed.path,
                "query_string": parsed.query.encode("ascii"),
                "headers": [
                    ("host", "127.0.0.1:8765"),
                    ("origin", "https://chatgpt.com"),
                ],
            },
        )
        wrong = await _asgi_request(
            app,
            {
                "method": "POST",
                "path": parsed.path,
                "headers": [
                    ("host", "127.0.0.1:8765"),
                    ("content-type", "application/x-www-form-urlencoded"),
                ],
                "body": urlencode({"state": state, "password": "wrong"}).encode(),
            },
        )
        assert runtime.approved_digests == []
        approved_page = await _asgi_request(
            app,
            {
                "method": "POST",
                "path": parsed.path,
                "headers": [
                    ("host", "127.0.0.1:8765"),
                    ("content-type", "application/x-www-form-urlencoded"),
                ],
                "body": urlencode(
                    {"state": state, "password": "human-approval-secret"}
                ).encode(),
            },
        )
        approved = await _asgi_request(app, tool_request)
        replay = await _asgi_request(app, tool_request)
        return first, page, wrong, approved_page, approved, replay

    first, page, wrong, approved_page, approved, replay = _run_with_lifespan(app, scenario)
    assert first.status == 200
    first_result = first.json()["result"]
    assert first_result["resultType"] == "complete"
    assert first_result["isError"] is True
    assert first_result["structuredContent"]["error_code"] == "approval_browser_required"
    assert page.status == 200
    assert "human-approval-secret" not in page.text
    assert "Approve one KaroX action" in page.text
    assert wrong.status == 403
    assert approved_page.status == 200
    assert "Action approved" in approved_page.text
    assert approved.status == 200
    approved_result = approved.json()["result"]
    assert approved_result["structuredContent"]["approved"] is True
    assert runtime.approved_digests == ["a" * 64]
    assert replay.status == 200
    replay_result = replay.json()["result"]
    assert replay_result["structuredContent"]["error_code"] == "approval_browser_required"
    assert runtime.approved_digests == ["a" * 64]


def test_trusted_approval_cookie_reduces_followup_to_one_click() -> None:
    runtime = _ApprovalRuntime()
    app = build_proxy_asgi_app(
        runtime,
        "modern-test-token",
        human_approval_secret="human-approval-secret",
        human_approval_base_url="https://public.example.test",
    )

    async def scenario() -> tuple[Any, Any, Any, Any, Any]:
        first_args = {"remote": "origin", "branch": "main"}
        first_call = _without_elicitation(
            _modern_request(
                "tools/call",
                {"name": "karox_git_push", "arguments": first_args},
                name="karox_git_push",
            )
        )
        first = await _asgi_request(app, first_call)
        first_url = urlsplit(first.json()["result"]["structuredContent"]["approval_url"])
        first_state = parse_qs(first_url.query)["state"][0]
        approved_page = await _asgi_request(
            app,
            {
                "method": "POST",
                "path": first_url.path,
                "headers": [
                    ("host", "127.0.0.1:8765"),
                    ("content-type", "application/x-www-form-urlencoded"),
                ],
                "body": urlencode(
                    {"state": first_state, "password": "human-approval-secret"}
                ).encode(),
            },
        )
        cookie_header = next(
            value.decode("latin-1")
            for name, value in approved_page.headers
            if name.lower() == b"set-cookie"
        )
        cookie = cookie_header.split(";", 1)[0]
        assert "HttpOnly" in cookie_header
        assert "Secure" in cookie_header
        await _asgi_request(app, first_call)

        second_args = {"remote": "origin", "branch": "release"}
        second_call = _without_elicitation(
            _modern_request(
                "tools/call",
                {"name": "karox_git_push", "arguments": second_args},
                name="karox_git_push",
            )
        )
        second = await _asgi_request(app, second_call)
        second_url = urlsplit(second.json()["result"]["structuredContent"]["approval_url"])
        trusted_page = await _asgi_request(
            app,
            {
                "method": "GET",
                "path": second_url.path,
                "query_string": second_url.query.encode("ascii"),
                "headers": [
                    ("host", "127.0.0.1:8765"),
                    ("cookie", cookie),
                ],
            },
        )
        assert 'name="password"' not in trusted_page.text
        assert "already trusted" in trusted_page.text
        second_state = parse_qs(second_url.query)["state"][0]
        click = await _asgi_request(
            app,
            {
                "method": "POST",
                "path": second_url.path,
                "headers": [
                    ("host", "127.0.0.1:8765"),
                    ("content-type", "application/x-www-form-urlencoded"),
                    ("cookie", cookie),
                ],
                "body": urlencode({"state": second_state}).encode(),
            },
        )
        executed = await _asgi_request(app, second_call)
        return approved_page, second, trusted_page, click, executed

    approved_page, second, trusted_page, click, executed = _run_with_lifespan(app, scenario)
    assert approved_page.status == 200
    assert second.status == 200
    assert trusted_page.status == 200
    assert click.status == 200
    assert executed.status == 200
    assert executed.json()["result"]["structuredContent"]["approved"] is True
    assert runtime.approved_digests == ["a" * 64, "a" * 64]


def test_tampered_trusted_cookie_does_not_bypass_password() -> None:
    runtime = _ApprovalRuntime()
    app = build_proxy_asgi_app(
        runtime,
        "modern-test-token",
        human_approval_secret="human-approval-secret",
        human_approval_base_url="https://public.example.test",
    )

    async def scenario() -> tuple[Any, Any]:
        arguments = {"remote": "origin", "branch": "main"}
        tool_call = _without_elicitation(
            _modern_request(
                "tools/call",
                {"name": "karox_git_push", "arguments": arguments},
                name="karox_git_push",
            )
        )
        first = await _asgi_request(app, tool_call)
        approval_url = urlsplit(first.json()["result"]["structuredContent"]["approval_url"])
        state = parse_qs(approval_url.query)["state"][0]
        page = await _asgi_request(
            app,
            {
                "method": "GET",
                "path": approval_url.path,
                "query_string": approval_url.query.encode("ascii"),
                "headers": [
                    ("host", "127.0.0.1:8765"),
                    ("cookie", "__Secure-karox-approval=v1.invalid.invalid"),
                ],
            },
        )
        click = await _asgi_request(
            app,
            {
                "method": "POST",
                "path": approval_url.path,
                "headers": [
                    ("host", "127.0.0.1:8765"),
                    ("content-type", "application/x-www-form-urlencoded"),
                    ("cookie", "__Secure-karox-approval=v1.invalid.invalid"),
                ],
                "body": urlencode({"state": state}).encode(),
            },
        )
        return page, click

    page, click = _run_with_lifespan(app, scenario)
    assert 'name="password"' in page.text
    assert click.status == 403
    assert runtime.approved_digests == []


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
