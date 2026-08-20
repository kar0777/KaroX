from __future__ import annotations

import io
import json
import os
import tempfile
from contextlib import redirect_stdout
from unittest.mock import patch

import anyio

from _support import SRC  # noqa: F401
from karox.cli import main
from karox.mcp_client import McpServerRecord
from karox.mcp_oauth import (
    McpOAuthAuthorizationResult,
    McpOAuthStorage,
    noninteractive_oauth_provider,
)
from karox.notion_mcp import NOTION_MCP_URL, notion_mcp_record
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken


class _Backend:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def set(self, service: str, account: str, secret: str) -> None:
        self.values[(service, account)] = secret

    def get(self, service: str, account: str) -> str | None:
        return self.values.get((service, account))

    def delete(self, service: str, account: str) -> None:
        self.values.pop((service, account), None)


def test_oauth_storage_round_trip_is_keyring_backed() -> None:
    backend = _Backend()
    storage = McpOAuthStorage("notion", backend=backend)
    token = OAuthToken(access_token="opaque-access", refresh_token="opaque-refresh")
    client = OAuthClientInformationFull(
        redirect_uris=["http://127.0.0.1:9000/callback"],
        token_endpoint_auth_method="none",
        client_id="client-id",
    )

    async def exercise() -> None:
        assert await storage.get_tokens() is None
        await storage.set_tokens(token)
        await storage.set_client_info(client)
        restored_token = await storage.get_tokens()
        restored_client = await storage.get_client_info()
        assert restored_token is not None
        assert restored_token.access_token == "opaque-access"
        assert restored_client is not None
        assert restored_client.client_id == "client-id"

    anyio.run(exercise)
    assert storage.available()
    storage.clear()
    assert not storage.available()


def test_oauth_record_is_http_only_and_has_no_static_secret_reference() -> None:
    record = notion_mcp_record()
    assert record.url == NOTION_MCP_URL
    assert record.transport == "streamable_http"
    assert record.oauth is True
    assert record.credential_ref is None
    assert "notion-search" in record.read_only_tools

    try:
        McpServerRecord(
            server_id="bad",
            namespace="bad",
            transport="stdio",
            command="python",
            oauth=True,
        )
    except ValueError as exc:
        assert "OAuth requires Streamable HTTP" in str(exc)
    else:
        raise AssertionError("stdio OAuth record unexpectedly accepted")


def test_noninteractive_provider_builds_without_starting_browser() -> None:
    provider = noninteractive_oauth_provider(
        "notion",
        NOTION_MCP_URL,
        storage=McpOAuthStorage("notion", backend=_Backend()),
    )
    assert provider.context.server_url == NOTION_MCP_URL


def test_cli_can_register_and_authorize_an_oauth_mcp_server() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        environment = {"KAROX_VNEXT_CONFIG_DIR": tmp}
        add_output = io.StringIO()
        with patch.dict(os.environ, environment, clear=False), redirect_stdout(add_output):
            code = main(
                (
                    "mcp", "server", "add", "notion",
                    "--namespace", "notion",
                    "--transport", "streamable_http",
                    "--url", NOTION_MCP_URL,
                    "--oauth",
                    "--json",
                )
            )
        assert code == 0
        assert json.loads(add_output.getvalue())["oauth"] is True

        authorization = McpOAuthAuthorizationResult(
            server_name="Notion MCP",
            tool_names=("notion-search", "notion-fetch"),
        )
        auth_output = io.StringIO()
        with (
            patch.dict(os.environ, environment, clear=False),
            patch("karox.mcp_oauth.authorize_mcp_oauth", return_value=authorization),
            redirect_stdout(auth_output),
        ):
            code = main(("mcp", "server", "authorize", "notion", "--json"))
        assert code == 0
        payload = json.loads(auth_output.getvalue())
        assert payload["status"] == "authorized"
        assert payload["tool_count"] == 2
