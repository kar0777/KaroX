"""Shared streaming adapter base and error/effort helpers."""

from __future__ import annotations

import json
import math
import threading
from dataclasses import replace
import time
from typing import Any, Dict, Iterator, Mapping, Optional
from urllib.parse import urlencode, urlsplit, urlunsplit
import httpx
from .net_config import NetworkConfigError, client_options
from .providers import (
    CredentialAccessor,
    ModelEvent,
    ModelEventKind,
    accumulate_response,
    ModelRequest,
    ModelResponse,
    OpenAIChatCompletionsProvider,
    ProviderError,
    ProviderErrorKind,
    ReasoningBlock,
    transport_timeout,
)



def _openai_effort(effort: str) -> str:
    """Map KaroX's five reasoning levels onto the four OpenAI accepts.

    OpenAI's scale tops out at ``high``, so ``xhigh`` and ``max`` land there.
    Sending a level the endpoint does not know would be rejected outright, and
    silently dropping the dial would be worse: the caller asked for maximum
    thinking and would get the default.
    """

    return "high" if effort in {"xhigh", "max"} else effort

def _gemini_thinking_level(effort: str) -> str:
    """Map the same five levels onto Gemini's thinking levels.

    Gemini names the idea ``thinking_level`` and offers low/medium/high, so the
    top two KaroX levels are clamped to ``high`` for the same reason.
    """

    return "high" if effort in {"xhigh", "max"} else effort

def stream_error_kind(error_body: Any) -> ProviderErrorKind:
    """Classify an error delivered inside an otherwise-200 SSE stream.

    The HTTP status classifier never runs for these: the body arrives as an
    ``error`` event (Anthropic/Gemini) or a ``response.failed`` payload
    (Responses). Mapping every one of them to PROVIDER_INTERNAL made routing
    retry and fallback-cycle auth and quota errors as if they were transient,
    unlike the same error delivered with a status code. The body's ``code``
    (int HTTP-like or vendor string) and ``type``/``status`` markers are read
    through the same ladder the status path uses; anything unreadable stays
    PROVIDER_INTERNAL so the conservative default is unchanged.
    """

    if not isinstance(error_body, dict):
        return ProviderErrorKind.PROVIDER_INTERNAL
    code = error_body.get("code")
    marker = " ".join(
        str(error_body.get(key) or "")
        for key in ("type", "status", "error_type")
    ).lower()
    text = str(error_body.get("message") or "").lower()
    if isinstance(code, bool):
        code = None
    status = code if isinstance(code, int) else None
    vendor = str(code).lower() if code is not None else ""
    combined = f"{marker} {vendor} {text}"
    if status in {401, 403} or any(
        token in combined for token in ("authentication", "permission", "unauthorized", "forbidden")
    ):
        return ProviderErrorKind.AUTHENTICATION
    if status == 402:
        # The ladder has to agree with the status path, which reads an explicit
        # 402 as an exhausted allowance rather than a permission problem.
        return ProviderErrorKind.BUDGET_EXCEEDED
    if "credit" in combined or "billing" in combined:
        return ProviderErrorKind.PERMISSION
    if status == 429 or "rate_limit" in combined or "rate limit" in combined or "quota" in combined:
        return ProviderErrorKind.RATE_LIMIT
    if status in {400, 405, 409, 415, 422} or "invalid_request" in combined:
        return ProviderErrorKind.INVALID_REQUEST
    if status in {404, 410} or "not_found" in combined or "model_not_found" in combined:
        return ProviderErrorKind.MODEL_UNAVAILABLE
    return ProviderErrorKind.PROVIDER_INTERNAL

