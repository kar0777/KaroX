"""OAuth 2.1 support for outbound Streamable HTTP MCP servers.

OAuth tokens and dynamic-client registration data are stored only in the OS
keyring.  Registry JSON stays secret-free.  Interactive authorization is an
explicit operation; normal MCP discovery/calls may refresh an existing grant
but never pop a browser or wait for consent on their own.
"""

from __future__ import annotations

import queue
import socket
import threading
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

import anyio
import httpx
from mcp import ClientSession
from pydantic import AnyUrl
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

from .credentials import CredentialBackend, CredentialError, KeyringBackend

_MCP_OAUTH_SERVICE = "KaroX/mcp-oauth"


class McpOAuthAuthorizationRequired(RuntimeError):
    """Raised when an outbound MCP server needs explicit user authorization."""


@dataclass(frozen=True)
class McpOAuthAuthorizationResult:
    server_name: str
    tool_names: tuple[str, ...]


class McpOAuthStorage:
    """SDK TokenStorage implementation backed by the secure OS keyring."""

    def __init__(self, server_id: str, backend: Optional[CredentialBackend] = None) -> None:
        if not isinstance(server_id, str) or not server_id:
            raise ValueError("MCP OAuth server ID is required")
        self.server_id = server_id
        self._backend = backend or KeyringBackend()

    @property
    def _tokens_account(self) -> str:
        return f"{self.server_id}:tokens"

    @property
    def _client_account(self) -> str:
        return f"{self.server_id}:client-info"

    async def get_tokens(self) -> OAuthToken | None:
        value = self._backend.get(_MCP_OAUTH_SERVICE, self._tokens_account)
        if not value:
            return None
        return OAuthToken.model_validate_json(value)

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._backend.set(_MCP_OAUTH_SERVICE, self._tokens_account, tokens.model_dump_json())

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        value = self._backend.get(_MCP_OAUTH_SERVICE, self._client_account)
        if not value:
            return None
        return OAuthClientInformationFull.model_validate_json(value)

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self._backend.set(
            _MCP_OAUTH_SERVICE,
            self._client_account,
            client_info.model_dump_json(),
        )

    def clear(self) -> None:
        for account in (self._tokens_account, self._client_account):
            try:
                self._backend.delete(_MCP_OAUTH_SERVICE, account)
            except CredentialError:
                pass

    def available(self) -> bool:
        try:
            return bool(self._backend.get(_MCP_OAUTH_SERVICE, self._tokens_account))
        except CredentialError:
            return False


class _CallbackState:
    def __init__(self) -> None:
        self.values: queue.Queue[tuple[str, Optional[str]]] = queue.Queue(maxsize=1)


class _CallbackHandler(BaseHTTPRequestHandler):
    callback_state: _CallbackState

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self.send_error(404)
            return
        query = parse_qs(parsed.query)
        code = (query.get("code") or [""])[0]
        state = (query.get("state") or [""])[0] or None
        error = (query.get("error") or [""])[0]
        if error:
            status = 400
            body = "MCP authorization failed. You can close this tab."
            if self.callback_state.values.empty():
                self.callback_state.values.put(("", state))
        elif code:
            status = 200
            body = "MCP authorization received. You can return to KaroX."
            if self.callback_state.values.empty():
                self.callback_state.values.put((code, state))
        else:
            status = 400
            body = "Missing authorization code."
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        return


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _metadata(redirect_uri: str, client_name: str) -> OAuthClientMetadata:
    return OAuthClientMetadata(
        redirect_uris=[AnyUrl(redirect_uri)],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        client_name=client_name,
    )


def noninteractive_oauth_provider(
    server_id: str,
    server_url: str,
    *,
    timeout_seconds: float = 300.0,
    storage: McpOAuthStorage | None = None,
) -> OAuthClientProvider:
    """Build an OAuth provider that may refresh but never starts consent UI."""

    async def redirect_handler(url: str) -> None:
        del url
        raise McpOAuthAuthorizationRequired(
            f"MCP OAuth authorization is required for {server_id}; run the explicit authorize flow"
        )

    async def callback_handler() -> tuple[str, str | None]:
        raise McpOAuthAuthorizationRequired(
            f"MCP OAuth authorization is required for {server_id}; run the explicit authorize flow"
        )

    return OAuthClientProvider(
        server_url,
        _metadata("http://127.0.0.1:1/callback", f"KaroX {server_id} MCP"),
        storage or McpOAuthStorage(server_id),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
        timeout=timeout_seconds,
    )


async def _authorize(
    server_id: str,
    server_url: str,
    *,
    open_authorization_url: Callable[[str], object],
    timeout_seconds: float,
    force: bool,
    storage: McpOAuthStorage,
) -> McpOAuthAuthorizationResult:
    if force:
        storage.clear()
    callback = _CallbackState()
    port = _free_port()
    _CallbackHandler.callback_state = callback
    server = ThreadingHTTPServer(("127.0.0.1", port), _CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    redirect_uri = f"http://127.0.0.1:{port}/callback"

    async def redirect_handler(url: str) -> None:
        open_authorization_url(url)

    async def callback_handler() -> tuple[str, str | None]:
        return await anyio.to_thread.run_sync(callback.values.get)

    provider = OAuthClientProvider(
        server_url,
        _metadata(redirect_uri, f"KaroX {server_id} MCP"),
        storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
        timeout=timeout_seconds,
    )
    try:
        timeout = httpx.Timeout(timeout_seconds)
        async with httpx.AsyncClient(
            auth=provider,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            async with streamable_http_client(server_url, http_client=client) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    initialized = await session.initialize()
                    tools = await session.list_tools()
                    return McpOAuthAuthorizationResult(
                        server_name=str(getattr(initialized.serverInfo, "name", server_id)),
                        tool_names=tuple(tool.name for tool in tools.tools),
                    )
    finally:
        server.shutdown()
        server.server_close()


def authorize_mcp_oauth(
    server_id: str,
    server_url: str,
    *,
    open_authorization_url: Callable[[str], object] = webbrowser.open,
    timeout_seconds: float = 300.0,
    force: bool = False,
    storage: McpOAuthStorage | None = None,
) -> McpOAuthAuthorizationResult:
    """Run one explicit OAuth consent flow and persist the resulting grant."""

    oauth_storage = storage or McpOAuthStorage(server_id)
    return anyio.run(
        lambda: _authorize(
            server_id,
            server_url,
            open_authorization_url=open_authorization_url,
            timeout_seconds=timeout_seconds,
            force=force,
            storage=oauth_storage,
        )
    )


__all__ = [
    "McpOAuthAuthorizationRequired",
    "McpOAuthAuthorizationResult",
    "McpOAuthStorage",
    "authorize_mcp_oauth",
    "noninteractive_oauth_provider",
]
