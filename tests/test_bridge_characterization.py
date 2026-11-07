"""Characterization coverage for the hosted MCP bridge's unrefactored paths.

These tests pin behavior the bridge exhibits today, before the hosted-bridge
refactor.  Three surfaces are covered:

* the stateless MCP 2026-07-28 JSON wire: ``server/discover``,
  ``tools/list``, ``tools/call``, method-header mismatch, GET and oversized
  bodies;
* the sealed one-shot approval ``requestState`` codec: round-trip, argument
  binding, replay, expiry and canonical base64url;
* ``CoreToolBridge.execute_approved`` digest matching and the chat-native
  one-shot approval consumption path.

Everything is in-process and loopback-only.  The ASGI app is driven directly,
no socket is opened, no public tunnel is started, and no credential is printed.
"""

from __future__ import annotations

import base64
import json
import os
import string
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

import anyio

from karox.bridge_proxy import (
    MODERN_MCP_PROTOCOL_VERSION,
    build_proxy_asgi_app,
    wire_tool_name,
)
from karox.hosted_core import (
    CoreToolBridge,
    HostedApprovalRequired,
    HostedBridgeAccessDenied,
    ProxyToolDescriptor,
)
from karox.models import AccessProfile, CoreCommand
from karox.risk_mapping import action_for_command
from karox.sessions import SessionStore
from karox.task_state import FactOrigin, TaskStateStore, fact


def _initialize_git_repository(path: Path) -> None:
    """Create a deterministic repository with no inherited git identity."""

    path.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_AUTHOR_NAME": "KaroX Test",
            "GIT_AUTHOR_EMAIL": "karox@example.invalid",
            "GIT_COMMITTER_NAME": "KaroX Test",
            "GIT_COMMITTER_EMAIL": "karox@example.invalid",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=path,
        env=environment,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=120,
    )


class _AsgiResponse:
    def __init__(self, status: int, headers: list[tuple[bytes, bytes]], body: bytes):
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)


async def _asgi_request(app: Any, request: dict[str, Any]) -> _AsgiResponse:
    body: bytes = request.get("body", b"")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": request.get("method", "POST"),
        "path": request["path"],
        "raw_path": request["path"].encode("utf-8"),
        "root_path": "",
        "scheme": request.get("scheme", "http"),
        "query_string": request.get("query_string", b""),
        "headers": [
            (name.lower().encode("utf-8"), value.encode("utf-8"))
            for name, value in request.get("headers", ())
        ],
        "client": ("127.0.0.1", 54321),
        "server": ("127.0.0.1", 8765),
    }
    pending = [{"type": "http.request", "body": body, "more_body": False}]
    state: dict[str, Any] = {"status": 0, "headers": [], "chunks": []}

    async def receive() -> dict[str, Any]:
        return pending.pop(0) if pending else {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            state["status"] = message["status"]
            state["headers"] = list(message.get("headers", []))
        elif message["type"] == "http.response.body":
            state["chunks"].append(message.get("body", b""))

    await app(scope, receive, send)
    return _AsgiResponse(state["status"], state["headers"], b"".join(state["chunks"]))


async def _asgi_session(app: Any, scenario: Any) -> Any:
    """Run one lifespan and a caller-supplied request scenario in-process."""

    to_app_send, to_app_receive = anyio.create_memory_object_stream(8)
    from_app_send, from_app_receive = anyio.create_memory_object_stream(8)
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
            result = await scenario(lambda request: _asgi_request(app, request))
        finally:
            await to_app_send.send({"type": "lifespan.shutdown"})
            await from_app_receive.receive()
    return result


def _session(app: Any, scenario: Any) -> Any:
    return anyio.run(_asgi_session, app, scenario)


def _wire_requests(app: Any, requests: list[dict[str, Any]]) -> list[_AsgiResponse]:
    async def scenario(call: Any) -> list[_AsgiResponse]:
        return [await call(request) for request in requests]

    responses: list[_AsgiResponse] = _session(app, scenario)
    return responses


def _modern_request(
    token: str,
    method: str,
    params: Optional[dict[str, Any]] = None,
    *,
    request_id: Any = 1,
    extra_headers: tuple[tuple[str, str], ...] = (),
    http_method: str = "POST",
    body: Optional[bytes] = None,
    path: str = "/mcp",
) -> dict[str, Any]:
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": {} if params is None else params,
    }
    return {
        "method": http_method,
        "path": path,
        "headers": [
            ("host", "127.0.0.1:8765"),
            ("authorization", f"Bearer {token}"),
            ("content-type", "application/json"),
            ("accept", "application/json, text/event-stream"),
            ("mcp-protocol-version", MODERN_MCP_PROTOCOL_VERSION),
            *extra_headers,
        ],
        "body": json.dumps(payload).encode("utf-8") if body is None else body,
    }


