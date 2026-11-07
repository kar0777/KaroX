"""MCP Streamable HTTP transport compatibility seam."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional
import httpx

try:
    from mcp.client.streamable_http import streamable_http_client

    _MODERN_STREAMABLE_HTTP = True
except ImportError:  # compatibility for pre-1.27 development environments
    from mcp.client.streamable_http import streamablehttp_client as streamable_http_client

    _MODERN_STREAMABLE_HTTP = False


def _http_client_factory(
    headers: Optional[dict[str, str]] = None,
    timeout: Optional[httpx.Timeout] = None,
    auth: Optional[httpx.Auth] = None,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers=headers,
        timeout=timeout,
        auth=auth,
        follow_redirects=False,
        trust_env=False,
    )

@asynccontextmanager
async def streamable_http_transport(
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    timeout_seconds: float = 30.0,
    terminate_on_close: bool = True,
    auth: Optional[httpx.Auth] = None,
) -> AsyncIterator[Any]:
    """Open the installed MCP SDK's Streamable HTTP transport safely.

    MCP 1.27 renamed ``streamablehttp_client`` and moved caller-supplied HTTP
    policy behind an explicit ``http_client``.  Keeping that compatibility seam
    here prevents runtime probes and test helpers from silently falling back to
    the deprecated function, while older supported development environments can
    still use their legacy signature.
    """

    if _MODERN_STREAMABLE_HTTP:
        async with _http_client_factory(
            headers=headers,
            timeout=httpx.Timeout(timeout_seconds),
            auth=auth,
        ) as http_client:
            async with streamable_http_client(url, http_client=http_client) as streams:
                try:
                    yield streams
                finally:
                    # The modern context manager currently leaves the memory
                    # object streams to its caller. Closing both is idempotent
                    # and avoids retaining SDK task-group resources.
                    await streams[0].aclose()
                    await streams[1].aclose()
        return

    async with streamable_http_client(
        url,
        headers=headers,
        timeout=timeout_seconds,
        sse_read_timeout=timeout_seconds,
        terminate_on_close=terminate_on_close,
        httpx_client_factory=_http_client_factory,
    ) as streams:
        yield streams
