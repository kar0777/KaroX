"""End-to-end coverage for built-in Core tools exposed to hosted clients."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Optional

import httpx
import uvicorn

from _support import SRC, initialize_git_repository  # noqa: F401

from karox.credentials import CredentialError
from karox.hosted_bridge import (
    CompositeHostedBridge,
    CoreToolBridge,
    HostedBridgeAccessDenied,
)
from karox.mcp_client import (
    McpClient,
    McpCredentialStore,
    McpRegistry,
    McpServerRecord,
)
from karox.models import AccessProfile
from karox.openapi_bridge import build_openapi_bridge_app
from karox.proxy_server import build_proxy_asgi_app
from karox.sessions import SessionStore


class _FakeCredentialBackend:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def set(self, service: str, account: str, secret: str) -> None:
        self.store[(service, account)] = secret

    def get(self, service: str, account: str) -> Optional[str]:
        return self.store.get((service, account))

    def delete(self, service: str, account: str) -> None:
        if (service, account) not in self.store:
            raise CredentialError("credential does not exist")
        self.store.pop((service, account), None)


class _WireServer:
    def __init__(self, app: object) -> None:
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 15
        while (
            not self.server.started or not self.server.servers
        ) and time.time() < deadline:
            time.sleep(0.02)
        if not self.server.started or not self.server.servers:
            raise RuntimeError("bridge wire server did not start")
        self.port = self.server.servers[0].sockets[0].getsockname()[1]

    def close(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            raise RuntimeError("bridge wire server did not stop")


async def _call_mcp_tool_async(
    url: str,
    token: str,
    name: str,
    arguments: dict[str, object],
    meta: Optional[dict[str, object]] = None,
) -> Any:
    """Call a tool over Streamable HTTP with full control over request _meta.

    The high-level :class:`McpClient` neither exposes ``meta`` nor preserves the
    server's error text when a tool returns ``isError``.  This helper drives a
    raw :class:`~mcp.ClientSession` so the wire tests can both supply an
    explicit ``_meta.karoxIdempotencyKey`` and inspect the error message.
    """
    from datetime import timedelta

    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {token}"}
    async with streamablehttp_client(
        url,
        headers=headers,
        timeout=15.0,
        sse_read_timeout=15.0,
    ) as streams:
        async with ClientSession(
            streams[0],
            streams[1],
            read_timeout_seconds=timedelta(seconds=15),
        ) as session:
            await session.initialize()
            return await session.call_tool(name, arguments, meta=meta)


def _call_mcp_tool(
    url: str,
    token: str,
    name: str,
    arguments: dict[str, object],
    meta: Optional[dict[str, object]] = None,
) -> Any:
    import anyio

    return anyio.run(_call_mcp_tool_async, url, token, name, arguments, meta)


def _result_text(result: Any) -> str:
    return "".join(getattr(block, "text", "") for block in result.content)


class HostedCoreBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        initialize_git_repository(self.repository)
        (self.repository / "sample.txt").write_bytes(b"before\n")
        self.sessions = SessionStore(self.root / "sessions")
        self.sessions.create(
            self.repository,
            "hosted task",
            AccessProfile.WORKSPACE_WRITE,
            session_id="hosted",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _bridge(self, *tools: str) -> CoreToolBridge:
        return CoreToolBridge(
            self.repository,
            self.sessions,
            "hosted",
            list(tools),
            audit_path=self.root / "audit.jsonl",
        )

    def test_allowlist_exposes_and_executes_only_selected_core_tools(self) -> None:
        bridge = self._bridge("karox.repo.read_file", "karox.git.status")
        self.assertEqual(
            [item.name for item in bridge.descriptors()],
            ["karox.repo.read_file", "karox.git.status"],
        )
        result = bridge.execute("karox.repo.read_file", {"path": "sample.txt"})
        self.assertEqual(result["data"]["content"], "before\n")
        with self.assertRaisesRegex(HostedBridgeAccessDenied, "not exposed"):
            bridge.execute("karox.repo.list_files", {})

    def test_mutation_requires_idempotency_and_replays_safely(self) -> None:
        bridge = self._bridge("karox.repo.write_file")
        arguments = {"path": "sample.txt", "content": "after\n"}
        with self.assertRaisesRegex(HostedBridgeAccessDenied, "idempotency"):
            bridge.execute("karox.repo.write_file", arguments)
        first = bridge.execute(
            "karox.repo.write_file", arguments, idempotency_key="hosted-write-1"
        )
        second = bridge.execute(
            "karox.repo.write_file", arguments, idempotency_key="hosted-write-1"
        )
        self.assertTrue(first["data"]["changed"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(
            (self.repository / "sample.txt").read_text(encoding="utf-8"),
            "after\n",
        )

    def test_checks_require_an_explicit_command_allowlist(self) -> None:
        with self.assertRaisesRegex(
            HostedBridgeAccessDenied, "verification commands"
        ):
            self._bridge("karox.checks.run")
        bridge = CoreToolBridge(
            self.repository,
            self.sessions,
            "hosted",
            ["karox.checks.run"],
            verification_commands=[["python", "-c", "print('ok')"]],
        )
        with self.assertRaisesRegex(Exception, "user-approved"):
            bridge.execute(
                "karox.checks.run",
                {"argv": ["python", "-c", "print('other')"]},
                idempotency_key="wrong-check",
            )

    def test_profile_and_revocation_fail_closed(self) -> None:
        self.sessions.create(
            self.repository,
            "read only",
            AccessProfile.READ_ONLY,
            session_id="read-only",
        )
        with self.assertRaisesRegex(HostedBridgeAccessDenied, "does not allow"):
            CoreToolBridge(
                self.repository,
                self.sessions,
                "read-only",
                ["karox.repo.write_file"],
            )
        bridge = self._bridge("karox.repo.read_file")
        self.sessions.revoke("hosted")
        with self.assertRaisesRegex(HostedBridgeAccessDenied, "revoked"):
            bridge.descriptors()

    def test_elevated_exposes_all_eight_tools_and_workspace_write_rejects_commit(
        self,
    ) -> None:
        # Under workspace_write the commit capability is absent from the
        # profile, so the bridge constructor must refuse to expose
        # karox.git.commit (the "hosted session does not allow git.commit"
        # case described for the hosted bridge).
        with self.assertRaisesRegex(HostedBridgeAccessDenied, "does not allow"):
            CoreToolBridge(
                self.repository,
                self.sessions,
                "hosted",
                ["karox.git.commit"],
                audit_path=self.root / "audit.jsonl",
            )

        self.sessions.create(
            self.repository,
            "elevated task",
            AccessProfile.ELEVATED,
            session_id="elevated",
        )
        all_tools = [
            "karox.repo.read_file",
            "karox.repo.write_file",
            "karox.repo.list_files",
            "karox.repo.search",
            "karox.checks.run",
            "karox.git.status",
            "karox.git.diff",
            "karox.git.commit",
        ]
        bridge = CoreToolBridge(
            self.repository,
            self.sessions,
            "elevated",
            list(all_tools),
            audit_path=self.root / "audit-elevated.jsonl",
            verification_commands=[
                [
                    "python",
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "tests",
                    "-p",
                    "test_*.py",
                ],
                ["python", "-m", "compileall", "-q", "src", "tests"],
            ],
        )
        self.assertEqual(
            sorted(item.name for item in bridge.descriptors()),
            sorted(all_tools),
        )


class HostedBridgeWireTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        initialize_git_repository(self.repository)
        (self.repository / "sample.txt").write_bytes(b"wire\n")
        self.sessions = SessionStore(self.root / "sessions")
        self.sessions.create(
            self.repository,
            "wire task",
            AccessProfile.WORKSPACE_WRITE,
            session_id="wire",
        )
        self.runtime = CompositeHostedBridge(
            [
                CoreToolBridge(
                    self.repository,
                    self.sessions,
                    "wire",
                    ["karox.repo.read_file", "karox.repo.write_file"],
                )
            ]
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_openapi_wire_auth_schema_call_idempotency_and_rotation(self) -> None:
        active = {"token": "first-wire-token"}
        server = _WireServer(
            build_openapi_bridge_app(self.runtime, lambda: active["token"])
        )
        base = f"http://127.0.0.1:{server.port}"
        try:
            with httpx.Client(base_url=base, timeout=15.0) as client:
                schema = client.get("/openapi.json")
                self.assertEqual(schema.status_code, 200)
                paths = schema.json()["paths"]
                self.assertIn("/tools/karox.repo.read_file", paths)
                self.assertTrue(
                    paths["/tools/karox.repo.write_file"]["post"]["parameters"][0][
                        "required"
                    ]
                )
                self.assertEqual(client.get("/health").status_code, 401)
                headers = {"Authorization": "Bearer first-wire-token"}
                session = client.get("/session", headers=headers)
                self.assertEqual(session.status_code, 200)
                self.assertEqual(session.json()["session_id"], "wire")
                self.assertEqual(
                    session.json()["repository"], str(self.repository.resolve())
                )
                response = client.post(
                    "/tools/karox.repo.read_file",
                    headers=headers,
                    json={"path": "sample.txt"},
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["data"]["content"], "wire\n")
                self.assertEqual(
                    client.post(
                        "/tools/karox.repo.write_file",
                        headers=headers,
                        json={"path": "sample.txt", "content": "changed\n"},
                    ).status_code,
                    400,
                )
                mutation_headers = {
                    **headers,
                    "X-KaroX-Idempotency-Key": "openapi-write-1",
                }
                response = client.post(
                    "/tools/karox.repo.write_file",
                    headers=mutation_headers,
                    json={"path": "sample.txt", "content": "changed\n"},
                )
                self.assertEqual(response.status_code, 200, response.text)
                active["token"] = "second-wire-token"
                self.assertEqual(client.get("/health", headers=headers).status_code, 401)
                self.assertEqual(
                    client.get(
                        "/health",
                        headers={"X-API-Key": "second-wire-token"},
                    ).status_code,
                    200,
                )
        finally:
            server.close()

    def test_streamable_http_mcp_exposes_real_core_tool(self) -> None:
        token = "mcp-core-wire-token"
        server = _WireServer(build_proxy_asgi_app(self.runtime, token))
        backend = _FakeCredentialBackend()
        credentials = McpCredentialStore(backend=backend)
        info = credentials.set("core-wire", token)
        registry = McpRegistry(self.root / "mcp-registry.json")
        record = McpServerRecord(
            server_id="karox-core",
            namespace="karox-core",
            transport="streamable_http",
            url=f"http://127.0.0.1:{server.port}/mcp",
            credential_ref=info["reference"],
            credential_target="Authorization",
            read_only_tools=("karox.repo.read_file",),
            timeout_seconds=15.0,
        )
        registry.put(record)
        client = McpClient(registry, credentials)
        try:
            tools = client.discover(record.server_id, self.repository)
            descriptor = next(
                item for item in tools if item.remote_name == "karox.repo.read_file"
            )
            result = client.call_record(
                record, descriptor, {"path": "sample.txt"}, self.repository
            )
            encoded = json.dumps(result)
            self.assertIn("wire", encoded)
            self.assertNotIn(token, encoded)
        finally:
            server.close()

    def test_mutating_mcp_call_without_meta_writes_file(self) -> None:
        token = "mcp-no-meta-wire-token"
        server = _WireServer(build_proxy_asgi_app(self.runtime, token))
        backend = _FakeCredentialBackend()
        credentials = McpCredentialStore(backend=backend)
        info = credentials.set("core-no-meta", token)
        registry = McpRegistry(self.root / "mcp-registry-no-meta.json")
        record = McpServerRecord(
            server_id="karox-core",
            namespace="karox-core",
            transport="streamable_http",
            url=f"http://127.0.0.1:{server.port}/mcp",
            credential_ref=info["reference"],
            credential_target="Authorization",
            read_only_tools=("karox.repo.read_file",),
            timeout_seconds=15.0,
        )
        registry.put(record)
        client = McpClient(registry, credentials)
        try:
            tools = client.discover(record.server_id, self.repository)
            descriptor = next(
                item for item in tools if item.remote_name == "karox.repo.write_file"
            )
            # A standards-compliant client (e.g. Notion Custom Agents) cannot set
            # _meta.karoxIdempotencyKey, so this call supplies none. The bridge
            # must still accept it and write the file to disk.
            first = client.call_record(
                record,
                descriptor,
                {"path": "sample.txt", "content": "no-meta-A\n"},
                self.repository,
            )
            self.assertFalse(first["result"]["isError"])
            self.assertEqual(
                (self.repository / "sample.txt").read_text(encoding="utf-8"),
                "no-meta-A\n",
            )
            # Each accepted call gets a fresh generated key, so a second call
            # with different content must execute instead of replaying the first
            # (no argument-hash-derived caching that would freeze stale output).
            second = client.call_record(
                record,
                descriptor,
                {"path": "sample.txt", "content": "no-meta-B\n"},
                self.repository,
            )
            self.assertFalse(second["result"]["isError"])
            self.assertEqual(
                (self.repository / "sample.txt").read_text(encoding="utf-8"),
                "no-meta-B\n",
            )
        finally:
            server.close()

    def test_invalid_explicit_idempotency_key_is_rejected_and_valid_key_takes_priority(
        self,
    ) -> None:
        token = "mcp-meta-wire-token"
        server = _WireServer(build_proxy_asgi_app(self.runtime, token))
        url = f"http://127.0.0.1:{server.port}/mcp"
        arguments = {"path": "sample.txt", "content": "rejected\n"}
        try:
            empty = _call_mcp_tool(
                url,
                token,
                "karox.repo.write_file",
                arguments,
                meta={"karoxIdempotencyKey": ""},
            )
            self.assertTrue(empty.isError)
            self.assertIn(
                "must be a string of 1-256 characters", _result_text(empty)
            )
            self.assertEqual(
                (self.repository / "sample.txt").read_text(encoding="utf-8"),
                "wire\n",
            )

            too_long = _call_mcp_tool(
                url,
                token,
                "karox.repo.write_file",
                arguments,
                meta={"karoxIdempotencyKey": "x" * 257},
            )
            self.assertTrue(too_long.isError)
            self.assertIn(
                "must be a string of 1-256 characters", _result_text(too_long)
            )
            self.assertEqual(
                (self.repository / "sample.txt").read_text(encoding="utf-8"),
                "wire\n",
            )

            # An explicitly supplied valid key still takes priority over the
            # generated fallback: repeating the same key replays the first
            # result instead of executing a second time. (Core rejects a
            # repeated key with different input, so the replay uses identical
            # arguments -- the point is that the supplied key is honoured as
            # the dedup key, not replaced by a fresh generated one.)
            first = _call_mcp_tool(
                url,
                token,
                "karox.repo.write_file",
                {"path": "sample.txt", "content": "explicit-A\n"},
                meta={"karoxIdempotencyKey": "explicit-wire-1"},
            )
            self.assertFalse(first.isError)
            self.assertTrue(first.structuredContent["data"]["changed"])
            self.assertEqual(
                (self.repository / "sample.txt").read_text(encoding="utf-8"),
                "explicit-A\n",
            )
            second = _call_mcp_tool(
                url,
                token,
                "karox.repo.write_file",
                {"path": "sample.txt", "content": "explicit-A\n"},
                meta={"karoxIdempotencyKey": "explicit-wire-1"},
            )
            self.assertFalse(second.isError)
            self.assertTrue(second.structuredContent["idempotent_replay"])
            self.assertEqual(
                (self.repository / "sample.txt").read_text(encoding="utf-8"),
                "explicit-A\n",
            )
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()
