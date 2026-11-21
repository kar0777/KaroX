"""Shared streaming HTTP transport for native providers."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from karox6.providers.base import STREAM_IDLE_S, ProviderError, stream_lines
from karox6.providers.openai_chat import _http_error

if TYPE_CHECKING:
    import httpx


@dataclass(slots=True)
class StreamProvider:
    base_url: str
    api_key: str | None = None
    timeout_s: float = 900.0
    stream: bool = True
    headers: dict[str, str] = field(default_factory=dict)
    stream_idle_s: float | None = STREAM_IDLE_S  # silence allowed once the answer flows
    _client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_s, connect=30.0))
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _stream(
        self,
        url: str,
        body: dict[str, Any],
        headers: dict[str, str],
        feed: Callable[[dict[str, Any]], None],
        flowing: Callable[[], bool] = lambda: False,
    ) -> None:
        """POST ``body`` and feed every SSE data event to ``feed``. ``flowing`` says
        whether answer tokens have started (see :func:`stream_lines`)."""
        import httpx

        try:
            async with self._http().stream("POST", url, json=body, headers=headers) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise _mapped_error(resp.status_code, resp.text, resp.headers)
                async for line in stream_lines(resp, self.stream_idle_s, flowing):
                    if line.startswith("data:"):
                        data = line[5:].strip()
                        if data and data != "[DONE]":
                            feed(json.loads(data))
        except httpx.TimeoutException as exc:
            raise ProviderError("timeout", f"request timed out: {exc!r}") from exc
        except httpx.TransportError as exc:
            raise ProviderError("transport", f"connection failed: {exc!r}") from exc
        except json.JSONDecodeError as exc:
            raise ProviderError("server", f"invalid JSON from server: {exc}") from exc


def _mapped_error(status: int, text: str, headers: Any) -> ProviderError:
    if status == 400 and "prompt is too long" in text.lower():
        return ProviderError("context_window", text[:500], status=status)
    return _http_error(status, text, headers)


def stream_error(error: Any) -> ProviderError:
    if not isinstance(error, dict):
        return _mapped_error(500, str(error), {})
    kind = str(error.get("type") or error.get("status") or error.get("code") or "")
    status = (
        int(kind)
        if kind.isdigit()
        else {
            "rate_limit_error": 429,
            "resource_exhausted": 429,
            "authentication_error": 401,
            "permission_denied": 403,
            "invalid_request_error": 400,
            "overloaded_error": 529,
        }.get(kind.lower(), 500)
    )
    return _mapped_error(status, str(error.get("message") or error), {})
