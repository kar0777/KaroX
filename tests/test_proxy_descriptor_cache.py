from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, Optional

from _support import SRC, initialize_git_repository  # noqa: F401
from karox.hosted_bridge import CompositeHostedBridge, CoreToolBridge
from karox.models import AccessProfile
from karox.proxy_server import build_proxy_asgi_app
from karox.sessions import SessionStore
from test_hosted_bridge import _jsonrpc_result, _tools_call, _wire_requests


class _RevokeAfterFirstExecute:
    def __init__(self, inner: CompositeHostedBridge, sessions: SessionStore, session_id: str) -> None:
        self.inner = inner
        self.sessions = sessions
        self.session_id = session_id
        self.execute_count = 0
        # Freeze only the public descriptor shape so MCP discovery cannot mask
        # the property under test. The concrete inner runtime remains live and
        # must still reject execution after revocation.
        self._descriptors = tuple(inner.descriptors())

    def descriptors(self):
        return list(self._descriptors)

    def session_info(self) -> dict[str, Any]:
        return self.inner.session_info()

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: Optional[str] = None,
        deadline_seconds: float = 30.0,
    ):
        self.execute_count += 1
        result = self.inner.execute(
            tool_name,
            arguments,
            idempotency_key=idempotency_key,
            deadline_seconds=deadline_seconds,
        )
        if self.execute_count == 1:
            self.sessions.revoke(self.session_id)
        return result


class ProxyDescriptorCacheTests(unittest.TestCase):
    def test_cached_route_still_fails_closed_after_session_revoke(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repo"
            initialize_git_repository(repository)
            (repository / "sample.txt").write_bytes(b"before\n")
            sessions = SessionStore(root / "sessions")
            session_id = "descriptor-cache-revoke"
            sessions.create(
                repository,
                "descriptor cache revoke",
                AccessProfile.WORKSPACE_WRITE,
                session_id=session_id,
            )
            core = CoreToolBridge(
                repository,
                sessions,
                session_id,
                ["karox.repo.read_file"],
                audit_path=root / "audit.jsonl",
            )
            runtime = _RevokeAfterFirstExecute(
                CompositeHostedBridge([core]), sessions, session_id
            )
            token = "descriptor-cache-revoke-token"
            app = build_proxy_asgi_app(runtime, token)
            request = lambda: _tools_call(
                token, "karox.repo.read_file", {"path": "sample.txt"}
            )
            first, second = _wire_requests(app, [request(), request()])

        first_result = _jsonrpc_result(first)
        second_result = _jsonrpc_result(second)
        self.assertFalse(first_result["isError"], first.text)
        self.assertTrue(second_result["isError"], second.text)
        # The second request must reach the cached route and then fail inside the
        # concrete runtime's live session check; that is the safety property this
        # cache relies on. The MCP SDK may omit structuredContent when it renders
        # an exception-path tool error, so do not couple this regression to that
        # transport formatting detail.
        self.assertEqual(runtime.execute_count, 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