class _StreamingAdapter:
    """Secret-safe HTTP/SSE transport and normalized response assembly."""

    provider_name = "provider"
    MAX_SSE_LINE_CHARS = 1_048_576
    # Tiny reasoning/tool deltas can legitimately produce far more than 10k
    # frames. The byte ceiling below is the authoritative memory/network bound;
    # this count only rejects pathological event fragmentation. Both are sized
    # for a maximal reasoning reply (one ~250-byte frame per token at a 384k
    # output cap), the same bounds as the Chat Completions transport.
    MAX_SSE_EVENTS = 1_000_000
    MAX_SSE_TOTAL_CHARS = 201_326_592

    def __init__(
        self,
        base_url: str,
        *,
        credential: Optional[CredentialAccessor] = None,
        headers: Optional[Mapping[str, str]] = None,
        query: Optional[Mapping[str, str]] = None,
        timeout_seconds: float = 60.0,
        max_transport_retries: int = 2,
        retry_backoff_seconds: float = 0.1,
    ) -> None:
        # Reuse the audited public-metadata validation from the compatible
        # adapter.  Authentication is added only after validation.
        self.base_url = self._base_url(base_url)
        self._credential = credential
        self._headers = OpenAIChatCompletionsProvider._validate_headers(headers or {})
        self._query = self._validate_query(query or {})
        if credential is not None and not OpenAIChatCompletionsProvider._credential_transport_is_secure(
            self.base_url
        ):
            raise ValueError(
                "provider credentials require HTTPS or a loopback HTTP endpoint"
            )
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or not 0.05 <= float(timeout_seconds) <= 3600
        ):
            raise ValueError("provider timeout must be between 0.05 and 3600 seconds")
        if (
            isinstance(max_transport_retries, bool)
            or not isinstance(max_transport_retries, int)
            or not 0 <= max_transport_retries <= 10
        ):
            raise ValueError("transport retries must be between 0 and 10")
        if (
            isinstance(retry_backoff_seconds, bool)
            or not isinstance(retry_backoff_seconds, (int, float))
            or not math.isfinite(float(retry_backoff_seconds))
            or retry_backoff_seconds < 0
        ):
            raise ValueError("retry backoff must be finite and non-negative")
        self.timeout_seconds = float(timeout_seconds)
        self.max_transport_retries = max_transport_retries
        self.retry_backoff_seconds = float(retry_backoff_seconds)
        self._client_lock = threading.Lock()
        self._client: Optional[httpx.Client] = None

    def _http_client(self) -> httpx.Client:
        client = self._client
        if client is not None and not bool(getattr(client, "is_closed", False)):
            return client
        with self._client_lock:
            client = self._client
            if client is None or bool(getattr(client, "is_closed", False)):
                try:
                    network = client_options()
                except NetworkConfigError as exc:
                    raise ProviderError(ProviderErrorKind.TRANSPORT, str(exc)) from exc
                client = httpx.Client(
                    follow_redirects=False,
                    limits=httpx.Limits(
                        max_connections=100,
                        max_keepalive_connections=20,
                        keepalive_expiry=120.0,
                    ),
                    **network,
                )
                self._client = client
            return client

    def close(self) -> None:
        with self._client_lock:
            client = self._client
            self._client = None
        closer = getattr(client, "close", None) if client is not None else None
        if callable(closer):
            closer()

    @staticmethod
    def _base_url(value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("provider base URL is required")
        parts = urlsplit(value.strip())
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
        ):
            raise ValueError(
                "provider base URL must be HTTP(S) without credentials, query, or fragment"
            )
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path.rstrip("/"), "", "")
        )

    @staticmethod
    def _validate_query(query: Mapping[str, str]) -> Dict[str, str]:
        # Building a dummy compatible endpoint applies the same secret checks.
        endpoint = OpenAIChatCompletionsProvider._endpoint(
            "https://validation.invalid", query
        )
        parsed = urlsplit(endpoint)
        # Values were already encoded by _endpoint.  Preserve the original
        # validated mapping instead of parsing and normalizing it again.
        assert parsed.query or not query
        return {str(name): str(value) for name, value in query.items()}

    def _secret(self) -> Optional[str]:
        if self._credential is None:
            return None
        try:
            value = self._credential()
        except Exception as exc:
            raise ProviderError(
                ProviderErrorKind.AUTHENTICATION,
                f"credential accessor failed: {type(exc).__name__}",
            ) from exc
        if not isinstance(value, str) or not value.strip():
            raise ProviderError(
                ProviderErrorKind.AUTHENTICATION,
                "credential accessor returned no API key",
            )
        if "\r" in value or "\n" in value:
            raise ProviderError(
                ProviderErrorKind.AUTHENTICATION,
                "credential contains invalid characters",
            )
        return value

    def _endpoint(self, request: ModelRequest) -> str:
        raise NotImplementedError

    def _request_payload(self, request: ModelRequest) -> Dict[str, Any]:
        raise NotImplementedError

    def _request_headers(self) -> Dict[str, str]:
        raise NotImplementedError

    def _events(
        self,
        response: httpx.Response,
        attempts: int,
        deadline: float,
    ) -> Iterator[ModelEvent]:
        raise NotImplementedError

    def stream(self, request: ModelRequest) -> Iterator[ModelEvent]:
        endpoint = self._endpoint(request)
        payload = self._request_payload(request)
        headers = self._request_headers()
        request_started = time.monotonic()
        deadline = request_started + float(request.deadline_seconds)
        attempts = 0
        while True:
            attempts += 1
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderError(
                    ProviderErrorKind.TRANSPORT, "provider request deadline expired"
                )
            response_started = False
            event_started = False
            try:
                client = self._http_client()
                with client.stream(
                    "POST",
                    endpoint,
                    json=payload,
                    headers=headers,
                    timeout=transport_timeout(self.timeout_seconds, remaining),
                ) as response:
                    response_started = True
                    response_headers_ms = round(
                        max(0.0, (time.monotonic() - request_started) * 1000.0),
                        3,
                    )
                    if response.status_code >= 400:
                        raise OpenAIChatCompletionsProvider._http_error(response)
                    first_event_ms: Optional[float] = None
                    try:
                        for event in self._events(response, attempts, deadline):
                            now = time.monotonic()
                            if first_event_ms is None:
                                first_event_ms = round(
                                    max(0.0, (now - request_started) * 1000.0),
                                    3,
                                )
                            if event.kind is ModelEventKind.COMPLETION:
                                event = replace(
                                    event,
                                    transport_metrics={
                                        "response_headers_ms": response_headers_ms,
                                        "first_event_ms": first_event_ms,
                                        "total_ms": round(
                                            max(
                                                0.0,
                                                (now - request_started) * 1000.0,
                                            ),
                                            3,
                                        ),
                                    },
                                )
                            event_started = True
                            yield event
                    except httpx.TransportError as exc:
                        if event_started:
                            raise ProviderError(
                                ProviderErrorKind.TRANSPORT,
                                f"provider stream interrupted: {type(exc).__name__}",
                                status_code=response.status_code,
                                transport_attempts=attempts,
                            ) from exc
                        raise
                    return
            except ProviderError as exc:
                if (
                    exc.kind is ProviderErrorKind.TRANSPORT
                    and exc.transport_attempts < attempts
                ):
                    raise ProviderError(
                        exc.kind,
                        exc.safe_message,
                        status_code=exc.status_code,
                        retry_after=exc.retry_after,
                        route_attempts=exc.route_attempts,
                        transport_attempts=attempts,
                    ) from exc
                raise
            except httpx.TransportError as exc:
                if (
                    response_started
                    and event_started
                    or attempts >= self.max_transport_retries + 1
                ):
                    raise ProviderError(
                        ProviderErrorKind.TRANSPORT,
                        (
                            f"provider stream interrupted: {type(exc).__name__}"
                            if response_started and event_started
                            else f"provider transport failed: {type(exc).__name__}"
                        ),
                        transport_attempts=attempts,
                    ) from exc
                delay = self.retry_backoff_seconds * (2 ** (attempts - 1))
                if delay:
                    if delay >= deadline - time.monotonic():
                        raise ProviderError(
                            ProviderErrorKind.TRANSPORT,
                            "provider request deadline expired during retry",
                            transport_attempts=attempts,
                        ) from exc
                    time.sleep(delay)

    def complete(self, request: ModelRequest) -> ModelResponse:
        return accumulate_response(self.stream(request))

    @classmethod
    def _sse_records(
        cls, response: httpx.Response, deadline: float
    ) -> Iterator[tuple[Optional[str], str]]:
        event_name: Optional[str] = None
        data_lines: list[str] = []
        event_count = 0
        total_chars = 0
        for line in response.iter_lines():
            if time.monotonic() >= deadline:
                raise ProviderError(
                    ProviderErrorKind.TRANSPORT,
                    "provider request deadline expired while reading stream",
                    status_code=response.status_code,
                )
            if not isinstance(line, str):
                raise ProviderError(
                    ProviderErrorKind.MALFORMED_RESPONSE,
                    "provider SSE stream contained a non-text line",
                    status_code=response.status_code,
                )
            if len(line) > cls.MAX_SSE_LINE_CHARS:
                raise ProviderError(
                    ProviderErrorKind.MALFORMED_RESPONSE,
                    "provider SSE line exceeds the size limit",
                    status_code=response.status_code,
                )
            total_chars += len(line) + 1
            if total_chars > cls.MAX_SSE_TOTAL_CHARS:
                raise ProviderError(
                    ProviderErrorKind.MALFORMED_RESPONSE,
                    "provider SSE stream exceeds the size limit",
                    status_code=response.status_code,
                )
            if line == "":
                if data_lines:
                    event_count += 1
                    if event_count > cls.MAX_SSE_EVENTS:
                        raise ProviderError(
                            ProviderErrorKind.MALFORMED_RESPONSE,
                            "provider SSE stream contains too many events",
                            status_code=response.status_code,
                        )
                    yield event_name, "\n".join(data_lines)
                event_name, data_lines = None, []
                continue
            if line.startswith(":"):
                continue
            field, separator, value = line.partition(":")
            if separator and value.startswith(" "):
                value = value[1:]
            if field == "event":
                event_name = value
            elif field == "data":
                data_lines.append(value if separator else "")
        if data_lines:
            event_count += 1
            if event_count > cls.MAX_SSE_EVENTS:
                raise ProviderError(
                    ProviderErrorKind.MALFORMED_RESPONSE,
                    "provider SSE stream contains too many events",
                    status_code=response.status_code,
                )
            yield event_name, "\n".join(data_lines)

    @staticmethod
    def _json_event(data: str, status_code: int) -> Dict[str, Any]:
        try:
            value = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise ProviderError(
                ProviderErrorKind.MALFORMED_RESPONSE,
                "provider SSE event contained invalid JSON",
                status_code=status_code,
            ) from exc
        if not isinstance(value, dict):
            raise ProviderError(
                ProviderErrorKind.MALFORMED_RESPONSE,
                "provider SSE event root must be an object",
                status_code=status_code,
            )
        return value

    @staticmethod
    def _usage(raw: Any, names: Mapping[str, str]) -> Dict[str, int]:
        if not isinstance(raw, dict):
            raise ValueError("usage must be an object")
        result: Dict[str, int] = {}
        for source, target in names.items():
            # A dotted source walks into a nested object: OpenAI reports the
            # cached share of the prompt under prompt_tokens_details rather than
            # at the top level, and a missing branch is simply an absent field.
            value: Any = raw
            for step in source.split("."):
                if not isinstance(value, dict) or step not in value:
                    value = None
                    break
                value = value[step]
            if value is None:
                continue
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
            ):
                raise ValueError("known usage field must be a non-negative integer")
            result[target] = value
        return result

    def _url(self, suffix: str, extra_query: Optional[Mapping[str, str]] = None) -> str:
        parts = urlsplit(self.base_url)
        query = {**self._query, **(extra_query or {})}
        return urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path.rstrip("/") + suffix,
                urlencode(query),
                "",
            )
        )

GEMINI_SIGNATURE_PREFIX = "gemini-signature:"

def _is_gemini_signature(block: ReasoningBlock) -> bool:
    return str(block.data or "").startswith(GEMINI_SIGNATURE_PREFIX)
