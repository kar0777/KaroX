"""Shared streaming HTTP transport for native providers."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from karox6.providers.base import STREAM_IDLE_S, ProviderError, stream_lines
from karox6.providers.openai_chat import _http_error, _sse_error_status

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
                        if data == "[DONE]":
                            break  # a proxy may keep the connection open after it
                        if data:
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


# What the providers call an error sent inside a stream, as the HTTP status of the same
# refusal (Anthropic: its ``type``; Gemini: the ``status`` beside a numeric ``code``).
_NAMED_STATUS = {
    "invalid_request_error": 400,
    "invalid_argument": 400,
    "failed_precondition": 400,
    "authentication_error": 401,
    "unauthenticated": 401,
    "permission_error": 403,
    "permission_denied": 403,
    "not_found_error": 404,
    "not_found": 404,
    "request_too_large": 413,
    "rate_limit_error": 429,
    "resource_exhausted": 429,
    "unavailable": 503,
    "deadline_exceeded": 504,
    "overloaded_error": 529,
}


def stream_error(error: Any) -> ProviderError:
    """An error sent inside the stream, classified like the HTTP refusal it stands for.
    Anthropic names it by ``type`` ("overloaded_error"), Gemini by an HTTP ``code`` plus a
    ``status`` name ("RESOURCE_EXHAUSTED"), the Responses API by a string ``code``
    ("rate_limit_exceeded"). The names stay in the message: quota and context-window
    wording is recognised there, and the user sees what the provider called it."""
    if not isinstance(error, dict):
        return _mapped_error(500, str(error), {})
    names = [str(error[key]) for key in ("code", "type", "status") if error.get(key) is not None]
    labels = [name for name in names if not name.isdigit()]
    numbers = [int(name) for name in names if name.isdigit() and 400 <= int(name) < 600]
    named = [_NAMED_STATUS[label.lower()] for label in labels if label.lower() in _NAMED_STATUS]
    status = (numbers or named or [_sse_error_status(None, " ".join(labels))])[0]
    text = ": ".join([*labels, str(error.get("message") or error)])
    return _mapped_error(status, text, {})