def _result(response: _AsgiResponse) -> dict[str, Any]:
    return response.json()["result"]


def _error(response: _AsgiResponse) -> dict[str, Any]:
    return response.json()["error"]


class _Clock:
    """A mutable wall clock used only through ``karox.bridge_proxy.time``."""

    def __init__(self, now: float) -> None:
        self.now = now

    def time(self) -> float:
        return self.now

    def perf_counter(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --- Modern MCP 2026-07-28 wire ------------------------------------------------

_READ_TOOL = "karox.repo.read_file"
_WRITE_TOOL = "karox.repo.write_file"


class ModernMcpWireTests(unittest.TestCase):
    """ASGI-level coverage for the stateless July-2026 protocol wire."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        _initialize_git_repository(self.repository)
        (self.repository / "sample.txt").write_bytes(b"origin\n")
        self.sessions = SessionStore(self.root / "sessions")
        self.sessions.create(
            self.repository,
            "modern wire task",
            AccessProfile.WORKSPACE_WRITE,
            session_id="modern",
        )
        self.runtime = CoreToolBridge(
            self.repository,
            self.sessions,
            "modern",
            [_READ_TOOL, _WRITE_TOOL],
        )
        self.token = "modern-wire-token"
        self.app = build_proxy_asgi_app(self.runtime, self.token)

    def test_server_discover_reports_the_modern_contract(self) -> None:
        (response,) = _wire_requests(
            self.app, [_modern_request(self.token, "server/discover")]
        )
        self.assertEqual(response.status, 200, response.text)
        result = _result(response)

        self.assertEqual(MODERN_MCP_PROTOCOL_VERSION, "2026-07-28")
        self.assertEqual(result["supportedVersions"][0], MODERN_MCP_PROTOCOL_VERSION)
        self.assertIn("2025-11-25", result["supportedVersions"])
        self.assertEqual(result["capabilities"], {"tools": {}})
        self.assertEqual(result["ttlMs"], 60_000)
        self.assertEqual(result["cacheScope"], "private")
        self.assertEqual(result["resultType"], "complete")
        server_info = result["_meta"]["io.modelcontextprotocol/serverInfo"]
        self.assertEqual(server_info["name"], "karox-proxy")
        self.assertNotIn(self.token, response.text)

    def test_tools_list_returns_wire_names_and_modern_metadata(self) -> None:
        (response,) = _wire_requests(
            self.app, [_modern_request(self.token, "tools/list")]
        )
        self.assertEqual(response.status, 200, response.text)
        result = _result(response)
        tools = {item["name"]: item for item in result["tools"]}

        self.assertEqual(
            sorted(tools),
            [wire_tool_name(_READ_TOOL), wire_tool_name(_WRITE_TOOL)],
        )
        self.assertEqual(result["ttlMs"], 60_000)
        self.assertEqual(result["cacheScope"], "private")
        self.assertEqual(result["resultType"], "complete")
        self.assertEqual(result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"], "karox-proxy")

        read = tools[wire_tool_name(_READ_TOOL)]
        write = tools[wire_tool_name(_WRITE_TOOL)]
        self.assertEqual(read["inputSchema"]["type"], "object")
        self.assertIn("workstream_id", read["inputSchema"]["properties"])
        self.assertTrue(read["annotations"]["readOnlyHint"])
        self.assertFalse(read["annotations"]["destructiveHint"])
        self.assertFalse(write["annotations"]["readOnlyHint"])
        self.assertTrue(write["annotations"]["destructiveHint"])

    def test_tools_call_returns_a_complete_result_envelope(self) -> None:
        request = _modern_request(
            self.token,
            "tools/call",
            {
                "name": wire_tool_name(_READ_TOOL),
                "arguments": {"path": "sample.txt"},
            },
        )
        (response,) = _wire_requests(self.app, [request])
        self.assertEqual(response.status, 200, response.text)
        result = _result(response)

        self.assertEqual(result["resultType"], "complete")
        self.assertFalse(result["isError"])
        structured = result["structuredContent"]
        self.assertTrue(structured["ok"])
        self.assertEqual(structured["data"]["content"], "origin\n")
        self.assertEqual(json.loads(result["content"][0]["text"]), structured)
        self.assertEqual(
            result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"],
            "karox-proxy",
        )

    def test_an_mcp_method_header_mismatch_is_rejected(self) -> None:
        request = _modern_request(
            self.token,
            "tools/list",
            extra_headers=(("mcp-method", "tools/call"),),
        )
        (response,) = _wire_requests(self.app, [request])
        self.assertEqual(response.status, 400, response.text)
        error = _error(response)
        self.assertEqual(error["code"], -32600)
        self.assertEqual(error["message"], "Mcp-Method header mismatch")
        self.assertNotIn("result", response.json())

    def test_a_body_protocol_version_mismatch_is_rejected(self) -> None:
        request = _modern_request(
            self.token,
            "server/discover",
            {
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2099-01-01",
                }
            },
        )
        (response,) = _wire_requests(self.app, [request])
        self.assertEqual(response.status, 400, response.text)
        error = _error(response)
        self.assertEqual(error["code"], -32600)
        self.assertEqual(error["message"], "MCP protocol version mismatch")

    def test_get_on_the_modern_wire_is_rejected_with_405(self) -> None:
        request = _modern_request(self.token, "tools/list", http_method="GET")
        (response,) = _wire_requests(self.app, [request])
        self.assertEqual(response.status, 405, response.text)
        error = _error(response)
        self.assertEqual(error["code"], -32600)
        self.assertEqual(error["message"], "Modern MCP requests must use POST")

    def test_oversized_body_is_rejected_with_413(self) -> None:
        body = b"x" * (16 * 1024 * 1024 + 1)
        request = _modern_request(self.token, "tools/list", body=body)
        (response,) = _wire_requests(self.app, [request])
        self.assertEqual(response.status, 413, response.text)
        error = _error(response)
        self.assertEqual(error["code"], -32600)
        self.assertEqual(error["message"], "MCP request body is too large")


# --- Sealed requestState approval codec ---------------------------------------

_APPROVAL_TOOL = "karox.test.approval"
_APPROVAL_WIRE_TOOL = wire_tool_name(_APPROVAL_TOOL)
_APPROVAL_DIGEST = "b" * 64
_APPROVAL_CEILING_SECONDS = 300.0


class _ApprovalRuntime:
    """A hosted runtime whose only tool always stops for one exact approval."""

    def __init__(self) -> None:
        self.executions: list[dict[str, Any]] = []
        self.approved: list[dict[str, Any]] = []

    def descriptors(self) -> list[ProxyToolDescriptor]:
        return [
            ProxyToolDescriptor(
                name=_APPROVAL_TOOL,
                description="A mutating tool that always needs user approval",
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
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
        deadline_seconds: float = 600.0,
    ) -> dict[str, Any]:
        self.executions.append(
            {
                "tool_name": tool_name,
                "arguments": dict(arguments),
                "idempotency_key": idempotency_key,
            }
        )
        raise HostedApprovalRequired(
            tool_name=tool_name,
            action_digest=_APPROVAL_DIGEST,
            action_kind="repo.write",
            risk="high",
            consequence="workspace",
            preview={"path": arguments.get("path")},
            message="Allow this one write?",
        )

    def execute_approved(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        expected_action_digest: str,
        idempotency_key: Optional[str] = None,
        deadline_seconds: float = 600.0,
    ) -> dict[str, Any]:
        self.approved.append(
            {
                "tool_name": tool_name,
                "arguments": dict(arguments),
                "expected_action_digest": expected_action_digest,
                "idempotency_key": idempotency_key,
            }
        )
        return {"ok": True, "data": {"approved": True, "tool": tool_name}}


def _approval_call(
    token: str,
    arguments: dict[str, Any],
    *,
    request_state: Optional[str] = None,
    input_responses: Optional[dict[str, Any]] = None,
    request_id: Any = 1,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "name": _APPROVAL_WIRE_TOOL,
        "arguments": dict(arguments),
        "_meta": {
            "io.modelcontextprotocol/clientCapabilities": {"elicitation": {}},
        },
    }
    if request_state is not None:
        params["requestState"] = request_state
    if input_responses is not None:
        params["inputResponses"] = input_responses
    return _modern_request(token, "tools/call", params, request_id=request_id)


def _approval_continuation(
    token: str,
    request_state: str,
    arguments: dict[str, Any],
    *,
    approve: bool = True,
    action: str = "accept",
    request_id: Any = 2,
) -> dict[str, Any]:
    return _approval_call(
        token,
        arguments,
        request_state=request_state,
        input_responses={
            "karox_approval": {"action": action, "content": {"approve": approve}}
        },
        request_id=request_id,
    )


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode((value + "=" * (-len(value) % 4)).encode("ascii"))


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _non_canonical_request_state(request_state: str) -> str:
    """Return a spelling that decodes identically but is not canonical base64url."""

    prefix, payload, signature = request_state.split(".")
    alphabet = string.ascii_uppercase + string.ascii_lowercase + string.digits + "-_"
    index = alphabet.index(signature[-1])
    # A 32-byte HMAC leaves the low two bits of the final 6-bit group unused.
    if index & 0b11:
        raise AssertionError("expected a canonical 32-byte HMAC spelling")
    mutated_signature = signature[:-1] + alphabet[index | 1]
    if _b64decode(mutated_signature) != _b64decode(signature):
        raise AssertionError("mutated state must decode to the same signature bytes")
    return f"{prefix}.{payload}.{mutated_signature}"


class SealedApprovalStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.token = "sealed-approval-token"
        self.runtime = _ApprovalRuntime()
        self.app = build_proxy_asgi_app(self.runtime, self.token)
        self.arguments = {"path": "sample.txt", "content": "approved\n"}

    async def _seal(self, call: Any, arguments: Optional[dict[str, Any]] = None) -> Any:
        sealed = await call(
            _approval_call(self.token, arguments or self.arguments)
        )
        self.assertEqual(sealed.status, 200, sealed.text)
        return sealed

    def test_sealed_state_executes_once_and_a_replay_is_rejected(self) -> None:
        async def scenario(call: Any) -> tuple[Any, ...]:
            sealed = await self._seal(call)
            state = _result(sealed)["requestState"]
            accepted = await call(
                _approval_continuation(self.token, state, self.arguments)
            )
            replay = await call(
                _approval_continuation(
                    self.token, state, self.arguments, request_id=3
                )
            )
            return sealed, accepted, replay

        sealed, accepted, replay = _session(self.app, scenario)
        sealed_result = _result(sealed)
        self.assertEqual(sealed_result["resultType"], "input_required")
        self.assertEqual(
            sealed_result["inputRequests"]["karox_approval"]["method"],
            "elicitation/create",
        )
        requested = sealed_result["inputRequests"]["karox_approval"]["params"]
        self.assertEqual(requested["requestedSchema"]["required"], ["approve"])
        self.assertFalse(requested["requestedSchema"]["additionalProperties"])

        state = sealed_result["requestState"]
        self.assertTrue(state.startswith("v1."))
        self.assertEqual(len(state.split(".")), 3)

        self.assertEqual(accepted.status, 200, accepted.text)
        accepted_result = _result(accepted)
        self.assertEqual(accepted_result["resultType"], "complete")
        self.assertFalse(accepted_result["isError"])
        self.assertTrue(accepted_result["structuredContent"]["ok"])
        self.assertEqual(len(self.runtime.approved), 1)
        self.assertEqual(
            self.runtime.approved[0]["expected_action_digest"], _APPROVAL_DIGEST
        )
        self.assertEqual(self.runtime.approved[0]["arguments"], self.arguments)

        self.assertEqual(replay.status, 400, replay.text)
        self.assertEqual(_error(replay)["code"], -32602)
        self.assertEqual(_error(replay)["message"], "Approval state was already used")
        self.assertEqual(len(self.runtime.approved), 1)

    def test_changed_arguments_are_rejected_without_consuming_the_state(self) -> None:
        changed = {"path": "sample.txt", "content": "different\n"}

        async def scenario(call: Any) -> tuple[Any, ...]:
            sealed = await self._seal(call)
            state = _result(sealed)["requestState"]
            rejected = await call(
                _approval_continuation(self.token, state, changed)
            )
            # A mismatched argument digest must not consume the one-shot state.
            self.assertEqual(self.runtime.approved, [])
            accepted = await call(
                _approval_continuation(
                    self.token, state, self.arguments, request_id=3
                )
            )
            return rejected, accepted

        rejected, accepted = _session(self.app, scenario)
        self.assertEqual(rejected.status, 400, rejected.text)
        self.assertEqual(
            _error(rejected)["message"], "Invalid or expired approval state"
        )

        self.assertEqual(accepted.status, 200, accepted.text)
        self.assertEqual(len(self.runtime.approved), 1)
        self.assertEqual(self.runtime.approved[0]["arguments"], self.arguments)

    def test_expired_state_is_rejected(self) -> None:
        clock = _Clock(1_000.0)

        async def scenario(call: Any) -> Any:
            sealed = await self._seal(call)
            state = _result(sealed)["requestState"]
            clock.advance(_APPROVAL_CEILING_SECONDS + 1.0)
            return await call(
                _approval_continuation(self.token, state, self.arguments)
            )

        with patch("karox.bridge_proxy.time", clock):
            expired = _session(self.app, scenario)

        self.assertEqual(expired.status, 400, expired.text)
        self.assertEqual(
            _error(expired)["message"], "Invalid or expired approval state"
        )
        self.assertEqual(self.runtime.approved, [])

    def test_non_canonical_base64url_state_is_rejected(self) -> None:
        async def scenario(call: Any) -> Any:
            sealed = await self._seal(call)
            state = _result(sealed)["requestState"]
            mutated = _non_canonical_request_state(state)
            return await call(
                _approval_continuation(self.token, mutated, self.arguments)
            )

        rejected = _session(self.app, scenario)
        self.assertEqual(rejected.status, 400, rejected.text)
        self.assertEqual(
            _error(rejected)["message"], "Invalid or expired approval state"
        )
        self.assertEqual(self.runtime.approved, [])

    def test_a_tampered_payload_fails_integrity_validation(self) -> None:
        async def scenario(call: Any) -> Any:
            sealed = await self._seal(call)
            prefix, payload, signature = _result(sealed)["requestState"].split(".")
            tampered = json.loads(_b64decode(payload))
            tampered["action_digest"] = "c" * 64
            tampered_payload = _b64encode(
                json.dumps(
                    tampered,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            return await call(
                _approval_continuation(
                    self.token,
                    f"{prefix}.{tampered_payload}.{signature}",
                    self.arguments,
                )
            )

        rejected = _session(self.app, scenario)
        self.assertEqual(rejected.status, 400, rejected.text)
        self.assertEqual(
            _error(rejected)["message"], "Invalid or expired approval state"
        )
        self.assertEqual(self.runtime.approved, [])

    def test_a_declined_continuation_consumes_the_one_shot_state(self) -> None:
        async def scenario(call: Any) -> tuple[Any, ...]:
            sealed = await self._seal(call)
            state = _result(sealed)["requestState"]
            declined = await call(
                _approval_continuation(
                    self.token, state, self.arguments, approve=False, action="decline"
                )
            )
            after_decline = await call(
                _approval_continuation(
                    self.token, state, self.arguments, request_id=3
                )
            )
            return declined, after_decline

        declined, after_decline = _session(self.app, scenario)
        self.assertEqual(declined.status, 200, declined.text)
        declined_result = _result(declined)
        self.assertTrue(declined_result["isError"])
        self.assertEqual(
            declined_result["structuredContent"]["error_code"], "approval_declined"
        )
        self.assertEqual(self.runtime.approved, [])

        self.assertEqual(after_decline.status, 400, after_decline.text)
        self.assertEqual(
            _error(after_decline)["message"], "Approval state was already used"
        )
        self.assertEqual(self.runtime.approved, [])


# --- CoreToolBridge.execute_approved and chat approval -----------------------


class _RecordedResult:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)


class _RecordingCore:
    """Records the CoreRuntime.execute call instead of touching a real remote."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def execute(
        self,
        command: CoreCommand,
        *,
        capability_token: Optional[str] = None,
        lease: Any = None,
    ) -> _RecordedResult:
        self.calls.append(
            {
                "command": command,
                "capability_token": capability_token,
                "lease": lease,
            }
        )
        return _RecordedResult({"ok": True, "data": {"accepted": True}})


class HostedCoreApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        _initialize_git_repository(self.repository)
        self.sessions = SessionStore(self.root / "sessions")
        self.sessions.create(
            self.repository,
            "approval task",
            AccessProfile.ELEVATED,
            session_id="approved",
        )
        self.bridge = CoreToolBridge(
            self.repository,
            self.sessions,
            "approved",
            ["karox.git.push", "karox.repo.write_file"],
            audit_path=self.root / "audit.jsonl",
        )
        self.arguments = {"remote": "origin", "branch": "main"}

    def _push_digest(self, arguments: Optional[dict[str, Any]] = None) -> str:
        command = CoreCommand(
            name="git.push",
            arguments=dict(arguments or self.arguments),
            session_id=self.bridge.session_id,
            origin=self.bridge.hosted_origin,
        )
        return action_for_command(command, repository=self.bridge.repository).digest

    def _bootstrap_chat_approval(self, **overrides: Any) -> None:
        value: dict[str, Any] = {
            "approved": True,
            "action_kind": "git.push",
            "remote": "origin",
            "branch": "main",
            "head": "unborn",
        }
        value.update(overrides)
        TaskStateStore(self.sessions).bootstrap(
            "approved",
            {
                "objective": fact("push the verified branch", FactOrigin.VERIFIED, "test"),
                "chat_user_approval": fact(
                    value,
                    FactOrigin.REPORTED_BY_AGENT,
                    "user.chat.explicit_approval",
                ),
            },
        )

    def _core(self) -> _RecordingCore:
        core = _RecordingCore()
        patcher = patch.object(self.bridge, "_core", return_value=core)
        patcher.start()
        self.addCleanup(patcher.stop)
        return core

    def test_a_mismatched_approval_digest_is_denied_before_execution(self) -> None:
        core = self._core()
        with self.assertRaisesRegex(
            HostedBridgeAccessDenied, "approved action no longer matches"
        ):
            self.bridge.execute_approved(
                "karox.git.push",
                self.arguments,
                expected_action_digest="0" * 64,
                idempotency_key="push-mismatch",
            )
        self.assertEqual(core.calls, [])

    def test_a_missing_approval_digest_is_denied(self) -> None:
        with self.assertRaisesRegex(HostedBridgeAccessDenied, "digest is missing"):
            self.bridge.execute_approved(
                "karox.git.push",
                self.arguments,
                expected_action_digest="",
                idempotency_key="push-missing",
            )

    def test_the_matching_digest_executes_the_exact_action_once(self) -> None:
        core = self._core()
        result = self.bridge.execute_approved(
            "karox.git.push",
            self.arguments,
            expected_action_digest=self._push_digest(),
            idempotency_key="push-approved",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(len(core.calls), 1)
        call = core.calls[0]
        self.assertEqual(call["command"].name, "git.push")
        self.assertEqual(call["command"].arguments, self.arguments)
        self.assertTrue(call["capability_token"])
        self.assertEqual(call["command"].confirmation_token, call["capability_token"])

    def test_an_approval_retry_for_an_ungated_action_is_denied(self) -> None:
        # ``repo.write_file`` is an ordinary workspace mutation, so a caller
        # cannot use the approval-retry entry point to claim it was gated.
        core = self._core()
        arguments = {"path": "sample.txt", "content": "ungated\n"}
        digest = action_for_command(
            CoreCommand(
                name="repo.write_file",
                arguments=arguments,
                session_id=self.bridge.session_id,
                origin=self.bridge.hosted_origin,
            ),
            repository=self.bridge.repository,
        ).digest

        with self.assertRaisesRegex(
            HostedBridgeAccessDenied, "does not correspond to an approval-gated action"
        ):
            self.bridge.execute_approved(
                "karox.repo.write_file",
                arguments,
                expected_action_digest=digest,
                idempotency_key="write-ungated",
            )
        self.assertEqual(core.calls, [])

    def test_reusing_a_digest_for_changed_arguments_is_denied(self) -> None:
        core = self._core()
        with self.assertRaisesRegex(
            HostedBridgeAccessDenied, "approved action no longer matches"
        ):
            self.bridge.execute_approved(
                "karox.git.push",
                {"remote": "origin", "branch": "release"},
                expected_action_digest=self._push_digest(),
                idempotency_key="push-changed",
            )
        self.assertEqual(core.calls, [])

    def test_chat_approval_bound_to_a_different_branch_is_not_executed(self) -> None:
        self._bootstrap_chat_approval(branch="release")
        core = self._core()
        with self.assertRaises(HostedApprovalRequired):
            self.bridge.execute(
                "karox.git.push",
                self.arguments,
                idempotency_key="chat-push-wrong-branch",
            )

        self.assertEqual(core.calls, [])
        state = TaskStateStore(self.sessions).load("approved")
        approval = state.facts["chat_user_approval"]
        self.assertIs(approval.origin, FactOrigin.REPORTED_BY_AGENT)
        self.assertTrue(approval.value["approved"])

    def test_a_declined_chat_approval_is_not_executed(self) -> None:
        self._bootstrap_chat_approval(approved=False)
        core = self._core()
        with self.assertRaises(HostedApprovalRequired):
            self.bridge.execute(
                "karox.git.push",
                self.arguments,
                idempotency_key="chat-push-declined",
            )
        self.assertEqual(core.calls, [])
        state = TaskStateStore(self.sessions).load("approved")
        approval = state.facts["chat_user_approval"]
        self.assertIs(approval.origin, FactOrigin.REPORTED_BY_AGENT)
        self.assertNotIn("consumed", approval.value)

    def test_chat_approval_executes_once_then_requires_a_new_approval(self) -> None:
        self._bootstrap_chat_approval()
        core = self._core()

        first = self.bridge.execute(
            "karox.git.push",
            self.arguments,
            idempotency_key="chat-push-1",
        )
        self.assertTrue(first["ok"])
        self.assertEqual(len(core.calls), 1)

        with self.assertRaises(HostedApprovalRequired):
            self.bridge.execute(
                "karox.git.push",
                self.arguments,
                idempotency_key="chat-push-2",
            )
        self.assertEqual(len(core.calls), 1)

        state = TaskStateStore(self.sessions).load("approved")
        consumed = state.facts["chat_user_approval"]
        self.assertIs(consumed.origin, FactOrigin.VERIFIED)
        self.assertFalse(consumed.value["approved"])
        self.assertTrue(consumed.value["consumed"])
        self.assertEqual(consumed.evidence, ("hosted.chat_approval.consume",))


if __name__ == "__main__":
    unittest.main()
